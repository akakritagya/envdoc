# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`envdoc` audits environment-variable configuration across a repository. Unlike the crowded
category of two-way scanners (*used in code* vs *listed in `.env.example`*), it audits a
**third** axis: what the deployment actually provides — `docker-compose.yml`, `Dockerfile`,
GitHub Actions, `fly.toml`, k8s manifests.

That third axis is the entire reason the project exists. It catches a variable the code
requires, that `.env.example` documents, and that the compose file never sets: works
locally, dies in the container. Secondary differentiator: reading schema-first config
(`pydantic-settings`, `environs`, `django-environ`), which regex scanners are blind to.

**Not going to PyPI.** The name is taken by `Yanflare/envdoc`, a near-identical two-way
tool that must be credited first in the README's prior-art section.

## Commands

```bash
uv sync --group dev                       # install
uv run pytest                             # all tests (-q, strict markers/config)
uv run pytest tests/test_aggregate.py     # one file
uv run pytest -k conflicting_defaults     # one test by substring
uv run pytest --cov                       # coverage (branch, source=envdoc)
uv run ruff check . && uv run ruff format --check .
uv run mypy                               # strict; files come from pyproject
uv lock --check                           # CI runs UV_FROZEN=1
```

Hooks are installed with three separate invocations — `pre-commit install`, then
`--hook-type commit-msg` (conventional commits), then `--hook-type pre-push` (pytest).
`no-commit-to-branch` blocks committing to `main`.

## Working agreement — binding

Work proceeds **one task group at a time**, in the order set by the plan at
`~/.claude/plans/groovy-rolling-hoare.md`. After each group:

1. Run that group's gate.
2. **Stop and report before starting the next.**
3. Open a PR, squash-merge, then continue.

Do not implement later groups speculatively. If a group turns out larger than estimated,
**say so and propose a cut** — never silently absorb it. Pre-authorised cuts (drop and
report, don't absorb): G10 baseline, G13 Go/Rust, G15's fly.toml + k8s half, G17
hook/Action.

## Architecture

```text
cli.py         argv, config resolution, exit codes, printing   <- ONLY layer with a terminal
   |
render/        Report -> table / markdown / json
   |
audit.py       three-way set algebra -> Report                 <- pure, no I/O
   |
aggregate.py   Finding[] -> Variable[]                         <- where severity is decided
   |
sources/       code extractors | example parser | deployment parsers
   |
discovery.py   file walking + ignore engine
```

**Hard rules.** Nothing under `sources/` or `audit.py` imports `cli`. No `print` below
`cli.py` — warnings accumulate on `Report.warnings` and the CLI decides whether `--quiet`
suppresses them. `typer.Exit` appears **exactly once**, in `cli.py`. `audit.py` performs no
I/O.

These exist so the core can be tested with plain assertions. Only a handful of tests should
need `CliRunner`; stdout-scraping tests break on column widths and say nothing when they
fail.

**Exit codes:** `0` clean, `1` drift at or above the threshold, `2` envdoc itself failed.
The 1-vs-2 split is mandatory — CI must distinguish "your config is out of sync" from "the
linter is broken". `scan` never returns 1; gating is `check`'s job alone.

## Data model

The load-bearing distinction is `Occurrence` (one reference at one location) vs `Variable`
(one name, aggregated across everywhere it appears). Aggregation is where severity is
decided, and it enables findings no per-line scanner can produce — notably **conflicting
defaults**, where two files disagree about a variable's fallback.

- `required` resolves to the **maximum**, never the average: one call site without a
  fallback makes the whole variable required.
- `required` and `default` are **code-only**. Non-code occurrences carry
  `required=False, default=None` and aggregation *ignores* rather than merely tolerates
  them.
- `aggregate()` leaves `status`/`statuses` as placeholders; `audit.py` fills them in, since
  status needs the example file and manifests.
- A `DynamicRef` (`os.getenv(key_var)`) has no name, never becomes a `Variable`, and never
  counts toward drift by default. **Never fabricate a name for one.**

## Determinism is a contract

Byte-identical output for the same repo on any machine. Without it, every CI diff is noise
and `sync` produces spurious commits.

- Paths are `PurePosixPath` **relative to root**. Absolute paths and Windows separators are
  forbidden in output — they leak machine identity and break cross-platform golden tests.
- Discovery yields **sorted** paths. `rglob` order is filesystem-dependent.
- Sets never reach rendering; sort into tuples first.
- No timestamp unless `--include-timestamp`.

## Deliberate deviations — do not "fix" these back

The build spec is wrong in several places. These were changed on purpose, with reasons in
the relevant commit messages:

| Spec says | Why it was changed |
| --- | --- |
| `Confidence.DYNAMIC` | Unreachable — `DynamicRef` has no `confidence` field |
| `order=True` on `Occurrence` | Falls through to comparing `default: str \| None`; `None < "8000"` raises `TypeError` |
| "error if required, else warning" | No severity type or `FailOn` enum was ever defined. `FailOn` is now a cumulative **set of statuses**; the required/optional split lives inside `has_drift` |
| `envdoc` on PyPI at G18 | Name taken by a near-identical tool; not publishing |
| Empty `__init__.py` (G0 commit) | Becomes a curated API at G8 — but as a design choice, **not** a semver promise, since nothing is published |

Also: `pyyaml` is required by the deployment parsers but is unlisted in the spec's layout.

## Conventions

Inherited from the sibling project `nepkit` (`../week-01/nepkit`), whose config this
repo's was derived from. Ruff's `ANN` applies to tests, so most of this is enforced.

**Tests.** Every function annotated `-> None`. Long full-sentence names
(`test_one_call_site_without_a_default_makes_the_whole_variable_required`). A module
docstring on each file saying what contract it pins. `@pytest.mark.parametrize` **always**
with `ids=`. `pytest.raises(X, match=r"...")` — never bare. Model objects are built with
the factories in `tests/helpers.py` so a test states only what it means.

**Git.** Branches `<type>/<kebab-slug>`. Conventional commits, lowercase imperative
subject, no scope, no trailing period. **Substantive multi-paragraph bodies wrapped at ~79
columns explaining the *why*** — this repo's history is part of the deliverable. Squash-merge;
subject gains a `(#NN)` suffix. Every commit ends with
`Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.

**Config.** Exact pins for tools that *grade* code (`ruff`, `mypy`); ranges for tools that
*execute* (`pytest`). CI matrix covers every version `requires-python` claims.

## Self-hosting

CI will eventually run `envdoc check .` on this repo. The test fixtures under
`tests/fixtures/` are deliberately full of undocumented variables, so this only works via
`[tool.envdoc] exclude = ["tests/fixtures/*"]` in `pyproject.toml` — which dogfoods the
ignore engine while it's at it.
