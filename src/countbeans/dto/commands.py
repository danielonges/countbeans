"""Inbound command DTOs — passed from the bot/HTTP adapter into the service core."""
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


class SettleUpCommand(BaseModel):
    model_config = ConfigDict(frozen=True)

    group_id: uuid.UUID
    from_user_id: uuid.UUID
    to_user_id: uuid.UUID
    amount_cents: int
    currency: str
    event_id: uuid.UUID | None = None
    created_by: uuid.UUID

    @field_validator("amount_cents")
    @classmethod
    def amount_must_be_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("amount_cents must be greater than 0")
        return v

    @field_validator("currency")
    @classmethod
    def currency_must_be_three_chars(cls, v: str) -> str:
        if len(v) != 3:
            raise ValueError("currency must be exactly 3 characters (ISO 4217)")
        return v

    @model_validator(mode="after")
    def users_must_differ(self) -> "SettleUpCommand":
        if self.from_user_id == self.to_user_id:
            raise ValueError("from_user_id and to_user_id must be different users")
        return self


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
