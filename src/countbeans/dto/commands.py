"""Inbound command DTOs."""
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator


class AddExpenseCommand(BaseModel):
    model_config = ConfigDict(frozen=True)

    group_id: uuid.UUID
    payer_id: uuid.UUID
    amount_cents: int
    currency: str
    description: str | None = None
    participants: list[uuid.UUID]
    split_mode: Literal["equal", "weighted", "percent", "exact"] = "equal"
    split_params: dict[uuid.UUID, int] | None = None
    event_id: uuid.UUID | None = None
    created_by: uuid.UUID

    @field_validator("amount_cents")
    @classmethod
    def amount_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("amount_cents must be greater than 0")
        return v

    @field_validator("currency")
    @classmethod
    def currency_three_chars(cls, v: str) -> str:
        if len(v) != 3:
            raise ValueError("currency must be exactly 3 ISO 4217 characters")
        return v

    @field_validator("participants")
    @classmethod
    def participants_not_empty(cls, v: list[uuid.UUID]) -> list[uuid.UUID]:
        if not v:
            raise ValueError("participants must not be empty")
        return v
