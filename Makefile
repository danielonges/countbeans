# Dev convenience targets. The one rule that matters: migrations are a separate,
# explicit step applied BEFORE the bot starts — the bot never auto-migrates (see
# CLAUDE.md "Database migrations"). `make dev` enforces that ordering so a fresh
# pull with a new migration doesn't start the bot against a stale schema (the
# `column ... does not exist` failure).
.PHONY: migrate dev up down test test-down audit

# Apply all pending migrations against the persistent db (starts db if needed).
migrate:
	docker compose run --rm migrate

# Migrate, then start the auto-reloading dev stack (code-change reloads via watchfiles).
dev: migrate
	docker compose -f compose.yml -f compose.dev.yml up --build

# Migrate, then start the production-like stack (code baked into the image).
up: migrate
	docker compose up --build

# Stop and remove containers (the db volume is preserved).
down:
	docker compose down

# Run the integration suite against an ephemeral test-db.
test:
	docker compose --profile test run --rm test

# Tear down the ephemeral test-db (tmpfs is wiped).
test-down:
	docker compose --profile test down

# Audit the locked dependency set for known vulnerabilities (CVEs). pip-audit is
# fetched on demand via uvx, so it isn't a project dependency; it checks the
# resolved lockfile (exported to requirements) against the PyPI advisory DB.
# Needs network. Wire this into CI to catch vulnerable deps before they ship.
#
# Triaged exceptions — RE-EVALUATE when aiogram relaxes its `aiohttp<3.14` pin
# (3.28.2, the latest, still blocks the patched aiohttp 3.14.0, so we cannot
# upgrade without an unsupported override). Both are in aiohttp's client cookie
# handling and are NOT reachable here — the bot runs no HTTP server and only
# calls the trusted Telegram API over TLS:
#   CVE-2026-34993 — RCE via CookieJar.load() on untrusted files; we never call it.
#   CVE-2026-47265 — per-request `cookies` leak across a cross-origin redirect;
#                    we set no per-request cookies and don't follow untrusted ones.
audit:
	uv export --frozen --no-emit-project --format requirements-txt \
		| uvx pip-audit --requirement /dev/stdin \
			--ignore-vuln CVE-2026-34993 \
			--ignore-vuln CVE-2026-47265
