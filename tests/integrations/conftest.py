import os
from collections.abc import AsyncGenerator, Generator

import pytest

os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, AsyncEngine, create_async_engine, async_sessionmaker
from testcontainers.postgres import PostgresContainer

from countbeans.db._base import Base


@pytest.fixture(scope="session")
def pg() -> Generator[PostgresContainer, None, None]:
    with PostgresContainer("postgres:16-alpine") as container:
        sync_url = container.get_connection_url()
        engine = create_engine(sync_url)
        Base.metadata.create_all(engine)
        engine.dispose()
        yield container


@pytest.fixture(scope="session")
def async_engine(pg: PostgresContainer) -> Generator[AsyncEngine, None, None]:
    sync_url = pg.get_connection_url()
    async_url = sync_url.replace("postgresql+psycopg2://", "postgresql+asyncpg://")
    engine = create_async_engine(async_url)
    yield engine


@pytest.fixture
async def session(async_engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    factory = async_sessionmaker(async_engine, expire_on_commit=False)
    async with factory() as s:
        yield s
        await s.rollback()
