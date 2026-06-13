"""Bot handler for /void — browse, preview, and undo recent ledger entries.

/void never writes on its own: it previews the most recent active entry
(expense **or** settlement) in the caller's current scope (the active event
when one is set, else the general ledger — resolved exactly as /addexpense
does), steppable back through the last few with ⬅ Older / Newer ➡ so a mistake
discovered later is still reachable. Only a confirmed tap voids, pinned to the
previewed entry's kind + id, so a write landing between preview and tap can
never redirect it. The ledger is append-only: voiding stamps `voided_at` /
`voided_by`, never deletes.

Permission: an expense's payer or recorder, a settlement's sender or recipient,
or a group admin — checked when rendering (an entry the caller can't void still
previews, naming who *can*, so they can step on to their own) and re-checked at
confirm. All buttons are bound to whoever ran /void — anyone else's tap gets an
alert, like /statements' owner-bound paging.
"""

import logging
import uuid
from typing import Literal

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
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
from countbeans.dto.results import VoidOutcome, VoidPreview
from countbeans.services.uow import UnitOfWork
from countbeans.services.void_expense import list_void_candidates, may_void, void_entry

logger = logging.getLogger(__name__)

router = Router()

_NOTHING_TO_VOID = "Nothing to void{scope} — no active expenses or settlements here."


@router.message(Command("void"))
async def cmd_void(message: Message, uow: UnitOfWork, bot: Bot) -> None:
    if message.from_user is None:
        return

    # Active-event mode: void within the active event's scope (general when none is
    # active) — mirrors /addexpense and /balance so /void undoes what was just added.
    ctx = await resolve_chat_context(uow, message)

    # The admin gate needs the Bot (getChatMember), so it lives here; the service
    # stays SQL-only and trusts this already-checked flag.
    caller_is_admin = await is_admin(bot, message.chat.id, message.from_user.id)

    screen = await _preview_screen(
        uow,
        ctx.group.id,
        ctx.caller.id,
        caller_is_admin,
        event_id=ctx.event_id,
        scope_note=ctx.scope_note,
        offset=0,
        owner_tg=message.from_user.id,
    )
    if screen is None:
        await message.reply(_NOTHING_TO_VOID.format(scope=ctx.scope_note))
        return
    text, keyboard = screen
    await message.reply(text, reply_markup=keyboard)


async def _preview_screen(
    uow: UnitOfWork,
    group_id: uuid.UUID,
    caller_id: uuid.UUID,
    caller_is_admin: bool,
    *,
    event_id: uuid.UUID | None,
    scope_note: str,
    offset: int,
    owner_tg: int,
) -> tuple[str, InlineKeyboardMarkup] | None:
    """The /void preview at one browse position: what the entry is, whether the
    caller may void it (confirm button only then), and Older/Newer stepping.
    None when the scope has nothing left to void."""
    candidates = await list_void_candidates(uow, group_id, event_id=event_id)
    if not candidates:
        return None
    offset = max(0, min(offset, len(candidates) - 1))
    entry = candidates[offset]

    ids = {entry.actor_id}
    if entry.counterparty_id is not None:
        ids.add(entry.counterparty_id)
    if entry.created_by is not None:
        ids.add(entry.created_by)
    names = await uow.balances.get_display_names(ids)

    def name(uid: uuid.UUID) -> str:
        username, first_name = names.get(uid, (None, None))
        return display_name(username, first_name)

    position = (
        f" ({offset + 1} of {len(candidates)} recent)" if len(candidates) > 1 else ""
    )
    when = entry.created_at.strftime("%b %d %H:%M")
    money = format_money(entry.amount_cents, entry.currency)
    if entry.kind == "expense":
        lines = [
            f"🗑️ Void this expense{scope_note}?{position}",
            f"{entry.description or 'expense'} — {money} · "
            f"paid by {name(entry.actor_id)} · {when}",
        ]
    else:
        assert entry.counterparty_id is not None
        lines = [
            f"🗑️ Void this settlement{scope_note}?{position}",
            f"{name(entry.actor_id)} → {name(entry.counterparty_id)}: {money} · {when}",
        ]

    permitted = may_void(entry, caller_id, allow_any=caller_is_admin)
    if not permitted:
        lines.append(_who_may_void(entry, name))
    lines.append("It stays in /statements (marked voided); balances stop counting it.")

    decide_row = []
    if permitted:
        kind_flag = "e" if entry.kind == "expense" else "s"
        decide_row.append(
            InlineKeyboardButton(
                text="🗑️ Yes, void it",
                callback_data=f"vd:ok:{kind_flag}:{entry.entry_id.hex}:{owner_tg}",
            )
        )
    decide_row.append(
        InlineKeyboardButton(text="✖ Keep it", callback_data=f"vd:x:{owner_tg}")
    )
    nav_row = []
    if offset + 1 < len(candidates):
        nav_row.append(
            InlineKeyboardButton(
                text="⬅ Older", callback_data=f"vd:o:{offset + 1}:{owner_tg}"
            )
        )
    if offset > 0:
        nav_row.append(
            InlineKeyboardButton(
                text="Newer ➡", callback_data=f"vd:o:{offset - 1}:{owner_tg}"
            )
        )
    rows = [decide_row] + ([nav_row] if nav_row else [])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def _who_may_void(entry: VoidPreview, name) -> str:
    """Name who is allowed to undo this entry when the viewer can't."""
    if entry.kind == "expense":
        who = name(entry.actor_id)
        if entry.created_by is not None and entry.created_by != entry.actor_id:
            who += f" or {name(entry.created_by)}"
    else:
        assert entry.counterparty_id is not None
        who = f"{name(entry.actor_id)} or {name(entry.counterparty_id)}"
    return f"Only {who} (or a group admin) can void this one."


@router.callback_query(F.data.startswith("vd:"))
async def on_void_decision(callback: CallbackQuery, uow: UnitOfWork, bot: Bot) -> None:
    """The preview's button taps: `vd:ok:<kind>:<entry_hex>:<tg_id>` voids the
    previewed entry, `vd:o:<offset>:<tg_id>` steps the browse, `vd:x:<tg_id>`
    keeps everything. The trailing tg id binds every button to whoever ran
    /void."""
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
    # Re-checked on every tap: the preview's check could be minutes stale.
    caller_is_admin = await is_admin(bot, chat.id, callback.from_user.id)

    if action == "o" and len(parts) == 4:
        try:
            offset = int(parts[2])
        except ValueError:
            await callback.answer()
            return
        active_event = (
            await uow.events.get(group.active_event_id)
            if group.active_event_id
            else None
        )
        scope_note = f' in "{active_event.name}"' if active_event else ""
        screen = await _preview_screen(
            uow,
            group.id,
            caller.id,
            caller_is_admin,
            event_id=active_event.id if active_event else None,
            scope_note=scope_note,
            offset=offset,
            owner_tg=owner_tg,
        )
        try:
            if screen is None:
                await callback.message.edit_text(
                    _NOTHING_TO_VOID.format(scope=scope_note)
                )
            else:
                text, keyboard = screen
                await callback.message.edit_text(text, reply_markup=keyboard)
        except TelegramBadRequest:
            # "message is not modified" — a double-tap on the same step.
            logger.debug("void browse edit skipped (not modified)")
        await callback.answer()
        return

    if action != "ok" or len(parts) != 5:
        await callback.answer()
        return
    kind: Literal["expense", "settlement"]
    if parts[2] == "e":
        kind = "expense"
    elif parts[2] == "s":
        kind = "settlement"
    else:
        await callback.answer()
        return
    try:
        entry_id = uuid.UUID(parts[3])
    except ValueError:
        await callback.answer()
        return

    result = await void_entry(
        uow, group.id, caller.id, kind, entry_id, allow_any=caller_is_admin
    )

    if result.outcome is VoidOutcome.NOTHING:
        # Already voided (double-tap, or undone by someone else meanwhile).
        await callback.message.edit_text(
            "That entry is already voided or gone — nothing changed."
        )
        await callback.answer()
        return
    if result.outcome is VoidOutcome.FORBIDDEN:
        await callback.answer("You can no longer void that entry.", show_alert=True)
        return

    assert result.amount_cents is not None and result.currency is not None
    assert result.actor_id is not None
    money = format_money(result.amount_cents, result.currency)
    scope_note = ""
    if result.event_id is not None:
        event = await uow.events.get(result.event_id)
        if event is not None:
            scope_note = f' in "{event.name}"'
    if result.kind == "expense":
        label = result.description or "expense"
        text = f"🗑️ Voided{scope_note}: {label} — {money}. Balances updated."
    else:
        assert result.counterparty_id is not None
        names = await uow.balances.get_display_names(
            {result.actor_id, result.counterparty_id}
        )

        def name(uid: uuid.UUID) -> str:
            username, first_name = names.get(uid, (None, None))
            return display_name(username, first_name)

        text = (
            f"🗑️ Voided settlement{scope_note}: {name(result.actor_id)} → "
            f"{name(result.counterparty_id)}: {money}. Balances updated."
        )
    await callback.message.edit_text(text)
    await callback.answer()
    logger.info(
        "Entry voided via /void: kind=%s id=%s by=%s",
        result.kind,
        result.entry_id,
        callback.from_user.id,
    )
