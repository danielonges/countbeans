"""Read-side domain DTOs for balance and group queries."""
import uuid
from datetime import datetime
from typing import Literal, NamedTuple

from pydantic import BaseModel, ConfigDict


class BalanceKey(NamedTuple):
    """Composite key for a derived balance: one net figure per (user, currency).

    A NamedTuple rather than a Pydantic model — it is used as a dict key, so it
    must be cheaply hashable, and it still unpacks like the ``(uid, cur)`` tuple
    it replaces (so an equal plain tuple indexes the same entry).
    """

    user_id: uuid.UUID
    currency: str


# Net cents keyed by (user, currency); positive = the group owes the user.
# Zero balances are omitted by the repository that builds it.
type BalanceMap = dict[BalanceKey, int]


class MemberBalance(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_id: uuid.UUID
    username: str | None
    balance_cents: int  # positive = group owes them; negative = they owe the group
    currency: str


class Transfer(BaseModel):
    model_config = ConfigDict(frozen=True)

    from_user_id: uuid.UUID  # debtor (pays)
    to_user_id: uuid.UUID    # creditor (receives)
    amount_cents: int
    currency: str


class GroupSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    group_id: uuid.UUID
    balances: list[MemberBalance]
    suggested_transfers: list[Transfer]


class MemberInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_id: uuid.UUID
    username: str | None
    first_name: str | None
    is_pending: bool  # True = placeholder, telegram_user_id IS NULL


class ActivitySummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    currency: str
    expense_count: int
    total_cents: int


class GroupInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    group_id: uuid.UUID
    group_name: str | None
    default_currency: str
    simplify_debts: bool
    members: list[MemberInfo]         # active group_members, placeholders flagged
    known_count: int                  # len(members)
    actual_count: int | None          # from getChatMemberCount - 1; None if unavailable
    activity: list[ActivitySummary]   # per-currency active expense totals


class StatementEntry(BaseModel):
    """One line in a chronological ledger statement — an expense or a settlement.

    A discriminated shape: ``kind`` selects which optional fields are populated.
    For an expense, ``actor`` is the payer and ``counterparty`` is None; for a
    settlement, ``actor`` pays ``counterparty``. Money stays integer cents.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["expense", "settlement"]
    created_at: datetime
    amount_cents: int
    currency: str
    description: str | None            # expense only
    actor_username: str | None         # expense payer / settlement sender
    counterparty_username: str | None  # settlement recipient; None for expense
    participant_count: int | None      # expense only
    voided: bool                       # expense only; always False for settlements


class StatementPage(BaseModel):
    """A slice of a statement plus the cursor needed to render paging controls."""

    model_config = ConfigDict(frozen=True)

    entries: list[StatementEntry]
    page: int        # 0-indexed
    page_size: int
    total: int       # entries across every page (for the page count + Prev/Next)
