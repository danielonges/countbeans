"""Repository classes — the only objects that hold SQLAlchemy models."""
import uuid
from collections import defaultdict

import uuid_utils.compat as uuid_utils  # .compat yields stdlib uuid.UUID instances
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from countbeans.db.models import Expense, ExpenseShare, Group, GroupMember, Settlement, User
from countbeans.dto.domain import ActivitySummary, BalanceKey, BalanceMap, MemberInfo
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

    async def activity_summary(self, group_id: uuid.UUID) -> list[ActivitySummary]:
        """Active (non-voided) expense count and total per currency for a group."""
        rows = await self._session.execute(
            select(Expense.currency, func.count(), func.sum(Expense.amount_cents))
            .where(Expense.group_id == group_id, Expense.voided_at.is_(None))
            .group_by(Expense.currency)
        )
        return [
            ActivitySummary(currency=cur, expense_count=count, total_cents=total)
            for cur, count, total in rows
        ]


class BalanceRepository:
    """Derives balances from the immutable ledger via SQL aggregation."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def compute_for_group(self, group_id: uuid.UUID) -> BalanceMap:
        """Return {BalanceKey(user_id, currency): net_cents}. Zero balances are omitted."""
        result: BalanceMap = defaultdict(int)

        # 1. Payer sums — money fronted
        rows = await self._session.execute(
            select(Expense.payer_id, Expense.currency, func.sum(Expense.amount_cents))
            .where(Expense.group_id == group_id, Expense.voided_at.is_(None))
            .group_by(Expense.payer_id, Expense.currency)
        )
        for payer_id, currency, total in rows:
            result[BalanceKey(payer_id, currency)] += total

        # 2. Share sums — money consumed
        rows = await self._session.execute(
            select(ExpenseShare.user_id, Expense.currency, func.sum(ExpenseShare.share_cents))
            .join(Expense, Expense.id == ExpenseShare.expense_id)
            .where(Expense.group_id == group_id, Expense.voided_at.is_(None))
            .group_by(ExpenseShare.user_id, Expense.currency)
        )
        for user_id, currency, total in rows:
            result[BalanceKey(user_id, currency)] -= total

        # 3. Settlements sent — debtor reduces their balance
        rows = await self._session.execute(
            select(Settlement.from_user_id, Settlement.currency, func.sum(Settlement.amount_cents))
            .where(Settlement.group_id == group_id)
            .group_by(Settlement.from_user_id, Settlement.currency)
        )
        for user_id, currency, total in rows:
            result[BalanceKey(user_id, currency)] += total

        # 4. Settlements received — creditor balance is reduced
        rows = await self._session.execute(
            select(Settlement.to_user_id, Settlement.currency, func.sum(Settlement.amount_cents))
            .where(Settlement.group_id == group_id)
            .group_by(Settlement.to_user_id, Settlement.currency)
        )
        for user_id, currency, total in rows:
            result[BalanceKey(user_id, currency)] -= total

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

    async def _by_telegram_id(self, telegram_user_id: int) -> User | None:
        result = await self._session.execute(
            select(User).where(User.telegram_user_id == telegram_user_id)
        )
        return result.scalar_one_or_none()

    async def _pending_placeholder(self, username: str) -> User | None:
        """The pending placeholder for a username (telegram_user_id IS NULL), if
        any. App invariant: at most one per username."""
        result = await self._session.execute(
            select(User).where(
                User.username == username,
                User.telegram_user_id.is_(None),
            )
        )
        return result.scalar_one_or_none()

    async def upsert(
        self,
        telegram_user_id: int,
        username: str | None,
        first_name: str | None,
        last_name: str | None,
    ) -> User:
        """Onboard or refresh the interacting user, claiming a placeholder if one
        is waiting.

        The first interaction of a previously-mentioned @handle is the *claim*: a
        single-row UPDATE sets telegram_user_id on the existing placeholder, so
        every expense_share / settlement already bound to that surrogate
        users.id follows automatically — no fan-out rewrite (CLAUDE.md
        "Onboarding & membership").
        """
        # Already a claimed identity: refresh the display alias and names.
        existing = await self._by_telegram_id(telegram_user_id)
        if existing is not None:
            existing.username = username
            existing.first_name = first_name
            existing.last_name = last_name
            await self._session.flush()
            return existing

        # First interaction: claim a matching pending placeholder if present.
        if username is not None:
            placeholder = await self._pending_placeholder(username)
            if placeholder is not None:
                placeholder.telegram_user_id = telegram_user_id
                placeholder.first_name = first_name
                placeholder.last_name = last_name
                await self._session.flush()
                return placeholder

        # Genuinely new user: insert a fresh claimed row.
        user = User(
            id=uuid_utils.uuid7(),
            telegram_user_id=telegram_user_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
        )
        self._session.add(user)
        await self._session.flush()
        return user

    async def resolve_mention(self, username: str) -> User:
        """Resolve an @mention to a user row. Prefer someone we've already seen
        (claimed: telegram_user_id IS NOT NULL) over a pending placeholder, and
        create a new placeholder only when the handle is entirely unknown.

        Matching is by username — a best-effort display-alias hint, never
        identity (CLAUDE.md) — which is why a claimed row wins when a
        rename/reuse has left both a claimed user and a placeholder under one
        handle.
        """
        result = await self._session.execute(
            select(User)
            .where(User.username == username)
            .order_by(User.telegram_user_id.is_(None))  # claimed (False) sorts first
        )
        existing = result.scalars().first()
        if existing is not None:
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

    async def list_members(self, group_id: uuid.UUID) -> list[MemberInfo]:
        """Active (left_at IS NULL) members with their user info."""
        rows = await self._session.execute(
            select(User)
            .join(GroupMember, GroupMember.user_id == User.id)
            .where(GroupMember.group_id == group_id, GroupMember.left_at.is_(None))
            .order_by(User.username)
        )
        return [
            MemberInfo(
                user_id=u.id,
                username=u.username,
                first_name=u.first_name,
                is_pending=u.telegram_user_id is None,
            )
            for u in rows.scalars()
        ]

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
