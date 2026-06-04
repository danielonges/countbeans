from datetime import datetime, timezone

from sqlalchemy import DateTime
from sqlalchemy.orm import Mapped, mapped_column

from ._base import UUIDpk


def _now() -> datetime:
    return datetime.now(timezone.utc)


class UUIDPrimaryKeyMixin:
    """Single-column UUID7 primary key named `id`."""

    id: Mapped[UUIDpk]


class CreatedAtMixin:
    """Adds created_at to immutable ledger events (expenses, settlements)."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )


class TimestampMixin(CreatedAtMixin):
    """Adds created_at + updated_at to mutable records (users, groups)."""

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )
