"""Inbound command DTOs — passed from the bot/HTTP adapter into the service core."""

import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


class OnboardUserCommand(BaseModel):
    """Register the caller into a group's ledger (claiming a placeholder if one
    waits). Unlike the other commands, this carries **raw Telegram IDs** rather
    than resolved `users.id`/`groups.id` UUIDs: onboarding is the operation that
    *creates* those rows, so there is nothing to pre-resolve."""

    model_config = ConfigDict(frozen=True)

    telegram_user_id: int
    telegram_chat_id: int
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    group_name: str | None = None


class MentionedUser(BaseModel):
    """A mention the bot resolved to a real Telegram identity via a message
    ``text_mention`` entity (a user without a public @handle, or a tap-selected
    user) — it carries the permanent ``telegram_user_id``, so it resolves to a
    *claimed* user, never a username placeholder (security review #1). aiogram-free
    so the service core stays decoupled from the bot layer."""

    model_config = ConfigDict(frozen=True)

    telegram_user_id: int
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None


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


class CreateEventCommand(BaseModel):
    """Open a new event scope. `default_currency` is optional (NULL = inherit the
    group default); the bot leaves it None today (per-event currency is deferred)."""

    model_config = ConfigDict(frozen=True)

    group_id: uuid.UUID
    name: str
    default_currency: str | None = None
    created_by: uuid.UUID

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("event name must not be blank")
        return v

    @field_validator("default_currency")
    @classmethod
    def currency_three_chars(cls, v: str | None) -> str | None:
        if v is not None and len(v) != 3:
            raise ValueError("currency must be exactly 3 characters (ISO 4217)")
        return v


class SetActiveEventCommand(BaseModel):
    """Point a group's active-event pointer: an event id (resume) or None (pause).
    Two commands → two transactions; see CLAUDE.md "Events"."""

    model_config = ConfigDict(frozen=True)

    group_id: uuid.UUID
    event_id: uuid.UUID | None


class SetEventStatusCommand(BaseModel):
    """Transition an event's lifecycle status (close today; reopen wired later)."""

    model_config = ConfigDict(frozen=True)

    event_id: uuid.UUID
    status: Literal["open", "closed"]


class EditEventRosterCommand(BaseModel):
    """Add or remove a single user from an event's roster."""

    model_config = ConfigDict(frozen=True)

    event_id: uuid.UUID
    user_id: uuid.UUID
    action: Literal["add", "remove"]
