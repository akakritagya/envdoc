"""Pins what the Dockerfile extractor recognises, and where the CODE/DEPLOYMENT
line falls.

`ENV` sets the *running container's* environment -- SourceKind.DEPLOYMENT,
the same relationship `environment:` has in a compose file. `ARG` is
build-time only and invisible at runtime unless a later `ENV NAME=$NAME`
re-exposes it, which ordinary `ENV` parsing already catches for free --
`ARG` on its own is SourceKind.CODE, the same relationship `os.getenv` has to
a variable it requires from its environment.

Both `ENV` syntaxes are pinned directly: the legacy `ENV NAME value`
(rest-of-line, unquoted spaces allowed) and the modern `ENV NAME=value ...`
(one or more pairs, quote-aware). So is the multi-stage case the build plan
names explicitly -- instructions from every `FROM` block are found, not just
the first or the last.
"""

from pathlib import PurePosixPath

from envdoc.models import Confidence, ExtractResult, Provider, SourceKind
from envdoc.sources.dockerfile import extract

FILE = PurePosixPath("Dockerfile")


def extract_text(text: str) -> ExtractResult:
    return extract(text, FILE)


def test_the_legacy_env_syntax_resolves_a_name() -> None:
    result = extract_text("ENV DATABASE_URL postgres://localhost\n")

    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.name == "DATABASE_URL"
    assert finding.confidence is Confidence.EXACT
    assert finding.occurrence.source is SourceKind.DEPLOYMENT
    assert finding.occurrence.provider is Provider.DOCKERFILE
    assert finding.occurrence.required is False
    assert finding.occurrence.default is None


def test_the_legacy_env_syntax_keeps_spaces_in_the_value_out_of_the_name() -> None:
    result = extract_text("ENV GREETING hello there world\n")

    assert [f.name for f in result.findings] == ["GREETING"]


def test_the_modern_env_syntax_resolves_a_single_pair() -> None:
    result = extract_text("ENV PORT=8000\n")

    assert [f.name for f in result.findings] == ["PORT"]


def test_the_modern_env_syntax_splits_multiple_pairs_on_one_line() -> None:
    result = extract_text('ENV ONE=1 TWO="two words" THREE=3\n')

    assert [f.name for f in result.findings] == ["ONE", "TWO", "THREE"]


def test_a_backslash_line_continuation_still_parses_as_one_instruction() -> None:
    text = "ENV \\\n    NAME=value\n"

    result = extract_text(text)

    assert [f.name for f in result.findings] == ["NAME"]


def test_a_bare_arg_with_no_default_is_required() -> None:
    result = extract_text("ARG STRIPE_KEY\n")

    finding = result.findings[0]
    assert finding.name == "STRIPE_KEY"
    assert finding.occurrence.source is SourceKind.CODE
    assert finding.occurrence.required is True
    assert finding.occurrence.default is None


def test_an_arg_with_a_default_is_optional_with_that_literal() -> None:
    result = extract_text("ARG PORT=8000\n")

    finding = result.findings[0]
    assert finding.occurrence.required is False
    assert finding.occurrence.default == "8000"


def test_an_args_quoted_default_has_the_quotes_stripped() -> None:
    result = extract_text('ARG GREETING="hello there"\n')

    assert result.findings[0].occurrence.default == "hello there"


def test_env_and_arg_from_two_different_from_stages_are_both_found() -> None:
    text = (
        "FROM python:3.12 AS build\n"
        "ARG STRIPE_KEY\n"
        "ENV PORT=8000\n\n"
        "FROM build AS final\n"
        "ENV DEBUG=false\n"
    )

    result = extract_text(text)

    assert sorted(f.name for f in result.findings) == ["DEBUG", "PORT", "STRIPE_KEY"]


def test_a_comment_line_and_a_blank_line_are_both_ignored() -> None:
    text = "# a comment\n\nENV PORT=8000\n"

    result = extract_text(text)

    assert [f.name for f in result.findings] == ["PORT"]


def test_instruction_keywords_are_matched_case_insensitively() -> None:
    result = extract_text("env PORT=8000\nEnv DEBUG=true\n")

    assert sorted(f.name for f in result.findings) == ["DEBUG", "PORT"]


def test_from_and_other_unrecognised_instructions_produce_nothing() -> None:
    result = extract_text("FROM python:3.12\nRUN pip install .\nCOPY . /app\n")

    assert result.findings == ()


def test_an_empty_file_yields_nothing() -> None:
    result = extract_text("")

    assert result.findings == ()
    assert result.warnings == ()


def test_findings_are_sorted_by_line_number() -> None:
    text = "ENV ZULU=1\nARG ALPHA\n"

    result = extract_text(text)

    assert [f.occurrence.line for f in result.findings] == sorted(
        f.occurrence.line for f in result.findings
    )
