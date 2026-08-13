# envdoc

[![CI](https://github.com/akakritagya/envdoc/actions/workflows/ci.yml/badge.svg)](https://github.com/akakritagya/envdoc/actions/workflows/ci.yml)

Audit a repository's environment-variable configuration — not just against
`.env.example`, but against what the deployment actually provides.

See [`DEMO.md`](DEMO.md) for a quick tour of each command, plus a worked case study of a
real repository adopting envdoc and catching a config bug before it reaches production.

## What this is

Every other environment-variable scanner is two-way: *used in code* vs *listed in
`.env.example`*. That misses the failure that actually ships to production — a variable
the code requires, that `.env.example` documents, and that the deployment manifest never
sets. Works on a laptop, dies in the container. envdoc audits a **third** axis:
`docker-compose.yml`, `Dockerfile`, GitHub Actions workflows, and `fly.toml`, so it can
catch that case directly instead of hoping someone notices in production.

Second differentiator: envdoc reads **schema-first config**, not just call sites. A
`class Settings(BaseSettings)` field with an `env_prefix` and a per-field `alias` resolves
to the correct environment-variable name — something no regex or call-matching scanner can
do, because the name isn't written anywhere near the field that reads it.

## Prior art

`envdoc` is already taken on PyPI, by a near-identical tool — credited first, since it's
the closest existing work and shares the name:

| Project | What it does | Where |
|---|---|---|
| [`Yanflare/envdoc`](https://pypi.org/project/envdoc/) | Scans Python for env-var usage, generates `.env.example` + a Markdown reference | PyPI |
| [`spotenv`](https://www.npmjs.com/package/spotenv) | `.env.example` drift checking for Node projects | npm |
| [`envscan`](https://pypi.org/project/envscan/) | Static analysis for env-var usage | PyPI |
| `envsniff` | `.env` file linting | — |
| `dotenv-linter` | Lints `.env`-format files themselves | — |
| `evnx` | Environment-variable extraction | — |

This project is **not published to PyPI** — the name collision is a non-issue for an
unpublished tool, but it does mean installation below is git-based rather than
`pip install envdoc`.

## Installation

No package index required — every path below installs directly from a git ref.

**As a standalone CLI:**

```bash
uv tool install git+https://github.com/akakritagya/envdoc
# or: pipx install git+https://github.com/akakritagya/envdoc
```

**As a pre-commit hook**, in your own `.pre-commit-config.yaml`:

```yaml
repos:
  - repo: https://github.com/akakritagya/envdoc
    rev: main  # pin a commit or tag in real use
    hooks:
      - id: envdoc-check
```

**As a GitHub Action**, in your own workflow:

```yaml
- uses: akakritagya/envdoc@main # pin a commit or tag in real use
  with:
    command: check      # scan | check | sync | baseline -- defaults to check
    path: .              # defaults to .
    args: --fail-on any  # optional, passed through verbatim
```

## Usage

```bash
envdoc scan [PATH]      # print a report; never fails on drift
envdoc check [PATH]     # print a report; exit 1 if drift meets --fail-on
envdoc sync [PATH]      # append code-read variables missing from .env.example
envdoc baseline [PATH]  # snapshot today's drift so `check --baseline` can adopt it
```

`PATH` defaults to the current directory for every command. `scan` and `check` share the
same three-way audit and the same output; `check` is the one CI should gate on. `sync` and
`baseline` never return exit code 1 — writing a fix or a snapshot isn't itself a policy
violation, and gating is `check`'s job alone.

Common flags:

| Flag | Applies to | What it does |
|---|---|---|
| `--exclude PATTERN` | scan, check, sync, baseline | Glob to skip; repeatable |
| `--format table\|markdown\|json` | scan, check | Output format |
| `--fail-on undocumented\|unset\|stale\|any` | check | Drift threshold (default: `unset`) |
| `--baseline PATH` | check | Suppress drift already recorded in a baseline file |
| `--dry-run` | sync, baseline | Show what would change without writing it |
| `--quiet` | all | Suppress warnings on stderr |
| `--include-timestamp` | scan, check | Embed a generation time in `--format json` output |

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Clean — no drift at or above the threshold |
| `1` | Drift found — `check` only; `scan`/`sync`/`baseline` never return this |
| `2` | envdoc itself failed — bad path, bad config, a crash |

The 1-vs-2 split is deliberate: a CI job needs to tell "your config is out of sync" apart
from "the linter is broken," and collapsing both into one exit code throws that away at
the one place it matters most.

## Statuses

| In code | In `.env.example` | In deployment | Status |
|:---:|:---:|:---:|---|
| ✓ | ✓ | ✓ | `ok` |
| ✓ | ✓ | | `unset_in_deployment` |
| ✓ | | ✓ | `undocumented` |
| ✓ | | | `undocumented` + `unset_in_deployment` |
| | ✓ | ✓ | `stale` |
| | ✓ | | `stale` |
| | | ✓ | `orphan_deployment` |

`unset_in_deployment` only gates when the variable is `required` — one with a usable
fallback degrades to its default instead of taking the process down, which is worth
printing but not worth breaking a build over. A repository with no deployment manifest at
all reports no `unset_in_deployment` findings — that means "never containerised," not
"unset."

## Configuration

Optional `[tool.envdoc]` table in `pyproject.toml`, at the scanned root. A CLI flag always
overrides the config value for the same field, decided independently per field:

```toml
[tool.envdoc]
exclude = ["tests/fixtures/*", "vendor/*"]
fail_on = "any"              # undocumented | unset | stale | any -- default: unset
format = "json"               # table | markdown | json -- default: table
quiet = false
include_timestamp = false
baseline = ".envdoc-baseline.json"  # never auto-detected; opt-in only
```

## What gets read

| Source | Recognises |
|---|---|
| Python | `os.getenv`/`os.environ[...]`, all four alias forms; `pydantic-settings` `BaseSettings` classes with `env_prefix` and per-field aliases |
| JS / TS / JSX / TSX | `process.env.X`, `process.env["X"]`, destructuring, `\|\|`/`??` fallbacks |
| `.env.example` | What's documented |
| `docker-compose.yml` | `environment:` (both forms), `env_file:` (referenced, not resolved), `${VAR}` interpolation anywhere in the file |
| `Dockerfile` | `ENV` (both syntaxes), `ARG`, multi-stage builds |
| GitHub Actions | `env:` at workflow/job/step level, `secrets.*`/`vars.*` |
| `fly.toml` | `[env]` |

A name envdoc can't statically resolve — a dynamic key, a non-literal alias — is reported
as an unresolved reference rather than guessed at. envdoc would rather tell you it doesn't
know than tell you something that isn't true.

## Development

```bash
uv sync --group dev
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

```bash
uv run pre-commit install
uv run pre-commit install --hook-type commit-msg
uv run pre-commit install --hook-type pre-push
```

## License

[MIT](https://github.com/akakritagya/envdoc/blob/main/LICENSE).
