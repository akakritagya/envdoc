"""Pins the per-occurrence to per-name aggregation rules.

These are the rules that let envdoc say things about a variable that no single
call site could support: that PORT is required because one of its four uses has
no fallback, or that two files disagree about what its default should be. Every
rule here resolves severity to the maximum rather than the average, on the
grounds that one crashing code path is not made safe by three that cope.

The Status placeholder contract is pinned here too. aggregate() cannot know
whether a name is undocumented -- that needs the example file and the manifests
-- so it leaves status alone and audit.py fills it in. A test guards that,
because a placeholder of OK is exactly the kind of thing that quietly becomes
load-bearing.
"""

from helpers import deployment_entry, example_entry, finding

from envdoc.aggregate import aggregate
from envdoc.models import Confidence, Status


def test_the_same_name_in_three_files_collapses_to_one_variable() -> None:
    result = aggregate(
        [
            finding("DATABASE_URL", "src/a.py", line=3),
            finding("DATABASE_URL", "src/b.py", line=7),
            finding("DATABASE_URL", "src/c.py", line=11),
        ]
    )

    assert len(result.variables) == 1
    variable = result.variables[0]
    assert variable.name == "DATABASE_URL"
    assert len(variable.occurrences) == 3


def test_one_call_site_without_a_default_makes_the_whole_variable_required() -> None:
    result = aggregate(
        [
            finding("PORT", "src/a.py", required=False, default="8000"),
            finding("PORT", "src/b.py", required=False, default="8000"),
            finding("PORT", "src/c.py", required=True, default=None),
        ]
    )

    assert result.variables[0].required is True


def test_every_call_site_having_a_default_leaves_the_variable_optional() -> None:
    result = aggregate(
        [
            finding("PORT", "src/a.py", required=False, default="8000"),
            finding("PORT", "src/b.py", required=False, default="8000"),
        ]
    )

    assert result.variables[0].required is False


def test_conflicting_defaults_warn_and_name_both_files_with_line_numbers() -> None:
    result = aggregate(
        [
            finding("PORT", "src/api.py", line=9, required=False, default="8000"),
            finding("PORT", "src/worker.py", line=4, required=False, default="3000"),
        ]
    )

    assert result.warnings == (
        "Conflicting defaults for PORT: '3000' (src/worker.py:4), '8000' (src/api.py:9)",
    )


def test_one_default_repeated_across_files_is_not_a_conflict() -> None:
    result = aggregate(
        [
            finding("PORT", "src/api.py", required=False, default="8000"),
            finding("PORT", "src/worker.py", required=False, default="8000"),
        ]
    )

    assert result.warnings == ()
    assert result.variables[0].defaults == ("8000",)


def test_defaults_are_deduplicated_and_sorted() -> None:
    result = aggregate(
        [
            finding("PORT", "src/c.py", required=False, default="9000"),
            finding("PORT", "src/a.py", required=False, default="8000"),
            finding("PORT", "src/b.py", required=False, default="9000"),
        ]
    )

    assert result.variables[0].defaults == ("8000", "9000")


def test_an_exact_sighting_outranks_a_heuristic_one() -> None:
    result = aggregate(
        [
            finding("API_KEY", "web/a.js", confidence=Confidence.HEURISTIC),
            finding("API_KEY", "src/b.py", confidence=Confidence.EXACT),
        ]
    )

    assert result.variables[0].confidence is Confidence.EXACT


def test_confidence_stays_heuristic_when_nothing_resolved_it_exactly() -> None:
    result = aggregate(
        [
            finding("API_KEY", "web/a.js", confidence=Confidence.HEURISTIC),
            finding("API_KEY", "web/b.js", confidence=Confidence.HEURISTIC),
        ]
    )

    assert result.variables[0].confidence is Confidence.HEURISTIC


def test_occurrences_are_sorted_by_file_then_line_then_column() -> None:
    result = aggregate(
        [
            finding("X", "src/b.py", line=1, column=0),
            finding("X", "src/a.py", line=9, column=4),
            finding("X", "src/a.py", line=2, column=8),
            finding("X", "src/a.py", line=2, column=2),
        ]
    )

    assert [(str(o.file), o.line, o.column) for o in result.variables[0].occurrences] == [
        ("src/a.py", 2, 2),
        ("src/a.py", 2, 8),
        ("src/a.py", 9, 4),
        ("src/b.py", 1, 0),
    ]


def test_variables_come_out_sorted_by_name() -> None:
    result = aggregate([finding("ZULU"), finding("ALPHA"), finding("MIKE")])

    assert [v.name for v in result.variables] == ["ALPHA", "MIKE", "ZULU"]


def test_an_example_entry_marks_the_variable_documented_without_making_it_required() -> None:
    result = aggregate([example_entry("DATABASE_URL")])

    variable = result.variables[0]
    assert variable.documented_in_example is True
    assert variable.required is False
    assert variable.defaults == ()


def test_deployment_targets_list_each_manifest_once_and_sorted() -> None:
    result = aggregate(
        [
            deployment_entry("PORT", "docker-compose.yml", line=4),
            deployment_entry("PORT", "fly.toml", line=2),
            deployment_entry("PORT", "docker-compose.yml", line=9),
        ]
    )

    assert result.variables[0].deployment_targets == ("docker-compose.yml", "fly.toml")


def test_non_code_occurrences_never_contribute_required_or_defaults() -> None:
    """The regression guard for Occurrence.required being code-only.

    An .env.example line and a compose key both arrive with required=False and
    default=None, but the rule is that they are ignored rather than merely
    falsy -- so this pins the case where the only code sighting is optional and
    the non-code ones must not drag anything else in.
    """
    result = aggregate(
        [
            finding("PORT", "src/a.py", required=False, default="8000"),
            example_entry("PORT"),
            deployment_entry("PORT"),
        ]
    )

    variable = result.variables[0]
    assert variable.required is False
    assert variable.defaults == ("8000",)
    assert len(variable.occurrences) == 3


def test_status_is_left_as_a_placeholder_for_the_audit_to_fill_in() -> None:
    result = aggregate([finding("DATABASE_URL")])

    variable = result.variables[0]
    assert variable.status is Status.OK
    assert variable.statuses == frozenset()


def test_aggregating_nothing_produces_nothing() -> None:
    result = aggregate([])

    assert result.variables == ()
    assert result.warnings == ()
