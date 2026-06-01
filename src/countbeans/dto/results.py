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
