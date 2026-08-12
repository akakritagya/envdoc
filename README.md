# envdoc

Audit a repository's environment-variable usage against its `.env.example`.

> **Scaffold in progress.** The full README — usage, exit codes, prior art, and
> the case for reaching for `pydantic-settings` instead — lands in the final PR.

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
