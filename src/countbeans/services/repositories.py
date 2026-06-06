"""Repository classes — the only objects that hold SQLAlchemy models."""

import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import uuid_utils.compat as uuid_utils  # .compat yields stdlib uuid.UUID instances
from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from countbeans.db._mixins import _now
from countbeans.db.models import (
    Event,
    EventMember,
    Expense,
    ExpenseShare,
    Group,
    GroupMember,
    Settlement,
    User,
)
from countbeans.dto.domain import ActivitySummary, BalanceKey, BalanceMap, MemberInfo
from countbeans.dto.results import EventCreatedResult, SettlementCreatedResult

logger = logging.getLogger(__name__)


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
    actor_id: uuid.UUID  # expense payer / settlement sender
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
            event_id=row.event_id,
        )


class ExpenseRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, expense: Expense, shares: dict[uuid.UUID, int]) -> None:
        self._session.add(expense)
        await self._session.flush()
        self._session.add_all(
            [
                ExpenseShare(expense_id=expense.id, user_id=uid, share_cents=cents)
                for uid, cents in shares.items()
            ]
        )
        await self._session.flush()

    async def latest_active_in_scope(
        self, group_id: uuid.UUID, *, event_id: uuid.UUID | None
    ) -> Expense | None:
        """The most-recent non-voided expense in one scope (newest `created_at`
        first), or None when the scope has nothing left to void.

        The scope is the **general** ledger (``event_id IS NULL``) when ``event_id``
        is None, else exactly that event's rows — mirroring BalanceRepository so a
        /void only ever touches the scope the caller is currently in (CLAUDE.md
        "Events")."""
        scope = (
            Expense.event_id == event_id
            if event_id is not None
            else Expense.event_id.is_(None)
        )
        result = await self._session.execute(
            select(Expense)
            .where(Expense.group_id == group_id, Expense.voided_at.is_(None), scope)
            .order_by(Expense.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def mark_voided(self, expense: Expense, *, voided_by: uuid.UUID) -> None:
        """Stamp the void fields on an already-fetched expense. The row stays in
        place (append-only ledger — never deleted); balances already exclude
        voided rows via `voided_at IS NULL`."""
        expense.voided_at = _now()
        expense.voided_by = voided_by
        await self._session.flush()

    async def activity_summary(self, group_id: uuid.UUID) -> list[ActivitySummary]:
        """Active (non-voided) expense count and total per currency for a group."""
        rows = await self._session.execute(
            select(Expense.currency, func.count(), func.sum(Expense.amount_cents))
            .where(Expense.group_id == group_id, Expense.voided_at.is_(None))
            .group_by(Expense.currency)
        )
        return [
            ActivitySummary(
                currency=cur, expense_count=int(count), total_cents=int(total)
            )
            for cur, count, total in rows
        ]


class BalanceRepository:
    """Derives balances from the immutable ledger via SQL aggregation."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def compute_for_group(
        self, group_id: uuid.UUID, *, event_id: uuid.UUID | None = None
    ) -> BalanceMap:
        """Return {BalanceKey(user_id, currency): net_cents} for one scope. Zero
        balances are omitted.

        The scope is the **general** ledger (``event_id IS NULL``) when ``event_id``
        is None, else exactly that event's rows. Scopes are isolated — the general
        balance excludes event-tagged rows and each event derives independently, so
        the sum-to-zero invariant holds per ``(scope, currency)`` (CLAUDE.md
        "Events")."""
        exp_scope = (
            Expense.event_id == event_id
            if event_id is not None
            else Expense.event_id.is_(None)
        )
        set_scope = (
            Settlement.event_id == event_id
            if event_id is not None
            else Settlement.event_id.is_(None)
        )
        result: BalanceMap = defaultdict(int)

        # 1. Payer sums — money fronted
        rows = await self._session.execute(
            select(Expense.payer_id, Expense.currency, func.sum(Expense.amount_cents))
            .where(Expense.group_id == group_id, Expense.voided_at.is_(None), exp_scope)
            .group_by(Expense.payer_id, Expense.currency)
        )
        for payer_id, currency, total in rows:
            result[BalanceKey(payer_id, currency)] += int(total)

        # 2. Share sums — money consumed
        rows = await self._session.execute(
            select(
                ExpenseShare.user_id,
                Expense.currency,
                func.sum(ExpenseShare.share_cents),
            )
            .join(Expense, Expense.id == ExpenseShare.expense_id)
            .where(Expense.group_id == group_id, Expense.voided_at.is_(None), exp_scope)
            .group_by(ExpenseShare.user_id, Expense.currency)
        )
        for user_id, currency, total in rows:
            result[BalanceKey(user_id, currency)] -= int(total)

        # 3. Settlements sent — debtor reduces their balance
        rows = await self._session.execute(
            select(
                Settlement.from_user_id,
                Settlement.currency,
                func.sum(Settlement.amount_cents),
            )
            .where(Settlement.group_id == group_id, set_scope)
            .group_by(Settlement.from_user_id, Settlement.currency)
        )
        for user_id, currency, total in rows:
            result[BalanceKey(user_id, currency)] += int(total)

        # 4. Settlements received — creditor balance is reduced
        rows = await self._session.execute(
            select(
                Settlement.to_user_id,
                Settlement.currency,
                func.sum(Settlement.amount_cents),
            )
            .where(Settlement.group_id == group_id, set_scope)
            .group_by(Settlement.to_user_id, Settlement.currency)
        )
        for user_id, currency, total in rows:
            result[BalanceKey(user_id, currency)] -= int(total)

        return {k: v for k, v in result.items() if v != 0}

    async def get_usernames(
        self, user_ids: set[uuid.UUID]
    ) -> dict[uuid.UUID, str | None]:
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
            shared = select(ExpenseShare.expense_id).where(
                ExpenseShare.user_id == user_id
            )
            exp_q = exp_q.where(
                or_(Expense.payer_id == user_id, Expense.id.in_(shared))
            )
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
                or_(
                    Settlement.from_user_id == user_id, Settlement.to_user_id == user_id
                )
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

    async def pending_placeholder(
        self, username: str, group_id: uuid.UUID
    ) -> User | None:
        """The pending placeholder for ``username`` (telegram_user_id IS NULL) that
        is an active member of ``group_id`` — the gate that lets a claim proceed.

        Scoping to a group the placeholder was actually referenced in (every
        mention ensures the placeholder onto group_members) stops a stranger who
        registers the same @handle in an *unrelated* group from inheriting its
        ledger (security review #1). App invariant: at most one pending placeholder
        per username globally, so this still resolves to one row."""
        result = await self._session.execute(
            select(User)
            .join(GroupMember, GroupMember.user_id == User.id)
            .where(
                User.username == username,
                User.telegram_user_id.is_(None),
                GroupMember.group_id == group_id,
                GroupMember.left_at.is_(None),
            )
        )
        return result.scalar_one_or_none()

    async def upsert(
        self,
        telegram_user_id: int,
        username: str | None,
        first_name: str | None,
        last_name: str | None,
        *,
        claim_in_group: uuid.UUID | None = None,
    ) -> User:
        """Onboard or refresh the interacting user, claiming a placeholder if one
        is waiting.

        The first interaction of a previously-mentioned @handle is the *claim*: a
        single-row UPDATE sets telegram_user_id on the existing placeholder, so
        every expense_share / settlement already bound to that surrogate
        users.id follows automatically — no fan-out rewrite (CLAUDE.md
        "Onboarding & membership").

        The claim is **gated on ``claim_in_group``**: it fires only when a group is
        given AND the placeholder is a member of it (see pending_placeholder), so a
        claim can't reach across groups (security review #1). Callers without a
        group context (seeds/tests) pass None and never claim.
        """
        # Already a claimed identity: refresh the display alias and names.
        existing = await self.get_by_telegram_id(telegram_user_id)
        if existing is not None:
            existing.username = username
            existing.first_name = first_name
            existing.last_name = last_name
            await self._session.flush()
            return existing

        # First interaction: claim a matching pending placeholder, but only one that
        # belongs to the group this interaction is happening in.
        if username is not None and claim_in_group is not None:
            placeholder = await self.pending_placeholder(username, claim_in_group)
            if placeholder is not None:
                placeholder.telegram_user_id = telegram_user_id
                placeholder.first_name = first_name
                placeholder.last_name = last_name
                await self._session.flush()
                # The moment a pending @handle becomes a real identity and
                # inherits its ledger rows — worth an audit line (security #1).
                logger.info(
                    "claimed placeholder user_id=%s username=%s group=%s "
                    "telegram_user_id=%s",
                    placeholder.id,
                    username,
                    claim_in_group,
                    telegram_user_id,
                )
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
        logger.debug("created placeholder user_id=%s for @%s", user.id, username)
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

    async def get_by_telegram_chat_id(self, telegram_chat_id: int) -> Group | None:
        """Read-only lookup by Telegram chat id — used by the admin gate, which
        must read `bot_is_admin` without upserting."""
        result = await self._session.execute(
            select(Group).where(Group.telegram_chat_id == telegram_chat_id)
        )
        return result.scalar_one_or_none()

    async def set_bot_admin(self, group_id: uuid.UUID, bot_is_admin: bool) -> None:
        await self._session.execute(
            update(Group).where(Group.id == group_id).values(bot_is_admin=bot_is_admin)
        )

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

    async def set_active_event(
        self, group_id: uuid.UUID, event_id: uuid.UUID | None
    ) -> None:
        """Point the active-event pointer at an open event (resume / new) or NULL
        (pause / close). Durable, shared group state — not aiogram FSM."""
        await self._session.execute(
            update(Group).where(Group.id == group_id).values(active_event_id=event_id)
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

    async def mark_left(self, group_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        """Set left_at on the user's active membership (left_at IS NULL), driven
        by the chat_member leave stream. Returns True when a row was updated,
        False when the user had no active membership."""
        result = await self._session.execute(
            update(GroupMember)
            .where(
                GroupMember.group_id == group_id,
                GroupMember.user_id == user_id,
                GroupMember.left_at.is_(None),
            )
            .values(left_at=_now())
            .returning(GroupMember.user_id)
        )
        return result.first() is not None


class EventRepository:
    """Event scopes and their rosters. The one-open-per-group invariant is held by
    the partial unique index `uq_events_one_open_per_group`; the service checks
    `get_open` first to surface a friendly error rather than the raw violation."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, event: Event) -> None:
        self._session.add(event)
        await self._session.flush()

    async def get(self, event_id: uuid.UUID) -> Event | None:
        result = await self._session.execute(select(Event).where(Event.id == event_id))
        return result.scalar_one_or_none()

    async def get_open(self, group_id: uuid.UUID) -> Event | None:
        """The group's single OPEN event, if any (at most one — see the index)."""
        result = await self._session.execute(
            select(Event).where(Event.group_id == group_id, Event.status == "open")
        )
        return result.scalar_one_or_none()

    async def set_status(
        self, event_id: uuid.UUID, status: str, closed_at: datetime | None
    ) -> None:
        await self._session.execute(
            update(Event)
            .where(Event.id == event_id)
            .values(status=status, closed_at=closed_at)
        )

    async def ensure_member(self, event_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        """Add a roster row if absent. Returns True when newly added."""
        result = await self._session.execute(
            select(EventMember).where(
                EventMember.event_id == event_id, EventMember.user_id == user_id
            )
        )
        if result.scalar_one_or_none() is None:
            self._session.add(EventMember(event_id=event_id, user_id=user_id))
            await self._session.flush()
            return True
        return False

    async def bulk_ensure_members(
        self, event_id: uuid.UUID, user_ids: list[uuid.UUID]
    ) -> int:
        """Add roster rows for all user_ids not already present in one INSERT.
        Returns the number of newly added rows."""
        if not user_ids:
            return 0
        stmt = (
            pg_insert(EventMember)
            .values([{"event_id": event_id, "user_id": uid} for uid in user_ids])
            .on_conflict_do_nothing()
            .returning(EventMember.user_id)
        )
        result = await self._session.execute(stmt)
        return len(result.fetchall())

    async def remove_member(self, event_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        """Drop a roster row. Returns True when one was removed, False if absent."""
        member = (
            await self._session.execute(
                select(EventMember).where(
                    EventMember.event_id == event_id, EventMember.user_id == user_id
                )
            )
        ).scalar_one_or_none()
        if member is None:
            return False
        await self._session.delete(member)
        await self._session.flush()
        return True

    async def list_members(self, event_id: uuid.UUID) -> list[MemberInfo]:
        """The event's roster with user info (placeholders flagged), like
        GroupMemberRepository.list_members but over event_members."""
        rows = await self._session.execute(
            select(User)
            .join(EventMember, EventMember.user_id == User.id)
            .where(EventMember.event_id == event_id)
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

    def _to_result(self, event: Event) -> EventCreatedResult:
        return EventCreatedResult(
            event_id=event.id,
            group_id=event.group_id,
            name=event.name,
            currency=event.default_currency,
        )
