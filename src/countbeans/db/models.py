import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from ._base import Base
from ._mixins import CreatedAtMixin, TimestampMixin, UUIDPrimaryKeyMixin, _now


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"

    telegram_user_id: Mapped[Optional[int]] = mapped_column(BigInteger, unique=True)
    username: Mapped[Optional[str]] = mapped_column(String(255))
    first_name: Mapped[Optional[str]] = mapped_column(String(255))
    last_name: Mapped[Optional[str]] = mapped_column(String(255))

    __table_args__ = (
        # App invariant: at most one PENDING placeholder (telegram_user_id IS
        # NULL) per username, so resolve_mention reuses a single placeholder per
        # handle and claiming stays unambiguous. Partial — it constrains only
        # placeholders; claimed rows may freely share a username across a
        # rename/reuse (cf. uq_events_one_open_per_group). See CLAUDE.md
        # "Onboarding & membership".
        Index(
            "uq_users_pending_placeholder_username",
            "username",
            unique=True,
            postgresql_where=text("telegram_user_id IS NULL"),
        ),
    )


class Group(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "groups"

    telegram_chat_id: Mapped[int] = mapped_column(
        BigInteger, unique=True, nullable=False
    )
    group_name: Mapped[Optional[str]] = mapped_column(String(255))
    default_currency: Mapped[str] = mapped_column(
        String(3), nullable=False, default="SGD"
    )
    simplify_debts: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Whether the bot itself is an administrator of this group. Maintained from
    # the my_chat_member stream and self-healed by a getChatMember(bot) check on
    # the false path (see AdminGateMiddleware). The bot refuses to process group
    # commands until this is true (CLAUDE.md "Onboarding & membership").
    bot_is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Active event for "active-event mode" (see CLAUDE.md "Events"): when non-NULL
    # it points at the group's single OPEN event and /addexpense & /settleup
    # auto-tag to it; NULL = general tracking (no open event, or the open event is
    # paused). use_alter breaks the circular groups<->events FK at DDL time.
    active_event_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("events.id", use_alter=True)
    )

    __table_args__ = (
        CheckConstraint("LENGTH(default_currency) = 3", name="default_currency_len"),
    )


class GroupMember(Base):
    __tablename__ = "group_members"

    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("groups.id"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), primary_key=True
    )
    # joined_at is a PK component and carries domain meaning; not a generic audit column.
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, default=_now
    )
    left_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class Event(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """An ad-hoc sub-scope within a group (e.g. a trip) — a scope dimension on the
    one shared ledger, never a separate ledger (see CLAUDE.md "Events")."""

    __tablename__ = "events"

    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("groups.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # NULL = inherit groups.default_currency.
    default_currency: Mapped[Optional[str]] = mapped_column(String(3))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("status IN ('open', 'closed')", name="status_valid"),
        CheckConstraint(
            "default_currency IS NULL OR LENGTH(default_currency) = 3",
            name="currency_len",
        ),
        # At most one OPEN event per group: a new event opens only after the
        # current one closes. Partial, like uq_users_pending_placeholder_username.
        Index(
            "uq_events_one_open_per_group",
            "group_id",
            unique=True,
            postgresql_where=text("status = 'open'"),
        ),
    )


class EventMember(Base):
    """Explicit per-event roster — a deliberate opt-in subset of the group. `@all`
    inside an active event means THIS roster, not the whole group."""

    __tablename__ = "event_members"

    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("events.id"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), primary_key=True
    )


class Expense(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "expenses"

    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("groups.id"), nullable=False
    )
    # NULL = general/regular tracking; else the event this expense is tagged to.
    event_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("events.id")
    )
    payer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(255))
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    voided_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    voided_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id")
    )

    __table_args__ = (
        CheckConstraint("amount_cents > 0", name="amount_positive"),
        CheckConstraint("LENGTH(currency) = 3", name="currency_len"),
    )


class ExpenseShare(Base):
    __tablename__ = "expense_shares"

    expense_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("expenses.id"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), primary_key=True
    )
    share_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (CheckConstraint("share_cents >= 0", name="nonneg"),)


class Settlement(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "settlements"

    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("groups.id"), nullable=False
    )
    # NULL = general/regular tracking; else the event this settlement is tagged to.
    event_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("events.id")
    )
    from_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    to_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    # Mirrors Expense: a mistaken settlement is corrected by voiding (stamp, never
    # delete), and the balance derivation filters `voided_at IS NULL`.
    voided_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    voided_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id")
    )

    __table_args__ = (
        CheckConstraint("amount_cents > 0", name="amount_positive"),
        CheckConstraint("LENGTH(currency) = 3", name="currency_len"),
        CheckConstraint("from_user_id <> to_user_id", name="different_users"),
    )
