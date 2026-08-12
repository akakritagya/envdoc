"""Report -> table / markdown / json. The last stop before a byte stream.

Three functions, one per format, and all three take the same input: a
`Report` and nothing else that isn't explicitly passed in. Reading the
installed package version or the system clock here would make this module
impure in the same way a parser reading a file itself would, and it would
make `test_render.py` a test of the environment it happened to run in rather
than of the rendering logic. `tool_version` and `generated_at` are therefore
parameters, not lookups -- `cli.py` is the layer that knows how to ask
`importlib.metadata` or `datetime.now()` for them, and it is also the only
layer that decides `generated_at` should exist at all (`--include-timestamp`).
Passing `generated_at=None` -- the default -- omits it from the JSON payload
entirely, which is what keeps two scans of an unchanged repository
byte-identical unless the caller explicitly asks for a timestamp.

The one non-obvious bug this module has to avoid: `Variable.statuses` is a
`frozenset[Status]`, and `Status` is a `StrEnum`, so its members hash the same
as their string value. CPython randomises string hashing per process
(`PYTHONHASHSEED`) unless it's pinned, which means iterating that frozenset
directly can order its members differently between two separate `uv run`
invocations of the same scan -- identical Report, different bytes out, and a
determinism contract broken by something that looks like a no-op refactor.
`_STATUS_ORDER` -- `tuple(Status)`, i.e. declaration order -- doesn't depend
on hashing at all, and every renderer below sorts against it before a set of
statuses reaches output. This is the concrete case CLAUDE.md's "sets never
reach rendering; sort into tuples first" rule exists to prevent.

`defaults`, `deployment_targets` and `occurrences` don't need the same
treatment: they arrive from `aggregate.py` and `audit.py` already as sorted
tuples, not sets.

Warnings are not printed here, even though every renderer receives the full
`Report`. `render_json`'s payload always carries them -- they're part of the
schema, for a consumer that isn't a terminal -- but `render_table` and
`render_markdown` leave them out entirely. Printing is `cli.py`'s job by the
architecture's own hard rule ("no print below cli.py"), and if a renderer
baked warnings into its returned string, `--quiet` could only suppress them
by cli.py reconstructing the `Report` with `warnings=()` first -- mutating a
supposedly-complete domain object to fake a display option. `cli.py` prints
the rendered report, then prints `report.warnings` itself, unless `--quiet`.
"""

import io
import json
from enum import StrEnum

from rich.console import Console
from rich.table import Table

from envdoc.models import DynamicRef, Occurrence, Report, Status, Variable

JSON_SCHEMA_VERSION = 1

_STATUS_ORDER = tuple(Status)

_TABLE_WIDTH = 100


class OutputFormat(StrEnum):
    """The formats `render()` knows how to produce, and nothing else does --
    `cli.py`'s `--format` option is a `typer.Option` typed against this enum
    rather than a bare string, so an unrecognised value is rejected by click
    before envdoc's own code ever sees it."""

    TABLE = "table"
    MARKDOWN = "markdown"
    JSON = "json"


def _sorted_statuses(statuses: frozenset[Status]) -> tuple[Status, ...]:
    """`statuses`, in declaration order rather than frozenset iteration order."""
    return tuple(status for status in _STATUS_ORDER if status in statuses)


def _status_cell(statuses: frozenset[Status]) -> str:
    return ", ".join(status.value for status in _sorted_statuses(statuses))


def _occurrence_cell(occurrences: tuple[Occurrence, ...]) -> str:
    return "; ".join(f"{occurrence.file}:{occurrence.line}" for occurrence in occurrences)


def render_table(report: Report) -> str:
    """A plain-text table for terminal display.

    Rendered through a `Console` pinned to a fixed width with color and the
    legacy-Windows box-drawing fallback both off. Left to detect its
    environment, `rich` would pick color and width from the terminal it
    happened to run in, which is exactly what a tool promising byte-identical
    output across machines cannot allow.
    """
    table = Table(title=f"envdoc report for {report.root}")
    table.add_column("Variable")
    table.add_column("Status")
    table.add_column("Required")
    table.add_column("Occurrences")

    for variable in report.variables:
        table.add_row(
            variable.name,
            _status_cell(variable.statuses),
            "yes" if variable.required else "no",
            _occurrence_cell(variable.occurrences),
        )

    buffer = io.StringIO()
    console = Console(
        file=buffer, width=_TABLE_WIDTH, no_color=True, highlight=False, legacy_windows=False
    )
    console.print(table)

    return buffer.getvalue()


def render_markdown(report: Report) -> str:
    """A Markdown table, one row per variable, plus a warnings list.

    This is the format `sync` would write into a docs directory or a PR
    comment, so it favours being readable as plain text over density -- full
    status names rather than symbols, one row per variable rather than one
    per occurrence.
    """
    lines = [f"# envdoc report for `{report.root}`", ""]

    if report.variables:
        lines.append("| Variable | Status | Required | Occurrences |")
        lines.append("| --- | --- | --- | --- |")
        for variable in report.variables:
            lines.append(
                f"| `{variable.name}` "
                f"| {_status_cell(variable.statuses)} "
                f"| {'yes' if variable.required else 'no'} "
                f"| {_occurrence_cell(variable.occurrences)} |"
            )
    else:
        lines.append("No environment variables found.")

    return "\n".join(lines) + "\n"


def _occurrence_payload(occurrence: Occurrence) -> dict[str, object]:
    return {
        "file": str(occurrence.file),
        "line": occurrence.line,
        "column": occurrence.column,
        "source": occurrence.source.value,
        "provider": occurrence.provider.value,
        "required": occurrence.required,
        "default": occurrence.default,
    }


def _variable_payload(variable: Variable) -> dict[str, object]:
    return {
        "name": variable.name,
        "required": variable.required,
        "confidence": variable.confidence.value,
        "status": variable.status.value,
        "statuses": [status.value for status in _sorted_statuses(variable.statuses)],
        "defaults": list(variable.defaults),
        "documented_in_example": variable.documented_in_example,
        "deployment_targets": list(variable.deployment_targets),
        "occurrences": [_occurrence_payload(o) for o in variable.occurrences],
    }


def _dynamic_payload(reference: DynamicRef) -> dict[str, object]:
    return {"expression": reference.expression, **_occurrence_payload(reference.occurrence)}


def render_json(report: Report, *, tool_version: str, generated_at: str | None = None) -> str:
    """Report -> JSON schema v1.

    Key order is fixed by how this dict literal is written, not by
    `sort_keys`: `schema_version` and `tool_version` lead so a consumer can
    check compatibility before parsing the rest. `schema_version` is bumped
    only for a breaking change to this shape -- adding a key is not one.
    """
    payload: dict[str, object] = {
        "schema_version": JSON_SCHEMA_VERSION,
        "tool_version": tool_version,
    }
    if generated_at is not None:
        payload["generated_at"] = generated_at
    payload["root"] = str(report.root)
    payload["files_scanned"] = report.files_scanned
    payload["deployment_files_found"] = list(report.deployment_files_found)
    payload["variables"] = [_variable_payload(v) for v in report.variables]
    payload["dynamic"] = [_dynamic_payload(d) for d in report.dynamic]
    payload["warnings"] = list(report.warnings)

    return json.dumps(payload, indent=2) + "\n"


def render(
    report: Report,
    output_format: OutputFormat,
    *,
    tool_version: str,
    generated_at: str | None = None,
) -> str:
    """Dispatch to the renderer for `output_format`.

    The one place the three functions above are named together, so `cli.py`
    doesn't need its own `if format == ...` chain to stay in sync with this
    module by hand.
    """
    if output_format is OutputFormat.TABLE:
        return render_table(report)
    if output_format is OutputFormat.MARKDOWN:
        return render_markdown(report)
    return render_json(report, tool_version=tool_version, generated_at=generated_at)
