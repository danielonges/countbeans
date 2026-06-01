"""Outbound result DTOs — returned from the service core after a mutating operation."""
import uuid

from pydantic import BaseModel, ConfigDict


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
