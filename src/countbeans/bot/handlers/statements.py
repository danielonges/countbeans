"""Bot handler for /statements [me|all] — a paginated ledger statement.

`/statements` (no arg) and `/statements me` show the caller's own activity
newest-first; `/statements all` shows the whole group. Paging is driven by
inline ◀ Prev / Next ▶ buttons whose state lives entirely in the button's
callback_data (``stmt:g:<page>`` or ``stmt:u:<tg_id>:<page>``) — no FSM, so the
buttons keep working across restarts. A ``u`` page is bound to its owner: only
that user's taps page it (anyone may still read the group view).

Replies are plain text on purpose — @usernames contain underscores, which a
Markdown parse would mangle into italics.
"""

import logging
import math

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from countbeans.bot.utils.formatting import display_name, format_money
from countbeans.bot.utils.parsing import is_all_selector
from countbeans.dto.domain import StatementEntry, StatementPage
from countbeans.services.statements import get_statement_page
from countbeans.services.uow import UnitOfWork

logger = logging.getLogger(__name__)

router = Router()


def _entry_lines(e: StatementEntry) -> str:
    when = e.created_at.strftime("%b %d %H:%M")
    actor = display_name(e.actor_username, e.actor_first_name)
    if e.kind == "expense":
        head = f"🧾 {when} · {e.description or 'expense'} — {format_money(e.amount_cents, e.currency)}"
        if e.voided:
            head = f"❌ {head} (voided)"
        sub = f"    paid by {actor}"
        if e.participant_count:
            sub += f" · split {e.participant_count}-way"
        return f"{head}\n{sub}"
    other = display_name(e.counterparty_username, e.counterparty_first_name)
    return f"💸 {when} · {actor} → {other}: {format_money(e.amount_cents, e.currency)}"


def _render(page: StatementPage, title: str) -> str:
    if page.total == 0:
        return f"{title}\n\nNo transactions yet."
    pages = math.ceil(page.total / page.page_size)
    header = f"{title}  (page {page.page + 1}/{pages}, {page.total} total)"
    return header + "\n\n" + "\n".join(_entry_lines(e) for e in page.entries)


def _keyboard(page: StatementPage, cb_prefix: str) -> InlineKeyboardMarkup | None:
    """Prev/Next only where they lead somewhere — Telegram has no disabled
    buttons, so an edge button is omitted rather than greyed out."""
    pages = math.ceil(page.total / page.page_size) if page.total else 1
    row: list[InlineKeyboardButton] = []
    if page.page > 0:
        row.append(
            InlineKeyboardButton(
                text="◀ Prev", callback_data=f"{cb_prefix}:{page.page - 1}"
            )
        )
    if page.page < pages - 1:
        row.append(
            InlineKeyboardButton(
                text="Next ▶", callback_data=f"{cb_prefix}:{page.page + 1}"
            )
        )
    return InlineKeyboardMarkup(inline_keyboard=[row]) if row else None


@router.message(Command("statements"))
async def cmd_statements(
    message: Message, command: CommandObject, uow: UnitOfWork
) -> None:
    if message.from_user is None:
        return

    args = (command.args or "").split()
    group_wide = is_all_selector(args)

    # Group first: the placeholder-claim in upsert is group-scoped (claim_in_group).
    group = await uow.groups.upsert(
        telegram_chat_id=message.chat.id,
        group_name=getattr(message.chat, "title", None),
    )
    caller = await uow.users.upsert(
        telegram_user_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
        claim_in_group=group.id,
    )
    await uow.group_members.ensure_member(group.id, caller.id)

    if group_wide:
        page = await get_statement_page(uow, group.id, page=0)
        title, cb_prefix = "📋 Group statement", "stmt:g"
    else:
        page = await get_statement_page(uow, group.id, user_id=caller.id, page=0)
        title, cb_prefix = "📋 Your statement", f"stmt:u:{message.from_user.id}"

    await message.reply(_render(page, title), reply_markup=_keyboard(page, cb_prefix))
    logger.debug(
        "statements: scope=%s group=%s page=0 total=%d",
        "all" if group_wide else "me",
        group.id,
        page.total,
    )


@router.callback_query(F.data.startswith("stmt:"))
async def on_statements_page(callback: CallbackQuery, uow: UnitOfWork) -> None:
    parts = (callback.data or "").split(":")
    # stmt:g:<page>  |  stmt:u:<tg_id>:<page>
    scope = parts[1] if len(parts) > 1 else ""

    # Narrow to a live Message: an InaccessibleMessage (too old to edit) has no
    # edit_text, and there's nothing to repaint anyway.
    if not isinstance(callback.message, Message):
        await callback.answer()
        return

    chat = callback.message.chat
    group = await uow.groups.upsert(
        telegram_chat_id=chat.id,
        group_name=getattr(chat, "title", None),
    )

    if scope == "g":
        page_no = int(parts[2])
        page = await get_statement_page(uow, group.id, page=page_no)
        title, cb_prefix = "📋 Group statement", "stmt:g"
    elif scope == "u":
        subject_tg, page_no = int(parts[2]), int(parts[3])
        if callback.from_user.id != subject_tg:
            logger.debug(
                "statements paging denied: user=%s tried to page subject=%s",
                callback.from_user.id,
                subject_tg,
            )
            await callback.answer(
                "That's not your statement — run /statements me.", show_alert=True
            )
            return
        viewer = await uow.users.upsert(
            telegram_user_id=callback.from_user.id,
            username=callback.from_user.username,
            first_name=callback.from_user.first_name,
            last_name=callback.from_user.last_name,
            claim_in_group=group.id,
        )
        page = await get_statement_page(uow, group.id, user_id=viewer.id, page=page_no)
        title, cb_prefix = "📋 Your statement", f"stmt:u:{subject_tg}"
    else:
        await callback.answer()
        return

    logger.debug("statements page: scope=%s group=%s page=%d", scope, group.id, page_no)
    try:
        await callback.message.edit_text(
            _render(page, title), reply_markup=_keyboard(page, cb_prefix)
        )
    except TelegramBadRequest:
        # "message is not modified" — e.g. a double-tap on the same page. Harmless.
        logger.debug("statements page edit skipped (not modified)")
    await callback.answer()
