"""Pins what the minimal compose extractor recognises, and what it refuses to.

Deliberately narrow, per G8b's scope: only `services.<name>.environment`, in
both spellings Compose accepts -- list (`KEY=value`, bare `KEY`) and mapping
(`KEY: value`, bare `KEY:`). Everything else in a compose file -- `env_file:`,
`${VAR}` interpolation, ports, volumes, build args -- is out of scope here;
G14 is where this parser gets deepened, not this one.

A bare name (no `=`, no value) still produces a Finding in both spellings:
the three-way audit only asks whether a manifest *declares* a name, never
what value it resolves to, and Compose passing a name through from the host
environment is exactly as deliberate a declaration as a literal value.
"""

from pathlib import PurePosixPath

import pytest

from envdoc.models import Confidence, ExtractResult, Provider, SourceKind
from envdoc.sources.docker_compose import extract

FILE = PurePosixPath("docker-compose.yml")


def extract_text(text: str) -> ExtractResult:
    return extract(text, FILE)


def test_a_list_form_key_value_pair_is_found() -> None:
    result = extract_text("services:\n  web:\n    environment:\n      - PORT=8000\n")

    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.name == "PORT"
    assert finding.confidence is Confidence.EXACT
    assert finding.occurrence.source is SourceKind.DEPLOYMENT
    assert finding.occurrence.provider is Provider.DOCKER_COMPOSE
    assert finding.occurrence.required is False
    assert finding.occurrence.default is None


def test_a_list_form_bare_name_is_found() -> None:
    """No `=` -- Compose passes this through from the host's own environment
    -- but the name is still a deliberate declaration."""
    result = extract_text("services:\n  web:\n    environment:\n      - DEBUG\n")

    assert [f.name for f in result.findings] == ["DEBUG"]


def test_a_mapping_form_key_value_pair_is_found() -> None:
    text = "services:\n  worker:\n    environment:\n      DATABASE_URL: postgres://localhost\n"

    assert [f.name for f in extract_text(text).findings] == ["DATABASE_URL"]


def test_a_mapping_form_bare_name_with_a_null_value_is_found() -> None:
    result = extract_text("services:\n  worker:\n    environment:\n      REDIS_URL:\n")

    assert [f.name for f in result.findings] == ["REDIS_URL"]


def test_flow_style_syntax_is_recognised_the_same_as_block_style() -> None:
    result = extract_text("services:\n  web:\n    environment: [PORT=8000, DEBUG]\n")

    assert sorted(f.name for f in result.findings) == ["DEBUG", "PORT"]


def test_names_from_every_service_are_collected() -> None:
    text = (
        "services:\n"
        "  web:\n"
        "    environment:\n"
        "      - PORT=8000\n"
        "  worker:\n"
        "    environment:\n"
        "      DATABASE_URL: postgres://localhost\n"
    )

    assert sorted(f.name for f in extract_text(text).findings) == ["DATABASE_URL", "PORT"]


def test_a_service_with_no_environment_key_contributes_nothing() -> None:
    result = extract_text("services:\n  web:\n    image: nginx\n")

    assert result.findings == ()
    assert result.warnings == ()


def test_a_compose_file_with_no_services_key_yields_nothing() -> None:
    result = extract_text("version: '3'\n")

    assert result.findings == ()
    assert result.warnings == ()


def test_an_empty_file_yields_nothing() -> None:
    result = extract_text("")

    assert result.findings == ()
    assert result.warnings == ()


def test_an_environment_key_with_a_null_value_yields_nothing() -> None:
    """Malformed Compose -- environment: with nothing under it -- is skipped
    rather than treated as a parse failure for the whole file."""
    result = extract_text("services:\n  web:\n    environment:\n")

    assert result.findings == ()


def test_invalid_yaml_is_reported_as_a_warning_not_an_exception() -> None:
    result = extract_text("services:\n  web:\n  - not: valid: yaml: [\n")

    assert result.findings == ()
    assert len(result.warnings) == 1
    assert "docker-compose.yml" in result.warnings[0]
    assert "could not parse" in result.warnings[0]


def test_the_line_number_points_at_the_declaration_not_the_environment_key() -> None:
    text = "services:\n  web:\n    environment:\n      - PORT=8000\n      - DEBUG\n"

    result = extract_text(text)

    assert [f.occurrence.line for f in result.findings] == [4, 5]


@pytest.mark.parametrize(
    "text",
    [
        "services:\n  web:\n    environment: []\n",
        "services: {}\n",
    ],
    ids=["empty_environment_list", "empty_services_mapping"],
)
def test_empty_collections_yield_nothing_without_crashing(text: str) -> None:
    assert extract_text(text).findings == ()


def test_findings_are_sorted_by_line_number() -> None:
    """Matches python_ast.py and dotenv.py: sorting here rather than trusting
    whatever order the parser visits nodes in is what makes output
    byte-identical run to run."""
    text = (
        "services:\n"
        "  b:\n"
        "    environment:\n"
        "      - ZULU=1\n"
        "  a:\n"
        "    environment:\n"
        "      - ALPHA=1\n"
    )

    result = extract_text(text)

    assert [f.occurrence.line for f in result.findings] == sorted(
        f.occurrence.line for f in result.findings
    )
