"""Void services — browse, preview, and undo recent ledger entries.

A mistaken /addexpense *or* /settleup is corrected by *voiding* it, never by
mutating a row in place: the ledger is append-only, so voiding stamps
`voided_at` / `voided_by` and the balance derivation (which filters
`voided_at IS NULL` on both tables) simply stops counting it. The row stays as
an audit trail (and still shows, flagged, in /statements).

/void is a two-step flow: `list_void_candidates` (a pure read) gives the bot
the recent active entries in the caller's scope to preview and step through,
and only a confirmed button tap writes — through `void_entry`, pinned to the
previewed entry's kind + id so a write landing between preview and confirm can
never redirect the void to a different entry.
"""

import logging
import uuid
from typing import Literal

from countbeans.db.models import Expense, Settlement
from countbeans.dto.results import EntryVoidedResult, VoidOutcome, VoidPreview

from .uow import UnitOfWork

logger = logging.getLogger(__name__)

# How far back /void can step. Mistakes discovered later than this are rare
# enough that the browse stops being a shortcut; /statements shows the rest.
VOID_BROWSE_LIMIT = 10


def may_void(entry: VoidPreview, caller_id: uuid.UUID, *, allow_any: bool) -> bool:
    """Who may undo an entry: an expense's payer or recorder; a settlement's
    sender *or* recipient (both standings move, and no recorder is stored);
    `allow_any` is the handler's already-checked "caller is a group admin"
    flag, which lifts the restriction either way."""
    if allow_any:
        return True
    if entry.kind == "expense":
        return caller_id in (entry.actor_id, entry.created_by)
    return caller_id in (entry.actor_id, entry.counterparty_id)


def _expense_preview(row: Expense) -> VoidPreview:
    return VoidPreview(
        kind="expense",
        entry_id=row.id,
        amount_cents=row.amount_cents,
        currency=row.currency,
        description=row.description,
        actor_id=row.payer_id,
        counterparty_id=None,
        created_by=row.created_by,
        created_at=row.created_at,
        event_id=row.event_id,
    )


def _settlement_preview(row: Settlement) -> VoidPreview:
    return VoidPreview(
        kind="settlement",
        entry_id=row.id,
        amount_cents=row.amount_cents,
        currency=row.currency,
        description=None,
        actor_id=row.from_user_id,
        counterparty_id=row.to_user_id,
        created_by=None,
        created_at=row.created_at,
        event_id=row.event_id,
    )


async def list_void_candidates(
    uow: UnitOfWork,
    group_id: uuid.UUID,
    *,
    event_id: uuid.UUID | None,
    limit: int = VOID_BROWSE_LIMIT,
) -> list[VoidPreview]:
    """The newest active entries (expenses + settlements merged, newest first)
    in the caller's current scope — what /void previews and steps through.

    Scope is the **general** ledger (``event_id IS NULL``) or exactly one event,
    resolved by the handler exactly as /addexpense does. Pure read: nothing is
    written until `void_entry`.
    """
    expenses = await uow.expenses.recent_active_in_scope(
        group_id, event_id=event_id, limit=limit
    )
    settlements = await uow.settlements.recent_active_in_scope(
        group_id, event_id=event_id, limit=limit
    )
    entries = [_expense_preview(e) for e in expenses] + [
        _settlement_preview(s) for s in settlements
    ]
    entries.sort(key=lambda p: p.created_at, reverse=True)
    logger.debug(
        "list_void_candidates: group=%s event=%s entries=%d",
        group_id,
        event_id,
        len(entries),
    )
    return entries[:limit]


async def void_entry(
    uow: UnitOfWork,
    group_id: uuid.UUID,
    caller_id: uuid.UUID,
    kind: Literal["expense", "settlement"],
    entry_id: uuid.UUID,
    *,
    allow_any: bool,
) -> EntryVoidedResult:
    """Void one specific entry — the row the caller previewed and confirmed.

    Voiding by kind + id (not "latest in scope", re-resolved at confirm time)
    is what makes the confirm race-free. ``group_id`` must match the row's
    group, so a crafted callback can never reach into another group's ledger.

    Permission is `may_void` (payer/recorder for expenses, either party for
    settlements, admin via ``allow_any``). The bot owns the admin check (it
    needs the Bot to call getChatMember); this stays SQL-only.

    The ``outcome`` on the returned DTO says what happened:

    * ``NOTHING``  — the entry is gone, in another group, or already voided
      (e.g. a double-tap); nothing written.
    * ``FORBIDDEN`` — the caller may not void it; nothing written, but the
      entry fields are populated so the handler can name who *is* allowed.
    * ``VOIDED``   — the entry was stamped voided; the fields echo what was undone.
    """
    row: Expense | Settlement | None
    if kind == "expense":
        row = await uow.expenses.get(entry_id)
    else:
        row = await uow.settlements.get(entry_id)
    if row is None or row.group_id != group_id or row.voided_at is not None:
        logger.debug(
            "void_entry: no active %s=%s in group=%s", kind, entry_id, group_id
        )
        return EntryVoidedResult(outcome=VoidOutcome.NOTHING)

    preview = (
        _expense_preview(row) if isinstance(row, Expense) else _settlement_preview(row)
    )
    fields = preview.model_dump(exclude={"created_at"})

    if not may_void(preview, caller_id, allow_any=allow_any):
        logger.debug(
            "void_entry: refused caller=%s not a party to %s=%s",
            caller_id,
            kind,
            entry_id,
        )
        return EntryVoidedResult(outcome=VoidOutcome.FORBIDDEN, **fields)

    if isinstance(row, Expense):
        await uow.expenses.mark_voided(row, voided_by=caller_id)
    else:
        await uow.settlements.mark_voided(row, voided_by=caller_id)
    logger.info(
        "Entry voided: kind=%s id=%s by=%s amount_cents=%d",
        kind,
        entry_id,
        caller_id,
        preview.amount_cents,
    )
    return EntryVoidedResult(outcome=VoidOutcome.VOIDED, **fields)
