## What & why

<!-- What does this change, and why does it need to happen? Link an issue if there is one. -->

## Checklist

- [ ] `uv run pytest` passes
- [ ] `uv run ruff check .` and `uv run ruff format --check .` pass
- [ ] `uv run mypy` passes
- [ ] Commit messages follow [Conventional Commits](../CONTRIBUTING.md#conventions) (lowercase imperative subject, no scope)
- [ ] New/changed behavior has test coverage
- [ ] `sources/` and `audit.py` changes keep the hard rules from [CLAUDE.md](../CLAUDE.md) (no `print`, no I/O in `audit.py`, no `cli` imports below the CLI layer)
