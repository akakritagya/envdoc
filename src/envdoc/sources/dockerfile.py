"""Read the environment variables a `Dockerfile` sets, and the build
arguments it requires, via a small line-based grammar of its own -- a
Dockerfile is not YAML and not a general-purpose language, so unlike every
other extractor in this codebase this one is neither `ast` nor `yaml`.

Two instructions matter here, and they sit on opposite sides of the same
line `docker_compose.py`'s `${VAR}` interpolation draws:

    ENV NAME=value      sets the *running container's* environment --
                         SourceKind.DEPLOYMENT, same relationship
                         `environment:` has in a compose file

    ARG NAME[=default]  a *build-time-only* value. It is not present at
                         runtime unless a later `ENV NAME=$NAME` re-exposes
                         it -- which ordinary ENV parsing already catches for
                         free, since ENV only looks at the key on its left,
                         never at what the value expression is made of.
                         SourceKind.CODE: the Dockerfile *requires* this from
                         its build environment, the same relationship
                         `os.getenv` has to a variable.

Both `ENV` syntaxes are handled: the legacy `ENV NAME value` (rest-of-line is
the value, unquoted spaces allowed) and the modern `ENV NAME1=value1
NAME2="value with spaces"` (one or more pairs on one line, tokenized with the
standard library's `shlex.split` -- verified to already reproduce
Dockerfile's own quote-handling without a hand-rolled quoting parser). Which
form a line uses is decided by whether its first whitespace-separated token
contains `=`.

Values are discarded for `ENV`: `required`/`default` are code-only per the
data model, and a Dockerfile-provided occurrence always carries
`required=False, default=None`, matching `docker_compose.py`'s
`environment:` reader. `ARG`'s default *is* kept -- it is the one place this
parser is on the CODE axis -- with a single matching pair of surrounding
quotes stripped if present.

Multi-stage Dockerfiles (`FROM ... AS name`, repeated) need no real
stage-scope tracking here: the audit only asks whether a name is declared
*anywhere* in the repository, not whether it is valid in one particular
stage, so a flat scan across the whole file already covers every stage's
`ENV`/`ARG` correctly.

Unlike every YAML- or `ast`-based extractor in this codebase, a Dockerfile
has no library parser to raise a clean, catchable error -- there is no
`DockerfileError` to catch. Lines this module doesn't recognise (anything
that isn't `ENV`/`ARG`, or a line with no argument at all) are silently
skipped rather than warned about, the same posture every other extractor
takes toward syntax it doesn't produce a Finding for.
"""

import re
import shlex
from pathlib import PurePosixPath

from envdoc.models import (
    Confidence,
    ExtractResult,
    Finding,
    Occurrence,
    Provider,
    SourceKind,
    sort_key,
)

_INSTRUCTION = re.compile(r"^(\w+)\s+(.*)$", re.DOTALL)


def _logical_lines(text: str) -> list[tuple[int, str]]:
    """Physical lines joined on a trailing backslash into one logical line
    each, paired with the physical line number the logical line starts on.
    Blank lines and `#`-led comments are dropped before continuation-joining
    starts, so a comment can never be mistaken for part of an instruction."""
    logical: list[tuple[int, str]] = []
    buffer: list[str] = []
    start_line = 1

    for lineno, raw in enumerate(text.split("\n"), start=1):
        line = raw.rstrip()
        if not buffer:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            start_line = lineno

        if line.endswith("\\"):
            buffer.append(line[:-1])
            continue

        buffer.append(line)
        logical.append((start_line, " ".join(part.strip() for part in buffer)))
        buffer = []

    if buffer:
        logical.append((start_line, " ".join(part.strip() for part in buffer)))

    return logical


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        return value[1:-1]
    return value


def _finding(
    name: str,
    line: int,
    file: PurePosixPath,
    *,
    source: SourceKind,
    required: bool,
    default: str | None,
) -> Finding:
    return Finding(
        name=name,
        occurrence=Occurrence(
            file=file,
            line=line,
            column=0,
            source=source,
            provider=Provider.DOCKERFILE,
            required=required,
            default=default,
        ),
        confidence=Confidence.EXACT,
    )


def _env_findings(rest: str, line: int, file: PurePosixPath) -> list[Finding]:
    first_token = rest.split(None, 1)[0]

    if "=" not in first_token:
        # Legacy form: ENV NAME value -- everything after the name is the
        # value verbatim, spaces and all, whatever it contains.
        name = first_token
        return [
            _finding(name, line, file, source=SourceKind.DEPLOYMENT, required=False, default=None)
        ]

    try:
        tokens = shlex.split(rest)
    except ValueError:
        # Unbalanced quoting -- degrade to a plain split rather than raising;
        # a malformed line shouldn't take the whole scan down.
        tokens = rest.split()

    findings: list[Finding] = []
    for token in tokens:
        name = token.split("=", 1)[0]
        if name:
            findings.append(
                _finding(
                    name, line, file, source=SourceKind.DEPLOYMENT, required=False, default=None
                )
            )
    return findings


def _arg_finding(rest: str, line: int, file: PurePosixPath) -> Finding | None:
    if "=" in rest:
        name, _, default = rest.partition("=")
        name = name.strip()
        if not name:
            return None
        return _finding(
            name,
            line,
            file,
            source=SourceKind.CODE,
            required=False,
            default=_unquote(default.strip()),
        )

    name = rest.split(None, 1)[0]
    return _finding(name, line, file, source=SourceKind.CODE, required=True, default=None)


def extract(text: str, file: PurePosixPath) -> ExtractResult:
    """Find every `ENV`/`ARG` declaration in one Dockerfile.

    `file` is recorded as given and should already be relative to the scan
    root, matching every other extractor's `extract(text, file)` signature.
    """
    findings: list[Finding] = []

    for line, logical in _logical_lines(text):
        match = _INSTRUCTION.match(logical)
        if match is None:
            continue

        keyword = match.group(1).upper()
        rest = match.group(2).strip()
        if not rest:
            continue

        if keyword == "ENV":
            findings.extend(_env_findings(rest, line, file))
        elif keyword == "ARG":
            finding = _arg_finding(rest, line, file)
            if finding is not None:
                findings.append(finding)

    findings.sort(key=lambda f: sort_key(f.occurrence))
    return ExtractResult(findings=tuple(findings), dynamic=(), warnings=())
