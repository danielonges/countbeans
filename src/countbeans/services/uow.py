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

    def _attach(self, session: AsyncSession) -> None:
        """Wire the repositories onto a session. Split out so a test harness can
        bind a UoW to a pre-opened (rolled-back) session without duplicating the
        repository list."""
        self.settlements = SettlementRepository(session)
        self.expenses = ExpenseRepository(session)
        self.balances = BalanceRepository(session)
        self.ledger = StatementRepository(session)
        self.users = UserRepository(session)
        self.groups = GroupRepository(session)
        self.group_members = GroupMemberRepository(session)

    async def __aenter__(self) -> "UnitOfWork":
        self._session = self._session_factory()
        await self._session.__aenter__()
        self._attach(self._session)
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        assert self._session is not None
        if exc_type is None:
            await self._session.commit()
        else:
            await self._session.rollback()
        await self._session.__aexit__(exc_type, exc, tb)
        self._session = None
