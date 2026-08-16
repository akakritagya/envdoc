# Contributing

## Setup

```bash
uv sync --group dev
uv run pre-commit install
uv run pre-commit install --hook-type commit-msg
uv run pre-commit install --hook-type pre-push
```

The three separate `pre-commit install` invocations matter: the default hook
type covers formatting/lint, `commit-msg` enforces conventional commits, and
`pre-push` runs the test suite. `no-commit-to-branch` blocks committing to
`main` directly, so all work happens on a branch.

## Before opening a PR

```bash
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv lock --check
```

These are the same checks CI runs. See the [Development section of the
README](README.md#development) for the full command reference, and
[CLAUDE.md](CLAUDE.md) for the project's architecture and hard rules
(layering, determinism, exit-code contract) that any change needs to respect.

## Conventions

- **Branches:** `<type>/<kebab-slug>`, e.g. `fix/baseline-key-collision`.
- **Commits:** [Conventional Commits](https://www.conventionalcommits.org/),
  lowercase imperative subject, no scope, no trailing period
  (`fix: resolve baseline key collision`, not `Fix: Resolve Baseline Key
  Collision.`). Substantive changes get a multi-paragraph body wrapped at ~79
  columns explaining *why*, not just what — this repo's history is treated as
  part of the deliverable.
- **Tests:** every test function is annotated `-> None`, named as a full
  sentence describing the contract it pins
  (`test_one_call_site_without_a_default_makes_the_whole_variable_required`),
  and uses `@pytest.mark.parametrize(..., ids=...)` and
  `pytest.raises(X, match=r"...")` rather than bare forms. Build model objects
  with the factories in `tests/helpers.py`.
- **PRs:** squash-merged; the squashed subject picks up a `(#NN)` suffix
  automatically.

## Reporting bugs and requesting features

Open an issue using the templates under **Issues → New issue**. For security
vulnerabilities, see [SECURITY.md](SECURITY.md) instead of filing a public
issue.
