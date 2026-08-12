"""Pins what `sync` writes to `.env.example`, and what it refuses to touch.

`sync` is append-only: it adds a name the code reads that the file does not
yet document, and never rewrites or removes a line it did not add. The two
properties the build plan's gate demands are both here -- a second run on an
already-synced file changes nothing, and a name with more than one distinct
default in code is never silently resolved to one of them (defect E); it gets
an empty value and a comment citing every default and where it came from.
"""

import stat
from pathlib import Path, PurePosixPath

import pytest
from helpers import deployment_entry, example_entry, finding

from envdoc.audit import audit
from envdoc.sources.dotenv import parse as parse_dotenv
from envdoc.sync import EXAMPLE_FILENAME, plan, write


def test_an_undocumented_variable_used_in_code_is_appended() -> None:
    report = audit([finding("DATABASE_URL")])

    result = plan(report)

    assert result.added == ("DATABASE_URL",)
    assert result.changed is True
    assert "DATABASE_URL=\n" in result.updated


def test_a_variable_already_documented_in_the_example_is_not_appended() -> None:
    report = audit([finding("DATABASE_URL"), example_entry("DATABASE_URL")])

    result = plan(report)

    assert result.added == ()
    assert result.changed is False


def test_a_stale_variable_documented_but_unused_is_left_alone() -> None:
    """STALE is a human's call, per the plan's decision -- sync never deletes."""
    report = audit([example_entry("OLD_FLAG")])

    result = plan(report, original="OLD_FLAG=true\n")

    assert result.added == ()
    assert result.updated == "OLD_FLAG=true\n"


def test_a_variable_only_set_in_deployment_is_left_alone() -> None:
    """ORPHAN_DEPLOYMENT means nothing reads it -- documenting it would invent
    a requirement rather than record one."""
    report = audit([deployment_entry("STRAY")], deployment_files=("docker-compose.yml",))

    result = plan(report)

    assert result.added == ()


def test_a_name_already_in_the_original_file_is_never_duplicated() -> None:
    """Deliberately redundant with the report's own UNDOCUMENTED status: an
    --exclude that skipped .env.example during discovery would otherwise make
    every documented name look absent and duplicate the whole file."""
    report = audit([finding("DATABASE_URL")])

    result = plan(report, original="DATABASE_URL=postgres://localhost\n")

    assert result.added == ()
    assert result.changed is False
    assert result.updated == result.original


def test_a_single_default_is_written_as_the_value() -> None:
    report = audit([finding("PORT", required=False, default="8000")])

    result = plan(report)

    assert "PORT=8000\n" in result.updated


def test_no_default_is_written_as_an_empty_value() -> None:
    report = audit([finding("DATABASE_URL", required=True, default=None)])

    result = plan(report)

    assert "DATABASE_URL=\n" in result.updated


def test_conflicting_defaults_are_never_silently_picked() -> None:
    """Defect E: the build spec says to write the literal default found in
    code, undefined when call sites disagree. Neither is picked -- both are
    cited and the value is left for a human to fill in."""
    report = audit(
        [
            finding("PORT", "src/api.py", line=9, required=False, default="8000"),
            finding("PORT", "src/worker.py", line=4, required=False, default="3000"),
        ]
    )

    result = plan(report)

    assert (
        "# envdoc: conflicting defaults -- '3000' (src/worker.py:4), '8000' (src/api.py:9)\nPORT=\n"
    ) in result.updated
    assert "PORT=8000" not in result.updated
    assert "PORT=3000" not in result.updated


def test_appended_entries_follow_the_reports_order() -> None:
    report = audit([finding("ZULU"), finding("ALPHA"), finding("MIKE")])

    result = plan(report)

    assert result.added == ("ALPHA", "MIKE", "ZULU")
    positions = [result.updated.index(f"{name}=") for name in result.added]
    assert positions == sorted(positions)


def test_appending_to_a_file_missing_a_trailing_newline_still_separates_cleanly() -> None:
    report = audit([finding("PORT", required=False, default="8000")])

    result = plan(report, original="EXISTING=1")

    assert result.updated == "EXISTING=1\n\n# Added by envdoc\nPORT=8000\n"


def test_appending_to_a_file_that_already_ends_in_a_blank_line_adds_no_extra_blank() -> None:
    report = audit([finding("PORT", required=False, default="8000")])

    result = plan(report, original="EXISTING=1\n\n")

    assert result.updated == "EXISTING=1\n\n# Added by envdoc\nPORT=8000\n"


def test_a_missing_file_is_created_from_nothing() -> None:
    report = audit([finding("DATABASE_URL")])

    result = plan(report, original="")

    assert result.updated == "# Added by envdoc\nDATABASE_URL=\n"


def test_a_fully_documented_repository_needs_no_changes() -> None:
    report = audit([finding("DATABASE_URL"), example_entry("DATABASE_URL")])

    result = plan(report, original="DATABASE_URL=\n")

    assert result.added == ()
    assert result.changed is False
    assert result.updated == "DATABASE_URL=\n"


@pytest.mark.parametrize(
    "value",
    [
        "postgres://localhost",
        "hello world",
        "a#b",
        'say "hi"',
        "back\\slash",
        "tab\ttab",
        " padded ",
        "",
    ],
    ids=[
        "plain",
        "space",
        "hash",
        "double_quote",
        "backslash",
        "tab",
        "padding",
        "empty",
    ],
)
def test_a_default_written_by_sync_reparses_to_the_same_value(value: str) -> None:
    report = audit([finding("SECRET", required=False, default=value)])

    result = plan(report)

    document = parse_dotenv(result.updated, PurePosixPath(EXAMPLE_FILENAME))
    assert document.entries[0].value == value


def test_write_lands_the_content_and_leaves_no_temporary_file_behind(tmp_path: Path) -> None:
    target = tmp_path / ".env.example"

    write("DATABASE_URL=\n", target)

    assert target.read_text(encoding="utf-8") == "DATABASE_URL=\n"
    assert list(tmp_path.iterdir()) == [target]


def test_a_write_failure_leaves_the_original_file_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / ".env.example"
    target.write_text("DATABASE_URL=\n", encoding="utf-8")

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("os.replace", _boom)

    with pytest.raises(OSError, match="disk full"):
        write("DATABASE_URL=postgres://localhost\n", target)

    assert target.read_text(encoding="utf-8") == "DATABASE_URL=\n"
    assert list(tmp_path.iterdir()) == [target]


def test_an_existing_files_permission_bits_survive_the_write(tmp_path: Path) -> None:
    target = tmp_path / ".env.example"
    target.write_text("DATABASE_URL=\n", encoding="utf-8")
    target.chmod(0o640)

    write("DATABASE_URL=postgres://localhost\n", target)

    assert stat.S_IMODE(target.stat().st_mode) == 0o640
