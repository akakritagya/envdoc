"""Command-line entry point for envdoc.

This is the only module allowed to touch a terminal. Everything below it --
discovery, extraction, auditing, rendering -- is pure and returns data, so
the scanning logic can be tested with plain assertions instead of stdout
scraping.

Two rules follow from that and are enforced by review rather than by the
type checker:

    - nothing under this module calls print(); envdoc.render never embeds
      Report.warnings in its output for exactly this reason -- this module
      prints the rendered report, then prints the warnings itself, unless
      --quiet
    - typer.Exit appears here and nowhere else -- funnelled through _exit(),
      so every exit code this file produces is one grep away

Exit codes:

    0  scan ran, no drift
    1  scan ran, drift found -- the finding being gated on
    2  envdoc itself failed -- bad path, bad config, crash

1 and 2 are separate on purpose. A CI job has to be able to tell "your
.env.example is out of sync" from "the linter is broken", and collapsing both
into 1 throws that away at the one boundary where it matters most. `scan`
never returns 1 -- it has no threshold to gate on -- so 1 is `check`'s exit
code alone.

`scan` and `check` share almost their entire body (`_run`): walk the
repository, extract, audit. They differ only in what they do with the
resulting `Report` -- `scan` always exits 0 on success, `check` exits 1 if
`report.has_drift(config.fail_on)`.

`docker-compose.yml`, `Dockerfile`, GitHub Actions workflows and `fly.toml`
are the deployment manifests with a parser so far -- k8s manifests are
cut from G15 (real complexity: list-shaped `env:`, cross-file `envFrom:`,
several resource kinds) and left for a later group. `_run` passes every
discovered manifest's path to `audit()` as `deployment_files`, not just the
ones that yielded a finding -- a manifest with no `environment:`/`ENV`/
`env:`/`[env]` in it at all is precisely the case that should make every
required variable `UNSET_IN_DEPLOYMENT`, and inferring "were there
manifests?" from the findings themselves would conclude there were none and
call the repository clean.
"""

from datetime import UTC, datetime
from importlib.metadata import version as _package_version
from pathlib import Path, PurePosixPath
from typing import Annotated, NoReturn

import typer

from envdoc.audit import audit
from envdoc.baseline import BASELINE_FILENAME, BaselineError
from envdoc.baseline import apply as apply_baseline
from envdoc.baseline import capture as capture_baseline
from envdoc.baseline import parse as parse_baseline
from envdoc.baseline import serialize as serialize_baseline
from envdoc.config import Config, ConfigError
from envdoc.config import resolve as resolve_config
from envdoc.discovery import DiscoveredFile, discover
from envdoc.models import DynamicRef, ExtractResult, FailOn, Finding, Report
from envdoc.render import OutputFormat, render
from envdoc.sources import (
    docker_compose,
    dockerfile,
    dotenv,
    fly_toml,
    github_actions,
    python_ast,
    python_settings,
    ts_js,
)
from envdoc.sync import EXAMPLE_FILENAME
from envdoc.sync import plan as plan_sync
from envdoc.sync import write as write_sync

app = typer.Typer(
    name="envdoc",
    help="Audit a repository's environment-variable usage against its .env.example.",
)


def _exit(code: int) -> NoReturn:
    """The one place typer.Exit is raised. Every exit path in this module
    calls this instead of raising directly, so the exit-code contract stays
    grep-able from a single line rather than scattered across commands."""
    raise typer.Exit(code)


def _version_callback(requested: bool) -> None:
    if requested:
        typer.echo(_package_version("envdoc"))
        _exit(0)


VersionOption = Annotated[
    bool,
    typer.Option(
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Print the installed version and exit.",
    ),
]


@app.callback()
def main(version: VersionOption = False) -> None:
    """Audit a repository's environment-variable usage against its .env.example."""


PathArgument = Annotated[Path, typer.Argument(help="Repository to scan. Defaults to the cwd.")]
ExcludeOption = Annotated[
    list[str] | None,
    typer.Option("--exclude", help="Glob pattern to skip; repeatable. Overrides pyproject.toml."),
]
FormatOption = Annotated[
    OutputFormat | None, typer.Option("--format", help="Output format. Overrides pyproject.toml.")
]
QuietOption = Annotated[bool, typer.Option("--quiet", help="Suppress warnings on stderr.")]
IncludeTimestampOption = Annotated[
    bool,
    typer.Option(
        "--include-timestamp", help="Embed a generation timestamp in --format json output."
    ),
]
FailOnOption = Annotated[
    FailOn | None,
    typer.Option("--fail-on", help="Drift threshold for `check`. Overrides pyproject.toml."),
]
DryRunOption = Annotated[
    bool, typer.Option("--dry-run", help="Show what sync would add without writing it.")
]
BaselineOption = Annotated[
    str | None,
    typer.Option(
        "--baseline",
        help="Suppress drift recorded in this file. Overrides pyproject.toml. Never auto-detected.",
    ),
]


_COMPOSE_FILENAME = "docker-compose.yml"
_FLY_TOML_FILENAME = "fly.toml"
_TS_JS_EXTENSIONS = (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx")
_WORKFLOWS_DIRECTORY = PurePosixPath(".github/workflows")


def _is_dockerfile(path: PurePosixPath) -> bool:
    """`Dockerfile`, `Dockerfile.dev`/`Dockerfile.prod`-style variants, and
    `*.dockerfile` -- all common in real repos. A compose service's own
    `build.dockerfile:` override pointing at a custom path is not chased;
    only discovery's own filename matching decides what's read."""
    return (
        path.name == "Dockerfile"
        or path.name.startswith("Dockerfile.")
        or path.suffix == ".dockerfile"
    )


def _is_github_workflow(path: PurePosixPath) -> bool:
    """`.github/workflows/*.yml`/`*.yaml` -- the first manifest matched by
    directory rather than by name or extension alone. Composite/reusable
    actions (`action.yml` elsewhere in the tree) are not matched."""
    return path.parent == _WORKFLOWS_DIRECTORY and path.suffix in (".yml", ".yaml")


def _select(path: PurePosixPath) -> bool:
    """Which discovered files this group's extractors can read."""
    return (
        path.suffix == ".py"
        or path.suffix in _TS_JS_EXTENSIONS
        or path.name in (".env.example", _COMPOSE_FILENAME, _FLY_TOML_FILENAME)
        or _is_dockerfile(path)
        or _is_github_workflow(path)
    )


def _extract(discovered: DiscoveredFile) -> tuple[list[Finding], list[DynamicRef], list[str]]:
    if discovered.path.suffix == ".py":
        # Two independent extractors read the same file: python_ast.py for
        # call-site reads, python_settings.py for pydantic-settings classes.
        # Neither is a mode of the other -- each parses the source itself and
        # returns its own ExtractResult, merged here rather than one handing
        # the other a shared tree.
        ast_result = python_ast.extract(discovered.text, discovered.path)
        settings_result = python_settings.extract(discovered.text, discovered.path)
        result = ExtractResult(
            findings=ast_result.findings + settings_result.findings,
            dynamic=ast_result.dynamic + settings_result.dynamic,
            warnings=ast_result.warnings + settings_result.warnings,
        )
    elif discovered.path.suffix in _TS_JS_EXTENSIONS:
        result = ts_js.extract(discovered.text, discovered.path)
    elif discovered.path.name == _COMPOSE_FILENAME:
        result = docker_compose.extract(discovered.text, discovered.path)
    elif discovered.path.name == _FLY_TOML_FILENAME:
        result = fly_toml.extract(discovered.text, discovered.path)
    elif _is_dockerfile(discovered.path):
        result = dockerfile.extract(discovered.text, discovered.path)
    elif _is_github_workflow(discovered.path):
        result = github_actions.extract(discovered.text, discovered.path)
    else:
        result = dotenv.extract(discovered.text, discovered.path)
    return list(result.findings), list(result.dynamic), list(result.warnings)


def _run(path: Path, config: Config) -> Report:
    """Walk `path`, extract, and audit -- the body `scan` and `check` share.

    Raises FileNotFoundError / NotADirectoryError exactly as discover() does;
    callers turn those into exit code 2 rather than a traceback.
    """
    discovered = discover(path, select=_select, exclude=config.exclude)

    findings: list[Finding] = []
    dynamic: list[DynamicRef] = []
    warnings: list[str] = list(discovered.warnings)
    deployment_files: list[str] = []
    for file in discovered.files:
        if (
            file.path.name in (_COMPOSE_FILENAME, _FLY_TOML_FILENAME)
            or _is_dockerfile(file.path)
            or _is_github_workflow(file.path)
        ):
            deployment_files.append(str(file.path))
        file_findings, file_dynamic, file_warnings = _extract(file)
        findings.extend(file_findings)
        dynamic.extend(file_dynamic)
        warnings.extend(file_warnings)

    return audit(
        findings,
        root=PurePosixPath(path.as_posix()),
        dynamic=dynamic,
        warnings=warnings,
        files_scanned=len(discovered.files),
        deployment_files=deployment_files,
    )


def _resolve(
    path: Path,
    *,
    exclude: list[str] | None,
    fail_on: FailOn | None,
    format: OutputFormat | None,
    quiet: bool,
    include_timestamp: bool,
    baseline: str | None = None,
) -> Config:
    return resolve_config(
        path,
        exclude=tuple(exclude) if exclude is not None else None,
        fail_on=fail_on,
        format=format,
        quiet=quiet,
        include_timestamp=include_timestamp,
        baseline=baseline,
    )


def _print_report(report: Report, config: Config) -> None:
    generated_at = datetime.now(UTC).isoformat() if config.include_timestamp else None
    text = render(
        report, config.format, tool_version=_package_version("envdoc"), generated_at=generated_at
    )
    typer.echo(text, nl=False)

    if not config.quiet:
        for warning in report.warnings:
            typer.echo(warning, err=True)


@app.command()
def scan(
    path: PathArgument = Path("."),
    exclude: ExcludeOption = None,
    format: FormatOption = None,
    quiet: QuietOption = False,
    include_timestamp: IncludeTimestampOption = False,
) -> None:
    """Audit PATH and print a report. Never fails on drift -- see `check`."""
    try:
        config = _resolve(
            path,
            exclude=exclude,
            fail_on=None,
            format=format,
            quiet=quiet,
            include_timestamp=include_timestamp,
        )
        report = _run(path, config)
    except (FileNotFoundError, NotADirectoryError, ConfigError) as exc:
        typer.echo(str(exc), err=True)
        _exit(2)

    _print_report(report, config)
    _exit(0)


@app.command()
def check(
    path: PathArgument = Path("."),
    exclude: ExcludeOption = None,
    fail_on: FailOnOption = None,
    format: FormatOption = None,
    quiet: QuietOption = False,
    include_timestamp: IncludeTimestampOption = False,
    baseline: BaselineOption = None,
) -> None:
    """Audit PATH and exit 1 if drift at or above --fail-on was found.

    --baseline suppresses drift already recorded in a file `envdoc baseline`
    wrote, so a repository with existing debt can turn this gate on without
    failing on day one -- new drift still fails. Opt-in only: a baseline is
    never auto-detected, and a configured path that doesn't exist is exit 2,
    not a silent pass -- the same failure a typo'd path produces read from the
    other side, where suppression would silently cover everything instead of
    nothing.
    """
    try:
        config = _resolve(
            path,
            exclude=exclude,
            fail_on=fail_on,
            format=format,
            quiet=quiet,
            include_timestamp=include_timestamp,
            baseline=baseline,
        )
        report = _run(path, config)
        if config.baseline is not None:
            baseline_path = path / config.baseline
            if not baseline_path.exists():
                raise FileNotFoundError(f"no such baseline file: {baseline_path}")
            text = baseline_path.read_text(encoding="utf-8")
            parsed = parse_baseline(text, PurePosixPath(config.baseline))
            report = apply_baseline(report, parsed).report
    except (FileNotFoundError, NotADirectoryError, ConfigError, BaselineError, OSError) as exc:
        typer.echo(str(exc), err=True)
        _exit(2)

    _print_report(report, config)
    _exit(1 if report.has_drift(config.fail_on) else 0)


@app.command()
def sync(
    path: PathArgument = Path("."),
    exclude: ExcludeOption = None,
    quiet: QuietOption = False,
    dry_run: DryRunOption = False,
) -> None:
    """Append variables read in code but missing from .env.example.

    Append-only: a STALE entry -- documented, but nothing reads it -- is left
    exactly as it was. `check` already reports it; removing someone's
    documentation on their behalf is not this command's call to make. Never
    exits 1 -- gating is `check`'s job alone, even with pending changes and
    `--dry-run` together.
    """
    example_path = path / EXAMPLE_FILENAME
    try:
        config = _resolve(
            path, exclude=exclude, fail_on=None, format=None, quiet=quiet, include_timestamp=False
        )
        report = _run(path, config)
        original = example_path.read_text(encoding="utf-8") if example_path.exists() else ""
        result = plan_sync(report, original=original)
    except (FileNotFoundError, NotADirectoryError, ConfigError, OSError) as exc:
        typer.echo(str(exc), err=True)
        _exit(2)

    if result.added:
        for name in result.added:
            typer.echo(f"+ {name}")
    else:
        typer.echo("up to date")

    if result.changed and not dry_run:
        try:
            write_sync(result.updated, example_path)
        except OSError as exc:
            typer.echo(str(exc), err=True)
            _exit(2)

    if not config.quiet:
        for warning in report.warnings:
            typer.echo(warning, err=True)

    _exit(0)


@app.command()
def baseline(
    path: PathArgument = Path("."),
    exclude: ExcludeOption = None,
    quiet: QuietOption = False,
    dry_run: DryRunOption = False,
) -> None:
    """Write today's drift to `.envdoc-baseline.json` for `check --baseline`.

    Adopting the gate on a repository that already has drift means capturing
    that drift first -- this records every non-OK `(name, status)` pair so
    `check --baseline` suppresses exactly what already existed and still
    fails on anything new. Keyed by name rather than file:line, so the file
    stays valid across renames and refactors. Re-running it after fixing
    something shrinks the file; re-running it on an unchanged repository
    produces byte-identical output. Never exits 1 -- same as `sync`.
    """
    baseline_path = path / BASELINE_FILENAME
    try:
        config = _resolve(
            path, exclude=exclude, fail_on=None, format=None, quiet=quiet, include_timestamp=False
        )
        report = _run(path, config)
        captured = capture_baseline(report)
        content = serialize_baseline(captured, tool_version=_package_version("envdoc"))
    except (FileNotFoundError, NotADirectoryError, ConfigError, OSError) as exc:
        typer.echo(str(exc), err=True)
        _exit(2)

    if captured.entries:
        for name, status in captured.entries:
            typer.echo(f"+ {name}: {status.value}")
    else:
        typer.echo("no drift to baseline")

    if not dry_run:
        try:
            write_sync(content, baseline_path)
        except OSError as exc:
            typer.echo(str(exc), err=True)
            _exit(2)

    if not config.quiet:
        for warning in report.warnings:
            typer.echo(warning, err=True)

    _exit(0)
