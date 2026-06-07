import os
from collections.abc import AsyncGenerator

import pytest
from aiogram import Dispatcher
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from countbeans.db._base import Base

from ._bot_harness import build_dispatcher

# Async DSN for the test database, injected by the Compose `test` service
# (points at the ephemeral `test-db`). When unset, integration tests skip — run
# them with `docker compose --profile test run --rm test`.
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    """A rolled-back AsyncSession against the test database.

    Function-scoped and fully async so the engine, schema setup, and session all
    live on one event loop (asyncpg pools are loop-bound) — no Testcontainers,
    no sync driver. `create_all` is idempotent: it builds the schema on the first
    test and no-ops thereafter. Each test rolls back, so rows never leak.
    """
    if not TEST_DATABASE_URL:
        pytest.skip(
            "integration tests need Postgres — run "
            "`docker compose --profile test run --rm test` (or set TEST_DATABASE_URL)"
        )
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
        await s.rollback()
    await engine.dispose()


@pytest.fixture(scope="session")
def dispatcher() -> Dispatcher:
    """One Dispatcher with every handler router, built once per test session.

    A router can attach to only one Dispatcher per process, so all handler tests
    share this single instance (the per-test `uow` is passed in via `feed`).
    """
    from countbeans.bot.handlers import (
        addexpense,
        addexpense_wizard,
        balance,
        currency,
        event,
        group,
        help,
        join,
        membership,
        settleup,
        simplify,
        start,
        statements,
        void,
    )

    return build_dispatcher(
        start.router,
        join.router,
        help.router,
        settleup.router,
        addexpense.router,
        addexpense_wizard.router,
        void.router,
        balance.router,
        simplify.router,
        currency.router,
        statements.router,
        event.router,
        group.router,
        membership.router,
    )


@pytest.fixture(autouse=True)
def _reset_fsm(dispatcher: Dispatcher):
    """Clear FSM state after each test. The dispatcher (and its MemoryStorage) is
    session-scoped and shared, so a wizard left mid-flow by one test must not leak
    its `(chat, user)` state into the next."""
    yield
    storage = getattr(dispatcher.storage, "storage", None)
    if storage is not None:
        storage.clear()
