"""Pins which spellings of `os.environ` and `os.getenv` the extractor follows.

G2 recognised exactly one spelling: the attribute reached through a name
literally spelled `os`. Real code does not cooperate. `from os import environ`
is idiomatic, `env = os.environ` is common in anything that touches config, and
a file that does either had its variables silently missed -- which is the worst
failure mode an audit tool has, because a clean report and a broken scanner
look identical from outside.

The rule that makes this safe is that every alias must be *proved by an import
in the same file*. A bare `getenv("X")` is only os.getenv if this file imported
it from os; a project with its own `getenv` helper must not be scanned as
though it were the standard library's. That check is the difference between
alias tracking and guessing, so it gets its own tests.

What is deliberately not attempted is scope analysis. Bindings are collected
file-wide, so a function parameter or local that shadows an alias is still read
as the alias and over-reports. That is pinned below rather than hidden: it is a
known limitation with a test that will start failing the day anyone fixes it.
"""

from pathlib import PurePosixPath

import pytest

from envdoc.models import Confidence
from envdoc.sources.python_ast import extract


def extract_one(source: str) -> tuple[str, bool, str | None]:
    """The name, requiredness and default of a source that holds exactly one."""
    result = extract(source, PurePosixPath("src/app.py"))
    assert result.warnings == ()
    assert len(result.findings) == 1, f"expected exactly one finding, got {result.findings}"
    finding = result.findings[0]
    return finding.name, finding.occurrence.required, finding.occurrence.default


def extract_names(source: str) -> list[str]:
    return [f.name for f in extract(source, PurePosixPath("src/app.py")).findings]


@pytest.mark.parametrize(
    "source",
    [
        'import os as o\no.environ["DATABASE_URL"]\n',
        'import os as o\no.environ.get("DATABASE_URL")\n',
        'import os as o\no.getenv("DATABASE_URL")\n',
        'import os.path\nos.environ["DATABASE_URL"]\n',
    ],
    ids=[
        "aliased_module_subscript",
        "aliased_module_environ_get",
        "aliased_module_getenv",
        "a_submodule_import_still_binds_os",
    ],
)
def test_the_os_module_is_followed_through_its_import_alias(source: str) -> None:
    """`import os.path` binds the name `os`, so `os.environ` works after it."""
    assert extract_one(source) == ("DATABASE_URL", True, None)


@pytest.mark.parametrize(
    "source",
    [
        'from os import environ\nenviron["DATABASE_URL"]\n',
        'from os import environ\nenviron.get("DATABASE_URL")\n',
        'from os import getenv\ngetenv("DATABASE_URL")\n',
        'from os import environ as env\nenv["DATABASE_URL"]\n',
        'from os import environ as env\nenv.get("DATABASE_URL")\n',
        'from os import getenv as ge\nge("DATABASE_URL")\n',
        'from os import *\nenviron["DATABASE_URL"]\n',
        'from os import *\ngetenv("DATABASE_URL")\n',
    ],
    ids=[
        "imported_environ_subscript",
        "imported_environ_get",
        "imported_getenv",
        "aliased_environ_subscript",
        "aliased_environ_get",
        "aliased_getenv",
        "star_imported_environ",
        "star_imported_getenv",
    ],
)
def test_names_imported_from_os_are_followed(source: str) -> None:
    assert extract_one(source) == ("DATABASE_URL", True, None)


@pytest.mark.parametrize(
    "source",
    [
        'import os\nenv = os.environ\nenv["DATABASE_URL"]\n',
        'import os\nenv = os.environ\nenv.get("DATABASE_URL")\n',
        'import os\nge = os.getenv\nge("DATABASE_URL")\n',
        'import os\nenv: dict[str, str] = os.environ\nenv["DATABASE_URL"]\n',
        'import os\ndef f():\n    env = os.environ\n    return env["DATABASE_URL"]\n',
    ],
    ids=[
        "assigned_environ_subscript",
        "assigned_environ_get",
        "assigned_getenv",
        "an_annotated_assignment",
        "assigned_inside_a_function",
    ],
)
def test_a_name_assigned_the_environ_mapping_is_followed(source: str) -> None:
    assert extract_one(source) == ("DATABASE_URL", True, None)


def test_an_alias_keeps_the_fallback_rules_it_would_have_had() -> None:
    """Aliasing changes how a read is spelled, not what it means."""
    assert extract_one('from os import environ\nenviron.get("PORT", "8000")\n') == (
        "PORT",
        False,
        "8000",
    )
    assert extract_one('from os import getenv as ge\nge("PORT", default=8000)\n') == (
        "PORT",
        False,
        "8000",
    )


def test_a_name_read_through_an_alias_is_still_exact() -> None:
    """Confidence is a statement about the *name*, not about the binding.

    The name still came from a string literal; following an alias to reach it
    does not make the literal less certain. Downgrading here would leave a
    project that writes `from os import environ` throughout with every one of
    its variables marked doubtful.
    """
    result = extract('from os import environ\nenviron["DATABASE_URL"]\n', PurePosixPath("a.py"))

    assert result.findings[0].confidence is Confidence.EXACT


@pytest.mark.parametrize(
    "source",
    [
        'from mymodule import getenv\ngetenv("SECRET")\n',
        'from mymodule import environ\nenviron["SECRET"]\n',
        'from .os import environ\nenviron["SECRET"]\n',
        'environ["SECRET"]\n',
        'getenv("SECRET")\n',
        'os.environ["SECRET"]\n',
        'import os.path as p\np.environ["SECRET"]\n',
        'import os\nenv = os.environ.copy()\nenv["SECRET"]\n',
        'import os\nenv = {"SECRET": 1}\nenv["SECRET"]\n',
    ],
    ids=[
        "getenv_from_another_module",
        "environ_from_another_module",
        "a_relative_import_that_merely_looks_like_os",
        "a_bare_environ_nothing_imported",
        "a_bare_getenv_nothing_imported",
        "os_used_without_importing_it",
        "a_submodule_bound_to_its_own_alias",
        "a_copy_of_the_environment",
        "an_unrelated_dict_literal",
    ],
)
def test_an_alias_must_be_proved_by_an_import_in_the_same_file(source: str) -> None:
    """The guard that separates alias tracking from guessing.

    Plenty of projects define their own `getenv` helper, and reporting its
    argument as an environment variable would put names into the report that
    the environment has never heard of. So the binding has to be provable here,
    in this file -- an import from os, or an assignment of `os.environ` or
    `os.getenv` reached through a name that import bound.

    Two of these are limitations rather than principle. `os.environ` with no
    import anywhere is broken code, and a `.copy()` of the environment is a
    plain dict this module does not follow into; both are simply not tracked.
    """
    result = extract(source, PurePosixPath("src/app.py"))

    assert result.findings == ()
    assert result.dynamic == ()


@pytest.mark.parametrize(
    "source",
    [
        'import os\nself.env = os.environ\nself.env["SECRET"]\n',
        'import os\nregistry["env"] = os.environ\nregistry["env"]["SECRET"]\n',
        'import os\nenv, other = os.environ, None\nenv["SECRET"]\n',
    ],
    ids=["an_attribute_target", "a_subscript_target", "a_tuple_unpacking_target"],
)
def test_an_alias_bound_to_anything_but_a_plain_name_is_not_followed(source: str) -> None:
    """Only `name = os.environ` binds an alias.

    `self.env = os.environ` is real code, and following it would mean tracking
    attribute expressions rather than names -- worth doing when something asks
    for it, but a different piece of machinery from the four spellings this
    group set out to cover. Unpacking is left out for the same reason: it would
    have to match targets to values positionally to be correct at all.
    """
    result = extract(source, PurePosixPath("src/app.py"))

    assert result.findings == ()


def test_importing_other_names_from_os_alongside_the_ones_that_matter() -> None:
    """`from os import path, environ` must bind environ and ignore path."""
    assert extract_one('from os import path, environ\nenviron["DATABASE_URL"]\n') == (
        "DATABASE_URL",
        True,
        None,
    )


def test_an_import_anywhere_in_the_file_counts_including_inside_a_guard() -> None:
    """Bindings are collected from the whole file, so a conditional or
    function-local import proves the alias just as a top-level one does."""
    source = (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from os import environ\n"
        'environ["DATABASE_URL"]\n'
    )

    assert extract_one(source) == ("DATABASE_URL", True, None)


def test_an_alias_read_with_an_unresolvable_name_is_still_a_dynamic_reference() -> None:
    result = extract("from os import environ\nenviron[key]\n", PurePosixPath("src/app.py"))

    assert result.findings == ()
    assert [d.expression for d in result.dynamic] == ["key"]


def test_several_aliases_for_the_same_thing_coexist_in_one_file() -> None:
    source = (
        "import os\n"
        "from os import environ, getenv as ge\n"
        "env = os.environ\n"
        'os.environ["FIRST"]\n'
        'environ["SECOND"]\n'
        'ge("THIRD")\n'
        'env["FOURTH"]\n'
    )

    assert extract_names(source) == ["FIRST", "SECOND", "THIRD", "FOURTH"]


def test_a_local_that_shadows_an_alias_is_still_read_as_the_alias() -> None:
    """The documented limitation: bindings are file-wide, with no scope analysis.

    Here `environ` is a parameter holding some caller's dictionary, and the
    read has nothing to do with the process environment -- but the import at
    the top of the file bound the name, and this module does not track that the
    parameter took it back. The result is an over-report.

    Over-reporting is the right way to be wrong here. A spurious row in the
    table is visible and someone deletes it; a silently missed variable is the
    failure that ships to production. Full scope analysis is what it would take
    to fix, and that is a great deal of machinery for a shadowing pattern that
    is rare in the first place.
    """
    source = 'from os import environ\ndef render(environ):\n    return environ["NOT_REALLY_ENV"]\n'

    assert extract_names(source) == ["NOT_REALLY_ENV"]
