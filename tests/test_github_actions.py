"""Pins what the GitHub Actions extractor recognises, and what it refuses to.

The same CODE/DEPLOYMENT split G14's compose deepening proved: `env:`
provides a value the CI runner sets -- `SourceKind.DEPLOYMENT`, read at all
three levels a workflow can declare it (workflow, job, step) -- and
`secrets.*`/`vars.*` reads a value the workflow requires from GitHub's
store -- `SourceKind.CODE`. Both forms of reference (`secrets.X` and
`secrets['X']`) are pinned, and so is the property that makes the scan
whole-document rather than scoped to `env:`: a secret passed through `with:`
to a reusable action is found exactly the same way one assigned to an
`env:` entry is.
"""

from pathlib import PurePosixPath

import pytest

from envdoc.models import Confidence, ExtractResult, Provider, SourceKind
from envdoc.sources.github_actions import extract

FILE = PurePosixPath(".github/workflows/ci.yml")


def extract_text(text: str) -> ExtractResult:
    return extract(text, FILE)


def test_workflow_level_env_is_found() -> None:
    result = extract_text("env:\n  NODE_ENV: production\n")

    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.name == "NODE_ENV"
    assert finding.confidence is Confidence.EXACT
    assert finding.occurrence.source is SourceKind.DEPLOYMENT
    assert finding.occurrence.provider is Provider.GITHUB_ACTIONS
    assert finding.occurrence.required is False
    assert finding.occurrence.default is None


def test_job_level_env_is_found() -> None:
    text = "jobs:\n  build:\n    env:\n      BUILD_MODE: release\n"

    result = extract_text(text)

    assert [f.name for f in result.findings] == ["BUILD_MODE"]


def test_step_level_env_is_found() -> None:
    text = (
        "jobs:\n"
        "  build:\n"
        "    steps:\n"
        "      - run: deploy.sh\n"
        "        env:\n"
        "          DEPLOY_KEY: ${{ secrets.DEPLOY_KEY }}\n"
    )

    result = extract_text(text)

    names = sorted(f.name for f in result.findings)
    assert names == ["DEPLOY_KEY", "DEPLOY_KEY"]


def test_a_secrets_dot_reference_resolves_as_required_code() -> None:
    result = extract_text("jobs:\n  build:\n    steps:\n      - run: echo ${{ secrets.API_KEY }}\n")

    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.name == "API_KEY"
    assert finding.occurrence.source is SourceKind.CODE
    assert finding.occurrence.required is True
    assert finding.occurrence.default is None


def test_a_vars_dot_reference_resolves_the_same_way() -> None:
    result = extract_text(
        "jobs:\n  build:\n    steps:\n      - run: echo ${{ vars.FEATURE_FLAG }}\n"
    )

    assert [f.name for f in result.findings] == ["FEATURE_FLAG"]


@pytest.mark.parametrize(
    "expression",
    ["${{ secrets['API_KEY'] }}", '${{ secrets["API_KEY"] }}'],
    ids=["single_quotes", "double_quotes"],
)
def test_the_bracket_form_resolves_the_same_as_the_dot_form(expression: str) -> None:
    result = extract_text(f"jobs:\n  build:\n    steps:\n      - run: echo {expression}\n")

    assert [f.name for f in result.findings] == ["API_KEY"]


def test_a_reference_inside_with_is_found_not_just_inside_env() -> None:
    """Pins the whole-document scan -- a secret passed to a reusable action
    is found the same way one assigned to env: is."""
    text = (
        "jobs:\n"
        "  build:\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "        with:\n"
        "          token: ${{ secrets.GITHUB_TOKEN }}\n"
    )

    result = extract_text(text)

    assert [f.name for f in result.findings] == ["GITHUB_TOKEN"]


def test_the_whole_secrets_object_used_without_a_property_produces_nothing() -> None:
    result = extract_text("jobs:\n  build:\n    steps:\n      - run: echo ${{ toJSON(secrets) }}\n")

    assert result.findings == ()


def test_invalid_yaml_is_reported_as_a_warning_not_an_exception() -> None:
    result = extract_text("jobs:\n  build:\n  - not: valid: yaml: [\n")

    assert result.findings == ()
    assert len(result.warnings) == 1
    assert "could not parse" in result.warnings[0]


def test_a_workflow_with_no_env_or_secrets_yields_nothing() -> None:
    result = extract_text("name: CI\non: push\njobs:\n  build:\n    runs-on: ubuntu-latest\n")

    assert result.findings == ()
    assert result.warnings == ()


def test_an_empty_file_yields_nothing() -> None:
    result = extract_text("")

    assert result.findings == ()
    assert result.warnings == ()
