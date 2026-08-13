"""Pins the CLI's observable contract: exit codes and stream discipline.

Deliberately thin. envdoc's core is pure and returns a Report, so almost
everything worth asserting is asserted a layer down with plain equality checks.
What is left here is the part only a real invocation can show -- which exit code
comes back, and which stream the bytes landed on.

Exit codes are the load-bearing part: 0 clean, 1 drift (check only -- scan
never gates), 2 envdoc itself failed. Warnings go to stderr, unless --quiet;
the report itself always goes to stdout, so a shell pipeline can consume one
without the other.
"""

import json
from importlib.metadata import version
from pathlib import Path

from typer.testing import CliRunner

from envdoc import cli

runner = CliRunner()


def _write_clean_repo(root: Path) -> None:
    """A required variable, used and documented, with no compose file at all
    -- OK, since a missing manifest means "never containerised", not "unset".
    """
    (root / "app.py").write_text('import os\nos.environ["DATABASE_URL"]\n', encoding="utf-8")
    (root / ".env.example").write_text("DATABASE_URL=\n", encoding="utf-8")


def _write_drifted_repo(root: Path) -> None:
    """A required variable used in code but never documented -- UNDOCUMENTED,
    which gates at every FailOn threshold including the built-in default.
    Reused by the `sync` tests (what it exists to fix) and the `baseline`
    tests (what a baseline suppresses)."""
    (root / "app.py").write_text('import os\nos.environ["DATABASE_URL"]\n', encoding="utf-8")


def test_the_flagship_case_a_required_variable_missing_from_compose_gates(tmp_path: Path) -> None:
    """The scenario this project is named for: required in code, documented
    in .env.example, and the compose file never sets it -- works on a
    developer's laptop and dies in the container. A two-way audit calls this
    clean; envdoc's third axis is what catches it."""
    _write_clean_repo(tmp_path)
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n  web:\n    environment:\n      - PORT=8000\n", encoding="utf-8"
    )

    result = runner.invoke(cli.app, ["check", str(tmp_path)])

    assert result.exit_code == 1
    assert "unset_in_deployment" in result.stdout


def test_check_exits_zero_when_the_compose_file_sets_the_variable(tmp_path: Path) -> None:
    _write_clean_repo(tmp_path)
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n  web:\n    environment:\n      - DATABASE_URL=postgres://localhost\n",
        encoding="utf-8",
    )

    result = runner.invoke(cli.app, ["check", str(tmp_path)])

    assert result.exit_code == 0


def test_version_flag_prints_the_installed_version_to_stdout_and_exits_zero() -> None:
    result = runner.invoke(cli.app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout == f"{version('envdoc')}\n"
    assert result.stderr == ""


def test_scan_exits_zero_on_a_clean_repository(tmp_path: Path) -> None:
    _write_clean_repo(tmp_path)

    result = runner.invoke(cli.app, ["scan", str(tmp_path)])

    assert result.exit_code == 0


def test_scan_exits_zero_even_when_the_repository_has_drift(tmp_path: Path) -> None:
    """scan never gates -- that's check's job alone."""
    _write_drifted_repo(tmp_path)

    result = runner.invoke(cli.app, ["scan", str(tmp_path)])

    assert result.exit_code == 0


def test_check_exits_zero_on_a_clean_repository(tmp_path: Path) -> None:
    _write_clean_repo(tmp_path)

    result = runner.invoke(cli.app, ["check", str(tmp_path)])

    assert result.exit_code == 0


def test_check_exits_one_when_drift_is_at_or_above_the_threshold(tmp_path: Path) -> None:
    _write_drifted_repo(tmp_path)

    result = runner.invoke(cli.app, ["check", str(tmp_path)])

    assert result.exit_code == 1


def test_check_exits_zero_when_the_fail_on_flag_is_looser_than_the_drift(tmp_path: Path) -> None:
    """A name documented in .env.example but never read in code is STALE,
    which gates at --fail-on stale but not at the built-in default (unset) --
    UNDOCUMENTED is in every threshold's gating set, so this needs a status
    below it rather than a looser flag on the same one."""
    (tmp_path / ".env.example").write_text("UNUSED=\n", encoding="utf-8")

    default_threshold = runner.invoke(cli.app, ["check", str(tmp_path)])
    stale_threshold = runner.invoke(cli.app, ["check", str(tmp_path), "--fail-on", "stale"])

    assert default_threshold.exit_code == 0
    assert stale_threshold.exit_code == 1


def test_scan_exits_two_for_a_nonexistent_path() -> None:
    result = runner.invoke(cli.app, ["scan", "/no/such/directory"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "no such directory" in result.stderr


def test_check_exits_two_for_a_nonexistent_path() -> None:
    result = runner.invoke(cli.app, ["check", "/no/such/directory"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "no such directory" in result.stderr


def test_check_exits_two_on_a_malformed_pyproject_toml(tmp_path: Path) -> None:
    _write_clean_repo(tmp_path)
    (tmp_path / "pyproject.toml").write_text('[tool.envdoc]\nfail_on = "bogus"\n', encoding="utf-8")

    result = runner.invoke(cli.app, ["check", str(tmp_path)])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "fail_on must be one of" in result.stderr


def test_the_report_is_printed_to_stdout(tmp_path: Path) -> None:
    """The exact table layout is rich's to own, not this test's to pin --
    just that the report, not just warnings, lands on stdout."""
    _write_clean_repo(tmp_path)

    result = runner.invoke(cli.app, ["scan", str(tmp_path)])

    assert "DATABASE_URL" in result.stdout


def test_warnings_go_to_stderr_not_stdout(tmp_path: Path) -> None:
    (tmp_path / "bad.py").write_bytes(b"x\xff\xfe")

    result = runner.invoke(cli.app, ["scan", str(tmp_path)])

    assert "skipped" in result.stderr
    assert "skipped" not in result.stdout


def test_quiet_suppresses_warnings_on_stderr(tmp_path: Path) -> None:
    (tmp_path / "bad.py").write_bytes(b"x\xff\xfe")

    result = runner.invoke(cli.app, ["scan", str(tmp_path), "--quiet"])

    assert result.stderr == ""


def test_format_json_selects_the_json_renderer(tmp_path: Path) -> None:
    _write_clean_repo(tmp_path)

    result = runner.invoke(cli.app, ["scan", str(tmp_path), "--format", "json"])

    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 1


def test_an_unrecognised_format_value_exits_two(tmp_path: Path) -> None:
    _write_clean_repo(tmp_path)

    result = runner.invoke(cli.app, ["scan", str(tmp_path), "--format", "yaml"])

    assert result.exit_code == 2


def test_an_exclude_flag_removes_a_file_from_the_scan(tmp_path: Path) -> None:
    _write_clean_repo(tmp_path)
    (tmp_path / "vendor").mkdir()
    (tmp_path / "vendor" / "lib.py").write_text(
        'import os\nos.environ["VENDORED"]\n', encoding="utf-8"
    )

    result = runner.invoke(
        cli.app, ["scan", str(tmp_path), "--format", "json", "--exclude", "vendor/*"]
    )

    names = [v["name"] for v in json.loads(result.stdout)["variables"]]
    assert names == ["DATABASE_URL"]


def test_sync_adds_a_missing_variable_and_exits_zero(tmp_path: Path) -> None:
    _write_drifted_repo(tmp_path)

    result = runner.invoke(cli.app, ["sync", str(tmp_path)])

    assert result.exit_code == 0
    assert "+ DATABASE_URL" in result.stdout
    assert (tmp_path / ".env.example").read_text(encoding="utf-8") == (
        "# Added by envdoc\nDATABASE_URL=\n"
    )


def test_sync_run_twice_leaves_the_file_byte_identical(tmp_path: Path) -> None:
    """The gate's idempotency check: a second run on an already-synced
    repository changes nothing."""
    _write_drifted_repo(tmp_path)
    runner.invoke(cli.app, ["sync", str(tmp_path)])
    first = (tmp_path / ".env.example").read_bytes()

    result = runner.invoke(cli.app, ["sync", str(tmp_path)])

    assert result.exit_code == 0
    assert "up to date" in result.stdout
    assert (tmp_path / ".env.example").read_bytes() == first


def test_sync_dry_run_reports_the_addition_and_writes_nothing(tmp_path: Path) -> None:
    _write_drifted_repo(tmp_path)

    result = runner.invoke(cli.app, ["sync", str(tmp_path), "--dry-run"])

    assert result.exit_code == 0
    assert "+ DATABASE_URL" in result.stdout
    assert not (tmp_path / ".env.example").exists()


def test_sync_preserves_comments_in_an_existing_file(tmp_path: Path) -> None:
    _write_drifted_repo(tmp_path)
    (tmp_path / ".env.example").write_text(
        "# where to get one: the dashboard\nSTRIPE_KEY=sk_test\n", encoding="utf-8"
    )

    result = runner.invoke(cli.app, ["sync", str(tmp_path)])

    text = (tmp_path / ".env.example").read_text(encoding="utf-8")
    assert result.exit_code == 0
    assert text.startswith("# where to get one: the dashboard\nSTRIPE_KEY=sk_test\n")
    assert "DATABASE_URL=" in text


def test_sync_never_gates_even_with_pending_changes(tmp_path: Path) -> None:
    """Exit 1 means drift, and gating is `check`'s job alone -- sync always
    exits 0, dry-run or not."""
    _write_drifted_repo(tmp_path)

    result = runner.invoke(cli.app, ["sync", str(tmp_path), "--dry-run"])

    assert result.exit_code == 0


def test_sync_exits_two_for_a_nonexistent_path() -> None:
    result = runner.invoke(cli.app, ["sync", "/no/such/directory"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "no such directory" in result.stderr


def test_check_with_baseline_exits_zero_when_all_drift_is_baselined(tmp_path: Path) -> None:
    _write_drifted_repo(tmp_path)
    runner.invoke(cli.app, ["baseline", str(tmp_path)])

    result = runner.invoke(cli.app, ["check", str(tmp_path), "--baseline", ".envdoc-baseline.json"])

    assert result.exit_code == 0


def test_check_with_baseline_still_gates_on_new_drift(tmp_path: Path) -> None:
    """Adopting a baseline doesn't grandfather in future drift -- only what
    was captured at baseline time is suppressed."""
    _write_drifted_repo(tmp_path)
    runner.invoke(cli.app, ["baseline", str(tmp_path)])
    (tmp_path / "worker.py").write_text('import os\nos.environ["STRIPE_KEY"]\n', encoding="utf-8")

    result = runner.invoke(cli.app, ["check", str(tmp_path), "--baseline", ".envdoc-baseline.json"])

    assert result.exit_code == 1
    assert "STRIPE_KEY" in result.stdout


def test_check_without_baseline_still_gates_normally(tmp_path: Path) -> None:
    """A baseline is opt-in -- writing one must not change `check`'s default
    behaviour for callers that never pass --baseline."""
    _write_drifted_repo(tmp_path)
    runner.invoke(cli.app, ["baseline", str(tmp_path)])

    result = runner.invoke(cli.app, ["check", str(tmp_path)])

    assert result.exit_code == 1


def test_check_exits_two_when_the_configured_baseline_file_is_missing(tmp_path: Path) -> None:
    """Opt-in means a typo'd or unwritten baseline path is a broken
    configuration, exit 2 -- never a silent pass that suppresses nothing."""
    _write_clean_repo(tmp_path)

    result = runner.invoke(cli.app, ["check", str(tmp_path), "--baseline", "nope.json"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "no such baseline file" in result.stderr


def test_baseline_command_writes_the_file_and_lists_captured_entries(tmp_path: Path) -> None:
    _write_drifted_repo(tmp_path)

    result = runner.invoke(cli.app, ["baseline", str(tmp_path)])

    assert result.exit_code == 0
    assert "+ DATABASE_URL: undocumented" in result.stdout
    assert (tmp_path / ".envdoc-baseline.json").exists()


def test_baseline_command_dry_run_reports_and_writes_nothing(tmp_path: Path) -> None:
    _write_drifted_repo(tmp_path)

    result = runner.invoke(cli.app, ["baseline", str(tmp_path), "--dry-run"])

    assert result.exit_code == 0
    assert "+ DATABASE_URL: undocumented" in result.stdout
    assert not (tmp_path / ".envdoc-baseline.json").exists()


def test_baseline_command_run_twice_leaves_the_file_byte_identical(tmp_path: Path) -> None:
    _write_drifted_repo(tmp_path)
    runner.invoke(cli.app, ["baseline", str(tmp_path)])
    first = (tmp_path / ".envdoc-baseline.json").read_bytes()

    result = runner.invoke(cli.app, ["baseline", str(tmp_path)])

    assert result.exit_code == 0
    assert (tmp_path / ".envdoc-baseline.json").read_bytes() == first


def test_baseline_command_never_gates_even_with_drift_present(tmp_path: Path) -> None:
    _write_drifted_repo(tmp_path)

    result = runner.invoke(cli.app, ["baseline", str(tmp_path)])

    assert result.exit_code == 0


def test_baseline_command_exits_two_for_a_nonexistent_path() -> None:
    result = runner.invoke(cli.app, ["baseline", "/no/such/directory"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "no such directory" in result.stderr
