"""Pins how the `pydantic-settings` extractor resolves a field's real
environment variable name, and where it refuses to guess.

Every rule pinned here was verified by actually instantiating
`pydantic-settings` 2.15 / `pydantic` 2.13 models before this module was
written, not assumed from documentation -- in particular the two rules a
naive reading would get wrong: an `alias`/`validation_alias` on a field
replaces the `env_prefix + FIELD_NAME` computation entirely rather than
narrowing it, and `AliasChoices("A", "B")` makes *both* names genuinely valid
input, confirmed by instantiating a model with only one of the two set.

The other load-bearing case is the one this module exists to get right
rather than merely convenient: when `env_prefix` cannot be resolved to a
literal (a variable, or a legacy `class Config` this module doesn't parse),
falling back to "no prefix" would report a name that is not the real
environment variable -- worse than reporting nothing. The whole class is
skipped instead, with a warning.
"""

from pathlib import PurePosixPath

import pytest

from envdoc.models import ExtractResult
from envdoc.sources.python_settings import extract

_IMPORTS = (
    "from pydantic import Field, AliasChoices\n"
    "from pydantic_settings import BaseSettings, SettingsConfigDict\n"
)


def extract_source(body: str, file: str = "src/config.py") -> ExtractResult:
    return extract(_IMPORTS + body, PurePosixPath(file))


def extract_one(body: str, file: str = "src/config.py") -> tuple[str, bool, str | None]:
    """The name, requiredness and default of a source that holds exactly one."""
    result = extract_source(body, file)
    assert result.warnings == ()
    assert len(result.findings) == 1, f"expected exactly one finding, got {result.findings}"
    finding = result.findings[0]
    return finding.name, finding.occurrence.required, finding.occurrence.default


def test_the_gate_a_prefixed_class_with_one_aliased_field_resolves_both_correctly() -> None:
    result = extract_source(
        "class Settings(BaseSettings):\n"
        '    model_config = SettingsConfigDict(env_prefix="APP_")\n'
        "    database_url: str\n"
        '    api_key: str = Field(alias="STRIPE_API_KEY")\n'
    )

    by_name = {f.name: f for f in result.findings}
    assert set(by_name) == {"APP_DATABASE_URL", "STRIPE_API_KEY"}
    assert by_name["APP_DATABASE_URL"].occurrence.required is True
    assert by_name["STRIPE_API_KEY"].occurrence.required is True


def test_no_alias_no_prefix_uppercases_the_field_name() -> None:
    assert extract_one("class Settings(BaseSettings):\n    database_url: str\n") == (
        "DATABASE_URL",
        True,
        None,
    )


def test_no_alias_with_prefix_prepends_it_to_the_uppercased_name() -> None:
    result = extract_one(
        "class Settings(BaseSettings):\n"
        '    model_config = SettingsConfigDict(env_prefix="APP_")\n'
        "    port: int = 8000\n"
    )

    assert result == ("APP_PORT", False, "8000")


def test_an_alias_inside_a_prefixed_class_ignores_the_prefix_entirely() -> None:
    """The specific trap this module is built to avoid: alias replaces the
    prefix computation, it does not narrow it."""
    result = extract_one(
        "class Settings(BaseSettings):\n"
        '    model_config = SettingsConfigDict(env_prefix="APP_")\n'
        '    api_key: str = Field(alias="STRIPE_API_KEY")\n'
    )

    assert result == ("STRIPE_API_KEY", True, None)


def test_validation_alias_takes_precedence_over_alias_when_both_are_given() -> None:
    result = extract_one(
        "class Settings(BaseSettings):\n"
        '    api_key: str = Field(alias="OLD_NAME", validation_alias="NEW_NAME")\n'
    )

    assert result[0] == "NEW_NAME"


def test_alias_choices_produces_one_finding_per_literal_name() -> None:
    result = extract_source(
        "class Settings(BaseSettings):\n"
        '    api_key: str = Field(validation_alias=AliasChoices("API_KEY", "STRIPE_KEY"))\n'
    )

    names = sorted(f.name for f in result.findings)
    assert names == ["API_KEY", "STRIPE_KEY"]
    assert all(f.occurrence.required for f in result.findings)


@pytest.mark.parametrize(
    "field_source",
    [
        "api_key: str = Field(alias=some_variable)\n",
        'api_key: str = Field(validation_alias=AliasChoices("A", some_variable))\n',
    ],
    ids=["non_literal_alias", "non_literal_choice_inside_alias_choices"],
)
def test_a_non_literal_alias_becomes_a_dynamic_ref_not_a_guess(field_source: str) -> None:
    result = extract_source(f"class Settings(BaseSettings):\n    {field_source}")

    assert result.findings == ()
    assert len(result.dynamic) == 1


@pytest.mark.parametrize(
    ("field_source", "expected"),
    [
        ("port: int = 8000\n", (False, "8000")),
        ("port: int = Field(default=8000)\n", (False, "8000")),
        ("port: int = Field(default_factory=lambda: 8000)\n", (False, None)),
        ("port: int = Field(...)\n", (True, None)),
        ("port: int\n", (True, None)),
    ],
    ids=[
        "bare_literal_default",
        "field_default_keyword",
        "field_default_factory",
        "field_ellipsis_is_explicitly_required",
        "no_default_at_all",
    ],
)
def test_default_handling_resolves_the_right_required_and_default_pair(
    field_source: str, expected: tuple[bool, str | None]
) -> None:
    result = extract_one(f"class Settings(BaseSettings):\n    {field_source}")

    assert result[1:] == expected


def test_model_config_as_a_bare_dict_literal_works_the_same_as_the_call_form() -> None:
    result = extract_one(
        "class Settings(BaseSettings):\n"
        '    model_config = {"env_prefix": "APP_"}\n'
        "    port: int = 8000\n"
    )

    assert result == ("APP_PORT", False, "8000")


def test_a_nested_legacy_config_class_skips_the_whole_settings_class_with_a_warning() -> None:
    result = extract_source(
        "class Settings(BaseSettings):\n"
        "    class Config:\n"
        '        env_prefix = "APP_"\n'
        "    port: int = 8000\n"
    )

    assert result.findings == ()
    assert len(result.warnings) == 1
    assert "Settings" in result.warnings[0]
    assert "skipped" in result.warnings[0]


def test_a_non_literal_env_prefix_skips_the_whole_class_with_a_warning() -> None:
    result = extract_source(
        "class Settings(BaseSettings):\n"
        "    model_config = SettingsConfigDict(env_prefix=some_variable)\n"
        "    port: int = 8000\n"
    )

    assert result.findings == ()
    assert len(result.warnings) == 1


def test_a_classvar_annotated_attribute_is_not_a_field() -> None:
    result = extract_source(
        "from typing import ClassVar\n\n\n"
        "class Settings(BaseSettings):\n"
        '    VERSION: ClassVar[str] = "1.0"\n'
        "    port: int = 8000\n"
    )

    assert [f.name for f in result.findings] == ["PORT"]


def test_a_class_that_does_not_inherit_base_settings_is_ignored() -> None:
    result = extract_source("class NotSettings:\n    port: int = 8000\n")

    assert result.findings == ()


def test_a_same_named_but_unrelated_base_settings_is_not_matched() -> None:
    """Proves the import-alias discipline actually gates the match, the same
    way python_ast.py refuses to treat an unrelated getenv() as os.getenv."""
    result = extract(
        "class BaseSettings:\n    pass\n\n\nclass Settings(BaseSettings):\n    port: int = 8000\n",
        PurePosixPath("src/config.py"),
    )

    assert result.findings == ()


def test_base_settings_and_field_imported_under_an_alias_still_resolve() -> None:
    result = extract(
        "from pydantic import Field as F\n"
        "from pydantic_settings import BaseSettings as Base\n\n\n"
        "class Settings(Base):\n"
        '    api_key: str = F(alias="STRIPE_API_KEY")\n',
        PurePosixPath("src/config.py"),
    )

    assert [f.name for f in result.findings] == ["STRIPE_API_KEY"]


def test_base_settings_accessed_through_a_module_attribute_still_resolves() -> None:
    result = extract(
        "import pydantic_settings\n\n\n"
        "class Settings(pydantic_settings.BaseSettings):\n"
        "    port: int = 8000\n",
        PurePosixPath("src/config.py"),
    )

    assert [f.name for f in result.findings] == ["PORT"]


def test_malformed_source_yields_nothing_rather_than_raising() -> None:
    result = extract("class Settings(BaseSettings)\n    port: int = 8000\n", PurePosixPath("x.py"))

    assert result.findings == ()
    assert result.warnings == ()
