# countbeans

A Telegram bot for tracking and splitting shared expenses within Telegram
groups — Splitwise-style, but Telegram-native. Users interact via commands like
`/addexpense`, `/balance`, `/settleup`, and `/statements` directly in group chats.

## Development setup

```bash
# Install dependencies (including dev tooling)
uv sync

# Install the git pre-commit hook (once per clone) — runs pyright before each
# commit. The .pre-commit-config.yaml is version-controlled, but the installed
# hook in .git/hooks is not, so each clone must run this.
uv run pre-commit install
```

After that, `pyright` runs automatically on every commit touching Python files
and blocks the commit on a type error. To run it on demand:

```bash
uv run pre-commit run --all-files   # all hooks over the whole tree
uv run pyright                       # pyright directly
```

## Tests

```bash
# Unit tests on the host (integration tests skip without TEST_DATABASE_URL)
uv run pytest tests/unit

# Integration tests in Docker (spins up an ephemeral Postgres)
docker compose --profile test run --rm test
```

## Running the bot

```bash
# Development — auto-reloads on code changes
docker compose -f compose.yml -f compose.dev.yml up --build

# Production-like — code baked into the image
docker compose up --build
```

See [CLAUDE.md](CLAUDE.md) for the architecture, database-migration workflow, and
configuration, and [docs/spec.md](docs/spec.md) for the full product spec (design
principles, command grammar, schema, and algorithms).
