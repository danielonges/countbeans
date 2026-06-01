"""add_expense service function with split-computation helpers."""
import uuid
from typing import cast

import uuid_utils

from countbeans.db.models import Expense
from countbeans.dto.commands import AddExpenseCommand
from countbeans.dto.results import ExpenseCreatedResult

from .uow import UnitOfWork

Id = uuid.UUID


def apportion(amount_cents: int, weights: dict[Id, int]) -> dict[Id, int]:
    """Split amount_cents proportionally to weights, summing exactly to amount_cents.

    Uses the largest-remainder method so cents always reconcile.
    Tie-breaks by UUID for determinism.
    """
    total = sum(weights.values())
    if total <= 0:
        raise ValueError("weights must sum to a positive value")
    shares: dict[Id, int] = {}
    remainders: list[tuple[int, Id]] = []
    allocated = 0
    for k, w in weights.items():
        exact = amount_cents * w
        shares[k] = exact // total
        allocated += shares[k]
        remainders.append((exact % total, k))
    remainders.sort(key=lambda r: (-r[0], r[1]))
    for _, k in remainders[: amount_cents - allocated]:
        shares[k] += 1
    return shares


def compute_shares(
    amount_cents: int,
    participants: list[Id],
    mode: str = "equal",
    params: dict[Id, int] | None = None,
) -> dict[Id, int]:
    match mode:
        case "equal":
            return apportion(amount_cents, {u: 1 for u in participants})
        case "weighted":
            assert params is not None
            return apportion(amount_cents, params)
        case "percent":
            assert params is not None
            if sum(params.values()) != 100:
                raise ValueError("percentages must sum to 100")
            return apportion(amount_cents, params)
        case "exact":
            assert params is not None
            if sum(params.values()) != amount_cents:
                raise ValueError("exact shares must sum to the expense amount")
            return dict(params)
        case _:
            raise ValueError(f"unknown split mode: {mode}")


async def add_expense(uow: UnitOfWork, cmd: AddExpenseCommand) -> ExpenseCreatedResult:
    shares = compute_shares(
        cmd.amount_cents, list(cmd.participants), cmd.split_mode, cmd.split_params
    )
    expense_id = cast(uuid.UUID, uuid_utils.uuid7())
    expense = Expense(
        id=expense_id,
        group_id=cmd.group_id,
        payer_id=cmd.payer_id,
        amount_cents=cmd.amount_cents,
        currency=cmd.currency,
        description=cmd.description,
        created_by=cmd.created_by,
    )
    await uow.expenses.add(expense, shares)
    return ExpenseCreatedResult(
        expense_id=expense_id,
        group_id=cmd.group_id,
        payer_id=cmd.payer_id,
        amount_cents=cmd.amount_cents,
        currency=cmd.currency,
        description=cmd.description,
        shares=shares,
        event_id=cmd.event_id,
    )
