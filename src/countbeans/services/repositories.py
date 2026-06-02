"""Repository classes — the only objects that hold SQLAlchemy models."""
import uuid
from collections import defaultdict

import uuid_utils
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from countbeans.db.models import Expense, ExpenseShare, Group, GroupMember, Settlement, User
from countbeans.dto.results import SettlementCreatedResult


class SettlementRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, settlement: Settlement) -> None:
        self._session.add(settlement)
        await self._session.flush()

    async def get(self, settlement_id: uuid.UUID) -> Settlement | None:
        result = await self._session.execute(
            select(Settlement).where(Settlement.id == settlement_id)
        )
        return result.scalar_one_or_none()

    def _to_dto(self, row: Settlement) -> SettlementCreatedResult:
        return SettlementCreatedResult(
            settlement_id=row.id,
            from_user_id=row.from_user_id,
            to_user_id=row.to_user_id,
            amount_cents=row.amount_cents,
            currency=row.currency,
            event_id=None,
        )


class ExpenseRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, expense: Expense, shares: dict[uuid.UUID, int]) -> None:
        self._session.add(expense)
        await self._session.flush()
        self._session.add_all([
            ExpenseShare(expense_id=expense.id, user_id=uid, share_cents=cents)
            for uid, cents in shares.items()
        ])
        await self._session.flush()


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

    async def get_or_create_placeholder(self, username: str) -> User:
        result = await self._session.execute(
            select(User).where(User.username == username, User.telegram_user_id.is_(None))
        )
        existing = result.scalar_one_or_none()
        if existing:
            return existing
        user = User(id=uuid_utils.uuid7(), username=username)
        self._session.add(user)
        await self._session.flush()
        return user


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

    async def set_simplify_debts(
        self, group_id: uuid.UUID, simplify_debts: bool
    ) -> None:
        await self._session.execute(
            update(Group)
            .where(Group.id == group_id)
            .values(simplify_debts=simplify_debts)
        )


class GroupMemberRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def ensure_member(self, group_id: uuid.UUID, user_id: uuid.UUID) -> None:
        result = await self._session.execute(
            select(GroupMember).where(
                GroupMember.group_id == group_id,
                GroupMember.user_id == user_id,
                GroupMember.left_at.is_(None),
            )
        )
        if result.scalar_one_or_none() is None:
            self._session.add(GroupMember(group_id=group_id, user_id=user_id))
            await self._session.flush()
