"""Void services — preview, then undo, the most recent expense in scope.

A mistaken /addexpense is corrected by *voiding* it, never by mutating a row in
place: the ledger is append-only, so voiding stamps `voided_at` / `voided_by` and
the balance derivation (which filters `voided_at IS NULL`) simply stops counting
it. The expense row stays as an audit trail (and still shows, flagged, in
/statements).

/void is a two-step flow: `preview_last_expense` (a pure read) tells the bot
what a void would undo, and only a confirmed button tap writes — through
`void_expense_by_id`, pinned to the previewed row's id so an expense recorded
between preview and confirm can never become the target.
"""

import logging
import uuid

from countbeans.dto.results import ExpenseVoidedResult, VoidOutcome, VoidPreview

from .uow import UnitOfWork

logger = logging.getLogger(__name__)


async def preview_last_expense(
    uow: UnitOfWork, group_id: uuid.UUID, *, event_id: uuid.UUID | None
) -> VoidPreview | None:
    """The single most-recent active expense in the caller's current scope — what
    a confirmed /void would undo — or None when the scope has nothing to void.

    Scope is the **general** ledger (``event_id IS NULL``) or exactly one event,
    resolved by the handler exactly as /addexpense does. Pure read: nothing is
    written until `void_expense_by_id`.
    """
    expense = await uow.expenses.latest_active_in_scope(group_id, event_id=event_id)
    if expense is None:
        logger.debug(
            "preview_last_expense: nothing to void group=%s event=%s",
            group_id,
            event_id,
        )
        return None
    return VoidPreview(
        expense_id=expense.id,
        amount_cents=expense.amount_cents,
        currency=expense.currency,
        description=expense.description,
        payer_id=expense.payer_id,
        created_by=expense.created_by,
        created_at=expense.created_at,
        event_id=expense.event_id,
    )


async def void_expense_by_id(
    uow: UnitOfWork,
    group_id: uuid.UUID,
    caller_id: uuid.UUID,
    expense_id: uuid.UUID,
    *,
    allow_any: bool,
) -> ExpenseVoidedResult:
    """Void one specific expense — the row the caller previewed and confirmed.

    Voiding by id (not "latest in scope", re-resolved at confirm time) is what
    makes the confirm race-free. ``group_id`` must match the row's group, so a
    crafted callback can never reach into another group's ledger.

    Permission: a caller may void an expense they paid (``payer_id``) or recorded
    (``created_by``); ``allow_any`` is the handler's already-checked "caller is a
    group admin" flag, which lifts that restriction. The bot owns the admin check
    (it needs the Bot to call getChatMember); this stays SQL-only.

    The ``outcome`` on the returned DTO says what happened:

    * ``NOTHING``  — the expense is gone, in another group, or already voided
      (e.g. a double-tap); nothing written.
    * ``FORBIDDEN`` — the caller is neither owner/creator nor admin; nothing
      written, but ``payer_id`` / ``created_by`` are populated so the handler can
      name who *is* allowed to void it.
    * ``VOIDED``   — the expense was stamped voided; the fields echo what was undone.
    """
    expense = await uow.expenses.get(expense_id)
    if expense is None or expense.group_id != group_id or expense.voided_at is not None:
        logger.debug(
            "void_expense_by_id: no active expense=%s in group=%s",
            expense_id,
            group_id,
        )
        return ExpenseVoidedResult(outcome=VoidOutcome.NOTHING)

    if not allow_any and caller_id not in (expense.payer_id, expense.created_by):
        logger.debug(
            "void_expense_by_id: refused caller=%s not owner of expense=%s",
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
