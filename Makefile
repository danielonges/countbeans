# Dev convenience targets. The one rule that matters: migrations are a separate,
# explicit step applied BEFORE the bot starts — the bot never auto-migrates (see
# CLAUDE.md "Database migrations"). `make dev` enforces that ordering so a fresh
# pull with a new migration doesn't start the bot against a stale schema (the
# `column ... does not exist` failure).
.PHONY: migrate dev up down test test-down

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
