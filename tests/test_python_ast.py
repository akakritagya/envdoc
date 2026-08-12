"""Pins what the naive Python extractor recognises, and what it refuses to.

Every test here feeds the extractor real Python source rather than a mocked
tree, because the whole argument for parsing with `ast` instead of a regex is
that source is not a bag of lines. Two tests carry that argument on their own:
a commented-out `os.getenv` and one quoted inside a string literal must both
yield nothing. A regex scanner reports both, and every user then learns to
distrust the whole report.

The other load-bearing decision pinned here is what counts as a fallback.
`os.getenv("X")` has no default -- it hands the caller None and the process
falls over somewhere further downstream -- so it is recorded `required=True`,
exactly like `os.environ["X"]`. An explicit `os.getenv("X", None)` is the same
situation written out longhand and is treated identically. Only a fallback the
code can actually use makes a variable optional.

Naive means naive: aliases (`from os import environ`) are G3's problem and are
pinned here as *not* detected, so that the day they start working, a test says
so.
"""

from pathlib import PurePosixPath

import pytest

from envdoc.models import Confidence, ExtractResult, Provider, SourceKind
from envdoc.sources.python_ast import extract


def extract_source(source: str, file: str = "src/app.py") -> ExtractResult:
    return extract(source, PurePosixPath(file))


def extract_one(source: str, file: str = "src/app.py") -> tuple[str, bool, str | None]:
    """The name, requiredness and default of a source that holds exactly one."""
    result = extract(source, PurePosixPath(file))
    assert result.warnings == ()
    assert len(result.findings) == 1, f"expected exactly one finding, got {result.findings}"
    finding = result.findings[0]
    return finding.name, finding.occurrence.required, finding.occurrence.default


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('import os\nos.environ["DATABASE_URL"]\n', ("DATABASE_URL", True, None)),
        ('import os\nos.environ.get("DATABASE_URL")\n', ("DATABASE_URL", True, None)),
        ('import os\nos.environ.get("PORT", "8000")\n', ("PORT", False, "8000")),
        ('import os\nos.environ.get("PORT", default="8000")\n', ("PORT", False, "8000")),
        ('import os\nos.getenv("DATABASE_URL")\n', ("DATABASE_URL", True, None)),
        ('import os\nos.getenv("PORT", "8000")\n', ("PORT", False, "8000")),
        ('import os\nos.getenv("PORT", default="8000")\n', ("PORT", False, "8000")),
    ],
    ids=[
        "environ_subscript_is_required",
        "environ_get_without_a_fallback_is_required",
        "environ_get_with_a_fallback_is_optional",
        "environ_get_with_a_keyword_fallback_is_optional",
        "getenv_without_a_fallback_is_required",
        "getenv_with_a_fallback_is_optional",
        "getenv_with_a_keyword_fallback_is_optional",
    ],
)
def test_the_four_standard_read_forms_resolve_to_a_name_and_a_fallback(
    source: str, expected: tuple[str, bool, str | None]
) -> None:
    assert extract_one(source) == expected


@pytest.mark.parametrize(
    ("source", "expected_default"),
    [
        ('import os\nos.getenv("PORT", 8000)\n', "8000"),
        ('import os\nos.getenv("DEBUG", False)\n', "False"),
        ('import os\nos.getenv("RATIO", 0.5)\n', "0.5"),
    ],
    ids=["integer", "boolean", "float"],
)
def test_a_non_string_fallback_is_recorded_as_the_literal_the_code_wrote(
    source: str, expected_default: str
) -> None:
    """`PORT=8000` is what belongs in .env.example, not `PORT=<int object>`."""
    _, required, default = extract_one(source)

    assert required is False
    assert default == expected_default


@pytest.mark.parametrize(
    "source",
    [
        'import os\nos.getenv("DATABASE_URL", None)\n',
        'import os\nos.environ.get("DATABASE_URL", None)\n',
        'import os\nos.getenv("DATABASE_URL", default=None)\n',
    ],
    ids=["getenv_positional", "environ_get_positional", "getenv_keyword"],
)
def test_an_explicit_none_fallback_is_no_fallback_at_all(source: str) -> None:
    """Spelling out the implicit return value does not create a default.

    `os.getenv("X", None)` leaves the caller in precisely the position
    `os.getenv("X")` does: holding None and no way to proceed. Recording it as
    an optional variable with a default would put a bare `X=None` line into
    .env.example and hide a variable that genuinely has to be set.
    """
    assert extract_one(source) == ("DATABASE_URL", True, None)


def test_a_fallback_the_extractor_cannot_read_still_makes_the_variable_optional() -> None:
    """Requiredness and the default value are separate questions.

    `os.getenv("PORT", compute_port())` plainly has a fallback, so the variable
    is not required -- but there is no literal to write into .env.example, and
    inventing `compute_port()` as the default would be a lie in a generated
    file. The honest answer is optional with no recorded default.
    """
    assert extract_one('import os\nos.getenv("PORT", compute_port())\n') == ("PORT", False, None)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('import os\nos.getenv(key="PORT")\n', ("PORT", True, None)),
        ('import os\nos.getenv(key="PORT", default="8000")\n', ("PORT", False, "8000")),
        ('import os\nos.environ.get(key="PORT", default="8000")\n', ("PORT", False, "8000")),
    ],
    ids=["key_only", "key_and_default", "environ_get_with_both_as_keywords"],
)
def test_the_name_may_be_passed_by_keyword_too(
    source: str, expected: tuple[str, bool, str | None]
) -> None:
    """`os.getenv(key=...)` is legal and occasionally written.

    Both parameters of `os.getenv` are ordinary named parameters, as is `key`
    on the `Mapping.get` that `os.environ.get` resolves to, so a caller is free
    to spell either out. Missing the variable entirely because of how the call
    was punctuated would be the kind of silent gap that makes an audit tool
    worth less than no tool.
    """
    assert extract_one(source) == expected


def test_a_read_with_no_arguments_at_all_is_ignored_rather_than_crashing() -> None:
    """`os.getenv()` is a TypeError waiting to happen, but it parses, and
    reaching for args[0] on it would take the scan down with an IndexError."""
    result = extract_source("import os\nos.getenv()\n")

    assert result.findings == ()
    assert result.dynamic == ()


def test_every_finding_carries_its_position_file_provider_and_source() -> None:
    source = 'import os\n\nvalue = os.getenv("API_KEY")\n'

    result = extract(source, PurePosixPath("src/client.py"))

    occurrence = result.findings[0].occurrence
    assert str(occurrence.file) == "src/client.py"
    assert occurrence.line == 3
    assert occurrence.column == 8
    assert occurrence.source is SourceKind.CODE
    assert occurrence.provider is Provider.PYTHON_AST
    assert result.findings[0].confidence is Confidence.EXACT


def test_a_name_read_twice_in_one_file_yields_two_findings() -> None:
    """Deduplication is aggregate()'s job, and it needs both locations to do it."""
    source = 'import os\nos.getenv("PORT", "8000")\nos.getenv("PORT", "3000")\n'

    result = extract(source, PurePosixPath("src/app.py"))

    assert [f.occurrence.line for f in result.findings] == [2, 3]
    assert [f.occurrence.default for f in result.findings] == ["8000", "3000"]


def test_findings_come_out_sorted_by_position_regardless_of_tree_order() -> None:
    """Determinism starts here. `ast.walk` visits breadth-first, so a nested
    read is reached after a later top-level one and would otherwise be emitted
    out of order."""
    source = (
        "import os\n"
        "def f():\n"
        '    return os.getenv("INNER")\n'
        'os.getenv("OUTER")\n'
        'os.getenv("LAST")\n'
    )

    result = extract(source, PurePosixPath("src/app.py"))

    assert [(f.name, f.occurrence.line) for f in result.findings] == [
        ("INNER", 3),
        ("OUTER", 4),
        ("LAST", 5),
    ]


@pytest.mark.parametrize(
    "source",
    [
        'import os\n# os.getenv("SECRET")\n',
        "import os\nprint(\"call os.getenv('SECRET') to read it\")\n",
        'import os\n\n\ndef f():\n    """Reads os.getenv("SECRET")."""\n',
        'import os\nos.path.join("SECRET")\n',
        'config = {}\nconfig["SECRET"]\n',
        'settings = {}\nsettings.get("SECRET", "8000")\n',
        "import os\ndict(os.environ)\n",
        'import os\nif "SECRET" in os.environ:\n    pass\n',
        'from os import environ\nenviron["SECRET"]\n',
        'import os\nos.getenv("")\n',
    ],
    ids=[
        "commented_out",
        "quoted_in_a_string_literal",
        "mentioned_in_a_docstring",
        "an_unrelated_os_function",
        "an_unrelated_subscript",
        "an_unrelated_get",
        "environ_used_as_a_whole_mapping",
        "a_membership_test_rather_than_a_read",
        "reached_through_an_import_alias",
        "an_empty_name",
    ],
)
def test_these_produce_nothing_at_all(source: str) -> None:
    """Two of these are the entire argument for parsing instead of grepping.

    A commented-out read and one quoted inside a string literal are invisible
    to `ast` for free, and a scanner that reports them teaches its users to
    ignore it.

    The last two pin scope rather than principle. Import aliases are G3's job
    and a membership test is not a read, so both are expected to yield nothing
    *today*; the day either starts working, this test is the one that says so.
    """
    result = extract_source(source)

    assert result.findings == ()
    assert result.dynamic == ()
    assert result.warnings == ()


@pytest.mark.parametrize(
    ("source", "expression"),
    [
        ("import os\nos.getenv(key)\n", "key"),
        ("import os\nos.environ[key]\n", "key"),
        ('import os\nos.environ.get(key, "8000")\n', "key"),
        ('import os\nos.getenv(f"PREFIX_{suffix}")\n', "f'PREFIX_{suffix}'"),
        ('import os\nos.getenv("PREFIX_" + suffix)\n', "'PREFIX_' + suffix"),
        ("import os\nos.getenv(NAMES[0])\n", "NAMES[0]"),
    ],
    ids=[
        "getenv_of_a_variable",
        "environ_subscripted_by_a_variable",
        "environ_get_of_a_variable",
        "an_f_string",
        "a_concatenation",
        "an_indexed_lookup",
    ],
)
def test_a_name_the_parser_cannot_resolve_becomes_a_dynamic_reference(
    source: str, expression: str
) -> None:
    """Reported, never guessed at.

    The name genuinely is not knowable without running the program, and a tool
    that invents one -- `PREFIX_` from that f-string, say -- is a tool people
    stop trusting the moment they notice. A DynamicRef has no name, so it
    cannot become a Variable and cannot count toward drift; it exists to tell
    the user there is a read here that no static analysis will see.
    """
    result = extract_source(source)

    assert result.findings == ()
    assert len(result.dynamic) == 1
    assert result.dynamic[0].expression == expression
    assert result.dynamic[0].occurrence.line == 2


def test_a_non_string_literal_name_is_dynamic_rather_than_stringified() -> None:
    """`os.getenv(123)` is broken code, not a variable called "123"."""
    result = extract_source("import os\nos.getenv(123)\n")

    assert result.findings == ()
    assert result.dynamic[0].expression == "123"


def test_dynamic_references_are_sorted_by_position_too() -> None:
    source = "import os\ndef f():\n    return os.getenv(inner)\nos.getenv(outer)\n"

    result = extract_source(source)

    assert [d.expression for d in result.dynamic] == ["inner", "outer"]


def test_resolved_and_dynamic_reads_in_one_file_are_reported_side_by_side() -> None:
    source = 'import os\nos.getenv("PORT")\nos.getenv(key)\n'

    result = extract_source(source)

    assert [f.name for f in result.findings] == ["PORT"]
    assert [d.expression for d in result.dynamic] == ["key"]


def test_a_file_that_does_not_parse_warns_instead_of_raising() -> None:
    """An unparseable file must not take the whole scan down.

    A repository being audited is exactly the kind of place a half-finished
    file lives, and refusing to report on the other two hundred files because
    one of them is mid-edit would make the tool useless in a pre-commit hook.
    The warning rides on the result rather than being printed, because only the
    CLI knows whether --quiet is in force.
    """
    result = extract_source("import os\ndef f(\n", file="src/broken.py")

    assert result.findings == ()
    assert result.dynamic == ()
    assert len(result.warnings) == 1
    assert result.warnings[0].startswith("src/broken.py: could not parse")


def test_a_source_containing_null_bytes_warns_rather_than_raising_valueerror() -> None:
    """A file that is really a binary must be skipped like any other.

    Which exception `ast.parse` raises here has moved between Python versions
    -- ValueError historically, SyntaxError on current ones -- so the parse is
    guarded against both rather than against whichever one this interpreter
    happens to throw.
    """
    result = extract_source('import os\nos.getenv("PORT")\x00\n', file="src/binary.py")

    assert result.findings == ()
    assert result.warnings[0].startswith("src/binary.py: could not parse")


def test_an_empty_file_produces_an_empty_result() -> None:
    result = extract_source("")

    assert result == ExtractResult(findings=(), dynamic=(), warnings=())
