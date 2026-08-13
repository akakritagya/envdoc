# Case study: adopting envdoc on an existing service

This walks through one continuous scenario, start to finish — a small backend called
"Aperture" adopting envdoc, then shipping a change that envdoc catches before it reaches
production. Every command and every table below is real output, captured by actually
running envdoc against a throwaway repository built to match this story, not written out
by hand. Reproduce any of it yourself; see [`README.md`](README.md) for installation.

## The service, before envdoc

Aperture is a small backend with the shape most real services have: config read through
a `pydantic-settings` class, deployed with `docker-compose.yml`. It's been running for a
while, nobody's audited its configuration surface, and — like most repositories that have
existed for more than a few weeks — it already has some drift nobody noticed.

```python
# service.py
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="APERTURE_")

    database_url: str
    redis_url: str
    port: int = 8000


settings = Settings()
```

```dotenv
# .env.example
APERTURE_DATABASE_URL=
APERTURE_PORT=8000
```

```yaml
# docker-compose.yml
services:
  web:
    image: aperture:latest
    environment:
      - APERTURE_DATABASE_URL=postgres://db/aperture
      - APERTURE_PORT=8000
```

Nothing here looks wrong at a glance. `redis_url` is a field like any other — the bug is
that it was added to the `Settings` class at some point and nobody updated
`.env.example` or `docker-compose.yml` to match. A regex scanner comparing "words that
look like env vars in the code" against "words in `.env.example`" might catch the
undocumented half of this. It has no way to know the deployment manifest is missing it
too.

## Day one: turning the gate on

The team runs `envdoc check` for the first time, expecting it to pass. It doesn't:

```
$ envdoc check .
                                        envdoc report for .
┏━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ Variable              ┃ Status                        ┃ Required ┃ Occurrences                   ┃
┡━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ APERTURE_DATABASE_URL │ ok                            │ yes      │ .env.example:1;               │
│                       │                               │          │ docker-compose.yml:5;         │
│                       │                               │          │ service.py:7                  │
│ APERTURE_PORT         │ ok                            │ no       │ .env.example:2;               │
│                       │                               │          │ docker-compose.yml:6;         │
│                       │                               │          │ service.py:9                  │
│ APERTURE_REDIS_URL    │ undocumented,                 │ yes      │ service.py:8                  │
│                       │ unset_in_deployment           │          │                               │
└───────────────────────┴───────────────────────────────┴──────────┴───────────────────────────────┘
$ echo $?
1
```

`APERTURE_REDIS_URL` is broken two ways at once: nothing documents it, and nothing
deploys it. This is the moment most teams give up on adding a config-audit tool to CI —
fixing every pre-existing finding just to unblock the next unrelated PR isn't a
reasonable ask. `baseline` exists for exactly this:

```
$ envdoc baseline .
+ APERTURE_REDIS_URL: undocumented
+ APERTURE_REDIS_URL: unset_in_deployment

$ envdoc check . --baseline .envdoc-baseline.json
                                       envdoc report for .
┏━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ Variable              ┃ Status ┃ Required ┃ Occurrences                                        ┃
┡━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ APERTURE_DATABASE_URL │ ok     │ yes      │ .env.example:1; docker-compose.yml:5; service.py:7 │
│ APERTURE_PORT         │ ok     │ no       │ .env.example:2; docker-compose.yml:6; service.py:9 │
│ APERTURE_REDIS_URL    │ ok     │ yes      │ service.py:8                                       │
└───────────────────────┴────────┴──────────┴────────────────────────────────────────────────────┘
2 findings suppressed by .envdoc-baseline.json
$ echo $?
0
```

`.envdoc-baseline.json` records exactly two facts — `(APERTURE_REDIS_URL,
undocumented)` and `(APERTURE_REDIS_URL, unset_in_deployment)` — keyed by name and
status, not by file and line number. `service.py` can be renamed or reformatted later
without silently reopening these two suppressions, the way a line-keyed baseline would.
The team wires `envdoc check . --baseline .envdoc-baseline.json` into CI (a pre-commit
hook or the GitHub Action — see the README) and moves on. `APERTURE_REDIS_URL` is still
broken, exactly as broken as it was five minutes ago — but it's now a known, tracked
debt instead of invisible, and the gate is live for anything new.

## Three weeks later: a new field lands

Aperture needs to verify Stripe webhooks now. Someone adds a field to `Settings`,
documents it, and opens a PR:

```python
# service.py
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="APERTURE_")

    database_url: str
    redis_url: str
    port: int = 8000
    stripe_webhook_secret: str = Field(alias="STRIPE_WEBHOOK_SECRET")


settings = Settings()
```

```dotenv
# .env.example
APERTURE_DATABASE_URL=
APERTURE_PORT=8000
STRIPE_WEBHOOK_SECRET=
```

Two things worth noticing already, before the deployment manifest even enters the
picture: the field is `alias`ed rather than prefixed, because Stripe's own docs specify
the exact env var name their tooling expects — `env_prefix="APERTURE_"` would otherwise
have computed `APERTURE_STRIPE_WEBHOOK_SECRET`, which is wrong. envdoc resolves the
literal alias, not the prefix computation, because that's what `pydantic-settings`
itself does. And the code, the field, and `.env.example` all agree — a two-way scanner
would call this PR clean. `docker-compose.yml` was never touched:

```
$ envdoc check . --baseline .envdoc-baseline.json
                                        envdoc report for .
┏━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ Variable              ┃ Status              ┃ Required ┃ Occurrences                             ┃
┡━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ APERTURE_DATABASE_URL │ ok                  │ yes      │ .env.example:1; docker-compose.yml:5;   │
│                       │                     │          │ service.py:8                            │
│ APERTURE_PORT         │ ok                  │ no       │ .env.example:2; docker-compose.yml:6;   │
│                       │                     │          │ service.py:10                           │
│ APERTURE_REDIS_URL    │ ok                  │ yes      │ service.py:9                            │
│ STRIPE_WEBHOOK_SECRET │ unset_in_deployment │ yes      │ .env.example:3; service.py:11           │
└───────────────────────┴─────────────────────┴──────────┴─────────────────────────────────────────┘
2 findings suppressed by .envdoc-baseline.json
$ echo $?
1
```

CI fails. `APERTURE_REDIS_URL` is still quietly suppressed by the baseline, exactly as
intended — this failure is entirely about the new field. This is the case envdoc exists
for: `STRIPE_WEBHOOK_SECRET` is required, it's documented, and the container that's
about to be deployed has no way to get a value for it. Every webhook verification would
fail in production, on the very first request, and nothing about the code review or the
`.env.example` diff would have shown it — the reviewer would have to separately, manually
cross-reference `docker-compose.yml` to catch this by eye.

## Fixing it

```yaml
# docker-compose.yml
services:
  web:
    image: aperture:latest
    environment:
      - APERTURE_DATABASE_URL=postgres://db/aperture
      - APERTURE_PORT=8000
      - STRIPE_WEBHOOK_SECRET=${STRIPE_WEBHOOK_SECRET}
```

```
$ envdoc check . --baseline .envdoc-baseline.json
                                        envdoc report for .
┏━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ Variable              ┃ Status ┃ Required ┃ Occurrences                                          ┃
┡━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ APERTURE_DATABASE_URL │ ok     │ yes      │ .env.example:1; docker-compose.yml:5; service.py:8   │
│ APERTURE_PORT         │ ok     │ no       │ .env.example:2; docker-compose.yml:6; service.py:10  │
│ APERTURE_REDIS_URL    │ ok     │ yes      │ service.py:9                                         │
│ STRIPE_WEBHOOK_SECRET │ ok     │ yes      │ .env.example:3; docker-compose.yml:7;                │
│                       │        │          │ docker-compose.yml:7; service.py:11                  │
└───────────────────────┴────────┴──────────┴──────────────────────────────────────────────────────┘
2 findings suppressed by .envdoc-baseline.json
$ echo $?
0
```

`docker-compose.yml:7` appears twice in `STRIPE_WEBHOOK_SECRET`'s occurrences, and that's
not a bug — it's the same line doing two different things envdoc tracks separately.
`STRIPE_WEBHOOK_SECRET=${STRIPE_WEBHOOK_SECRET}` both *sets* the container's
`STRIPE_WEBHOOK_SECRET` (the left side — deployment provides it) and *reads*
`STRIPE_WEBHOOK_SECRET` from whatever's running `docker compose up` (the `${...}` on the
right — the compose file itself requires it, the same relationship code has to a
variable it reads). Passing a secret through from the host's own environment is common
practice, and envdoc's three-way model has a place for both halves of it on the same
line without conflating them.

## What just happened

Two failures, two different shapes, both real:

- **Day one**: pre-existing drift, adopted with `baseline` in one command instead of
  blocking on a full cleanup.
- **Three weeks later**: a config the code needs, correctly documented, silently missing
  from the one place that actually determines whether the running container has it —
  caught in CI, before a deploy, not after one.

Neither would have been visible to a tool that only compares code against
`.env.example`. That comparison passed at every step here; the deployment manifest is
what was wrong, and it's the axis two-way tools don't look at.

## Beyond this example

This walkthrough used Python and `pydantic-settings` against `docker-compose.yml`.
envdoc reads the same three axes across `os.getenv`/`os.environ`, `process.env` in
JS/TS/JSX/TSX, `Dockerfile`, GitHub Actions workflows, and `fly.toml` — see
[`README.md`](README.md) for the full command reference, exit codes, the complete status
table, and `[tool.envdoc]` configuration.
