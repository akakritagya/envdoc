"""Bring `.env.example` up to date with what the code actually reads.

The one write command in the tool, and the only module that touches a file the
user cares about. `.env.example` is frequently the only place a variable's
purpose is written down at all -- the comment above `STRIPE_WEBHOOK_SECRET`
explaining where to get one lives nowhere else in the repository -- so the
design here is dominated by never losing that. Two rules follow:

    - sync is append-only. It adds a name the code reads that the file does
      not yet document; it never rewrites or removes a line it did not add.
      A STALE entry (documented, nothing reads it) is left exactly as it was
      -- `check` already reports it, and deleting someone's documentation on
      their behalf is not this command's call to make.

    - The work here is split the way the rest of this codebase is: `plan()`
      is pure and turns a `Report` plus the current file text into new file
      text, so every interesting case is a plain string assertion. `write()`
      is the only I/O, and it exists solely to make that write atomic.

Which variables get added is exactly `Status.UNDOCUMENTED`: in code, absent
from the example. `STALE` is already in the file by definition; adding a
`ORPHAN_DEPLOYMENT` name would document a variable nothing reads, which is
inventing a requirement rather than recording one.
"""

import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from envdoc.aggregate import cite_defaults
from envdoc.models import Report, SourceKind, Status, Variable
from envdoc.sources.dotenv import parse as parse_dotenv

EXAMPLE_FILENAME = ".env.example"

_BANNER = "# Added by envdoc\n"

# Anything outside this class gets double-quoted rather than written bare:
# whitespace would be stripped by the parser's own .strip(), a quote character
# would be read as opening a quoted value, and '#' risks being read as a
# comment opener even though the parser only treats " #" that way -- treating
# both alike is the conservative reading, not the permissive one.
_BARE_SAFE = re.compile(r"\A[^\s#'\"\\]*\Z")

_ESCAPES = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t"}


@dataclass(frozen=True, slots=True)
class SyncPlan:
    """What `sync` would do to one `.env.example`, computed but not written."""

    path: PurePosixPath
    original: str
    updated: str
    added: tuple[str, ...]

    @property
    def changed(self) -> bool:
        return self.updated != self.original


def _quote(value: str) -> str:
    """`value`, written the way it will read back out as itself.

    Bare when it is safe to be bare; double-quoted with the same escapes
    `dotenv._unescape` reverses otherwise. Single quotes are not escaped --
    they are not special inside a double-quoted value, only `"` closes one.
    """
    if _BARE_SAFE.match(value):
        return value
    escaped = "".join(_ESCAPES.get(character, character) for character in value)
    return f'"{escaped}"'


def _entry_block(variable: Variable) -> str:
    """The line(s) `sync` writes for one undocumented variable.

    More than one distinct default is defect E from the build plan: the spec
    says to write "the literal default found in code", but the model allows
    several call sites to disagree about what that default is. Guessing which
    one is right would be a worse answer than admitting the conflict, so the
    value is left empty and a comment names every default and where it came
    from -- the same citation `aggregate.py`'s conflict warning uses, so the
    file and the warning never tell two different stories about the same
    disagreement.
    """
    defaults = variable.defaults
    if len(defaults) > 1:
        cited = cite_defaults(o for o in variable.occurrences if o.source is SourceKind.CODE)
        return f"# envdoc: conflicting defaults -- {cited}\n{variable.name}=\n"
    if len(defaults) == 1:
        return f"{variable.name}={_quote(defaults[0])}\n"
    return f"{variable.name}=\n"


def plan(report: Report, *, original: str = "") -> SyncPlan:
    """What `sync` would do to `.env.example`, given its current text.

    Every name already in `original` is skipped even when the report calls it
    undocumented -- deliberately redundant with `Status.UNDOCUMENTED`, because
    an `--exclude` that skips the example file during discovery would
    otherwise make every documented variable look absent and duplicate the
    entire file on the next write.
    """
    example_path = PurePosixPath(EXAMPLE_FILENAME)
    documented = {line.name for line in parse_dotenv(original, example_path).entries}

    missing = [
        variable
        for variable in report.variables
        if Status.UNDOCUMENTED in variable.statuses and variable.name not in documented
    ]

    if not missing:
        return SyncPlan(path=example_path, original=original, updated=original, added=())

    body = original
    if body and not body.endswith("\n"):
        body += "\n"
    if body and not body.endswith("\n\n"):
        body += "\n"
    body += _BANNER + "".join(_entry_block(variable) for variable in missing)

    return SyncPlan(
        path=example_path,
        original=original,
        updated=body,
        added=tuple(variable.name for variable in missing),
    )


def write(updated: str, target: Path) -> None:
    """Atomically replace `target` with `updated`.

    Written to a temp file in the same directory first, then swapped in with
    `os.replace` -- atomic because it never leaves the filesystem in a state
    where `target` is partially written. A crash or exception before the
    replace leaves `target` completely untouched and is cleaned up in the
    `except`; one after it has nothing left to clean up.
    """
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        # newline="" -- .env.example may carry CRLF line endings that
        # dotenv.parse preserved verbatim in `raw`; the platform's default
        # newline translation would otherwise rewrite every one of them.
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(updated)
        if target.exists():
            os.chmod(tmp_name, stat.S_IMODE(target.stat().st_mode))
        os.replace(tmp_name, target)
    except BaseException:
        os.unlink(tmp_name)
        raise
