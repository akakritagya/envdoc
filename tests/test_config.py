"""Pins config resolution: a CLI flag beats `[tool.envdoc]`, which beats the
built-in default -- decided independently per field, never all-or-nothing.

`quiet` and `include_timestamp` are pinned separately from the other three
fields: neither has a CLI spelling for "explicitly off", so their precedence
collapses to `flag or pyproject_value` rather than "flag, if given, else
pyproject" -- see config.py's module docstring for why.
"""

from pathlib import Path

import pytest

from envdoc.config import Config, ConfigError, resolve
from envdoc.models import FailOn
from envdoc.render import OutputFormat


def _write_pyproject(tmp_path: Path, text: str) -> None:
    (tmp_path / "pyproject.toml").write_text(text, encoding="utf-8")


def test_the_built_in_defaults_apply_with_no_pyproject_toml_and_no_flags(tmp_path: Path) -> None:
    assert resolve(tmp_path) == Config()


def test_a_pyproject_toml_with_no_tool_envdoc_table_uses_the_defaults(tmp_path: Path) -> None:
    _write_pyproject(tmp_path, "[tool.other]\nx = 1\n")

    assert resolve(tmp_path) == Config()


def test_resolving_config_for_a_nonexistent_root_uses_the_defaults(tmp_path: Path) -> None:
    """Config resolution doesn't require the scanned root to exist -- that
    check belongs to discover(), and is the caller's problem, not this
    module's."""
    assert resolve(tmp_path / "does-not-exist") == Config()


@pytest.mark.parametrize(
    ("toml", "field", "expected"),
    [
        ('[tool.envdoc]\nexclude = ["tests/fixtures/*"]\n', "exclude", ("tests/fixtures/*",)),
        ('[tool.envdoc]\nfail_on = "any"\n', "fail_on", FailOn.ANY),
        ('[tool.envdoc]\nformat = "json"\n', "format", OutputFormat.JSON),
        ("[tool.envdoc]\nquiet = true\n", "quiet", True),
        ("[tool.envdoc]\ninclude_timestamp = true\n", "include_timestamp", True),
    ],
    ids=["exclude", "fail_on", "format", "quiet", "include_timestamp"],
)
def test_a_pyproject_value_is_used_when_no_flag_overrides_it(
    tmp_path: Path, toml: str, field: str, expected: object
) -> None:
    _write_pyproject(tmp_path, toml)

    assert getattr(resolve(tmp_path), field) == expected


def test_an_exclude_flag_overrides_pyproject_toml(tmp_path: Path) -> None:
    _write_pyproject(tmp_path, '[tool.envdoc]\nexclude = ["a/*"]\n')

    assert resolve(tmp_path, exclude=("b/*",)).exclude == ("b/*",)


def test_a_fail_on_flag_overrides_pyproject_toml(tmp_path: Path) -> None:
    _write_pyproject(tmp_path, '[tool.envdoc]\nfail_on = "any"\n')

    assert resolve(tmp_path, fail_on=FailOn.STALE).fail_on is FailOn.STALE


def test_a_format_flag_overrides_pyproject_toml(tmp_path: Path) -> None:
    _write_pyproject(tmp_path, '[tool.envdoc]\nformat = "json"\n')

    assert resolve(tmp_path, format=OutputFormat.MARKDOWN).format is OutputFormat.MARKDOWN


def test_the_quiet_flag_forces_quiet_on_even_when_pyproject_does_not_set_it(
    tmp_path: Path,
) -> None:
    assert resolve(tmp_path, quiet=True).quiet is True


def test_the_include_timestamp_flag_forces_it_on_even_when_pyproject_does_not_set_it(
    tmp_path: Path,
) -> None:
    assert resolve(tmp_path, include_timestamp=True).include_timestamp is True


def test_an_empty_exclude_flag_still_overrides_a_nonempty_pyproject_value(
    tmp_path: Path,
) -> None:
    """An empty tuple is a real, explicit choice -- "scan everything" -- not
    the same as None ("the user didn't say"), so it must still win."""
    _write_pyproject(tmp_path, '[tool.envdoc]\nexclude = ["a/*"]\n')

    assert resolve(tmp_path, exclude=()).exclude == ()


def test_invalid_toml_raises_a_config_error(tmp_path: Path) -> None:
    _write_pyproject(tmp_path, "not valid toml [[[")

    with pytest.raises(ConfigError, match="not valid TOML"):
        resolve(tmp_path)


def test_a_tool_envdoc_that_is_not_a_table_raises_a_config_error(tmp_path: Path) -> None:
    _write_pyproject(tmp_path, "[tool]\nenvdoc = 1\n")

    with pytest.raises(ConfigError, match=r"\[tool\.envdoc\] must be a table"):
        resolve(tmp_path)


@pytest.mark.parametrize(
    ("toml", "match"),
    [
        ('[tool.envdoc]\nexclude = "not-a-list"\n', "exclude must be a list of strings"),
        ("[tool.envdoc]\nexclude = [1, 2]\n", "exclude must be a list of strings"),
        ("[tool.envdoc]\nfail_on = 1\n", "fail_on must be a string"),
        ('[tool.envdoc]\nfail_on = "bogus"\n', "fail_on must be one of"),
        ("[tool.envdoc]\nformat = 1\n", "format must be a string"),
        ('[tool.envdoc]\nformat = "bogus"\n', "format must be one of"),
        ('[tool.envdoc]\nquiet = "yes"\n', "quiet must be a boolean"),
        (
            '[tool.envdoc]\ninclude_timestamp = "yes"\n',
            "include_timestamp must be a boolean",
        ),
    ],
    ids=[
        "exclude_not_a_list",
        "exclude_not_all_strings",
        "fail_on_not_a_string",
        "fail_on_not_a_valid_member",
        "format_not_a_string",
        "format_not_a_valid_member",
        "quiet_not_a_boolean",
        "include_timestamp_not_a_boolean",
    ],
)
def test_a_malformed_field_raises_a_config_error(tmp_path: Path, toml: str, match: str) -> None:
    _write_pyproject(tmp_path, toml)

    with pytest.raises(ConfigError, match=match):
        resolve(tmp_path)


def test_the_default_fail_on_is_unset_not_the_narrower_undocumented() -> None:
    """The flagship case -- required in code, documented in .env.example,
    absent from the deployment manifest -- produces only
    UNSET_IN_DEPLOYMENT, never UNDOCUMENTED. A default that didn't gate on it
    would make `check` pass on exactly the bug envdoc is named for."""
    assert Config().fail_on is FailOn.UNSET
