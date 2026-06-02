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
CMD ["uv", "run", "countbeans"]
