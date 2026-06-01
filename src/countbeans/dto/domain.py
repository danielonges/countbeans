"""Read-side domain DTOs for balance queries."""
import uuid

from pydantic import BaseModel, ConfigDict


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
