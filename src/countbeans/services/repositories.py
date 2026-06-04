"""Repository classes — the only objects that hold SQLAlchemy models."""
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import uuid_utils.compat as uuid_utils  # .compat yields stdlib uuid.UUID instances
from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from countbeans.db.models import Expense, ExpenseShare, Group, GroupMember, Settlement, User
from countbeans.dto.domain import ActivitySummary, BalanceKey, BalanceMap, MemberInfo
from countbeans.dto.results import SettlementCreatedResult


@dataclass(slots=True)
class RawStatementEntry:
    """A ledger row carrying user *ids*, before the service resolves usernames.

    Kept internal to the read path (not a DTO) so the public ``StatementEntry``
    never has to expose surrogate ids the bot would only discard."""

    kind: Literal["expense", "settlement"]
    created_at: datetime
    amount_cents: int
    currency: str
    description: str | None
    actor_id: uuid.UUID            # expense payer / settlement sender
    counterparty_id: uuid.UUID | None  # settlement recipient; None for expense
    participant_count: int | None
    voided: bool


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
            ActivitySummary(currency=cur, expense_count=int(count), total_cents=int(total))
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
            result[BalanceKey(payer_id, currency)] += int(total)

        # 2. Share sums — money consumed
        rows = await self._session.execute(
            select(ExpenseShare.user_id, Expense.currency, func.sum(ExpenseShare.share_cents))
            .join(Expense, Expense.id == ExpenseShare.expense_id)
            .where(Expense.group_id == group_id, Expense.voided_at.is_(None))
            .group_by(ExpenseShare.user_id, Expense.currency)
        )
        for user_id, currency, total in rows:
            result[BalanceKey(user_id, currency)] -= int(total)

        # 3. Settlements sent — debtor reduces their balance
        rows = await self._session.execute(
            select(Settlement.from_user_id, Settlement.currency, func.sum(Settlement.amount_cents))
            .where(Settlement.group_id == group_id)
            .group_by(Settlement.from_user_id, Settlement.currency)
        )
        for user_id, currency, total in rows:
            result[BalanceKey(user_id, currency)] += int(total)

        # 4. Settlements received — creditor balance is reduced
        rows = await self._session.execute(
            select(Settlement.to_user_id, Settlement.currency, func.sum(Settlement.amount_cents))
            .where(Settlement.group_id == group_id)
            .group_by(Settlement.to_user_id, Settlement.currency)
        )
        for user_id, currency, total in rows:
            result[BalanceKey(user_id, currency)] -= int(total)

        return {k: v for k, v in result.items() if v != 0}

    async def get_usernames(self, user_ids: set[uuid.UUID]) -> dict[uuid.UUID, str | None]:
        if not user_ids:
            return {}
        rows = await self._session.execute(
            select(User.id, User.username).where(User.id.in_(user_ids))
        )
        return {row.id: row.username for row in rows}

    async def get_display_names(
        self, user_ids: set[uuid.UUID]
    ) -> dict[uuid.UUID, tuple[str | None, str | None]]:
        """(username, first_name) per id — lets the bot prefer @handle, fall back
        to a first name, and avoid ever surfacing a raw UUID."""
        if not user_ids:
            return {}
        rows = await self._session.execute(
            select(User.id, User.username, User.first_name).where(User.id.in_(user_ids))
        )
        return {row.id: (row.username, row.first_name) for row in rows}


class StatementRepository:
    """Reads the merged ledger (expenses + settlements) for /statements.

    Spans both event tables, like BalanceRepository — but returns the raw rows
    chronologically instead of aggregating. Scope is the whole group, or (when
    ``user_id`` is given) only the entries that user is involved in: expenses
    they paid or hold a share in, and settlements they sent or received.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_entries(
        self, group_id: uuid.UUID, *, user_id: uuid.UUID | None = None
    ) -> list[RawStatementEntry]:
        """All ledger entries for the scope, newest first. Voided expenses are
        included (and flagged) — a statement is an audit trail, unlike the
        balance derivation which excludes them."""
        exp_q = select(Expense).where(Expense.group_id == group_id)
        if user_id is not None:
            shared = select(ExpenseShare.expense_id).where(ExpenseShare.user_id == user_id)
            exp_q = exp_q.where(or_(Expense.payer_id == user_id, Expense.id.in_(shared)))
        expenses = (await self._session.execute(exp_q)).scalars().all()

        # Participant counts in one grouped pass over the relevant expenses.
        counts: dict[uuid.UUID, int] = {}
        if expenses:
            rows = await self._session.execute(
                select(ExpenseShare.expense_id, func.count())
                .where(ExpenseShare.expense_id.in_([e.id for e in expenses]))
                .group_by(ExpenseShare.expense_id)
            )
            counts = {eid: int(c) for eid, c in rows}

        entries = [
            RawStatementEntry(
                kind="expense",
                created_at=e.created_at,
                amount_cents=e.amount_cents,
                currency=e.currency,
                description=e.description,
                actor_id=e.payer_id,
                counterparty_id=None,
                participant_count=counts.get(e.id, 0),
                voided=e.voided_at is not None,
            )
            for e in expenses
        ]

        set_q = select(Settlement).where(Settlement.group_id == group_id)
        if user_id is not None:
            set_q = set_q.where(
                or_(Settlement.from_user_id == user_id, Settlement.to_user_id == user_id)
            )
        settlements = (await self._session.execute(set_q)).scalars().all()
        entries.extend(
            RawStatementEntry(
                kind="settlement",
                created_at=s.created_at,
                amount_cents=s.amount_cents,
                currency=s.currency,
                description=None,
                actor_id=s.from_user_id,
                counterparty_id=s.to_user_id,
                participant_count=None,
                voided=False,
            )
            for s in settlements
        )

        entries.sort(key=lambda e: e.created_at, reverse=True)
        return entries


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_telegram_id(self, telegram_user_id: int) -> User | None:
        result = await self._session.execute(
            select(User).where(User.telegram_user_id == telegram_user_id)
        )
        return result.scalar_one_or_none()

    async def pending_placeholder(self, username: str) -> User | None:
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
        existing = await self.get_by_telegram_id(telegram_user_id)
        if existing is not None:
            existing.username = username
            existing.first_name = first_name
            existing.last_name = last_name
            await self._session.flush()
            return existing

        # First interaction: claim a matching pending placeholder if present.
        if username is not None:
            placeholder = await self.pending_placeholder(username)
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

    async def find_by_mention(self, username: str) -> User | None:
        """Look up an existing user by @handle without creating anything. Prefers
        a claimed row (telegram_user_id IS NOT NULL) over a pending placeholder.

        Matching is by username — a best-effort display-alias hint, never
        identity (CLAUDE.md) — which is why a claimed row wins when a
        rename/reuse has left both a claimed user and a placeholder under one
        handle. Use this where a mention must resolve to *someone already known*
        (e.g. /settleup), so a typo'd handle never spawns a stray placeholder.
        """
        result = await self._session.execute(
            select(User)
            .where(User.username == username)
            .order_by(User.telegram_user_id.is_(None))  # claimed (False) sorts first
        )
        return result.scalars().first()

    async def resolve_mention(self, username: str) -> User:
        """Resolve an @mention to a user row, creating a pending placeholder when
        the handle is entirely unknown (the right behavior for /addexpense, where
        naming a not-yet-seen person should track them)."""
        existing = await self.find_by_mention(username)
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

    async def set_default_currency(
        self, group_id: uuid.UUID, default_currency: str
    ) -> None:
        await self._session.execute(
            update(Group)
            .where(Group.id == group_id)
            .values(default_currency=default_currency)
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

    async def ensure_member(self, group_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        """Add an active membership row if one doesn't exist. Returns True when a
        new row was inserted, False when the user was already a member."""
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
            return True
        return False
