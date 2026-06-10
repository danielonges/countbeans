"""Bot handler for the /void command — undo the most recent expense in scope.

A fat-fingered /addexpense is corrected here: /void voids the single most-recent
active (non-voided) expense in the caller's current scope (the active event when
one is set, else the general ledger — resolved exactly as /addexpense does) and
the balance derivation stops counting it. The ledger is append-only, so the row
is stamped `voided_at` / `voided_by`, never deleted.

Permission mirrors /settleup @all's admin gate: the expense's payer or recorder
may always undo their own; anyone else must be a group admin. The service does
the SQL and reports an outcome (voided / nothing-to-void / forbidden); this
handler only formats the reply.
"""

import logging
import uuid

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import Message

from countbeans.bot.utils.context import resolve_chat_context
from countbeans.bot.utils.formatting import display_name, format_money
from countbeans.bot.utils.permissions import is_admin
from countbeans.dto.results import ExpenseVoidedResult, VoidOutcome
from countbeans.services.uow import UnitOfWork
from countbeans.services.void_expense import void_last_expense

logger = logging.getLogger(__name__)

router = Router()


@router.message(Command("void"))
async def cmd_void(message: Message, uow: UnitOfWork, bot: Bot) -> None:
    if message.from_user is None:
        return

    # Active-event mode: void within the active event's scope (general when none is
    # active) — mirrors /addexpense and /balance so /void undoes what was just added.
    ctx = await resolve_chat_context(uow, message)
    scope_note = ctx.scope_note

    # The admin gate needs the Bot (getChatMember), so it lives here; the service
    # stays SQL-only and trusts this already-checked flag.
    caller_is_admin = await is_admin(bot, message.chat.id, message.from_user.id)

    result = await void_last_expense(
        uow,
        ctx.group.id,
        ctx.caller.id,
        event_id=ctx.event_id,
        allow_any=caller_is_admin,
    )

    if result.outcome is VoidOutcome.NOTHING:
        await message.reply(f"Nothing to void{scope_note} — no expenses recorded yet.")
        return

    if result.outcome is VoidOutcome.FORBIDDEN:
        await message.reply(await _forbidden_reply(uow, result))
        return

    label = result.description or "expense"
    assert result.amount_cents is not None and result.currency is not None
    await message.reply(
        f"🗑️ Voided{scope_note}: {label} — "
        f"{format_money(result.amount_cents, result.currency)}. Balances updated."
    )
    logger.info(
        "Expense voided via /void: expense_id=%s by=%s",
        result.expense_id,
        message.from_user.id,
    )


async def _forbidden_reply(uow: UnitOfWork, result: ExpenseVoidedResult) -> str:
    """Name who is allowed to undo this expense (its payer, its recorder, or a
    group admin) when a non-owner non-admin tries to void it."""
    assert result.payer_id is not None and result.created_by is not None
    ids: set[uuid.UUID] = {result.payer_id, result.created_by}
    names = await uow.balances.get_display_names(ids)

    def name(uid: uuid.UUID) -> str:
        username, first_name = names.get(uid, (None, None))
        return display_name(username, first_name)

    who = name(result.payer_id)
    if result.created_by != result.payer_id:
        who += f" or {name(result.created_by)}"
    return (
        f"Only {who} (or a group admin) can void that expense — it's the most "
        "recent one in this scope and you didn't record it."
    )
