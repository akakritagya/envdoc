# Demo

Every transcript below is real output from running `envdoc` against a throwaway
repository — nothing here is invented. Reproduce any of it yourself with the commands
shown; see [`README.md`](README.md) for installation.

## The flagship case

A required variable, used in code, documented in `.env.example` — and never set by the
deployment manifest. This works on a laptop and dies the moment it's containerized, and
it's invisible to any tool that only compares code against `.env.example`.

```python
# app.py
import os

DATABASE_URL = os.environ["DATABASE_URL"]
PORT = os.getenv("PORT", "8000")
```

```dotenv
# .env.example
DATABASE_URL=
PORT=8000
```

```yaml
# docker-compose.yml
services:
  web:
    image: myapp:latest
    environment:
      - PORT=8000
```

```
$ envdoc check .
                                       envdoc report for .
┏━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ Variable     ┃ Status              ┃ Required ┃ Occurrences                                    ┃
┡━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ DATABASE_URL │ unset_in_deployment │ yes      │ .env.example:1; app.py:3                      │
│ PORT         │ ok                  │ no       │ .env.example:2; app.py:4; docker-compose.yml:5 │
└──────────────┴─────────────────────┴──────────┴────────────────────────────────────────────────┘
$ echo $?
1
```

`PORT` has a fallback and a value from `docker-compose.yml`, so it's `ok`.
`DATABASE_URL` has no fallback and no line in `docker-compose.yml`'s `environment:` — the
process reads it on boot and there is nothing there to give it a value.

## Fixing it: `sync`

`sync` never touches an `unset_in_deployment` finding — that's a deployment problem, not
a documentation one — but it will append a variable the code reads that
`.env.example` never mentions at all.

```python
# app.py
import os

STRIPE_KEY = os.environ["STRIPE_KEY"]
```

```
$ envdoc sync .
+ STRIPE_KEY

$ cat .env.example
# Added by envdoc
STRIPE_KEY=
```

## Adopting the gate on an existing repo: `baseline`

`check` failing the first time it's ever run on a real repository is why most audit tools
never get past the "let's try this" stage. `baseline` snapshots today's drift so `check`
can be turned on immediately, without fixing sixty variables in the same PR — new drift
still fails.

```python
# app.py
import os

DATABASE_URL = os.environ["DATABASE_URL"]
REDIS_URL = os.environ["REDIS_URL"]
```

```
$ envdoc check .
                  envdoc report for .
┏━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━━┓
┃ Variable     ┃ Status       ┃ Required ┃ Occurrences ┃
┡━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━━┩
│ DATABASE_URL │ undocumented │ yes      │ app.py:3    │
│ REDIS_URL    │ undocumented │ yes      │ app.py:4    │
└──────────────┴──────────────┴──────────┴─────────────┘
$ echo $?
1

$ envdoc baseline .
+ DATABASE_URL: undocumented
+ REDIS_URL: undocumented

$ envdoc check . --baseline .envdoc-baseline.json
               envdoc report for .
┏━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━━┓
┃ Variable     ┃ Status ┃ Required ┃ Occurrences ┃
┡━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━━┩
│ DATABASE_URL │ ok     │ yes      │ app.py:3    │
│ REDIS_URL    │ ok     │ yes      │ app.py:4    │
└──────────────┴────────┴──────────┴─────────────┘
2 findings suppressed by .envdoc-baseline.json
$ echo $?
0
```

The baseline is keyed by `(name, status)`, not `file:line` — moving `app.py` or
reformatting it doesn't invalidate the entries the way a line-keyed baseline would.

## Schema-first config: what a regex scanner can't see

```python
# config.py
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="APP_")

    database_url: str
    api_key: str = Field(alias="STRIPE_API_KEY")
```

```
$ envdoc scan .
                    envdoc report for .
┏━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━━┓
┃ Variable         ┃ Status       ┃ Required ┃ Occurrences ┃
┡━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━━┩
│ APP_DATABASE_URL │ undocumented │ yes      │ config.py:8 │
│ STRIPE_API_KEY   │ undocumented │ yes      │ config.py:9 │
└──────────────────┴──────────────┴──────────┴─────────────┘
```

A regex scanner sees the field names `database_url` and `api_key` and stops there — both
wrong. envdoc computes the actual environment-variable name: `env_prefix` plus the
uppercased field name by default, or the literal `alias` when one overrides it entirely.

## JavaScript and TypeScript

```typescript
// config.ts
const apiKey = process.env.API_KEY;
const port = process.env.PORT || "8000";
const { DATABASE_URL } = process.env;
```

```
$ envdoc scan .
                  envdoc report for .
┏━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━━┓
┃ Variable     ┃ Status       ┃ Required ┃ Occurrences ┃
┡━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━━┩
│ API_KEY      │ undocumented │ yes      │ config.ts:1 │
│ DATABASE_URL │ undocumented │ yes      │ config.ts:3 │
│ PORT         │ undocumented │ no       │ config.ts:2 │
└──────────────┴──────────────┴──────────┴─────────────┘
```

Direct reads, destructuring, and a `||` fallback are all resolved the same way as their
Python equivalents — `PORT` is optional with default `"8000"`, the other two are required.

## See also

[`README.md`](README.md) covers installation (standalone CLI, pre-commit hook, GitHub
Action — no PyPI release), the full command/flag reference, exit codes, the complete
status table, and `[tool.envdoc]` configuration.
