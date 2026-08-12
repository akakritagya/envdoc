"""Pins the .env.example parser, and the round-trip that `sync` will stand on.

The gate for this group is one property: parsing a file and serialising it back
returns the same bytes. It sounds modest and it is the whole design. `sync`
rewrites .env.example in place, and a rewrite that loses the comment explaining
what DATABASE_URL is for, or reflows every line it did not touch, produces a
diff nobody can review and a tool nobody runs twice. Preserving the file means
holding on to the parts a parser normally throws away -- blank lines, comment
text, quoting style, spacing around `=`, `export` prefixes, even CRLF line
endings and a missing final newline.

So each line keeps its raw text alongside whatever was parsed out of it. The
round-trip is then true by construction, which is the point: the property that
matters is the one that cannot quietly stop holding.

The other half is that a value in .env.example is not a default. It is
documentation of what the variable looks like, written by whoever last touched
the file, and treating it as a fallback the code would use would let a stale
example silently answer questions about live behaviour. Findings from here
carry required=False and default=None, and a test pins that.
"""

from pathlib import PurePosixPath

import pytest

from envdoc.models import Confidence, Provider, SourceKind
from envdoc.sources.dotenv import extract, parse

MESSY = (
    "# Database configuration\r\n"
    "\r\n"
    "DATABASE_URL=postgres://user:pass@localhost:5432/db\n"
    "export REDIS_URL='redis://localhost:6379'\n"
    "  PORT   =   8000   # the port to bind\n"
    'GREETING="hello  world"\n'
    "EMPTY=\n"
    "\n"
    "# A key, because why not\n"
    'PRIVATE_KEY="-----BEGIN-----\n'
    "line two\n"
    '-----END-----"\n'
    "QUOTED_HASH='a # not a comment'\n"
    "UNSPACED=a#b\n"
    "NO_TRAILING_NEWLINE=yes"
)


def parse_one(line: str) -> tuple[str, str]:
    """The name and value of a source holding exactly one assignment."""
    document = parse(line, PurePosixPath(".env.example"))
    entries = document.entries
    assert len(entries) == 1, f"expected exactly one entry, got {entries}"
    name, value = entries[0].name, entries[0].value
    assert name is not None and value is not None
    return name, value


def test_a_messy_file_survives_a_parse_and_serialise_round_trip_byte_for_byte() -> None:
    """The gate. Every awkward thing a real .env.example does, in one file.

    CRLF and LF in the same file, a value spanning three physical lines, an
    inline comment, `export`, ragged spacing, a `#` inside quotes and another
    with no space before it, and no final newline. If any of those is
    normalised on the way through, `sync` would rewrite lines it never touched.
    """
    assert parse(MESSY, PurePosixPath(".env.example")).text == MESSY


def test_the_messy_file_parses_to_the_names_it_actually_contains() -> None:
    """The round-trip alone would pass if the parser understood nothing at all
    and kept every line as an opaque blob, so this pins that it does not."""
    document = parse(MESSY, PurePosixPath(".env.example"))

    assert [entry.name for entry in document.entries] == [
        "DATABASE_URL",
        "REDIS_URL",
        "PORT",
        "GREETING",
        "EMPTY",
        "PRIVATE_KEY",
        "QUOTED_HASH",
        "UNSPACED",
        "NO_TRAILING_NEWLINE",
    ]
    assert document.warnings == ()


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("NAME=value\n", ("NAME", "value")),
        ("NAME=\n", ("NAME", "")),
        ("export NAME=value\n", ("NAME", "value")),
        ("  NAME  =  value  \n", ("NAME", "value")),
        ("NAME=value with spaces\n", ("NAME", "value with spaces")),
        (
            "DATABASE_URL=postgres://u:p@h:5432/db?a=b\n",
            ("DATABASE_URL", "postgres://u:p@h:5432/db?a=b"),
        ),
        ("NAME=value # trailing comment\n", ("NAME", "value")),
        ("NAME=a#b\n", ("NAME", "a#b")),
        ("_UNDERSCORED=x\n", ("_UNDERSCORED", "x")),
        ("lowercase=x\n", ("lowercase", "x")),
        ("N2=x\n", ("N2", "x")),
    ],
    ids=[
        "a_plain_assignment",
        "an_empty_value",
        "an_export_prefix",
        "ragged_whitespace",
        "spaces_inside_the_value",
        "a_url_containing_equals_signs",
        "an_inline_comment_is_not_part_of_the_value",
        "a_hash_with_no_space_before_it_is",
        "a_leading_underscore",
        "a_lowercase_name",
        "a_digit_after_the_first_character",
    ],
)
def test_unquoted_assignments_parse(line: str, expected: tuple[str, str]) -> None:
    """A `#` only starts a comment when whitespace precedes it.

    `PASSWORD=a#b` is a password containing a hash, not a password called `a`,
    and every dotenv implementation worth copying draws the line in that place.
    """
    assert parse_one(line) == expected


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ('NAME="value"\n', "value"),
        ("NAME='value'\n", "value"),
        ('NAME="  padded  "\n', "  padded  "),
        ('NAME="a # not a comment"\n', "a # not a comment"),
        ("NAME='a # not a comment'\n", "a # not a comment"),
        ('NAME="value"  # a real comment\n', "value"),
        ('NAME="line\\nbreak"\n', "line\nbreak"),
        ('NAME="tab\\there"\n', "tab\there"),
        ('NAME="quote\\"inside"\n', 'quote"inside'),
        ('NAME="back\\\\slash"\n', "back\\slash"),
        ("NAME='literal\\nbackslash'\n", "literal\\nbackslash"),
        ('NAME="a\\qb"\n', "a\\qb"),
        ('NAME=""\n', ""),
    ],
    ids=[
        "double_quoted",
        "single_quoted",
        "whitespace_is_kept_inside_quotes",
        "a_hash_inside_double_quotes",
        "a_hash_inside_single_quotes",
        "a_comment_after_the_closing_quote",
        "an_escaped_newline",
        "an_escaped_tab",
        "an_escaped_quote",
        "an_escaped_backslash",
        "single_quotes_do_not_unescape",
        "an_escape_that_means_nothing_is_left_alone",
        "empty_quotes",
    ],
)
def test_quoted_assignments_parse(line: str, expected: str) -> None:
    """Single quotes are literal and double quotes unescape, as in a shell.

    The distinction matters for a file people paste secrets into: a Windows
    path in single quotes must survive with its backslashes intact.
    """
    assert parse_one(line)[1] == expected


def test_a_double_quoted_value_may_span_several_lines() -> None:
    """A PEM key in .env.example is the usual reason.

    Without this the continuation lines would each be parsed on their own, and
    `-----END-----"` would be reported as a variable that does not exist.
    """
    text = 'KEY="first\nsecond\nthird"\nAFTER=yes\n'

    document = parse(text, PurePosixPath(".env.example"))

    assert [(e.name, e.value) for e in document.entries] == [
        ("KEY", "first\nsecond\nthird"),
        ("AFTER", "yes"),
    ]


def test_a_multiline_value_reports_the_line_it_started_on() -> None:
    text = 'FIRST=1\nKEY="a\nb"\nAFTER=2\n'

    document = parse(text, PurePosixPath(".env.example"))

    assert [(e.name, e.line) for e in document.entries] == [("FIRST", 1), ("KEY", 2), ("AFTER", 4)]


@pytest.mark.parametrize(
    "text",
    [
        "# just a comment\n",
        "   # indented comment\n",
        "\n",
        "   \n",
        "\t\n",
        "",
    ],
    ids=[
        "a_comment",
        "an_indented_comment",
        "a_blank_line",
        "a_whitespace_only_line",
        "a_tab_only_line",
        "an_empty_file",
    ],
)
def test_lines_that_declare_nothing_produce_no_entry_and_no_warning(text: str) -> None:
    document = parse(text, PurePosixPath(".env.example"))

    assert document.entries == ()
    assert document.warnings == ()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("just some words\n", ".env.example:1: not an assignment, ignored: 'just some words'"),
        ("1INVALID=x\n", ".env.example:1: not a valid variable name, ignored: '1INVALID'"),
        ("has-a-dash=x\n", ".env.example:1: not a valid variable name, ignored: 'has-a-dash'"),
        ('UNTERMINATED="oops\n', ".env.example:1: unterminated quote, ignored: 'UNTERMINATED'"),
    ],
    ids=["no_equals_sign", "a_name_starting_with_a_digit", "a_name_with_a_dash", "an_open_quote"],
)
def test_a_line_that_cannot_be_understood_warns_and_is_kept_verbatim(
    text: str, expected: str
) -> None:
    """Ignored, never guessed at, and never dropped.

    The line stays in the document exactly as written so the round-trip holds
    and `sync` cannot delete something it failed to understand -- which, in a
    file that may hold the only copy of a comment explaining a variable, is the
    difference between a tool people trust with a rewrite and one they do not.
    """
    document = parse(text, PurePosixPath(".env.example"))

    assert document.entries == ()
    assert document.warnings == (expected,)
    assert document.text == text


def test_a_repeated_name_warns_and_names_both_lines() -> None:
    """The later one wins at load time, so the earlier is dead documentation --
    and if they disagree, one of them is a lie about the deployment."""
    text = "PORT=8000\nDEBUG=1\nPORT=3000\n"

    document = parse(text, PurePosixPath(".env.example"))

    assert document.warnings == (".env.example:3: duplicate entry for PORT, first seen on line 1",)


def test_a_repeated_name_still_yields_both_entries() -> None:
    """Reporting one and hiding the other would make the warning unactionable."""
    document = parse("PORT=8000\nPORT=3000\n", PurePosixPath(".env.example"))

    assert [(e.name, e.value, e.line) for e in document.entries] == [
        ("PORT", "8000", 1),
        ("PORT", "3000", 2),
    ]


def test_a_byte_order_mark_does_not_become_part_of_the_first_name() -> None:
    """A file saved by a Windows editor otherwise documents a variable called
    `﻿DATABASE_URL`, which nothing will ever set."""
    document = parse("﻿DATABASE_URL=x\n", PurePosixPath(".env.example"))

    assert [e.name for e in document.entries] == ["DATABASE_URL"]
    assert document.text == "﻿DATABASE_URL=x\n"


def test_extract_reports_one_finding_per_entry_on_the_example_axis() -> None:
    result = extract("DATABASE_URL=x\nPORT=8000\n", PurePosixPath(".env.example"))

    assert [f.name for f in result.findings] == ["DATABASE_URL", "PORT"]
    occurrence = result.findings[0].occurrence
    assert occurrence.source is SourceKind.EXAMPLE
    assert occurrence.provider is Provider.DOTENV_EXAMPLE
    assert occurrence.line == 1
    assert str(occurrence.file) == ".env.example"
    assert result.findings[0].confidence is Confidence.EXACT


def test_an_example_value_is_never_recorded_as_a_default() -> None:
    """`PORT=8000` in .env.example does not mean the code falls back to 8000.

    It is documentation of the shape of the value, written by whoever last
    touched the file. Treating it as a fallback would let a stale example
    answer questions about live behaviour, which is the exact failure this tool
    exists to catch.
    """
    result = extract("PORT=8000\n", PurePosixPath(".env.example"))

    assert result.findings[0].occurrence.required is False
    assert result.findings[0].occurrence.default is None


def test_extract_passes_parser_warnings_along() -> None:
    result = extract("PORT=8000\nnonsense\n", PurePosixPath(".env.example"))

    assert result.warnings == (".env.example:2: not an assignment, ignored: 'nonsense'",)


def test_extract_never_produces_a_dynamic_reference() -> None:
    """There is no such thing here -- every name in a dotenv file is literal."""
    assert extract(MESSY, PurePosixPath(".env.example")).dynamic == ()


@pytest.mark.parametrize(
    "text",
    [
        "A=1\nB=2\n",
        "A=1\r\nB=2\r\n",
        "A=1\nB=2",
        "\n\n\n",
        "# comment only, no newline",
        MESSY,
    ],
    ids=[
        "unix_endings",
        "windows_endings",
        "no_final_newline",
        "blank_lines_only",
        "a_comment_with_no_newline",
        "the_messy_fixture",
    ],
)
def test_the_round_trip_holds_for_every_shape_of_file(text: str) -> None:
    assert parse(text, PurePosixPath(".env.example")).text == text
