# syntax=docker/dockerfile:1

# Shared base: uv + dependency manifests (cached until pyproject.toml/uv.lock change).
FROM python:3.14-slim AS base
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./

# Test image: all deps incl. the dev group (pytest, ...). The project source and
# tests are bind-mounted at runtime (with PYTHONPATH=/app/src), so this layer
# only depends on the manifests and need not rebuild when code/tests change.
# Used by the profile-gated `test` service in compose.yml.
FROM base AS test
RUN uv sync --frozen --no-install-project
CMD ["uv", "run", "--no-sync", "pytest", "tests/integrations"]

# Production image — last stage, so a plain `build: .` (bot, migrate) targets it.
FROM base AS prod
RUN uv sync --frozen --no-dev --no-install-project
COPY src/ ./src/
RUN uv sync --frozen --no-dev
# Ship migrations so the container can run `alembic upgrade head` on deploy
COPY alembic.ini ./
COPY alembic/ ./alembic/

# Run as an unprivileged user — the bot is a long-lived network client and has no
# reason to run as root. `uv run` keeps its existing behavior (so the compose
# `migrate` and dev `watchfiles` overrides still work); we only relocate uv's
# cache to a writable path and hand /app to the new owner so any runtime sync
# can still write the venv.
ENV UV_CACHE_DIR=/tmp/uv-cache
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser
# `--frozen --no-dev` keeps the prod runtime lean and offline: the image is
# already fully synced at build time, so this is a no-op sync (no network at
# boot) and the dev toolchain (pyright/pytest/...) never lands in the production
# container. The compose `migrate` and dev `watchfiles` services override this
# command, so their own sync behavior is unaffected.
CMD ["uv", "run", "--frozen", "--no-dev", "countbeans"]
