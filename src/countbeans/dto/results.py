"""Outbound result DTOs."""
import uuid

from pydantic import BaseModel, ConfigDict


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
