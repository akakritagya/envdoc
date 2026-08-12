"""Pins which files a scan sees, in what order, and what it refuses to open.

Discovery is the bottom of the stack and the only place that touches a
filesystem, so two contracts meet here. The first is determinism: paths come
out sorted, relative, and POSIX-separated, because `rglob` order varies by
filesystem and an absolute path in a report leaks whose machine produced it.
Sorting at the source is what makes every golden test downstream possible.

The second is that a repository is a hostile place to walk. It contains a
virtualenv with thousands of files that are not this project's code, symlinks
that point at their own ancestors, vendored minified bundles megabytes long,
and the occasional file that is not UTF-8 at all. Any of those can turn a scan
into a hang or a crash, and a linter that dies on the repo it was pointed at is
worse than no linter. Each is skipped, and the two that suggest a real problem
say so in a warning.

Warnings ride on the result rather than being printed, because only the CLI
knows whether --quiet is in force.
"""

import os
from pathlib import Path, PurePosixPath

import pytest

from envdoc.discovery import DEFAULT_MAX_BYTES, discover


def build(root: Path, files: dict[str, str | bytes]) -> None:
    """Write a tree from a {relative path: contents} mapping."""
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")


def paths(root: Path, **kwargs: object) -> list[str]:
    return [str(f.path) for f in discover(root, **kwargs).files]  # type: ignore[arg-type]


def test_files_come_out_sorted_relative_and_posix_separated(tmp_path: Path) -> None:
    """The determinism contract, at the one layer that could break it.

    `os.walk` yields whatever order the filesystem hands back, which differs
    between ext4, APFS and a CI runner's overlay. Sorting here is what lets
    every golden test downstream compare bytes.
    """
    build(tmp_path, {"z.py": "", "src/b.py": "", "src/a.py": "", "src/deep/nested/c.py": ""})

    assert paths(tmp_path) == ["src/a.py", "src/b.py", "src/deep/nested/c.py", "z.py"]


def test_a_discovered_path_is_relative_to_the_root_and_never_absolute(tmp_path: Path) -> None:
    build(tmp_path, {"src/app.py": ""})

    discovered = discover(tmp_path).files[0]

    assert discovered.path == PurePosixPath("src/app.py")
    assert not str(discovered.path).startswith("/")


def test_the_decoded_text_of_each_file_comes_back_with_it(tmp_path: Path) -> None:
    """Extractors take a string, so nothing above this layer needs to open a file."""
    build(tmp_path, {"app.py": 'import os\nos.getenv("PORT")\n'})

    assert discover(tmp_path).files[0].text == 'import os\nos.getenv("PORT")\n'


@pytest.mark.parametrize(
    "ignored",
    [".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache", ".tox"],
    ids=[".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache", ".tox"],
)
def test_directories_that_are_never_this_projects_code_are_skipped(
    tmp_path: Path, ignored: str
) -> None:
    build(tmp_path, {f"{ignored}/vendored.py": "", "app.py": ""})

    assert paths(tmp_path) == ["app.py"]


def test_an_ignored_directory_is_skipped_at_any_depth(tmp_path: Path) -> None:
    build(tmp_path, {"src/nested/__pycache__/app.cpython-312.py": "", "src/nested/app.py": ""})

    assert paths(tmp_path) == ["src/nested/app.py"]


def test_a_dotted_directory_is_not_ignored_merely_for_being_dotted(tmp_path: Path) -> None:
    """`.github/workflows/` holds deployment manifests -- the third axis, and
    the entire reason this tool exists. A blanket dot-directory rule would
    quietly delete the differentiator."""
    build(tmp_path, {".github/workflows/ci.yml": "", ".config/settings.yml": ""})

    assert paths(tmp_path) == [".config/settings.yml", ".github/workflows/ci.yml"]


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ("tests/fixtures/*", ["src/app.py", "src/vendor.js", "tests/test_app.py"]),
        ("*.js", ["src/app.py", "tests/fixtures/bad.py", "tests/test_app.py"]),
        ("tests/*", ["src/app.py", "src/vendor.js"]),
        ("src/app.py", ["src/vendor.js", "tests/fixtures/bad.py", "tests/test_app.py"]),
    ],
    ids=[
        "a_directory_prefix",
        "an_extension_at_any_depth",
        "a_star_crossing_separators",
        "one_exact_path",
    ],
)
def test_exclude_patterns_are_matched_against_the_relative_path(
    tmp_path: Path, pattern: str, expected: list[str]
) -> None:
    """`*` crosses directory separators, so `tests/*` takes the whole subtree.

    This is what `[tool.envdoc] exclude = ["tests/fixtures/*"]` will lean on
    when envdoc scans itself: its own fixtures are deliberately full of
    undocumented variables.
    """
    build(
        tmp_path,
        {
            "src/app.py": "",
            "src/vendor.js": "",
            "tests/test_app.py": "",
            "tests/fixtures/bad.py": "",
        },
    )

    assert paths(tmp_path, exclude=[pattern]) == expected


def test_an_excluded_directory_named_on_its_own_is_pruned(tmp_path: Path) -> None:
    """`exclude = ["migrations"]` should mean the directory, not a file of that
    name, because that is plainly what someone writing it meant."""
    build(tmp_path, {"db/migrations/0001.py": "", "db/models.py": ""})

    assert paths(tmp_path, exclude=["db/migrations"]) == ["db/models.py"]


def test_several_exclude_patterns_all_apply(tmp_path: Path) -> None:
    build(tmp_path, {"a.py": "", "b.js": "", "c.py": "", "d.txt": ""})

    assert paths(tmp_path, exclude=["*.js", "c.py"]) == ["a.py", "d.txt"]


def test_only_selected_files_are_opened(tmp_path: Path) -> None:
    """Discovery does not know which files a parser exists for, so the caller
    says. Reading everything would mean decoding every PNG in the repository
    and warning about each one."""
    build(tmp_path, {"app.py": "", "logo.png": "", "README.md": ""})

    assert paths(tmp_path, select=lambda path: path.suffix == ".py") == ["app.py"]


def test_the_selector_sees_the_same_relative_posix_path_that_is_reported(
    tmp_path: Path,
) -> None:
    build(tmp_path, {"src/deep/app.py": ""})
    seen: list[PurePosixPath] = []

    def record(path: PurePosixPath) -> bool:
        seen.append(path)
        return True

    discover(tmp_path, select=record)

    assert seen == [PurePosixPath("src/deep/app.py")]


def test_a_file_larger_than_the_limit_is_skipped_with_a_warning(tmp_path: Path) -> None:
    """Minified bundles and vendored data files are megabytes of one line.

    Parsing them is slow, the findings are meaningless, and doing it silently
    would leave someone wondering why a scan takes a minute.
    """
    build(tmp_path, {"bundle.js": "x" * 2000, "app.py": ""})

    result = discover(tmp_path, max_bytes=1000)

    assert [str(f.path) for f in result.files] == ["app.py"]
    assert result.warnings == ("bundle.js: skipped, 2000 bytes exceeds the 1000 byte limit",)


def test_a_file_that_is_not_utf_8_is_skipped_with_a_warning(tmp_path: Path) -> None:
    """Latin-1 source still exists, and decoding it as UTF-8 raises.

    Guessing an encoding would be worse than skipping: a mis-decoded file
    yields variable names that were never in it.
    """
    build(tmp_path, {"legacy.py": b'# caf\xe9\nimport os\nos.getenv("PORT")\n', "app.py": ""})

    result = discover(tmp_path)

    assert [str(f.path) for f in result.files] == ["app.py"]
    assert result.warnings == ("legacy.py: skipped, not valid UTF-8",)


def test_a_file_that_cannot_be_opened_at_all_is_skipped_with_a_warning(tmp_path: Path) -> None:
    (tmp_path / "dangling").symlink_to(tmp_path / "nowhere")
    build(tmp_path, {"app.py": ""})

    result = discover(tmp_path)

    assert [str(f.path) for f in result.files] == ["app.py"]
    assert len(result.warnings) == 1
    assert result.warnings[0].startswith("dangling: skipped, could not be read")


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read a file whatever its mode says")
def test_a_file_that_stats_but_will_not_open_is_skipped_with_a_warning(tmp_path: Path) -> None:
    """The size check and the read are separate syscalls, so a file can pass
    the first and fail the second -- unreadable by mode here, but equally a
    file someone deleted in between."""
    build(tmp_path, {"secret.py": "", "app.py": ""})
    (tmp_path / "secret.py").chmod(0o000)

    result = discover(tmp_path)

    assert [str(f.path) for f in result.files] == ["app.py"]
    assert result.warnings[0].startswith("secret.py: skipped, could not be read")


def test_a_symlinked_directory_is_not_followed(tmp_path: Path) -> None:
    """Following one means reporting the same file under two paths, and the
    same variable as though two places used it."""
    build(tmp_path, {"src/app.py": ""})
    (tmp_path / "mirror").symlink_to(tmp_path / "src", target_is_directory=True)

    assert paths(tmp_path) == ["src/app.py"]


def test_a_symlink_loop_terminates(tmp_path: Path) -> None:
    """The pathological case: a directory that contains a link to itself.

    A walker that follows directory symlinks descends forever, and a linter
    that hangs on the repository it was pointed at is worse than no linter.
    """
    build(tmp_path, {"src/app.py": ""})
    (tmp_path / "src" / "loop").symlink_to(tmp_path, target_is_directory=True)

    assert paths(tmp_path) == ["src/app.py"]


def test_warnings_come_out_in_path_order(tmp_path: Path) -> None:
    build(tmp_path, {"z.py": b"\xff", "a.py": b"\xff", "m.py": b"\xff"})

    result = discover(tmp_path)

    assert result.warnings == (
        "a.py: skipped, not valid UTF-8",
        "m.py: skipped, not valid UTF-8",
        "z.py: skipped, not valid UTF-8",
    )


def test_an_empty_directory_produces_an_empty_result(tmp_path: Path) -> None:
    result = discover(tmp_path)

    assert result.files == ()
    assert result.warnings == ()


def test_a_root_that_does_not_exist_raises_rather_than_returning_nothing(tmp_path: Path) -> None:
    """The 1-versus-2 exit code split starts here.

    An empty result would render as a clean report, and `envdoc check` would
    pass green on a path typo. That is the one failure a CI gate must never
    have, so this raises and the CLI turns it into exit code 2.
    """
    with pytest.raises(FileNotFoundError, match=r"no such directory: .*missing"):
        discover(tmp_path / "missing")


def test_a_root_that_is_a_file_raises(tmp_path: Path) -> None:
    build(tmp_path, {"app.py": ""})

    with pytest.raises(NotADirectoryError, match=r"not a directory: .*app\.py"):
        discover(tmp_path / "app.py")


def test_the_same_tree_discovers_identically_twice(tmp_path: Path) -> None:
    build(tmp_path, {"b.py": "two", "a.py": "one", "sub/c.py": "three", "bad.py": b"\xff"})

    assert discover(tmp_path) == discover(tmp_path)


def test_the_default_size_limit_is_a_megabyte(tmp_path: Path) -> None:
    """Pinned because it is the one default a user notices only by its absence:
    a file over it is silently absent from the report but for the warning."""
    assert DEFAULT_MAX_BYTES == 1_048_576


def test_a_hostile_tree_is_walked_to_completion_skipping_all_four_hazards(
    tmp_path: Path,
) -> None:
    """The gate for this group, with all four hazards in one tree.

    Each of these has a different failure mode -- the virtualenv floods the
    report, the loop hangs the walk, the huge file stalls it, the latin-1 file
    raises -- and the point is that one pass survives all of them and still
    finds the two real files. Two of the four warn: a skipped virtualenv and an
    unfollowed symlink are normal, while a file too big to read or impossible
    to decode is a thing someone may want to know about.
    """
    build(
        tmp_path,
        {
            "src/app.py": 'import os\nos.getenv("PORT")\n',
            ".env.example": "PORT=8000\n",
            ".venv/lib/site.py": 'import os\nos.getenv("VENV_NOISE")\n',
            "vendor/bundle.js": "x" * (5 * 1024 * 1024),
            "legacy/latin1.py": b'# caf\xe9\nimport os\nos.getenv("LEGACY")\n',
        },
    )
    (tmp_path / "src" / "loop").symlink_to(tmp_path, target_is_directory=True)

    result = discover(tmp_path)

    assert [str(f.path) for f in result.files] == [".env.example", "src/app.py"]
    assert result.warnings == (
        "legacy/latin1.py: skipped, not valid UTF-8",
        "vendor/bundle.js: skipped, 5242880 bytes exceeds the 1048576 byte limit",
    )
