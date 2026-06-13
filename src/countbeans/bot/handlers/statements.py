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
from typing import Literal

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    User,
)

from countbeans.bot.utils.context import resolve_chat_context
from countbeans.bot.utils.formatting import display_name, format_money
from countbeans.bot.utils.parsing import parse_view_selector
from countbeans.bot.utils.permissions import is_admin
from countbeans.bot.utils.settle_buttons import decode_id, encode_id
from countbeans.db.models import Group
from countbeans.dto.domain import StatementEntry, StatementPage
from countbeans.dto.results import VoidPreview
from countbeans.services.statements import get_statement_page
from countbeans.services.uow import UnitOfWork
from countbeans.services.void_expense import get_void_preview, may_void

logger = logging.getLogger(__name__)

router = Router()


def _entry_lines(e: StatementEntry) -> str:
    when = e.created_at.strftime("%b %d %H:%M")
    actor = display_name(e.actor_username, e.actor_first_name)
    # Scope tag — appended after "(voided)" so it never reads as "<event> (voided)".
    tag = f"  ·  🏷️ {e.event_name}" if e.event_name else ""
    if e.kind == "expense":
        head = f"🧾 {when} · {e.description or 'expense'} — {format_money(e.amount_cents, e.currency)}"
        if e.voided:
            head = f"❌ {head} (voided)"
        sub = f"    paid by {actor}"
        if e.participant_count:
            sub += f" · split {e.participant_count}-way"
        return f"{head}{tag}\n{sub}"
    other = display_name(e.counterparty_username, e.counterparty_first_name)
    head = f"💸 {when} · {actor} → {other}: {format_money(e.amount_cents, e.currency)}"
    if e.voided:
        head = f"❌ {head} (voided)"
    return f"{head}{tag}"


def _render(page: StatementPage, title: str) -> str:
    if page.total == 0:
        return f"{title}\n\nNo transactions yet."
    pages = math.ceil(page.total / page.page_size)
    header = f"{title}  (page {page.page + 1}/{pages}, {page.total} total)"
    body = "\n".join(_entry_lines(e) for e in page.entries)
    # Timestamps are server UTC — note it once so a traveller doesn't misread
    # "Jun 03 12:30" as local time.
    return f"{header}\n\n{body}\n\n🕓 Times are UTC."


def _scope_token(group_wide: bool, subject_tg: int) -> str:
    """Compact scope for the void callbacks (``sv:``): ``g`` (group) or
    ``u<tg>`` (one user's statement). Colon-free so it stays one callback field."""
    return "g" if group_wide else f"u{subject_tg}"


def _parse_scope(token: str) -> tuple[bool, int | None]:
    """Inverse of _scope_token: ``(group_wide, subject_tg)``."""
    if token == "g":
        return True, None
    return False, int(token[1:])  # "u<tg>"


def _keyboard(
    page: StatementPage, cb_prefix: str, scope_token: str
) -> InlineKeyboardMarkup | None:
    """Prev/Next only where they lead somewhere — Telegram has no disabled
    buttons, so an edge button is omitted rather than greyed out. A
    "🗑️ Void an entry" row appears whenever the page holds something still
    voidable; the void flow's `sv:` callbacks carry the scope + page so they can
    repaint this exact view afterwards."""
    pages = math.ceil(page.total / page.page_size) if page.total else 1
    rows: list[list[InlineKeyboardButton]] = []
    nav: list[InlineKeyboardButton] = []
    if page.page > 0:
        nav.append(
            InlineKeyboardButton(
                text="◀ Prev", callback_data=f"{cb_prefix}:{page.page - 1}"
            )
        )
    if page.page < pages - 1:
        nav.append(
            InlineKeyboardButton(
                text="Next ▶", callback_data=f"{cb_prefix}:{page.page + 1}"
            )
        )
    if nav:
        rows.append(nav)
    if any(not e.voided for e in page.entries):
        rows.append(
            [
                InlineKeyboardButton(
                    text="🗑️ Void an entry",
                    callback_data=f"sv:m:{scope_token}:{page.page}",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


@router.message(Command("statements"))
async def cmd_statements(
    message: Message, command: CommandObject, uow: UnitOfWork
) -> None:
    if message.from_user is None:
        return

    args = (command.args or "").split()
    group_wide, unrecognized = parse_view_selector(args)

    ctx = await resolve_chat_context(uow, message)
    group, caller = ctx.group, ctx.caller

    if group_wide:
        page = await get_statement_page(uow, group.id, page=0)
        title, cb_prefix = "📋 Group statement", "stmt:g"
        scope_token = _scope_token(True, 0)
    else:
        page = await get_statement_page(uow, group.id, user_id=caller.id, page=0)
        title, cb_prefix = "📋 Your statement", f"stmt:u:{message.from_user.id}"
        scope_token = _scope_token(False, message.from_user.id)

    text = _render(page, title)
    # Forgiving but not silent (theme T4): an unrecognized arg still shows the
    # personal statement, with a note rather than a silent swallow.
    if unrecognized is not None:
        text = (
            f'ℹ️ I didn\'t recognize "{unrecognized}" — showing your own '
            "statement. Use /statements all for the whole group.\n\n" + text
        )
    await message.reply(text, reply_markup=_keyboard(page, cb_prefix, scope_token))
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
        scope_token = _scope_token(True, 0)
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
        scope_token = _scope_token(False, subject_tg)
    else:
        await callback.answer()
        return

    logger.debug("statements page: scope=%s group=%s page=%d", scope, group.id, page_no)
    try:
        await callback.message.edit_text(
            _render(page, title), reply_markup=_keyboard(page, cb_prefix, scope_token)
        )
    except TelegramBadRequest:
        # "message is not modified" — e.g. a double-tap on the same page. Harmless.
        logger.debug("statements page edit skipped (not modified)")
    await callback.answer()


# ── void-from-statement: pick an entry, confirm, then void.py's vd: voids it ──
#
# `sv:m:<scope>:<page>` enters pick mode (each voidable entry becomes a button),
# `sv:k:<kind><id>:<scope>:<page>` shows a permission-aware confirm, and
# `sv:c:<scope>:<page>` returns to the statement. The confirm's "Yes" reuses
# void.py's `vd:ok:<kind>:<hex>:<tg>` so the actual void + result message goes
# through the one void path; only the *entry point* lives here.


def _title(group_wide: bool) -> str:
    return "📋 Group statement" if group_wide else "📋 Your statement"


def _short(e: StatementEntry) -> str:
    money = format_money(e.amount_cents, e.currency)
    if e.kind == "expense":
        return f"{e.description or 'expense'} — {money}"
    other = display_name(e.counterparty_username, e.counterparty_first_name)
    return f"→ {other} {money}"


def _owner_ok(from_user: User, group_wide: bool, subject_tg: int | None) -> bool:
    """A personal statement's void actions are owner-bound (like its paging); the
    group statement is open (the confirm's permission check still gates the void)."""
    return group_wide or from_user.id == subject_tg


async def _load_page(
    uow: UnitOfWork, group: Group, group_wide: bool, from_user: User, page_no: int
) -> StatementPage:
    if group_wide:
        return await get_statement_page(uow, group.id, page=page_no)
    viewer = await uow.users.upsert(
        telegram_user_id=from_user.id,
        username=from_user.username,
        first_name=from_user.first_name,
        last_name=from_user.last_name,
        claim_in_group=group.id,
    )
    return await get_statement_page(uow, group.id, user_id=viewer.id, page=page_no)


async def _paint_statement(
    message: Message,
    uow: UnitOfWork,
    group: Group,
    group_wide: bool,
    from_user: User,
    page_no: int,
) -> None:
    """Repaint the normal statement view (used to leave pick/confirm mode)."""
    page = await _load_page(uow, group, group_wide, from_user, page_no)
    prefix = "stmt:g" if group_wide else f"stmt:u:{from_user.id}"
    token = _scope_token(group_wide, from_user.id)
    try:
        await message.edit_text(
            _render(page, _title(group_wide)),
            reply_markup=_keyboard(page, prefix, token),
        )
    except TelegramBadRequest:
        logger.debug("statement repaint skipped (not modified)")


def _blockers(preview: VoidPreview, name) -> str:
    if preview.kind == "expense":
        who = name(preview.actor_id)
        if preview.created_by is not None and preview.created_by != preview.actor_id:
            who += f" or {name(preview.created_by)}"
    else:
        assert preview.counterparty_id is not None
        who = f"{name(preview.actor_id)} or {name(preview.counterparty_id)}"
    return f"Only {who} (or a group admin) can void this one."


@router.callback_query(F.data.startswith("sv:"))
async def on_statement_void(callback: CallbackQuery, uow: UnitOfWork, bot: Bot) -> None:
    parts = (callback.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    if not isinstance(callback.message, Message):
        await callback.answer()
        return
    chat = callback.message.chat
    group = await uow.groups.upsert(
        telegram_chat_id=chat.id, group_name=getattr(chat, "title", None)
    )

    if action == "m" and len(parts) == 4:
        scope_token, page_no = parts[2], int(parts[3])
        group_wide, subject_tg = _parse_scope(scope_token)
        if not _owner_ok(callback.from_user, group_wide, subject_tg):
            await callback.answer(
                "That's not your statement — run /statements me.", show_alert=True
            )
            return
        page = await _load_page(uow, group, group_wide, callback.from_user, page_no)
        rows = [
            [
                InlineKeyboardButton(
                    text=f"🗑️ {_short(e)}"[:60],
                    callback_data=(
                        f"sv:k:{'e' if e.kind == 'expense' else 's'}"
                        f"{encode_id(e.entry_id)}:{scope_token}:{page_no}"
                    ),
                )
            ]
            for e in page.entries
            if not e.voided
        ]
        rows.append(
            [
                InlineKeyboardButton(
                    text="✖ Cancel", callback_data=f"sv:c:{scope_token}:{page_no}"
                )
            ]
        )
        text = _render(page, _title(group_wide)) + "\n\n🗑️ Tap an entry to void it."
        try:
            await callback.message.edit_text(
                text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
            )
        except TelegramBadRequest:
            logger.debug("void pick repaint skipped (not modified)")
        await callback.answer()
        return

    if action == "c" and len(parts) == 4:
        scope_token, page_no = parts[2], int(parts[3])
        group_wide, subject_tg = _parse_scope(scope_token)
        if not _owner_ok(callback.from_user, group_wide, subject_tg):
            await callback.answer()
            return
        await _paint_statement(
            callback.message, uow, group, group_wide, callback.from_user, page_no
        )
        await callback.answer()
        return

    if action == "k" and len(parts) == 5:
        payload, scope_token, page_no = parts[2], parts[3], int(parts[4])
        group_wide, subject_tg = _parse_scope(scope_token)
        if not _owner_ok(callback.from_user, group_wide, subject_tg):
            await callback.answer(
                "That's not your statement — run /statements me.", show_alert=True
            )
            return
        kind: Literal["expense", "settlement"] | None = (
            "expense"
            if payload[:1] == "e"
            else "settlement" if payload[:1] == "s" else None
        )
        if kind is None:
            await callback.answer()
            return
        try:
            entry_id = decode_id(payload[1:])
        except ValueError:
            await callback.answer()
            return
        preview = await get_void_preview(uow, group.id, kind, entry_id)
        if preview is None:
            await callback.answer(
                "That entry is already voided or gone.", show_alert=True
            )
            await _paint_statement(
                callback.message, uow, group, group_wide, callback.from_user, page_no
            )
            return

        caller = await uow.users.upsert(
            telegram_user_id=callback.from_user.id,
            username=callback.from_user.username,
            first_name=callback.from_user.first_name,
            last_name=callback.from_user.last_name,
            claim_in_group=group.id,
        )
        caller_is_admin = await is_admin(bot, chat.id, callback.from_user.id)
        permitted = may_void(preview, caller.id, allow_any=caller_is_admin)

        ids = {preview.actor_id}
        if preview.counterparty_id is not None:
            ids.add(preview.counterparty_id)
        if preview.created_by is not None:
            ids.add(preview.created_by)
        names = await uow.balances.get_display_names(ids)

        def name(uid):
            u, f = names.get(uid, (None, None))
            return display_name(u, f)

        money = format_money(preview.amount_cents, preview.currency)
        when = preview.created_at.strftime("%b %d %H:%M")
        if preview.kind == "expense":
            lines = [
                "🗑️ Void this expense?",
                f"{preview.description or 'expense'} — {money} · "
                f"paid by {name(preview.actor_id)} · {when}",
            ]
        else:
            lines = [
                "🗑️ Void this settlement?",
                f"{name(preview.actor_id)} → {name(preview.counterparty_id)}: "
                f"{money} · {when}",
            ]
        keep = InlineKeyboardButton(
            text="✖ Keep it", callback_data=f"sv:c:{scope_token}:{page_no}"
        )
        if permitted:
            decide = [
                InlineKeyboardButton(
                    text="🗑️ Yes, void it",
                    callback_data=(
                        f"vd:ok:{'e' if preview.kind == 'expense' else 's'}:"
                        f"{preview.entry_id.hex}:{callback.from_user.id}"
                    ),
                ),
                keep,
            ]
        else:
            lines.append(_blockers(preview, name))
            decide = [keep]
        lines.append(
            "It stays in /statements (marked voided); balances stop counting it."
        )
        try:
            await callback.message.edit_text(
                "\n".join(lines),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[decide]),
            )
        except TelegramBadRequest:
            logger.debug("void confirm repaint skipped (not modified)")
        await callback.answer()
        return

    await callback.answer()
