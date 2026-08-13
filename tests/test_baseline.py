"""Pins what `envdoc baseline` writes and what `check --baseline` suppresses.

A baseline is the adoption ramp for the gate `check` provides: a repository
with sixty existing undocumented variables can turn `check` on without going
red on day one, because everything captured today is suppressed and only new
drift still fails. The property that makes envdoc's version of this better
than a `file:line`-keyed one is pinned here directly -- a baseline entry is
`(name, status)`, so moving or renaming a file never invalidates it.

Two more things this pins: suppressing every status a variable carries lands
on `Status.OK`, never an empty set (`audit.py`'s invariant has to survive
`apply()` too), and an obsolete entry -- one whose finding has since been
fixed -- is reported but never makes the build fail.
"""

import json
from dataclasses import replace
from pathlib import PurePosixPath

import pytest
from helpers import example_entry, finding

from envdoc.audit import audit
from envdoc.baseline import (
    BASELINE_FILENAME,
    BASELINE_SCHEMA_VERSION,
    Baseline,
    BaselineError,
    apply,
    capture,
    parse,
    serialize,
)
from envdoc.models import FailOn, Status

_PATH = PurePosixPath(BASELINE_FILENAME)


def test_capture_records_every_non_ok_status_and_skips_ok() -> None:
    report = audit(
        [
            finding("DATABASE_URL"),
            finding("PORT", required=False, default="8000"),
            example_entry("PORT"),
        ]
    )

    result = capture(report)

    assert result.entries == (("DATABASE_URL", Status.UNDOCUMENTED),)


def test_a_baselined_finding_is_suppressed() -> None:
    report = audit([finding("DATABASE_URL")])
    assert report.has_drift(FailOn.UNDOCUMENTED) is True

    result = apply(report, capture(report))

    assert result.report.has_drift(FailOn.UNDOCUMENTED) is False


def test_a_finding_not_in_the_baseline_still_gates() -> None:
    old_report = audit([finding("DATABASE_URL")])
    baseline = capture(old_report)

    new_report = audit([finding("DATABASE_URL"), finding("STRIPE_KEY")])
    result = apply(new_report, baseline)

    assert result.report.has_drift(FailOn.UNDOCUMENTED) is True


def test_suppressing_every_status_leaves_a_variable_ok_never_empty() -> None:
    report = audit([finding("DATABASE_URL")])

    result = apply(report, capture(report))

    variable = result.report.variables[0]
    assert variable.statuses == frozenset({Status.OK})
    assert variable.status is Status.OK


def test_suppressing_one_of_two_statuses_recomputes_the_headline() -> None:
    """UNDOCUMENTED and UNSET_IN_DEPLOYMENT both apply; baselining only the
    one that leads must promote the other to headline, not fall back to OK."""
    report = audit([finding("DATABASE_URL")], deployment_files=("docker-compose.yml",))
    variable = report.variables[0]
    assert variable.statuses == frozenset({Status.UNDOCUMENTED, Status.UNSET_IN_DEPLOYMENT})
    assert variable.status is Status.UNSET_IN_DEPLOYMENT

    only_unset = Baseline(entries=(("DATABASE_URL", Status.UNSET_IN_DEPLOYMENT),))
    result = apply(report, only_unset)

    stripped = result.report.variables[0]
    assert stripped.statuses == frozenset({Status.UNDOCUMENTED})
    assert stripped.status is Status.UNDOCUMENTED


def test_a_variable_absent_from_the_baseline_is_untouched() -> None:
    report = audit([finding("DATABASE_URL"), finding("STRIPE_KEY")])
    baseline = Baseline(entries=(("DATABASE_URL", Status.UNDOCUMENTED),))

    result = apply(report, baseline)

    stripe = next(v for v in result.report.variables if v.name == "STRIPE_KEY")
    assert stripe.statuses == frozenset({Status.UNDOCUMENTED})


def test_a_file_move_does_not_invalidate_a_baseline_entry() -> None:
    """The property that justifies keying by (name, status) rather than
    file:line -- a rename that would silently stop suppressing in a
    line-keyed baseline changes nothing here."""
    before = audit([finding("DATABASE_URL", "src/old_location.py")])
    baseline = capture(before)

    after = audit([finding("DATABASE_URL", "src/new_location.py")])
    result = apply(after, baseline)

    assert result.report.has_drift(FailOn.UNDOCUMENTED) is False


def test_obsolete_entries_are_counted_and_warned_about_without_changing_drift() -> None:
    old_report = audit([finding("DATABASE_URL")])
    baseline = capture(old_report)

    fixed_report = audit([finding("DATABASE_URL"), example_entry("DATABASE_URL")])
    result = apply(fixed_report, baseline)

    assert result.obsolete == (("DATABASE_URL", Status.UNDOCUMENTED),)
    assert result.suppressed == 0
    assert result.report.has_drift(FailOn.ANY) is False
    assert any("no longer applies" in warning for warning in result.report.warnings)


def test_warnings_stay_sorted_after_the_suppression_line_is_appended() -> None:
    report = audit([finding("DATABASE_URL")])
    report = replace(report, warnings=("Zzz an unrelated warning",))

    result = apply(report, capture(report))

    assert result.report.warnings == tuple(sorted(result.report.warnings))
    assert "Zzz an unrelated warning" in result.report.warnings


def test_serialize_then_parse_round_trips() -> None:
    original = Baseline(
        entries=(("DATABASE_URL", Status.UNDOCUMENTED), ("PORT", Status.UNSET_IN_DEPLOYMENT))
    )

    text = serialize(original, tool_version="0.1.0")

    assert parse(text, _PATH) == original


def test_serializing_the_same_baseline_twice_is_byte_identical() -> None:
    report = audit([finding("DATABASE_URL"), finding("ZULU")])
    baseline = capture(report)

    first = serialize(baseline, tool_version="0.1.0")
    second = serialize(baseline, tool_version="0.1.0")

    assert first == second


def test_parsed_entries_sort_independent_of_input_order() -> None:
    def _document(order: list[str]) -> str:
        by_name = {
            "PORT": {"name": "PORT", "status": "unset_in_deployment"},
            "DATABASE_URL": {"name": "DATABASE_URL", "status": "undocumented"},
        }
        return json.dumps(
            {
                "schema_version": BASELINE_SCHEMA_VERSION,
                "tool_version": "0.1.0",
                "entries": [by_name[name] for name in order],
            }
        )

    forward = parse(_document(["PORT", "DATABASE_URL"]), _PATH)
    backward = parse(_document(["DATABASE_URL", "PORT"]), _PATH)

    assert forward == backward
    assert serialize(forward, tool_version="0.1.0") == serialize(backward, tool_version="0.1.0")


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        ("not json", "not valid JSON"),
        ("[]", "must be a JSON object"),
        (
            json.dumps({"schema_version": 2, "tool_version": "x", "entries": []}),
            "schema_version must be 1",
        ),
        (
            json.dumps({"schema_version": 1, "tool_version": "x", "entries": "nope"}),
            "entries must be a list",
        ),
        (
            json.dumps({"schema_version": 1, "tool_version": "x", "entries": [{"name": "X"}]}),
            "must be an object with name and status",
        ),
        (
            json.dumps(
                {
                    "schema_version": 1,
                    "tool_version": "x",
                    "entries": [{"name": "X", "status": "bogus"}],
                }
            ),
            "status must be one of",
        ),
    ],
    ids=[
        "malformed_json",
        "not_an_object",
        "unknown_schema_version",
        "entries_not_a_list",
        "entry_missing_status",
        "unknown_status",
    ],
)
def test_parse_rejects_a_malformed_baseline(payload: str, match: str) -> None:
    with pytest.raises(BaselineError, match=match):
        parse(payload, _PATH)
