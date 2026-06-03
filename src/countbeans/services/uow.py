"""Caller-managed Unit of Work."""
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .repositories import (
    BalanceRepository,
    ExpenseRepository,
    GroupMemberRepository,
    GroupRepository,
    SettlementRepository,
    StatementRepository,
    UserRepository,
)


class UnitOfWork:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._session: AsyncSession | None = None

    async def __aenter__(self) -> "UnitOfWork":
        self._session = self._session_factory()
        await self._session.__aenter__()
        self.settlements = SettlementRepository(self._session)
        self.expenses = ExpenseRepository(self._session)
        self.balances = BalanceRepository(self._session)
        self.ledger = StatementRepository(self._session)
        self.users = UserRepository(self._session)
        self.groups = GroupRepository(self._session)
        self.group_members = GroupMemberRepository(self._session)
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        assert self._session is not None
        if exc_type is None:
            await self._session.commit()
        else:
            await self._session.rollback()
        await self._session.__aexit__(exc_type, exc, tb)
        self._session = None
