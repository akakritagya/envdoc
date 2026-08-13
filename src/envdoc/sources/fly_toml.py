"""Read the environment variables a `fly.toml`'s `[env]` table provides.

The simplest of the deployment-manifest readers: unlike `docker-compose.yml`,
a `Dockerfile`, or a GitHub Actions workflow, `fly.toml` has no substitution
syntax at all. Fly secrets are set with `fly secrets set` at deploy time and
never referenced inside `fly.toml` itself, so there is no consumer-axis
scan to pair with this one -- every key under `[env]` is `SourceKind.
DEPLOYMENT`, full stop.

Parsed with the standard library's `tomllib` -- no new dependency, and the
same module `config.py` already imports for `[tool.envdoc]`. Its one real
limitation for this use is that it reports no position information at all,
unlike `yaml.compose()`. A small auxiliary text scan recovers a real line
number for the common case -- an `[env]` section header followed by plain
`KEY = value` lines -- and any key that scan can't locate (an inline
`env = { ... }` table, an unusual TOML key syntax) falls back to `line=1`
rather than failing outright, the same "degrade gracefully" posture every
other extractor already takes toward syntax it doesn't specially handle.
"""

import re
import tomllib
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

_SECTION_HEADER = re.compile(r"^\s*\[")
_ENV_HEADER = re.compile(r"^\s*\[env\]\s*$")
_KEY_LINE = re.compile(r"^\s*([A-Za-z0-9_-]+)\s*=")


def _env_line_numbers(text: str) -> dict[str, int]:
    """A best-effort `{key: line}` map for the `[env]` section's plain
    `KEY = value` lines -- not a TOML parser, just enough to recover
    positions for what `tomllib` already validated exists."""
    lines: dict[str, int] = {}
    in_env = False
    for lineno, line in enumerate(text.split("\n"), start=1):
        if _ENV_HEADER.match(line):
            in_env = True
            continue
        if in_env and _SECTION_HEADER.match(line):
            break
        if in_env:
            match = _KEY_LINE.match(line)
            if match:
                lines[match.group(1)] = lineno
    return lines


def extract(text: str, file: PurePosixPath) -> ExtractResult:
    """Every key declared under `fly.toml`'s `[env]` table."""
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return ExtractResult(
            findings=(), dynamic=(), warnings=(f"{file}: could not parse, skipped ({exc})",)
        )

    env = document.get("env")
    if not isinstance(env, dict):
        return ExtractResult(findings=(), dynamic=(), warnings=())

    line_numbers = _env_line_numbers(text)

    findings = [
        Finding(
            name=name,
            occurrence=Occurrence(
                file=file,
                line=line_numbers.get(name, 1),
                column=0,
                source=SourceKind.DEPLOYMENT,
                provider=Provider.FLY_TOML,
                required=False,
                default=None,
            ),
            confidence=Confidence.EXACT,
        )
        for name in env
        if isinstance(name, str)
    ]

    findings.sort(key=lambda f: sort_key(f.occurrence))
    return ExtractResult(findings=tuple(findings), dynamic=(), warnings=())
