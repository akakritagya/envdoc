"""Pins the shape of each render format, and the one bug they all share.

`Variable.statuses` is a `frozenset[Status]`, and `Status` is a `StrEnum`, so
its members hash the same as their string value -- which CPython randomises
per process unless `PYTHONHASHSEED` is pinned. Iterating that frozenset
directly can therefore order its members differently between two separate
`uv run` invocations of the identical scan. Every test below that builds a
variable with more than one status checks the output lands in `Status`'s
declaration order, not whatever order the frozenset happened to iterate in --
that is the property `_sorted_statuses` exists to guarantee, and it is cheap
to break by replacing a call to it with a bare `for status in statuses`.

JSON and Markdown are each pinned once against a full, representative report
via an exact string/dict comparison, standing in for a golden file: any
change to the shape of either format has to touch this test.
"""

import json
from pathlib import PurePosixPath

from helpers import occurrence

from envdoc.models import (
    Confidence,
    DynamicRef,
    Occurrence,
    Provider,
    Report,
    SourceKind,
    Status,
    Variable,
)
from envdoc.render import render_json, render_markdown, render_table


def variable(
    name: str = "DATABASE_URL",
    *,
    required: bool = True,
    status: Status = Status.OK,
    statuses: frozenset[Status] = frozenset({Status.OK}),
    defaults: tuple[str, ...] = (),
    documented_in_example: bool = True,
    deployment_targets: tuple[str, ...] = (),
    occurrences: tuple[Occurrence, ...] = (),
) -> Variable:
    return Variable(
        name=name,
        required=required,
        confidence=Confidence.EXACT,
        status=status,
        statuses=statuses,
        defaults=defaults,
        occurrences=occurrences or (occurrence(),),
        documented_in_example=documented_in_example,
        deployment_targets=deployment_targets,
    )


def report(
    *variables: Variable,
    root: str = ".",
    dynamic: tuple[DynamicRef, ...] = (),
    warnings: tuple[str, ...] = (),
    files_scanned: int = 1,
    deployment_files_found: tuple[str, ...] = (),
) -> Report:
    return Report(
        root=PurePosixPath(root),
        variables=variables,
        dynamic=dynamic,
        warnings=warnings,
        files_scanned=files_scanned,
        deployment_files_found=deployment_files_found,
    )


# ---------------------------------------------------------------------------
# The frozenset-ordering trap, pinned once per format.
# ---------------------------------------------------------------------------


def test_json_lists_statuses_in_declaration_order_not_frozenset_order() -> None:
    target = variable(
        statuses=frozenset(
            {Status.ORPHAN_DEPLOYMENT, Status.UNDOCUMENTED, Status.UNSET_IN_DEPLOYMENT}
        )
    )

    payload = json.loads(render_json(report(target), tool_version="0.1.0"))

    assert payload["variables"][0]["statuses"] == [
        "undocumented",
        "unset_in_deployment",
        "orphan_deployment",
    ]


def test_markdown_lists_statuses_in_declaration_order_not_frozenset_order() -> None:
    target = variable(statuses=frozenset({Status.ORPHAN_DEPLOYMENT, Status.STALE}))

    assert "stale, orphan_deployment" in render_markdown(report(target))


def test_table_lists_statuses_in_declaration_order_not_frozenset_order() -> None:
    target = variable(statuses=frozenset({Status.ORPHAN_DEPLOYMENT, Status.STALE}))

    assert "stale, orphan_deployment" in render_table(report(target))


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------


def test_the_full_json_payload_for_a_representative_report() -> None:
    """Stands in for a golden file: pins the entire shape in one place."""
    target = variable(
        name="DATABASE_URL",
        required=True,
        status=Status.UNSET_IN_DEPLOYMENT,
        statuses=frozenset({Status.UNDOCUMENTED, Status.UNSET_IN_DEPLOYMENT}),
        defaults=("postgres://localhost",),
        documented_in_example=False,
        deployment_targets=(),
        occurrences=(occurrence("src/app.py", line=3, column=4),),
    )
    reference = DynamicRef(
        occurrence=Occurrence(
            file=PurePosixPath("src/config.py"),
            line=10,
            column=0,
            source=SourceKind.CODE,
            provider=Provider.PYTHON_AST,
            required=True,
            default=None,
        ),
        expression="key",
    )

    payload = json.loads(
        render_json(
            report(
                target,
                root="src/project",
                dynamic=(reference,),
                warnings=("a.py: skipped, not valid UTF-8",),
                files_scanned=7,
                deployment_files_found=("docker-compose.yml",),
            ),
            tool_version="0.1.0",
        )
    )

    assert payload == {
        "schema_version": 1,
        "tool_version": "0.1.0",
        "root": "src/project",
        "files_scanned": 7,
        "deployment_files_found": ["docker-compose.yml"],
        "variables": [
            {
                "name": "DATABASE_URL",
                "required": True,
                "confidence": "exact",
                "status": "unset_in_deployment",
                "statuses": ["undocumented", "unset_in_deployment"],
                "defaults": ["postgres://localhost"],
                "documented_in_example": False,
                "deployment_targets": [],
                "occurrences": [
                    {
                        "file": "src/app.py",
                        "line": 3,
                        "column": 4,
                        "source": "code",
                        "provider": "python_ast",
                        "required": True,
                        "default": None,
                    }
                ],
            }
        ],
        "dynamic": [
            {
                "expression": "key",
                "file": "src/config.py",
                "line": 10,
                "column": 0,
                "source": "code",
                "provider": "python_ast",
                "required": True,
                "default": None,
            }
        ],
        "warnings": ["a.py: skipped, not valid UTF-8"],
    }


def test_json_omits_generated_at_when_not_given() -> None:
    payload = json.loads(render_json(report(), tool_version="0.1.0"))

    assert "generated_at" not in payload


def test_json_includes_generated_at_immediately_after_tool_version_when_given() -> None:
    payload = json.loads(
        render_json(report(), tool_version="0.1.0", generated_at="2026-08-12T00:00:00Z")
    )

    assert payload["generated_at"] == "2026-08-12T00:00:00Z"
    assert list(payload.keys())[:3] == ["schema_version", "tool_version", "generated_at"]


def test_json_ends_with_exactly_one_trailing_newline() -> None:
    text = render_json(report(), tool_version="0.1.0")

    assert text.endswith("\n")
    assert not text.endswith("\n\n")


def test_json_paths_are_posix_strings_not_python_repr() -> None:
    target = variable(occurrences=(occurrence("src/app.py"),))

    payload = json.loads(render_json(report(target), tool_version="0.1.0"))

    assert payload["variables"][0]["occurrences"][0]["file"] == "src/app.py"


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------


def test_the_full_markdown_output_for_a_representative_report() -> None:
    target = variable(
        name="PORT",
        required=False,
        status=Status.OK,
        statuses=frozenset({Status.OK}),
        occurrences=(occurrence("src/app.py", line=3),),
    )

    text = render_markdown(
        report(target, root="src/project", warnings=("a.py: skipped, not valid UTF-8",))
    )

    assert text == (
        "# envdoc report for `src/project`\n"
        "\n"
        "| Variable | Status | Required | Occurrences |\n"
        "| --- | --- | --- | --- |\n"
        "| `PORT` | ok | no | src/app.py:3 |\n"
        "\n"
        "## Warnings\n"
        "\n"
        "- a.py: skipped, not valid UTF-8\n"
    )


def test_markdown_reports_no_variables_found_on_an_empty_report() -> None:
    assert "No environment variables found." in render_markdown(report())


def test_markdown_omits_the_warnings_section_when_there_are_none() -> None:
    assert "## Warnings" not in render_markdown(report(variable()))


# ---------------------------------------------------------------------------
# Table
# ---------------------------------------------------------------------------


def test_table_output_has_no_ansi_escape_codes() -> None:
    """Color has to come from the caller, not from whatever terminal ran the
    scan -- otherwise two machines produce different bytes for the same
    report."""
    text = render_table(report(variable()))

    assert "\x1b[" not in text


def test_table_includes_the_variable_name_and_status() -> None:
    text = render_table(
        report(
            variable(name="DATABASE_URL", status=Status.STALE, statuses=frozenset({Status.STALE}))
        )
    )

    assert "DATABASE_URL" in text
    assert "stale" in text


def test_table_lists_warnings_after_the_table() -> None:
    text = render_table(report(warnings=("a.py: skipped, not valid UTF-8",)))

    assert "a.py: skipped, not valid UTF-8" in text


def test_table_does_not_crash_on_an_empty_report() -> None:
    text = render_table(report())

    assert "envdoc report for ." in text
