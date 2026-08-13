"""Pins what the compose extractor recognises, and what it refuses to.

G8b's slice -- `services.<name>.environment`, in both spellings Compose
accepts, list (`KEY=value`, bare `KEY`) and mapping (`KEY: value`, bare
`KEY:`) -- is pinned first below, unchanged. G14 deepened it with two more
things: `env_file:` (warned about, not resolved -- see docker_compose.py's
module docstring for why) and `${VAR}`/`$VAR` interpolation, which is read
from *every* scalar in the document, not just `environment:`, and lands on
`SourceKind.CODE` rather than `DEPLOYMENT` -- the file reads that name from
the host, it doesn't provide it.

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


def test_env_file_as_a_bare_string_produces_one_warning_and_no_finding() -> None:
    text = "services:\n  web:\n    env_file: .env.production\n"

    result = extract_text(text)

    assert result.findings == ()
    assert result.warnings == (
        "docker-compose.yml: env_file: .env.production referenced, not resolved",
    )


def test_env_file_as_a_list_produces_one_warning_per_path() -> None:
    text = "services:\n  web:\n    env_file:\n      - .env.base\n      - .env.production\n"

    result = extract_text(text)

    assert result.findings == ()
    assert len(result.warnings) == 2
    assert any(".env.base" in w for w in result.warnings)
    assert any(".env.production" in w for w in result.warnings)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("${TAG}", ("TAG", True, None)),
        ("$TAG", ("TAG", True, None)),
        ("${TAG:-latest}", ("TAG", False, "latest")),
        ("${TAG-latest}", ("TAG", False, "latest")),
        ("${TAG:?missing}", ("TAG", True, None)),
        ("${TAG?missing}", ("TAG", True, None)),
    ],
    ids=[
        "braced_bare",
        "unbraced_bare",
        "colon_dash_default",
        "dash_default",
        "colon_question_required",
        "question_required",
    ],
)
def test_every_interpolation_form_resolves_the_right_required_and_default(
    value: str, expected: tuple[str, bool, str | None]
) -> None:
    text = f'services:\n  web:\n    image: "myapp:{value}"\n'

    result = extract_text(text)

    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.occurrence.source is SourceKind.CODE
    assert finding.occurrence.provider is Provider.DOCKER_COMPOSE
    assert (finding.name, finding.occurrence.required, finding.occurrence.default) == expected


def test_a_double_dollar_sign_is_never_a_reference() -> None:
    text = "services:\n  web:\n    environment:\n      - LITERAL=$$NOT_A_VAR\n"

    result = extract_text(text)

    assert [f.name for f in result.findings] == ["LITERAL"]


def test_interpolation_is_found_outside_the_environment_block() -> None:
    """The scan is whole-document, not scoped to services.*.environment --
    image:, container_name:, and every other key can interpolate too."""
    text = 'services:\n  web:\n    image: "myapp:${TAG}"\n    container_name: "${APP_NAME}_web"\n'

    result = extract_text(text)

    assert sorted(f.name for f in result.findings) == ["APP_NAME", "TAG"]
    assert all(f.occurrence.source is SourceKind.CODE for f in result.findings)


def test_interpolation_inside_an_environment_value_is_found_alongside_the_key() -> None:
    """`- DATABASE_URL=${DATABASE_URL}` produces two distinct findings for the
    same name on different axes: DEPLOYMENT for the key compose sets, CODE
    for the host variable it reads to set it."""
    text = "services:\n  web:\n    environment:\n      - DATABASE_URL=${DATABASE_URL}\n"

    result = extract_text(text)

    sources = sorted(f.occurrence.source for f in result.findings)
    assert [f.name for f in result.findings] == ["DATABASE_URL", "DATABASE_URL"]
    assert sources == sorted([SourceKind.DEPLOYMENT, SourceKind.CODE])


def test_a_default_containing_a_further_dollar_sign_is_never_fabricated() -> None:
    text = 'services:\n  web:\n    image: "myapp:${TAG:-${FALLBACK}}"\n'

    result = extract_text(text)

    tag = next(f for f in result.findings if f.name == "TAG")
    assert tag.occurrence.required is False
    assert tag.occurrence.default is None
