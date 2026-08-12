"""Pins the CLI's observable contract: exit codes and stream discipline.

Deliberately thin. envdoc's core is pure and returns a Report, so almost
everything worth asserting is asserted a layer down with plain equality checks.
What is left here is the part only a real invocation can show -- which exit code
comes back, and which stream the bytes landed on.
"""

from importlib.metadata import version

from typer.testing import CliRunner

from envdoc import cli

runner = CliRunner()


def test_version_flag_prints_the_installed_version_to_stdout_and_exits_zero() -> None:
    result = runner.invoke(cli.app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout == f"{version('envdoc')}\n"
    assert result.stderr == ""
