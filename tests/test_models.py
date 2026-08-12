"""Pins the model's immutability and the gating rule `envdoc check` runs on.

has_drift is the whole product surface of the exit-code contract: everything
`check` does is call it and turn the answer into 0 or 1. Two things about it are
easy to get subtly wrong and are therefore tested exhaustively here -- that the
thresholds are cumulative, and that an unset-in-deployment variable only gates
when it is required. A variable with a fallback that no manifest sets degrades
to its default; one without takes the process down on boot. Breaking a build
over the first is how a linter gets removed from CI.
"""

from dataclasses import FrozenInstanceError
from pathlib import PurePosixPath

import pytest
from helpers import finding, occurrence

from envdoc.models import _GATING_STATUSES, Confidence, FailOn, Report, Status, Variable


def variable(
    name: str = "PORT",
    *,
    required: bool = True,
    statuses: frozenset[Status] = frozenset(),
) -> Variable:
    return Variable(
        name=name,
        required=required,
        confidence=Confidence.EXACT,
        status=next(iter(sorted(statuses)), Status.OK),
        statuses=statuses,
        defaults=(),
        occurrences=(occurrence(),),
        documented_in_example=False,
        deployment_targets=(),
    )


def report(*variables: Variable) -> Report:
    return Report(
        root=PurePosixPath("."),
        variables=variables,
        dynamic=(),
        warnings=(),
        files_scanned=1,
        deployment_files_found=(),
    )


def test_a_finding_cannot_be_mutated_after_construction() -> None:
    with pytest.raises(FrozenInstanceError, match="cannot assign to field 'name'"):
        finding("PORT").name = "OTHER"  # type: ignore[misc]


def test_a_finding_is_hashable_so_it_can_live_in_a_set() -> None:
    one = finding("PORT", "src/a.py", line=3)
    same = finding("PORT", "src/a.py", line=3)
    other = finding("PORT", "src/b.py", line=3)

    assert {one, same, other} == {one, other}


@pytest.mark.parametrize(
    ("status", "threshold", "expected"),
    [
        (Status.UNDOCUMENTED, FailOn.UNDOCUMENTED, True),
        (Status.UNDOCUMENTED, FailOn.UNSET, True),
        (Status.UNDOCUMENTED, FailOn.STALE, True),
        (Status.UNDOCUMENTED, FailOn.ANY, True),
        (Status.UNSET_IN_DEPLOYMENT, FailOn.UNDOCUMENTED, False),
        (Status.UNSET_IN_DEPLOYMENT, FailOn.UNSET, True),
        (Status.UNSET_IN_DEPLOYMENT, FailOn.STALE, True),
        (Status.UNSET_IN_DEPLOYMENT, FailOn.ANY, True),
        (Status.STALE, FailOn.UNDOCUMENTED, False),
        (Status.STALE, FailOn.UNSET, False),
        (Status.STALE, FailOn.STALE, True),
        (Status.STALE, FailOn.ANY, True),
        (Status.ORPHAN_DEPLOYMENT, FailOn.UNDOCUMENTED, False),
        (Status.ORPHAN_DEPLOYMENT, FailOn.UNSET, False),
        (Status.ORPHAN_DEPLOYMENT, FailOn.STALE, False),
        (Status.ORPHAN_DEPLOYMENT, FailOn.ANY, True),
        (Status.OK, FailOn.ANY, False),
    ],
    ids=[
        "undocumented_at_undocumented",
        "undocumented_at_unset",
        "undocumented_at_stale",
        "undocumented_at_any",
        "unset_at_undocumented",
        "unset_at_unset",
        "unset_at_stale",
        "unset_at_any",
        "stale_at_undocumented",
        "stale_at_unset",
        "stale_at_stale",
        "stale_at_any",
        "orphan_at_undocumented",
        "orphan_at_unset",
        "orphan_at_stale",
        "orphan_at_any",
        "ok_never_gates",
    ],
)
def test_each_status_gates_at_exactly_the_thresholds_that_cover_it(
    status: Status, threshold: FailOn, expected: bool
) -> None:
    assert report(variable(statuses=frozenset({status}))).has_drift(threshold) is expected


def test_an_optional_variable_unset_in_deployment_does_not_break_the_build() -> None:
    drifted = report(variable(required=False, statuses=frozenset({Status.UNSET_IN_DEPLOYMENT})))

    assert drifted.has_drift(FailOn.UNSET) is False
    assert drifted.has_drift(FailOn.ANY) is False


def test_a_required_variable_unset_in_deployment_does_break_the_build() -> None:
    drifted = report(variable(required=True, statuses=frozenset({Status.UNSET_IN_DEPLOYMENT})))

    assert drifted.has_drift(FailOn.UNSET) is True


def test_a_second_status_still_gates_when_the_headline_one_does_not() -> None:
    """A variable can carry more than one problem, and any of them can gate."""
    both = report(
        variable(
            required=False,
            statuses=frozenset({Status.UNSET_IN_DEPLOYMENT, Status.UNDOCUMENTED}),
        )
    )

    assert both.has_drift(FailOn.UNDOCUMENTED) is True


def test_a_clean_report_has_no_drift_at_the_loosest_threshold() -> None:
    assert report(variable(statuses=frozenset({Status.OK}))).has_drift(FailOn.ANY) is False


def test_an_empty_report_has_no_drift() -> None:
    assert report().has_drift(FailOn.ANY) is False


def test_by_status_finds_a_variable_by_a_secondary_status_not_just_the_headline() -> None:
    target = variable(
        "DATABASE_URL",
        statuses=frozenset({Status.UNDOCUMENTED, Status.UNSET_IN_DEPLOYMENT}),
    )
    other = variable("PORT", statuses=frozenset({Status.STALE}))

    assert report(target, other).by_status(Status.UNSET_IN_DEPLOYMENT) == (target,)


def test_by_status_returns_nothing_when_no_variable_carries_it() -> None:
    assert report(variable(statuses=frozenset({Status.OK}))).by_status(Status.STALE) == ()


def test_every_fail_on_threshold_names_a_gating_set() -> None:
    """has_drift indexes _GATING_STATUSES directly, so a FailOn member left
    out of it would raise KeyError the first time someone gated on it, rather
    than failing at import time where it costs nothing to notice. The
    module-level assertion in models.py is what enforces this; this test pins
    that the assertion holds and stays holding."""
    assert set(_GATING_STATUSES) == set(FailOn)


def test_fail_on_any_gates_on_every_status_except_ok() -> None:
    assert _GATING_STATUSES[FailOn.ANY] == set(Status) - {Status.OK}
