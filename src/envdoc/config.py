"""Merge CLI flags, `[tool.envdoc]` in `pyproject.toml`, and built-in defaults.

Precedence, highest wins: a flag actually passed on the command line;
failing that, `[tool.envdoc]` in the pyproject.toml at the scanned root;
failing that, the default on `Config` below. There is no upward search past
that one file -- envdoc audits one repository at a time, and a parent
directory's pyproject.toml usually belongs to a project this one doesn't
share config with.

`exclude`, `fail_on`, `format` and `baseline` use `None` at the CLI layer to
mean "the user didn't say", so this module can tell that apart from "said the
default on purpose". For `baseline` specifically, `None` is also the built-in
default -- there is no auto-detected baseline file, only an explicit one, on
the same reasoning as `ConfigError` below: a suppression file nobody asked for
must never silently turn a red build green. `quiet` and `include_timestamp`
don't need the same treatment:
neither flag has a CLI spelling for "explicitly off" (there is no
`--no-quiet`), so presence on the command line is unambiguous and the
precedence collapses to `cli_flag or pyproject_value`.

`FailOn.UNSET` is the built-in default for `fail_on`, not the narrower
`FailOn.UNDOCUMENTED`: the flagship case this tool exists to catch --
required in code, documented in `.env.example`, absent from the deployment
manifest -- produces only `UNSET_IN_DEPLOYMENT`, never `UNDOCUMENTED`, and a
default that didn't gate on it would make `envdoc check` pass on exactly the
bug the project is named for.
"""

import tomllib
from dataclasses import dataclass
from pathlib import Path

from envdoc.models import FailOn
from envdoc.render import OutputFormat


class ConfigError(Exception):
    """`[tool.envdoc]` exists but isn't shaped like one envdoc can read.

    Raised rather than defaulted-and-warned: a typo in `fail_on` that
    silently fell back to the built-in default would make `check` pass when
    the user meant to make it stricter, and a CI job going green on a typo in
    its own gate is the one failure mode a linter's config system cannot
    have.
    """


@dataclass(frozen=True, slots=True)
class Config:
    exclude: tuple[str, ...] = ()
    fail_on: FailOn = FailOn.UNSET
    format: OutputFormat = OutputFormat.TABLE
    quiet: bool = False
    include_timestamp: bool = False
    baseline: str | None = None


def _read_table(root: Path) -> dict[str, object]:
    """The `[tool.envdoc]` table at `root`, or `{}` if there's nothing to read.

    A missing pyproject.toml is not an error -- most repositories envdoc
    scans have one, but nothing requires it. A pyproject.toml that exists but
    has no `[tool.envdoc]` table is the same case: defaults apply, silently.
    """
    path = root / "pyproject.toml"
    if not path.is_file():
        return {}

    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: not valid TOML ({exc})") from exc

    tool = document.get("tool", {})
    if not isinstance(tool, dict):
        return {}
    envdoc = tool.get("envdoc", {})
    if not isinstance(envdoc, dict):
        raise ConfigError(f"{path}: [tool.envdoc] must be a table")
    return envdoc


def _exclude(raw: dict[str, object]) -> tuple[str, ...]:
    if "exclude" not in raw:
        return ()
    value = raw["exclude"]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError("[tool.envdoc] exclude must be a list of strings")
    return tuple(value)


def _fail_on(raw: dict[str, object]) -> FailOn:
    if "fail_on" not in raw:
        return FailOn.UNSET
    value = raw["fail_on"]
    if not isinstance(value, str):
        raise ConfigError("[tool.envdoc] fail_on must be a string")
    try:
        return FailOn(value)
    except ValueError:
        options = ", ".join(member.value for member in FailOn)
        raise ConfigError(
            f"[tool.envdoc] fail_on must be one of {options}, got {value!r}"
        ) from None


def _format(raw: dict[str, object]) -> OutputFormat:
    if "format" not in raw:
        return OutputFormat.TABLE
    value = raw["format"]
    if not isinstance(value, str):
        raise ConfigError("[tool.envdoc] format must be a string")
    try:
        return OutputFormat(value)
    except ValueError:
        options = ", ".join(member.value for member in OutputFormat)
        raise ConfigError(f"[tool.envdoc] format must be one of {options}, got {value!r}") from None


def _baseline(raw: dict[str, object]) -> str | None:
    if "baseline" not in raw:
        return None
    value = raw["baseline"]
    if not isinstance(value, str):
        raise ConfigError("[tool.envdoc] baseline must be a string")
    return value


def _bool(raw: dict[str, object], key: str) -> bool:
    if key not in raw:
        return False
    value = raw[key]
    if not isinstance(value, bool):
        raise ConfigError(f"[tool.envdoc] {key} must be a boolean")
    return value


def resolve(
    root: Path,
    *,
    exclude: tuple[str, ...] | None = None,
    fail_on: FailOn | None = None,
    format: OutputFormat | None = None,
    quiet: bool = False,
    include_timestamp: bool = False,
    baseline: str | None = None,
) -> Config:
    """CLI flag > `[tool.envdoc]` > default, decided independently per field."""
    raw = _read_table(root)

    return Config(
        exclude=exclude if exclude is not None else _exclude(raw),
        fail_on=fail_on if fail_on is not None else _fail_on(raw),
        format=format if format is not None else _format(raw),
        quiet=quiet or _bool(raw, "quiet"),
        include_timestamp=include_timestamp or _bool(raw, "include_timestamp"),
        baseline=baseline if baseline is not None else _baseline(raw),
    )
