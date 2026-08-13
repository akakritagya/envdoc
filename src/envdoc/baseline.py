"""Report -> a suppression file, and a suppression file -> a stripped Report.

`check` is a gate, and a gate that goes red the first time anyone points it at
a repository older than a month never gets merged into CI -- the branch is
abandoned and the tool uninstalled. A baseline is the adoption ramp: it
records the drift that exists *today* so `check` suppresses exactly that and
still fails on anything new, the same mechanism as PHPStan's `baseline.neon`
or `detect-secrets`' `.secrets.baseline`.

The one thing worth knowing about envdoc's version of this idea: entries are
keyed by `(name, status)`, never by `file:line`. A line-keyed baseline churns
on every edit and silently stops suppressing the moment code moves. Keying by
name falls out of the Occurrence/Variable split for free -- see models.py --
and it means a baseline entry survives a file being renamed, reformatted or
moved wholesale.

Two functions here are pure Report transforms and get tested with plain
assertions: `capture` turns today's drift into a Baseline, `apply` strips a
Baseline's entries back out of a Report. `serialize`/`parse` are the on-disk
JSON encoding, following the same schema conventions render_json set:
`schema_version` first so a consumer can check compatibility before parsing
the rest, `indent=2`, a trailing newline, and no timestamp -- determinism
applies to this file exactly as much as to a report, and a timestamp would
turn every `envdoc baseline` re-run into a spurious commit.

No I/O happens here, by the same rule audit.py follows: cli.py reads the file
and calls write_sync from sync.py to write it back.
"""

import json
from dataclasses import dataclass
from pathlib import PurePosixPath

from envdoc.audit import headline
from envdoc.models import Report, Status, Variable

BASELINE_FILENAME = ".envdoc-baseline.json"
BASELINE_SCHEMA_VERSION = 1


class BaselineError(Exception):
    """A baseline file exists but isn't shaped like one envdoc wrote.

    Raised rather than skipped-with-a-warning: `--baseline` is opt-in, which
    means the user explicitly asked for suppression, so silently scanning
    unsuppressed on a malformed file would gate on more than they expected,
    and a baseline that partially suppresses whatever JSON happened to parse
    would be scarier still -- see ConfigError for the identical reasoning
    about `[tool.envdoc]`.
    """


@dataclass(frozen=True, slots=True)
class Baseline:
    """`(name, status)` pairs to suppress, sorted and deduplicated."""

    entries: tuple[tuple[str, Status], ...]


@dataclass(frozen=True, slots=True)
class ApplyResult:
    """A Report with baselined statuses removed, plus what changed."""

    report: Report
    suppressed: int
    obsolete: tuple[tuple[str, Status], ...]


def capture(report: Report) -> Baseline:
    """Every non-OK `(name, status)` pair on `report`, sorted.

    OK is skipped -- there is nothing to suppress about a clean variable, and
    recording it would make the file grow with the repository instead of with
    its debt.
    """
    entries = {
        (variable.name, status)
        for variable in report.variables
        for status in variable.statuses
        if status is not Status.OK
    }
    return Baseline(entries=tuple(sorted(entries)))


def _strip(variable: Variable, baselined: frozenset[Status]) -> Variable:
    remaining = variable.statuses - baselined
    # Never empty: audit._statuses guarantees a variable that clears every
    # other status still carries OK, and suppressing every status a variable
    # had must land on the same placeholder rather than an empty set no
    # renderer or has_drift call expects.
    statuses = remaining if remaining else frozenset({Status.OK})
    return Variable(
        name=variable.name,
        required=variable.required,
        confidence=variable.confidence,
        status=headline(statuses),
        statuses=statuses,
        defaults=variable.defaults,
        occurrences=variable.occurrences,
        documented_in_example=variable.documented_in_example,
        deployment_targets=variable.deployment_targets,
    )


def apply(report: Report, baseline: Baseline) -> ApplyResult:
    """Strip `baseline`'s entries out of `report`, recomputing each headline.

    An entry naming a variable this report no longer has, or a status that
    variable no longer carries, is obsolete -- reported so `check --baseline`
    can warn on stderr, per the decision that a fixed finding should never
    make the build fail just because nobody refreshed the baseline yet.
    """
    by_name: dict[str, frozenset[Status]] = {}
    for name, status in baseline.entries:
        by_name[name] = by_name.get(name, frozenset()) | {status}

    current = {variable.name: variable.statuses for variable in report.variables}
    obsolete = tuple(
        (name, status)
        for name, status in baseline.entries
        if status not in current.get(name, frozenset())
    )
    suppressed = len(baseline.entries) - len(obsolete)

    variables = tuple(
        _strip(variable, by_name.get(variable.name, frozenset())) for variable in report.variables
    )

    warnings = list(report.warnings)
    if suppressed:
        noun = "finding" if suppressed == 1 else "findings"
        warnings.append(f"{suppressed} {noun} suppressed by {BASELINE_FILENAME}")
    if obsolete:
        noun, verb = ("entry", "applies") if len(obsolete) == 1 else ("entries", "apply")
        warnings.append(
            f"{len(obsolete)} baseline {noun} no longer {verb} -- run `envdoc baseline` to refresh"
        )

    stripped = Report(
        root=report.root,
        variables=variables,
        dynamic=report.dynamic,
        warnings=tuple(sorted(warnings)),
        files_scanned=report.files_scanned,
        deployment_files_found=report.deployment_files_found,
    )
    return ApplyResult(report=stripped, suppressed=suppressed, obsolete=obsolete)


def serialize(baseline: Baseline, *, tool_version: str) -> str:
    """`Baseline` -> JSON schema v1. Key order fixed by the dict literal."""
    payload: dict[str, object] = {
        "schema_version": BASELINE_SCHEMA_VERSION,
        "tool_version": tool_version,
        "entries": [{"name": name, "status": status.value} for name, status in baseline.entries],
    }
    return json.dumps(payload, indent=2) + "\n"


def parse(text: str, path: PurePosixPath) -> Baseline:
    """JSON -> `Baseline`, or `BaselineError` naming `path` and what was wrong.

    Never a partial parse -- a baseline half-read is a baseline that
    suppresses the wrong things, and this file's only job is deciding what CI
    ignores.
    """
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BaselineError(f"{path}: not valid JSON ({exc})") from exc

    if not isinstance(document, dict):
        raise BaselineError(f"{path}: must be a JSON object")

    schema_version = document.get("schema_version")
    if schema_version != BASELINE_SCHEMA_VERSION:
        raise BaselineError(
            f"{path}: schema_version must be {BASELINE_SCHEMA_VERSION}, got {schema_version!r}"
        )

    raw_entries = document.get("entries")
    if not isinstance(raw_entries, list):
        raise BaselineError(f"{path}: entries must be a list")

    entries: set[tuple[str, Status]] = set()
    for item in raw_entries:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("name"), str)
            or not isinstance(item.get("status"), str)
        ):
            raise BaselineError(f"{path}: each entry must be an object with name and status")
        try:
            status = Status(item["status"])
        except ValueError:
            options = ", ".join(member.value for member in Status)
            raise BaselineError(
                f"{path}: entry status must be one of {options}, got {item['status']!r}"
            ) from None
        entries.add((item["name"], status))

    return Baseline(entries=tuple(sorted(entries)))
