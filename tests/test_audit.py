"""Pins the three-way set algebra: which axes a name appears on, and the verdict.

This is the module the tool is named for. Two-way scanners compare *used in
code* against *listed in .env.example* and stop there. The third axis -- what
the deployment actually provides -- is what catches a variable the code
requires, the example documents, and the compose file never sets. It works on
the developer's laptop and the container dies on boot, and no two-way audit can
see it coming.

Every combination of the three axes is pinned below, because the table is the
specification and a gap in it is a verdict somebody invented later.

Two asymmetries in the rules are deliberate and easy to mistake for
inconsistency:

    - A missing .env.example makes every variable UNDOCUMENTED, but missing
      deployment manifests make nothing UNSET_IN_DEPLOYMENT. The absence of an
      example file *is* the finding -- that is what this tool is for. The
      absence of a compose file means the project is not containerised, which
      is not a defect, and flagging every required variable in a library would
      teach people to ignore the one status that matters most.

    - UNSET_IN_DEPLOYMENT is reported for every variable but gates the build
      only for required ones. A variable with a fallback that no manifest sets
      degrades to its default; one without takes the process down on boot.
"""

from pathlib import PurePosixPath

import pytest
from helpers import deployment_entry, example_entry, finding

from envdoc.audit import audit
from envdoc.models import DynamicRef, FailOn, Occurrence, Provider, SourceKind, Status

COMPOSE = ("docker-compose.yml",)


def statuses_of(*findings: object, deployment_files: tuple[str, ...] = COMPOSE) -> set[Status]:
    report = audit(findings, deployment_files=deployment_files)  # type: ignore[arg-type]
    assert len(report.variables) == 1, f"expected one variable, got {report.variables}"
    return set(report.variables[0].statuses)


@pytest.mark.parametrize(
    ("sources", "expected"),
    [
        (("code", "example", "deployment"), {Status.OK}),
        (("code", "example"), {Status.UNSET_IN_DEPLOYMENT}),
        (("code", "deployment"), {Status.UNDOCUMENTED}),
        (("code",), {Status.UNDOCUMENTED, Status.UNSET_IN_DEPLOYMENT}),
        (("example", "deployment"), {Status.STALE}),
        (("example",), {Status.STALE}),
        (("deployment",), {Status.ORPHAN_DEPLOYMENT}),
    ],
    ids=[
        "on_all_three_axes_is_ok",
        "used_and_documented_but_never_set",
        "used_and_set_but_undocumented",
        "used_only",
        "documented_and_set_but_unused",
        "documented_only",
        "set_only",
    ],
)
def test_every_row_of_the_three_way_table(sources: tuple[str, ...], expected: set[Status]) -> None:
    """The gate for this group. Eight combinations, one of which cannot occur.

    A name on none of the three axes produces no occurrences and therefore no
    Variable, so there are seven rows rather than eight.
    """
    builders = {"code": finding, "example": example_entry, "deployment": deployment_entry}

    assert statuses_of(*(builders[source]("DATABASE_URL") for source in sources)) == expected


def test_a_variable_used_and_documented_but_absent_from_the_compose_file() -> None:
    """The flagship case, spelled out on its own because it is the whole thesis.

    Required in code, present in .env.example, and the compose file never sets
    it. A two-way audit calls this clean and the container dies on boot.
    """
    report = audit(
        [finding("DATABASE_URL", required=True), example_entry("DATABASE_URL")],
        deployment_files=COMPOSE,
    )

    variable = report.variables[0]
    assert variable.status is Status.UNSET_IN_DEPLOYMENT
    assert report.has_drift(FailOn.UNSET) is True


def test_nothing_is_unset_in_deployment_when_no_manifests_were_found() -> None:
    """A library with no compose file is not a repository full of defects.

    Reporting every required variable as unset in a project that was never
    containerised is noise, and noise in the one status this tool exists for is
    worse than silence -- people learn to skip it.
    """
    assert statuses_of(finding("DATABASE_URL"), deployment_files=()) == {Status.UNDOCUMENTED}


def test_a_manifest_that_sets_nothing_still_counts_as_a_manifest() -> None:
    """The case that makes it wrong to infer the manifests from the findings.

    A docker-compose.yml with no `environment:` block at all is precisely the
    situation where every variable is unset in deployment. Deriving "were there
    manifests?" from deployment occurrences would conclude there were none and
    report the file as clean.
    """
    report = audit(
        [finding("DATABASE_URL"), example_entry("DATABASE_URL")], deployment_files=COMPOSE
    )

    assert report.variables[0].statuses == frozenset({Status.UNSET_IN_DEPLOYMENT})


def test_a_missing_example_file_leaves_everything_undocumented() -> None:
    """The asymmetry with deployment, stated as a test.

    No .env.example is not "nothing to compare against" -- it is the finding
    itself, and the one `sync` exists to fix.
    """
    report = audit([finding("A"), finding("B")], deployment_files=())

    assert [v.status for v in report.variables] == [Status.UNDOCUMENTED, Status.UNDOCUMENTED]


@pytest.mark.parametrize(
    ("sources", "expected"),
    [
        (("code",), Status.UNSET_IN_DEPLOYMENT),
        (("example", "deployment"), Status.STALE),
        (("deployment",), Status.ORPHAN_DEPLOYMENT),
        (("code", "example", "deployment"), Status.OK),
    ],
    ids=[
        "unset_leads_over_undocumented",
        "stale_leads_over_nothing_else",
        "orphan_when_it_is_the_only_one",
        "ok_when_there_is_no_problem",
    ],
)
def test_the_headline_status_is_the_one_most_likely_to_take_production_down(
    sources: tuple[str, ...], expected: Status
) -> None:
    """One variable can carry two problems, and a table shows one per row.

    The order is by consequence, not by how the enum happens to be written:
    unset in deployment kills the process on boot, undocumented means nobody
    knows to set it, stale is dead documentation, orphaned is dead config.
    """
    builders = {"code": finding, "example": example_entry, "deployment": deployment_entry}
    report = audit(
        [builders[source]("DATABASE_URL") for source in sources], deployment_files=COMPOSE
    )

    assert report.variables[0].status is expected


def test_the_full_status_set_is_kept_even_though_one_leads() -> None:
    """Collapsing to the headline would hide half of what needs fixing."""
    report = audit([finding("DATABASE_URL")], deployment_files=COMPOSE)

    assert report.variables[0].status is Status.UNSET_IN_DEPLOYMENT
    assert report.variables[0].statuses == frozenset(
        {Status.UNDOCUMENTED, Status.UNSET_IN_DEPLOYMENT}
    )


def test_an_ok_variable_carries_ok_rather_than_an_empty_set() -> None:
    """`statuses` is never empty once audited, so by_status(OK) can find them.

    aggregate() leaves an empty set as its placeholder, and a placeholder that
    survives into a report is exactly the kind of thing that quietly becomes
    load-bearing.
    """
    report = audit(
        [finding("A"), example_entry("A"), deployment_entry("A")], deployment_files=COMPOSE
    )

    assert report.variables[0].statuses == frozenset({Status.OK})


def test_by_status_finds_a_variable_by_a_status_that_is_not_the_headline() -> None:
    report = audit([finding("DATABASE_URL")], deployment_files=COMPOSE)

    assert [v.name for v in report.by_status(Status.UNDOCUMENTED)] == ["DATABASE_URL"]


def test_an_optional_variable_unset_in_deployment_is_reported_but_does_not_gate() -> None:
    """It degrades to its default. Worth printing, not worth breaking a build."""
    report = audit(
        [finding("PORT", required=False, default="8000"), example_entry("PORT")],
        deployment_files=COMPOSE,
    )

    assert report.variables[0].status is Status.UNSET_IN_DEPLOYMENT
    assert report.has_drift(FailOn.UNSET) is False


def test_a_required_variable_unset_in_deployment_gates() -> None:
    report = audit(
        [finding("DATABASE_URL", required=True), example_entry("DATABASE_URL")],
        deployment_files=COMPOSE,
    )

    assert report.has_drift(FailOn.UNSET) is True


def test_a_clean_repository_has_no_drift_at_any_threshold() -> None:
    report = audit(
        [finding("A"), example_entry("A"), deployment_entry("A")], deployment_files=COMPOSE
    )

    assert report.has_drift(FailOn.UNDOCUMENTED) is False
    assert report.has_drift(FailOn.ANY) is False


def test_the_report_carries_the_scan_context_it_was_given() -> None:
    report = audit(
        [finding("A")],
        root=PurePosixPath("src/project"),
        files_scanned=42,
        deployment_files=("docker-compose.yml", "fly.toml"),
    )

    assert report.root == PurePosixPath("src/project")
    assert report.files_scanned == 42
    assert report.deployment_files_found == ("docker-compose.yml", "fly.toml")


def test_deployment_files_are_deduplicated_and_sorted() -> None:
    """They reach rendering, so they follow the same rule as everything else."""
    report = audit([], deployment_files=("fly.toml", "docker-compose.yml", "fly.toml"))

    assert report.deployment_files_found == ("docker-compose.yml", "fly.toml")


def test_dynamic_references_are_carried_through_without_becoming_variables() -> None:
    """A DynamicRef has no name, so it cannot be audited -- but dropping it
    would hide a read that no static analysis will ever see."""
    reference = DynamicRef(
        occurrence=Occurrence(
            file=PurePosixPath("src/app.py"),
            line=3,
            column=0,
            source=SourceKind.CODE,
            provider=Provider.PYTHON_AST,
            required=True,
            default=None,
        ),
        expression="key",
    )

    report = audit([finding("A")], dynamic=[reference])

    assert report.dynamic == (reference,)
    assert [v.name for v in report.variables] == ["A"]


def test_warnings_from_every_layer_arrive_together_in_a_fixed_order() -> None:
    """Discovery, the parsers and aggregation all produce them, and the CLI
    prints one list. Sorting is what keeps that list identical run to run."""
    report = audit(
        [
            finding("PORT", "src/api.py", line=9, required=False, default="8000"),
            finding("PORT", "src/worker.py", line=4, required=False, default="3000"),
        ],
        warnings=["zebra.py: skipped, not valid UTF-8", "apple.py: skipped, not valid UTF-8"],
    )

    assert report.warnings == (
        "Conflicting defaults for PORT: '3000' (src/worker.py:4), '8000' (src/api.py:9)",
        "apple.py: skipped, not valid UTF-8",
        "zebra.py: skipped, not valid UTF-8",
    )


def test_variables_come_out_sorted_by_name() -> None:
    report = audit([finding("ZULU"), finding("ALPHA"), finding("MIKE")])

    assert [v.name for v in report.variables] == ["ALPHA", "MIKE", "ZULU"]


def test_auditing_the_same_findings_twice_gives_the_same_report() -> None:
    findings = [finding("B"), example_entry("B"), finding("A"), deployment_entry("C")]

    assert audit(findings, deployment_files=COMPOSE) == audit(findings, deployment_files=COMPOSE)


def test_auditing_nothing_produces_an_empty_report() -> None:
    report = audit([])

    assert report.variables == ()
    assert report.warnings == ()
    assert report.has_drift(FailOn.ANY) is False
