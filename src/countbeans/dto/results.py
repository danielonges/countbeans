"""Outbound result DTOs — returned from the service core after a mutating operation."""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict


class OnboardResult(BaseModel):
    """Outcome of onboarding a caller. `claimed_placeholder` is True when this
    interaction claimed a pending placeholder (the caller had been @mentioned in
    expenses before they ever interacted); `newly_added` is True when a fresh
    group membership row was created (vs. already being a member)."""

    model_config = ConfigDict(frozen=True)

    user_id: uuid.UUID
    username: str | None
    first_name: str | None
    claimed_placeholder: bool
    newly_added: bool


class SettlementCreatedResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    settlement_id: uuid.UUID
    from_user_id: uuid.UUID
    to_user_id: uuid.UUID
    amount_cents: int
    currency: str
    event_id: uuid.UUID | None


class ExpenseCreatedResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    expense_id: uuid.UUID
    group_id: uuid.UUID
    payer_id: uuid.UUID
    amount_cents: int
    currency: str
    description: str | None
    shares: dict[uuid.UUID, int]
    event_id: uuid.UUID | None


class VoidPreview(BaseModel):
    """One recent ledger entry (expense or settlement) a confirmed /void could
    undo — shown to the caller before any write. A pure read: the void itself
    only happens in `void_entry`, pinned to this `kind` + `entry_id`."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["expense", "settlement"]
    entry_id: uuid.UUID
    amount_cents: int
    currency: str
    description: str | None  # expenses only
    actor_id: uuid.UUID  # expense payer / settlement sender
    counterparty_id: uuid.UUID | None  # settlement recipient; None for expenses
    created_by: uuid.UUID | None  # expense recorder; settlements store none
    created_at: datetime
    event_id: uuid.UUID | None


class VoidOutcome(StrEnum):
    """Why a /void did or didn't happen, so the handler can pick the reply without
    re-deriving it (and without holding any SQLAlchemy state)."""

    VOIDED = "voided"  # the entry was stamped voided
    NOTHING = "nothing"  # the entry is gone / already voided / another group's
    FORBIDDEN = "forbidden"  # caller is neither a party to the entry nor an admin


class EntryVoidedResult(BaseModel):
    """Outcome of a /void confirm. `outcome` says what happened; the entry
    fields are populated for VOIDED (echo what was undone) and FORBIDDEN (name
    who *can* void it), and are None for NOTHING."""

    model_config = ConfigDict(frozen=True)

    outcome: VoidOutcome
    kind: Literal["expense", "settlement"] | None = None
    entry_id: uuid.UUID | None = None
    amount_cents: int | None = None
    currency: str | None = None
    description: str | None = None
    actor_id: uuid.UUID | None = None
    counterparty_id: uuid.UUID | None = None
    created_by: uuid.UUID | None = None
    event_id: uuid.UUID | None = None


class EventCreatedResult(BaseModel):
    """Confirmation returned after opening an event — what the bot needs to echo
    the new scope. `currency` is the resolved per-event currency (None = inherits
    the group default)."""

    model_config = ConfigDict(frozen=True)

    event_id: uuid.UUID
    group_id: uuid.UUID
    name: str
    currency: str | None
