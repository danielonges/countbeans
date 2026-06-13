"""Bot handler for the /void command — preview, then undo, the most recent expense.

/void never writes on its own: it shows the single most-recent active
(non-voided) expense in the caller's current scope (the active event when one is
set, else the general ledger — resolved exactly as /addexpense does) with a
confirm/keep button pair, and only a confirmed tap voids it. The confirm is
pinned to the previewed expense's id, so an expense recorded between preview and
tap can never become the target. The ledger is append-only, so the row is
stamped `voided_at` / `voided_by`, never deleted.

Permission mirrors /settleup @all's admin gate: the expense's payer or recorder
may always undo their own; anyone else must be a group admin. It's checked at
preview time (a non-owner gets the refusal, not buttons) and re-checked at
confirm. Both buttons are bound to whoever ran /void — anyone else's tap gets an
alert, like /statements' owner-bound paging.
"""

import logging
import uuid

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from countbeans.bot.utils.context import resolve_chat_context
from countbeans.bot.utils.formatting import display_name, format_money
from countbeans.bot.utils.permissions import is_admin
from countbeans.dto.results import VoidOutcome
from countbeans.services.uow import UnitOfWork
from countbeans.services.void_expense import preview_last_expense, void_expense_by_id

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

    preview = await preview_last_expense(uow, ctx.group.id, event_id=ctx.event_id)
    if preview is None:
        await message.reply(f"Nothing to void{scope_note} — no expenses recorded yet.")
        return

    # The admin gate needs the Bot (getChatMember), so it lives here; the service
    # stays SQL-only and trusts this already-checked flag.
    caller_is_admin = await is_admin(bot, message.chat.id, message.from_user.id)
    if not caller_is_admin and ctx.caller.id not in (
        preview.payer_id,
        preview.created_by,
    ):
        await message.reply(
            await _forbidden_reply(uow, preview.payer_id, preview.created_by)
        )
        return

    names = await uow.balances.get_display_names({preview.payer_id})
    payer = display_name(*names.get(preview.payer_id, (None, None)))
    label = preview.description or "expense"
    when = preview.created_at.strftime("%b %d %H:%M")
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🗑️ Yes, void it",
                    callback_data=f"vd:ok:{preview.expense_id.hex}:{message.from_user.id}",
                ),
                InlineKeyboardButton(
                    text="✖ Keep it",
                    callback_data=f"vd:x:{message.from_user.id}",
                ),
            ]
        ]
    )
    await message.reply(
        f"🗑️ Void this expense{scope_note}?\n"
        f"{label} — {format_money(preview.amount_cents, preview.currency)} · "
        f"paid by {payer} · {when}\n"
        "It stays in /statements (marked voided); balances stop counting it.",
        reply_markup=keyboard,
    )


@router.callback_query(F.data.startswith("vd:"))
async def on_void_decision(callback: CallbackQuery, uow: UnitOfWork, bot: Bot) -> None:
    """The preview's button taps: `vd:ok:<expense_hex>:<tg_id>` voids the
    previewed expense, `vd:x:<tg_id>` keeps it. The trailing tg id binds both
    buttons to whoever ran /void."""
    parts = (callback.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""

    # Narrow to a live Message: an InaccessibleMessage (too old to edit) has no
    # edit_text, and there's nothing to repaint anyway.
    if not isinstance(callback.message, Message):
        await callback.answer()
        return

    try:
        owner_tg = int(parts[-1])
    except ValueError:
        await callback.answer()
        return
    if callback.from_user.id != owner_tg:
        await callback.answer(
            "Only the person who ran /void can decide this one.", show_alert=True
        )
        return

    if action == "x":
        await callback.message.edit_text("✖ Kept — nothing voided.")
        await callback.answer()
        return

    if action != "ok" or len(parts) != 4:
        await callback.answer()
        return
    try:
        expense_id = uuid.UUID(parts[2])
    except ValueError:
        await callback.answer()
        return

    chat = callback.message.chat
    group = await uow.groups.upsert(
        telegram_chat_id=chat.id, group_name=getattr(chat, "title", None)
    )
    caller = await uow.users.upsert(
        telegram_user_id=callback.from_user.id,
        username=callback.from_user.username,
        first_name=callback.from_user.first_name,
        last_name=callback.from_user.last_name,
        claim_in_group=group.id,
    )
    # Re-checked at confirm: the preview's check could be minutes stale.
    caller_is_admin = await is_admin(bot, chat.id, callback.from_user.id)

    result = await void_expense_by_id(
        uow, group.id, caller.id, expense_id, allow_any=caller_is_admin
    )

    if result.outcome is VoidOutcome.NOTHING:
        # Already voided (double-tap, or undone by someone else meanwhile).
        await callback.message.edit_text(
            "That expense is already voided or gone — nothing changed."
        )
        await callback.answer()
        return
    if result.outcome is VoidOutcome.FORBIDDEN:
        await callback.answer("You can no longer void that expense.", show_alert=True)
        return

    label = result.description or "expense"
    assert result.amount_cents is not None and result.currency is not None
    scope_note = ""
    if result.event_id is not None:
        event = await uow.events.get(result.event_id)
        if event is not None:
            scope_note = f' in "{event.name}"'
    await callback.message.edit_text(
        f"🗑️ Voided{scope_note}: {label} — "
        f"{format_money(result.amount_cents, result.currency)}. Balances updated."
    )
    await callback.answer()
    logger.info(
        "Expense voided via /void: expense_id=%s by=%s",
        result.expense_id,
        callback.from_user.id,
    )


async def _forbidden_reply(
    uow: UnitOfWork, payer_id: uuid.UUID, created_by: uuid.UUID
) -> str:
    """Name who is allowed to undo this expense (its payer, its recorder, or a
    group admin) when a non-owner non-admin tries to void it."""
    names = await uow.balances.get_display_names({payer_id, created_by})

    def name(uid: uuid.UUID) -> str:
        username, first_name = names.get(uid, (None, None))
        return display_name(username, first_name)

    who = name(payer_id)
    if created_by != payer_id:
        who += f" or {name(created_by)}"
    return (
        f"Only {who} (or a group admin) can void that expense — it's the most "
        "recent one in this scope and you didn't record it."
    )
