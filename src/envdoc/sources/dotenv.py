"""Parse .env.example, keeping enough of it to write the file back unchanged.

The design is set by a single requirement: `sync` rewrites this file in place.
A rewrite that loses the comment explaining what DATABASE_URL is for, or
reflows every line it did not touch, produces a diff nobody can review and a
tool nobody runs twice. So each line keeps its raw text alongside whatever was
parsed out of it, and serialising is concatenation. The round-trip is true by
construction, which is the point -- it is a property that cannot quietly stop
holding one refactor later.

Keeping the raw text is also what makes it safe to *ignore* a line. Anything
this parser cannot understand stays exactly where it was and warns, so a
rewrite can never delete something it failed to read. In a file that may hold
the only written record of what a variable is for, that is the difference
between a tool people will let near their repository and one they will not.

The value of an entry is not a default. It is documentation of what the
variable looks like, written by whoever last touched the file; treating it as a
fallback the code would use would let a stale example answer questions about
live behaviour, which is the exact failure this tool exists to catch. Findings
from here carry required=False and default=None, per the model's rule that both
fields are code-only.
"""

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from envdoc.models import (
    Confidence,
    ExtractResult,
    Finding,
    Occurrence,
    Provider,
    SourceKind,
)

# The name is captured loosely and validated separately, so that `1INVALID=x`
# can be reported as a bad name rather than silently failing to look like an
# assignment at all.
_ASSIGNMENT = re.compile(r"[ \t]*(?:export[ \t]+)?(?P<name>[^\s=]+)[ \t]*=(?P<rest>.*)")
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")

# A `#` only opens a comment when whitespace precedes it. `PASSWORD=a#b` is a
# password containing a hash, not a password called `a`.
_INLINE_COMMENT = re.compile(r"[ \t]#")

_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\", '"': '"', "'": "'"}


@dataclass(frozen=True, slots=True)
class EnvLine:
    """One logical line, and the assignment it holds if it holds one.

    Logical rather than physical because a double-quoted value may span several
    lines -- a PEM key in .env.example is the usual reason -- in which case
    `raw` holds all of them and `line` is where it started.
    """

    line: int
    raw: str
    name: str | None = None
    value: str | None = None


@dataclass(frozen=True, slots=True)
class EnvDocument:
    """A parsed .env file that still knows how to be itself again."""

    lines: tuple[EnvLine, ...]
    warnings: tuple[str, ...]

    @property
    def text(self) -> str:
        """The original file, byte for byte."""
        return "".join(line.raw for line in self.lines)

    @property
    def entries(self) -> tuple[EnvLine, ...]:
        """Only the lines that declare a variable."""
        return tuple(line for line in self.lines if line.name is not None)


def _unescape(value: str) -> str:
    """Apply shell-style backslash escapes, as double quotes imply.

    Single-quoted values never come through here: they are literal, so a
    Windows path keeps its backslashes.
    """
    out: list[str] = []
    index = 0
    while index < len(value):
        character = value[index]
        if character == "\\" and index + 1 < len(value):
            following = value[index + 1]
            if following in _ESCAPES:
                out.append(_ESCAPES[following])
                index += 2
                continue
        out.append(character)
        index += 1
    return "".join(out)


def _closing_quote(text: str, quote: str, start: int) -> int:
    """Index of the quote that closes one opened at `start`, or -1.

    Backslash escapes only count inside double quotes, matching the unescaping
    rule, so `'it\\'` closes on its own quote where `"it\\"` does not.
    """
    index = start
    while index < len(text):
        character = text[index]
        if character == "\\" and quote == '"' and index + 1 < len(text):
            index += 2
            continue
        if character == quote:
            return index
        index += 1
    return -1


def _strip_comment(rest: str) -> str:
    """An unquoted value: everything up to an inline comment, whitespace gone."""
    match = _INLINE_COMMENT.search(rest)
    return (rest[: match.start()] if match else rest).strip()


def parse(text: str, file: PurePosixPath) -> EnvDocument:
    """Read a dotenv file into lines, preserving every byte of it."""
    physical = text.splitlines(keepends=True)
    lines: list[EnvLine] = []
    warnings: list[str] = []
    first_seen: dict[str, int] = {}

    index = 0
    while index < len(physical):
        raw = physical[index]
        number = index + 1
        index += 1

        # The BOM belongs to the file, not to the first variable's name -- a
        # file saved by a Windows editor would otherwise document a variable
        # nothing will ever set.
        content = raw.lstrip("﻿").rstrip("\r\n")
        stripped = content.strip()

        if not stripped or stripped.startswith("#"):
            lines.append(EnvLine(line=number, raw=raw))
            continue

        match = _ASSIGNMENT.fullmatch(content)
        if match is None:
            warnings.append(f"{file}:{number}: not an assignment, ignored: {stripped!r}")
            lines.append(EnvLine(line=number, raw=raw))
            continue

        name = match["name"]
        if not _NAME.match(name):
            warnings.append(f"{file}:{number}: not a valid variable name, ignored: {name!r}")
            lines.append(EnvLine(line=number, raw=raw))
            continue

        rest = match["rest"].lstrip()
        quote = rest[0] if rest[:1] in ('"', "'") else ""

        if not quote:
            value = _strip_comment(rest)
        else:
            end = _closing_quote(rest, quote, 1)
            while end == -1 and index < len(physical):
                # An unterminated quote continues onto the next physical line.
                # Without this, each continuation line is parsed on its own and
                # `-----END-----"` is reported as a variable that does not
                # exist.
                raw += physical[index]
                # The separator is written as "\n" whatever the file uses, so a
                # value read out of a CRLF file is the same string as one read
                # out of an LF file. The bytes on disk are unaffected: `raw`
                # keeps them, and that is what gets written back.
                rest += "\n" + physical[index].rstrip("\r\n")
                end = _closing_quote(rest, quote, 1)
                index += 1

            if end == -1:
                warnings.append(f"{file}:{number}: unterminated quote, ignored: {name!r}")
                lines.append(EnvLine(line=number, raw=raw))
                continue

            inner = rest[1:end]
            value = _unescape(inner) if quote == '"' else inner

        if name in first_seen:
            # The later one wins at load time, so the earlier is dead
            # documentation -- and if the two disagree, one of them is a lie
            # about the deployment.
            warnings.append(
                f"{file}:{number}: duplicate entry for {name},"
                f" first seen on line {first_seen[name]}"
            )
        else:
            first_seen[name] = number

        lines.append(EnvLine(line=number, raw=raw, name=name, value=value))

    return EnvDocument(lines=tuple(lines), warnings=tuple(warnings))


def extract(text: str, file: PurePosixPath) -> ExtractResult:
    """Every variable documented in a dotenv file.

    There is no dynamic case: every name in a dotenv file is a literal, so the
    confidence is always EXACT and no DynamicRef can arise.
    """
    document = parse(text, file)

    findings = tuple(
        Finding(
            name=entry.name,
            occurrence=Occurrence(
                file=file,
                line=entry.line,
                column=0,
                source=SourceKind.EXAMPLE,
                provider=Provider.DOTENV_EXAMPLE,
                # Both are code-only by the model's rule. The value on this
                # line documents the variable; it is not a fallback anything
                # actually uses.
                required=False,
                default=None,
            ),
            confidence=Confidence.EXACT,
        )
        for entry in document.entries
        if entry.name is not None
    )

    return ExtractResult(findings=findings, dynamic=(), warnings=document.warnings)
