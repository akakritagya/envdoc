"""Walk a repository and hand back the files worth parsing.

The bottom of the stack, and the only module that touches a filesystem. Two
contracts meet here.

The first is determinism. Paths come out sorted, relative to the root, and
POSIX-separated, because `os.walk` yields whatever order the filesystem hands
back and an absolute path in a report leaks whose machine produced it. Sorting
at the source is what makes every golden test downstream possible.

The second is that a repository is a hostile place to walk. It contains a
virtualenv holding thousands of files that are not this project's code,
symlinks that point at their own ancestors, vendored bundles megabytes long,
and the occasional file that is not UTF-8 at all. Any of those turns a naive
walk into a hang, a crash or a flood of meaningless findings, and a linter that
dies on the repository it was pointed at is worse than no linter. Each is
skipped; the two that suggest a real problem say so in a warning that rides on
the result, since only the CLI knows whether --quiet is in force.
"""

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath

DEFAULT_MAX_BYTES = 1_048_576

# Matched against a directory's *name*, at any depth. Everything here holds
# code that belongs to something other than the project being audited, and
# scanning it produces findings nobody can act on -- a variable read inside
# site-packages is not this repository's variable.
#
# Deliberately conservative. `build/` and `dist/` are not here despite usually
# being artifacts, because sometimes they are somebody's source directory, and
# the failure modes are not symmetric: an extra row in the table is visible and
# gets deleted, while a silently missed variable is what ships to production.
DEFAULT_IGNORED_DIRECTORIES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        ".direnv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".nox",
        ".eggs",
        "site-packages",
        ".idea",
        ".vscode",
    }
)


@dataclass(frozen=True, slots=True)
class DiscoveredFile:
    """One file that was found, read and decoded.

    The text travels with the path so that nothing above this layer has to open
    a file: every extractor takes a string, which is what lets them be tested
    with a source literal instead of a fixture tree.
    """

    path: PurePosixPath
    text: str


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    """Everything one walk produced."""

    files: tuple[DiscoveredFile, ...]
    warnings: tuple[str, ...]


def _is_excluded(path: PurePosixPath, patterns: Sequence[str]) -> bool:
    """Whether `path` matches any exclude pattern.

    `fnmatch` is used rather than `PurePath.match` because its `*` crosses
    directory separators, which is what people mean by both `*.js` and
    `tests/fixtures/*`. A pattern is also matched against a directory on the
    way down, so `exclude = ["db/migrations"]` prunes the directory -- plainly
    what someone writing that meant, and it saves walking it at all.
    """
    return any(fnmatchcase(str(path), pattern) for pattern in patterns)


def discover(
    root: Path,
    *,
    select: Callable[[PurePosixPath], bool] | None = None,
    exclude: Sequence[str] = (),
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> DiscoveryResult:
    """Every readable, selected, non-ignored file under `root`.

    `select` decides which paths are worth opening. Discovery does not know
    which files a parser exists for, so the caller says; without it, a scan
    would decode every PNG in the repository and warn about each one.

    A missing or non-directory root raises rather than returning nothing. An
    empty result renders as a clean report, and `envdoc check` passing green on
    a path typo is the one failure a CI gate must never have.
    """
    if not root.exists():
        raise FileNotFoundError(f"no such directory: {root}")
    if not root.is_dir():
        raise NotADirectoryError(f"not a directory: {root}")

    base = root.resolve()
    files: list[DiscoveredFile] = []
    warnings: list[str] = []

    # followlinks stays False, which is what makes a symlink loop terminate: a
    # directory link is never descended into, so a link pointing at its own
    # ancestor is simply not walked. It also stops one file being reported
    # under two paths, and the same variable counted as though two places used
    # it.
    for directory, subdirectories, filenames in os.walk(base, followlinks=False):
        here = PurePosixPath(Path(directory).relative_to(base).as_posix())

        # Pruning in place is what os.walk reads back on the next iteration.
        # Sorting it makes the traversal order stable as well as the output.
        subdirectories[:] = sorted(
            name
            for name in subdirectories
            if name not in DEFAULT_IGNORED_DIRECTORIES
            and not _is_excluded(_join(here, name), exclude)
        )

        for filename in sorted(filenames):
            path = _join(here, filename)
            if _is_excluded(path, exclude):
                continue
            if select is not None and not select(path):
                continue

            text = _read(base / path, path, max_bytes, warnings)
            if text is not None:
                files.append(DiscoveredFile(path=path, text=text))

    files.sort(key=lambda f: str(f.path))
    warnings.sort()
    return DiscoveryResult(files=tuple(files), warnings=tuple(warnings))


def _join(directory: PurePosixPath, name: str) -> PurePosixPath:
    """`directory / name`, with the root itself spelled as no prefix at all."""
    return PurePosixPath(name) if str(directory) == "." else directory / name


def _read(absolute: Path, path: PurePosixPath, max_bytes: int, warnings: list[str]) -> str | None:
    """Decoded contents, or None with a warning explaining why not.

    Size is checked before opening, since the point of the limit is to not read
    the file.
    """
    try:
        size = absolute.stat().st_size
    except OSError as exc:
        warnings.append(f"{path}: skipped, could not be read ({exc.strerror or exc})")
        return None

    if size > max_bytes:
        # Minified bundles and vendored data files are megabytes on one line.
        # Parsing them is slow and the findings are meaningless, but doing it
        # silently leaves someone wondering why the scan takes a minute.
        warnings.append(f"{path}: skipped, {size} bytes exceeds the {max_bytes} byte limit")
        return None

    try:
        return absolute.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        # Guessing an encoding would be worse than skipping. A mis-decoded file
        # yields variable names that were never in it, and a name that does not
        # exist is the one thing a report cannot recover from.
        warnings.append(f"{path}: skipped, not valid UTF-8")
        return None
    except OSError as exc:
        warnings.append(f"{path}: skipped, could not be read ({exc.strerror or exc})")
        return None
