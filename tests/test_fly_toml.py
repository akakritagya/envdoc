"""Pins what the `fly.toml` extractor recognises.

The simplest deployment-manifest reader in this codebase: `fly.toml` has no
substitution syntax at all -- Fly secrets are set with `fly secrets set`,
never referenced inside the file -- so every key under `[env]` is
unconditionally `SourceKind.DEPLOYMENT`. The one thing worth pinning
directly is the line-number recovery: `tomllib` gives no position
information, so a small auxiliary text scan finds it for the common
`[env]`-section-header case, and degrades to `line=1` rather than failing
for anything that scan doesn't recognise.
"""

from pathlib import PurePosixPath

from envdoc.models import Confidence, ExtractResult, Provider, SourceKind
from envdoc.sources.fly_toml import extract

FILE = PurePosixPath("fly.toml")


def extract_text(text: str) -> ExtractResult:
    return extract(text, FILE)


def test_every_key_under_env_produces_a_deployment_finding() -> None:
    text = '[env]\n  PORT = "8080"\n  LOG_LEVEL = "info"\n'

    result = extract_text(text)

    names = sorted(f.name for f in result.findings)
    assert names == ["LOG_LEVEL", "PORT"]
    finding = result.findings[0]
    assert finding.confidence is Confidence.EXACT
    assert finding.occurrence.source is SourceKind.DEPLOYMENT
    assert finding.occurrence.provider is Provider.FLY_TOML
    assert finding.occurrence.required is False
    assert finding.occurrence.default is None


def test_the_line_number_points_at_the_key_not_the_env_header() -> None:
    text = 'app = "myapp"\n\n[env]\n  PORT = "8080"\n  LOG_LEVEL = "info"\n'

    result = extract_text(text)

    by_name = {f.name: f.occurrence.line for f in result.findings}
    assert by_name == {"PORT": 4, "LOG_LEVEL": 5}


def test_a_section_other_than_env_contributes_nothing() -> None:
    text = "[[services]]\n  internal_port = 8080\n"

    result = extract_text(text)

    assert result.findings == ()
    assert result.warnings == ()


def test_a_file_with_no_env_table_yields_nothing() -> None:
    result = extract_text('app = "myapp"\n')

    assert result.findings == ()
    assert result.warnings == ()


def test_an_empty_file_yields_nothing() -> None:
    result = extract_text("")

    assert result.findings == ()
    assert result.warnings == ()


def test_invalid_toml_is_reported_as_a_warning_not_an_exception() -> None:
    result = extract_text("[env\nPORT = 8080\n")

    assert result.findings == ()
    assert len(result.warnings) == 1
    assert "fly.toml" in result.warnings[0]
    assert "could not parse" in result.warnings[0]


def test_an_inline_env_table_still_resolves_its_keys() -> None:
    """The line-number scan is written for the `[env]` section-header form;
    an inline table's keys aren't found by it and fall back to line=1
    rather than being dropped."""
    result = extract_text('env = { PORT = "8080", LOG_LEVEL = "info" }\n')

    names = sorted(f.name for f in result.findings)
    assert names == ["LOG_LEVEL", "PORT"]
    assert all(f.occurrence.line == 1 for f in result.findings)
