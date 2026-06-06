"""void_last_expense service function — undoes the most recent active expense.

A mistaken /addexpense is corrected by *voiding* it, never by mutating a row in
place: the ledger is append-only, so voiding stamps `voided_at` / `voided_by` and
the balance derivation (which filters `voided_at IS NULL`) simply stops counting
it. The expense row stays as an audit trail (and still shows, flagged, in
/statements).
"""

import logging
import uuid

from countbeans.dto.results import ExpenseVoidedResult, VoidOutcome

from .uow import UnitOfWork

logger = logging.getLogger(__name__)


async def void_last_expense(
    uow: UnitOfWork,
    group_id: uuid.UUID,
    caller_id: uuid.UUID,
    *,
    event_id: uuid.UUID | None,
    allow_any: bool,
) -> ExpenseVoidedResult:
    """Void the single most-recent active expense in the caller's current scope.

    Scope is the **general** ledger (``event_id IS NULL``) or exactly one event,
    resolved by the handler exactly as /addexpense does.

    Permission: a caller may void an expense they paid (``payer_id``) or recorded
    (``created_by``); ``allow_any`` is the handler's already-checked "caller is a
    group admin" flag, which lifts that restriction. The bot owns the admin check
    (it needs the Bot to call getChatMember); this stays SQL-only.

    The ``outcome`` on the returned DTO says what happened:

    * ``NOTHING``  — the scope had no active expense; nothing written.
    * ``FORBIDDEN`` — the caller is neither owner/creator nor admin; nothing
      written, but ``payer_id`` / ``created_by`` are populated so the handler can
      name who *is* allowed to void it.
    * ``VOIDED``   — the expense was stamped voided; the fields echo what was undone.
    """
    expense = await uow.expenses.latest_active_in_scope(group_id, event_id=event_id)
    if expense is None:
        logger.debug(
            "void_last_expense: nothing to void group=%s event=%s", group_id, event_id
        )
        return ExpenseVoidedResult(outcome=VoidOutcome.NOTHING)

    if not allow_any and caller_id not in (expense.payer_id, expense.created_by):
        logger.debug(
            "void_last_expense: refused caller=%s not owner of expense=%s",
            caller_id,
            expense.id,
        )
        return ExpenseVoidedResult(
            outcome=VoidOutcome.FORBIDDEN,
            expense_id=expense.id,
            amount_cents=expense.amount_cents,
            currency=expense.currency,
            description=expense.description,
            payer_id=expense.payer_id,
            created_by=expense.created_by,
            event_id=expense.event_id,
        )

    await uow.expenses.mark_voided(expense, voided_by=caller_id)
    logger.info(
        "Expense voided: expense_id=%s by=%s amount_cents=%d",
        expense.id,
        caller_id,
        expense.amount_cents,
    )
    return ExpenseVoidedResult(
        outcome=VoidOutcome.VOIDED,
        expense_id=expense.id,
        amount_cents=expense.amount_cents,
        currency=expense.currency,
        description=expense.description,
        payer_id=expense.payer_id,
        created_by=expense.created_by,
        event_id=expense.event_id,
    )
