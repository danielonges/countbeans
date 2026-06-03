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

    telegram_chat_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    group_name: Mapped[Optional[str]] = mapped_column(String(255))
    default_currency: Mapped[str] = mapped_column(String(3), nullable=False, default="SGD")
    simplify_debts: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

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


class Expense(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "expenses"

    group_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("groups.id"), nullable=False)
    payer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(255))
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    voided_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    voided_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))

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

    __table_args__ = (
        CheckConstraint("share_cents >= 0", name="nonneg"),
    )


class Settlement(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "settlements"

    group_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("groups.id"), nullable=False)
    from_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    to_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)

    __table_args__ = (
        CheckConstraint("amount_cents > 0", name="amount_positive"),
        CheckConstraint("LENGTH(currency) = 3", name="currency_len"),
        CheckConstraint("from_user_id <> to_user_id", name="different_users"),
    )
