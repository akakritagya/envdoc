"""Command-line entry point for envdoc.

This is the only module allowed to touch a terminal. Everything below it --
discovery, extraction, diffing, rendering -- is pure and returns data, so the
scanning logic can be tested with plain assertions instead of stdout scraping.

Two rules follow from that and are enforced by review rather than by the type
checker:

    - nothing under this module calls print(); warnings ride along on the
      Report and this layer decides whether to show them
    - typer.Exit appears here and nowhere else

Exit codes:

    0  scan ran, no drift
    1  scan ran, drift found -- the finding being gated on
    2  envdoc itself failed -- bad path, crash

1 and 2 are separate on purpose. A CI job has to be able to tell "your
.env.example is out of sync" from "the linter is broken", and collapsing both
into 1 throws that away at the one boundary where it matters most.
"""

from importlib.metadata import version as _package_version
from typing import Annotated

import typer

app = typer.Typer(
    name="envdoc",
    help="Audit a repository's environment-variable usage against its .env.example.",
)


def _version_callback(requested: bool) -> None:
    if requested:
        typer.echo(_package_version("envdoc"))
        raise typer.Exit(0)


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
