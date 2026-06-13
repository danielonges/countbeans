"""Outbound result DTOs — returned from the service core after a mutating operation."""

import uuid
from datetime import datetime
from enum import StrEnum

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
    """The expense a confirmed /void would undo — shown to the caller before any
    write. A pure read: the void itself only happens in `void_expense_by_id`,
    pinned to this `expense_id`."""

    model_config = ConfigDict(frozen=True)

    expense_id: uuid.UUID
    amount_cents: int
    currency: str
    description: str | None
    payer_id: uuid.UUID
    created_by: uuid.UUID
    created_at: datetime
    event_id: uuid.UUID | None


class VoidOutcome(StrEnum):
    """Why a /void did or didn't happen, so the handler can pick the reply without
    re-deriving it (and without holding any SQLAlchemy state)."""

    VOIDED = "voided"  # the expense was stamped voided
    NOTHING = "nothing"  # the scope had no active expense to void
    FORBIDDEN = "forbidden"  # caller is neither owner/creator nor a group admin


class ExpenseVoidedResult(BaseModel):
    """Outcome of a /void. `outcome` says what happened; the expense fields are
    populated for VOIDED (echo what was undone) and FORBIDDEN (name who *can*
    void it via `payer_id` / `created_by`), and are None for NOTHING."""

    model_config = ConfigDict(frozen=True)

    outcome: VoidOutcome
    expense_id: uuid.UUID | None = None
    amount_cents: int | None = None
    currency: str | None = None
    description: str | None = None
    payer_id: uuid.UUID | None = None
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
