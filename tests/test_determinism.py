"""Pins the cross-cutting contract: byte-identical output for the same repo
on any machine, in any input order.

Three separate guarantees are tested here, because they can fail
independently. `audit()` has to reach the same `Report` regardless of the
order findings arrive in -- discovery order is filesystem-dependent, so
nothing downstream of it may depend on it. Each renderer then has to turn one
`Report` into the same bytes every time it is called, which is a weaker
property but the one a CI diff actually checks (`envdoc scan . --format
json` run twice). The third guarantee needs a subprocess to even observe:
`Status` is a `StrEnum`, so its members hash the same as their string value,
and CPython randomises string hashing per process. A single pytest run
samples exactly one hash seed and so cannot catch a renderer that iterates a
`frozenset[Status]` directly instead of going through `render._sorted_statuses`
-- every in-process test would pass, and the regression would only surface
as two live `envdoc scan` runs of an unchanged repository disagreeing.

Discovery's own sort is pinned separately in test_discovery.py; this file
starts from findings rather than files.
"""

import os
import subprocess
import sys

from helpers import deployment_entry, example_entry, finding

from envdoc.audit import audit
from envdoc.render import render_json, render_markdown, render_table


def test_auditing_the_same_findings_in_reverse_order_produces_an_identical_report() -> None:
    findings = [
        finding("DATABASE_URL", "src/db.py", line=5),
        example_entry("DATABASE_URL"),
        finding("PORT", "src/api.py", line=9, required=False, default="8000"),
        deployment_entry("PORT"),
        finding("SECRET_KEY", "src/auth.py", line=1),
    ]

    forward = audit(findings, deployment_files=("docker-compose.yml",))
    backward = audit(list(reversed(findings)), deployment_files=("docker-compose.yml",))

    assert forward == backward


def test_rendering_the_same_report_twice_produces_byte_identical_json() -> None:
    findings = [finding("DATABASE_URL"), example_entry("DATABASE_URL")]
    report = audit(findings, deployment_files=("docker-compose.yml",))

    first = render_json(report, tool_version="0.1.0")
    second = render_json(report, tool_version="0.1.0")

    assert first == second


def test_rendering_the_same_report_twice_produces_byte_identical_markdown() -> None:
    findings = [finding("DATABASE_URL"), example_entry("DATABASE_URL")]
    report = audit(findings, deployment_files=("docker-compose.yml",))

    assert render_markdown(report) == render_markdown(report)


def test_rendering_the_same_report_twice_produces_byte_identical_table() -> None:
    findings = [finding("DATABASE_URL"), example_entry("DATABASE_URL")]
    report = audit(findings, deployment_files=("docker-compose.yml",))

    assert render_table(report) == render_table(report)


def test_the_full_pipeline_gives_the_same_json_regardless_of_finding_order() -> None:
    """The property the CI verification script actually exercises, minus the
    filesystem: build a report from findings in two different orders and
    confirm the rendered JSON -- not just the Report -- is identical."""
    findings = [
        finding("DATABASE_URL", "src/db.py", line=5),
        example_entry("DATABASE_URL"),
        finding("PORT", "src/api.py", line=9, required=False, default="8000"),
        deployment_entry("PORT"),
    ]

    forward = render_json(
        audit(findings, deployment_files=("docker-compose.yml",)), tool_version="0.1.0"
    )
    backward = render_json(
        audit(list(reversed(findings)), deployment_files=("docker-compose.yml",)),
        tool_version="0.1.0",
    )

    assert forward == backward


# ---------------------------------------------------------------------------
# The one guarantee no in-process test can make: PYTHONHASHSEED is read once
# at interpreter startup, so observing its effect on set iteration order
# requires spawning a fresh interpreter per seed.
# ---------------------------------------------------------------------------

_HASH_SEED_SCRIPT = """
from pathlib import PurePosixPath

from envdoc.models import Confidence, Report, Status, Variable
from envdoc.render import render_json

variables = (
    Variable(
        name="DATABASE_URL",
        required=True,
        confidence=Confidence.EXACT,
        status=Status.UNSET_IN_DEPLOYMENT,
        statuses=frozenset(
            {Status.ORPHAN_DEPLOYMENT, Status.UNDOCUMENTED, Status.UNSET_IN_DEPLOYMENT}
        ),
        defaults=(),
        occurrences=(),
        documented_in_example=False,
        deployment_targets=(),
    ),
    Variable(
        name="PORT",
        required=False,
        confidence=Confidence.EXACT,
        status=Status.STALE,
        statuses=frozenset({Status.STALE, Status.OK}),
        defaults=("8000",),
        occurrences=(),
        documented_in_example=True,
        deployment_targets=("docker-compose.yml",),
    ),
)
report = Report(
    root=PurePosixPath("."),
    variables=variables,
    dynamic=(),
    warnings=(),
    files_scanned=2,
    deployment_files_found=("docker-compose.yml",),
)
print(render_json(report, tool_version="0.1.0"), end="")
"""


def _render_json_under_hash_seed(seed: str) -> str:
    """Render the fixture report in a fresh interpreter pinned to `seed`.

    `sys.executable` is used directly rather than `uv run python`: this test
    runs inside the same virtualenv pytest is already running in, where
    envdoc is installed editable, so no re-resolution is needed and the extra
    process is cheaper.
    """
    result = subprocess.run(
        [sys.executable, "-c", _HASH_SEED_SCRIPT],
        env={**os.environ, "PYTHONHASHSEED": seed},
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def test_json_output_is_identical_across_different_hash_seeds() -> None:
    """The regression test for the bug render._sorted_statuses exists to
    prevent. Five seeds rather than two: the raw (buggy) frozenset iteration
    order groups into a handful of buckets rather than being fully random per
    seed, so two seeds can coincidentally land in the same bucket and a
    two-seed version of this test would pass on a genuine regression."""
    seeds = ("0", "1", "2", "3", "42")
    outputs = {seed: _render_json_under_hash_seed(seed) for seed in seeds}

    assert len(set(outputs.values())) == 1, outputs
