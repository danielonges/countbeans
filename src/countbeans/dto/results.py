"""Outbound result DTOs — returned from the service core after a mutating operation."""
import uuid

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


class EventCreatedResult(BaseModel):
    """Confirmation returned after opening an event — what the bot needs to echo
    the new scope. `currency` is the resolved per-event currency (None = inherits
    the group default)."""

    model_config = ConfigDict(frozen=True)

    event_id: uuid.UUID
    group_id: uuid.UUID
    name: str
    currency: str | None
