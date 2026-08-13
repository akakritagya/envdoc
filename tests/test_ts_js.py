"""Pins what the tree-sitter JS/TS extractor recognises, and what it refuses to.

The central claim this module pins directly, rather than leaving as an
unverified plan assertion: extraction logic is language-agnostic.
`process.env.X` and its variants parse to the same node shapes whether the
file is `.js`, `.ts` or `.tsx`, so the four-standard-forms test below runs
against all three grammars from one parametrize table.

Two cases carry the same argument `test_python_ast.py` already made for a
different parser: a `process.env` read sitting inside an ordinary string or a
comment is invisible, because the extractor walks parsed nodes rather than
scanning text. A third case is specific to this parser and has no Python
analogue -- a template literal's own literal text is a `string_fragment`, not
an expression, so `` `process.env` `` typed as prose inside backticks produces
zero `member_expression` nodes on its own, while `${process.env.X}` inside the
same template does resolve, because only that part is actually parsed as code.
"""

from pathlib import PurePosixPath

import pytest

from envdoc.models import Confidence, ExtractResult
from envdoc.sources.ts_js import extract


def extract_source(source: str, file: str = "src/app.ts") -> ExtractResult:
    return extract(source, PurePosixPath(file))


def extract_one(source: str, file: str = "src/app.ts") -> tuple[str, bool, str | None]:
    """The name, requiredness and default of a source that holds exactly one."""
    result = extract_source(source, file)
    assert result.warnings == ()
    assert len(result.findings) == 1, f"expected exactly one finding, got {result.findings}"
    finding = result.findings[0]
    return finding.name, finding.occurrence.required, finding.occurrence.default


@pytest.mark.parametrize(
    "extension",
    [".js", ".ts", ".tsx"],
    ids=["javascript", "typescript", "tsx"],
)
@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("const x = process.env.API_KEY;", ("API_KEY", True, None)),
        ('const y = process.env["API_KEY"];', ("API_KEY", True, None)),
        ('const p = process.env.PORT || "8000";', ("PORT", False, "8000")),
        ('const p = process.env.PORT ?? "8000";', ("PORT", False, "8000")),
    ],
    ids=[
        "member_expression_is_required",
        "subscript_expression_is_required",
        "double_pipe_fallback_is_optional",
        "nullish_coalescing_fallback_is_optional",
    ],
)
def test_the_standard_read_forms_resolve_identically_across_js_ts_and_tsx(
    source: str, expected: tuple[str, bool, str | None], extension: str
) -> None:
    assert extract_one(source, f"src/app{extension}") == expected


def test_a_finding_is_exact_confidence() -> None:
    result = extract_source("process.env.API_KEY;")

    assert result.findings[0].confidence is Confidence.EXACT


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("const { API_KEY } = process.env;", ("API_KEY", True, None)),
        ('const { PORT = "8000" } = process.env;', ("PORT", False, "8000")),
        ("const { API_KEY: key } = process.env;", ("API_KEY", True, None)),
        ('const { API_KEY: key = "fallback" } = process.env;', ("API_KEY", False, "fallback")),
    ],
    ids=[
        "bare_name",
        "defaulted",
        "renamed_name_comes_from_the_key_not_the_local_binding",
        "renamed_and_defaulted",
    ],
)
def test_destructuring_process_env_resolves_a_name_and_a_fallback(
    source: str, expected: tuple[str, bool, str | None]
) -> None:
    assert extract_one(source) == expected


def test_a_rest_pattern_in_a_destructure_names_nothing() -> None:
    """`...rest` captures every other key at once -- there is no single name
    to resolve, and unlike a dynamic subscript key there isn't even an
    expression worth recording as a DynamicRef."""
    result = extract_source("const { API_KEY, ...rest } = process.env;")

    assert [f.name for f in result.findings] == ["API_KEY"]
    assert result.dynamic == ()


def test_a_destructure_of_something_other_than_process_env_is_ignored() -> None:
    result = extract_source("const { API_KEY } = someOtherObject;")

    assert result.findings == ()


def test_a_bare_assignment_destructure_is_recognised_too() -> None:
    """Not every destructure is a declaration -- `({ X } = process.env)` is
    valid JS and structurally different (assignment_expression, not
    variable_declarator), so it needs its own parent-shape check."""
    result = extract_source("let apiKey;\n({ API_KEY: apiKey } = process.env);")

    assert [f.name for f in result.findings] == ["API_KEY"]


def test_a_process_env_mention_inside_ordinary_prose_text_is_invisible() -> None:
    """The same argument test_python_ast.py makes for a commented-out or
    string-literal os.getenv, restated for a parser that walks nodes instead
    of scanning text."""
    result = extract_source('// process.env.COMMENTED_OUT\nconst s = "process.env.IN_A_STRING";')

    assert result.findings == ()
    assert result.dynamic == ()


def test_process_env_mentioned_in_a_templates_literal_text_is_not_a_finding() -> None:
    """The gate's named false-positive case: a template literal's own literal
    text is a string_fragment, not an expression -- it never becomes a
    member_expression the way a real interpolation does."""
    result = extract_source("const s = `this text just mentions process.env, nothing more`;")

    assert result.findings == ()


def test_a_real_interpolation_inside_a_template_literal_still_resolves() -> None:
    result = extract_source("const s = `db at ${process.env.DATABASE_URL}`;")

    assert [f.name for f in result.findings] == ["DATABASE_URL"]


def test_a_non_literal_fallback_is_optional_with_no_documentable_default() -> None:
    """process.env.PORT || computeDefault() plainly has a fallback, so the
    read is optional, but there is no literal to put in .env.example and
    inventing one would be a lie -- the same (True, None) case
    python_ast._fallback returns for a non-constant getenv default."""
    result = extract_source("const p = process.env.PORT || computeDefault();")

    assert result.findings[0].occurrence.required is False
    assert result.findings[0].occurrence.default is None


def test_a_dynamic_subscript_key_becomes_a_dynamic_ref_not_a_guess() -> None:
    result = extract_source("const v = process.env[someKeyVariable];")

    assert result.findings == ()
    assert len(result.dynamic) == 1
    assert result.dynamic[0].expression == "someKeyVariable"


def test_a_template_literal_used_as_a_subscript_key_is_also_dynamic() -> None:
    """Never unwrapped even with no interpolation -- the same posture
    python_ast.py takes with an f-string default that happens to be static."""
    result = extract_source("const v = process.env[`API_KEY`];")

    assert result.findings == ()
    assert len(result.dynamic) == 1


def test_bare_process_env_passed_around_whole_produces_nothing() -> None:
    """Object.keys(process.env) and friends name no single variable -- silence
    is the honest answer, not a guess."""
    result = extract_source("console.log(process.env);")

    assert result.findings == ()
    assert result.dynamic == ()


def test_jsx_interpolation_resolves_the_same_as_a_plain_read() -> None:
    source = "function App() { return <div>{process.env.API_KEY}</div>; }"

    result = extract_source(source, "src/App.jsx")

    assert [f.name for f in result.findings] == ["API_KEY"]


def test_tsx_interpolation_resolves_the_same_as_a_plain_read() -> None:
    source = "const App = () => <div>{process.env.API_KEY}</div>;"

    result = extract_source(source, "src/App.tsx")

    assert [f.name for f in result.findings] == ["API_KEY"]


def test_typescript_casts_and_assertions_do_not_hide_a_read() -> None:
    result = extract_source("const x = process.env.API_KEY as string;", "src/app.ts")

    assert [f.name for f in result.findings] == ["API_KEY"]


def test_malformed_source_yields_nothing_rather_than_raising() -> None:
    """Tree-sitter is error-tolerant by design -- a read swallowed by an
    unterminated string is simply not found, not an exception that would
    take the whole scan down."""
    result = extract_source('const x = "unterminated string process.env.API_KEY')

    assert result.findings == ()
    assert result.warnings == ()


def test_the_second_declarator_in_a_multi_declaration_statement_still_resolves() -> None:
    result = extract_source("const a = 1, b = process.env.API_KEY;")

    assert [f.name for f in result.findings] == ["API_KEY"]


def test_occurrences_come_out_sorted_by_line_then_column() -> None:
    source = "const b = process.env.B;\nconst a = process.env.A;\n"

    result = extract_source(source)

    assert [f.name for f in result.findings] == ["B", "A"]
    assert [f.occurrence.line for f in result.findings] == [1, 2]
