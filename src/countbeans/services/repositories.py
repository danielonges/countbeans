"""Repository classes for balance queries."""
import uuid
from collections import defaultdict

import uuid_utils
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from countbeans.db.models import Expense, ExpenseShare, Group, Settlement, User


class BalanceRepository:
    """Derives balances from the immutable ledger via SQL aggregation."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def compute_for_group(
        self, group_id: uuid.UUID
    ) -> dict[tuple[uuid.UUID, str], int]:
        """Return {(user_id, currency): net_cents}. Zero balances are omitted."""
        result: dict[tuple[uuid.UUID, str], int] = defaultdict(int)

        # 1. Payer sums — money fronted
        rows = await self._session.execute(
            select(Expense.payer_id, Expense.currency, func.sum(Expense.amount_cents))
            .where(Expense.group_id == group_id, Expense.voided_at.is_(None))
            .group_by(Expense.payer_id, Expense.currency)
        )
        for payer_id, currency, total in rows:
            result[(payer_id, currency)] += total

        # 2. Share sums — money consumed
        rows = await self._session.execute(
            select(ExpenseShare.user_id, Expense.currency, func.sum(ExpenseShare.share_cents))
            .join(Expense, Expense.id == ExpenseShare.expense_id)
            .where(Expense.group_id == group_id, Expense.voided_at.is_(None))
            .group_by(ExpenseShare.user_id, Expense.currency)
        )
        for user_id, currency, total in rows:
            result[(user_id, currency)] -= total

        # 3. Settlements sent — debtor reduces their balance
        rows = await self._session.execute(
            select(Settlement.from_user_id, Settlement.currency, func.sum(Settlement.amount_cents))
            .where(Settlement.group_id == group_id)
            .group_by(Settlement.from_user_id, Settlement.currency)
        )
        for user_id, currency, total in rows:
            result[(user_id, currency)] += total

        # 4. Settlements received — creditor balance is reduced
        rows = await self._session.execute(
            select(Settlement.to_user_id, Settlement.currency, func.sum(Settlement.amount_cents))
            .where(Settlement.group_id == group_id)
            .group_by(Settlement.to_user_id, Settlement.currency)
        )
        for user_id, currency, total in rows:
            result[(user_id, currency)] -= total

        return {k: v for k, v in result.items() if v != 0}

    async def get_usernames(self, user_ids: set[uuid.UUID]) -> dict[uuid.UUID, str | None]:
        if not user_ids:
            return {}
        rows = await self._session.execute(
            select(User.id, User.username).where(User.id.in_(user_ids))
        )
        return {row.id: row.username for row in rows}


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(
        self,
        telegram_user_id: int,
        username: str | None,
        first_name: str | None,
        last_name: str | None,
    ) -> User:
        stmt = (
            pg_insert(User)
            .values(
                id=uuid_utils.uuid7(),
                telegram_user_id=telegram_user_id,
                username=username,
                first_name=first_name,
                last_name=last_name,
            )
            .on_conflict_do_update(
                index_elements=["telegram_user_id"],
                set_={"username": username, "first_name": first_name, "last_name": last_name},
            )
            .returning(User)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one()


class GroupRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(self, telegram_chat_id: int, group_name: str | None) -> Group:
        stmt = (
            pg_insert(Group)
            .values(
                id=uuid_utils.uuid7(),
                telegram_chat_id=telegram_chat_id,
                group_name=group_name,
            )
            .on_conflict_do_update(
                index_elements=["telegram_chat_id"],
                set_={"group_name": group_name},
            )
            .returning(Group)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one()
