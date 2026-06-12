"""Rendering for the wizard — each step paints the one anchor message.

All steps after the amount edit a single **anchor** message in place (like
``/statements`` paging), keeping the group thread clean. The ForceReply prompt
chrome (ask / re-ask / clear) and the best-effort deletion helpers live here
too, so the free-text steps and the button actions share one painting
vocabulary.
"""

import logging
import re

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    User,
)

from countbeans.bot.utils.formatting import display_name, format_money
from countbeans.bot.utils.parsing import GENERAL_KEYWORD

from .states import AddExpenseFlow, WizardDraft, _is_reconciled, _scope_label, get_draft

logger = logging.getLogger(__name__)

# How many member toggles per roster page (Telegram has no scrolling keyboards,
# so a long roster pages like /statements does).
_PAGE_SIZE = 8

_MODE_DISPLAY = {
    "equal": "Equal",
    "exact": "Exact amounts",
    "percent": "Percentages",
    "weighted": "Weights",
}

# The example shown in the per-person share prompt, keyed by split mode.
_SHARE_HINTS = {
    "exact": "e.g. 30 or 30.50",
    "percent": "e.g. 60 for 60%",
    "weighted": "e.g. 2 for 2x",
}


def _fmt_share(value: int | None, mode: str, currency: str) -> str:
    if value is None:
        return "—"
    if mode == "exact":
        return format_money(value, currency)
    return f"{value}%" if mode == "percent" else f"{value}x"


def _summary_lines(data: WizardDraft) -> list[str]:
    lines = [
        "🧾 New expense",
        f"Amount: {format_money(data['amount_cents'], data['currency'])}",
    ]
    if data.get("description"):
        lines.append(f"For: {data['description']}")
    lines.append(f"Scope: {_scope_label(data)}")
    return lines


def _render_participants(data: WizardDraft) -> tuple[str, InlineKeyboardMarkup]:
    # No "Split:" line here — the roster screen doesn't own a mode; the two
    # commit buttons below say what each tap does.
    selected = set(data.get("selected", []))
    lines = _summary_lines(data)
    lines.append("")
    lines.append(f"Who's in? ({len(selected)} selected) — tap a name to toggle:")
    return "\n".join(lines), _participants_keyboard(data, selected)


def _participants_keyboard(
    data: WizardDraft, selected: set[int]
) -> InlineKeyboardMarkup:
    roster = data.get("roster", [])
    page = data.get("page", 0)
    total = len(roster)
    start = page * _PAGE_SIZE
    end = min(start + _PAGE_SIZE, total)

    rows: list[list[InlineKeyboardButton]] = []
    for idx in range(start, end):
        member = roster[idx]
        mark = "✅" if idx in selected else "⬜"
        name = display_name(member["username"], member["first_name"])
        if member["is_pending"]:
            # A placeholder (mentioned but never seen) — flag it where people
            # are chosen, so a typo'd handle is caught at the decision point.
            name += " ⏳"
        rows.append(
            [InlineKeyboardButton(text=f"{mark} {name}", callback_data=f"ax:p:{idx}")]
        )

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀", callback_data=f"ax:pg:{page - 1}"))
    if end < total:
        nav.append(InlineKeyboardButton(text="▶", callback_data=f"ax:pg:{page + 1}"))
    if nav:
        rows.append(nav)

    rows.append(
        [
            InlineKeyboardButton(text="Everyone", callback_data="ax:all"),
            InlineKeyboardButton(text="Clear", callback_data="ax:clear"),
        ]
    )

    rows.append(
        [
            InlineKeyboardButton(text="✏️ Amount", callback_data="ax:amt"),
            InlineKeyboardButton(text="📝 Description", callback_data="ax:desc"),
        ]
    )
    if data.get("active_event_id"):
        # Plain words, not the #general keyword — the button must make sense to
        # someone who has never read the grammar (the keyword stays in the
        # one-liner, the tip, and /event info).
        event_name = data.get("active_event_name") or "the event"
        rows.append(
            [
                InlineKeyboardButton(
                    text=(
                        f"📂 Tag to {event_name}"
                        if data.get("force_general")
                        else f"📂 Don't tag to {event_name}"
                    ),
                    callback_data="ax:gen",
                )
            ]
        )

    # The fast path: an equal split commits straight from this screen — the
    # anchor above already previews the draft, and /void is the undo, exactly
    # like the inline one-liner. The mode screen is only for uneven splits.
    rows.append(
        [InlineKeyboardButton(text="✅ Add — split equally", callback_data="ax:eq")]
    )
    rows.append(
        [
            InlineKeyboardButton(text="Uneven split ▶", callback_data="ax:pdone"),
            InlineKeyboardButton(text="✖ Cancel", callback_data="ax:x"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _render_split_mode(data: WizardDraft) -> tuple[str, InlineKeyboardMarkup]:
    # Equal isn't offered here — it commits straight from the roster screen.
    lines = _summary_lines(data)
    lines.append("")
    lines.append(f"How should it split among {len(data.get('selected', []))}?")
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Exact", callback_data="ax:m:exact"),
                InlineKeyboardButton(text="Percent", callback_data="ax:m:percent"),
                InlineKeyboardButton(text="Weight", callback_data="ax:m:weighted"),
            ],
            [
                InlineKeyboardButton(text="◀ Back", callback_data="ax:back"),
                InlineKeyboardButton(text="✖ Cancel", callback_data="ax:x"),
            ],
        ]
    )
    return "\n".join(lines), kb


def _render_share_entry(data: WizardDraft) -> tuple[str, InlineKeyboardMarkup]:
    mode = data["split_mode"]
    currency = data["currency"]
    selected = data.get("selected", [])
    roster = data["roster"]
    shares = data.get("shares", {})

    lines = _summary_lines(data)
    lines.append(f"Split: {_MODE_DISPLAY[mode]}")
    total = sum(shares.get(str(i), 0) for i in selected)
    if mode == "percent":
        lines.append(f"Assigned: {total}% / 100%")
    elif mode == "exact":
        lines.append(
            f"Assigned: {format_money(total, currency)} / "
            f"{format_money(data['amount_cents'], currency)}"
        )
    else:
        lines.append(f"Total weight: {total}")
    lines.append("")
    lines.append("Tap a name to set their share:")

    rows: list[list[InlineKeyboardButton]] = []
    for i in selected:
        member = roster[i]
        val = _fmt_share(shares.get(str(i)), mode, currency)
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{display_name(member['username'], member['first_name'])}: {val}",
                    callback_data=f"ax:s:{i}",
                )
            ]
        )
    if _is_reconciled(data):
        rows.append([InlineKeyboardButton(text="✅ Confirm", callback_data="ax:ok")])
    rows.append(
        [
            InlineKeyboardButton(text="◀ Back", callback_data="ax:back"),
            InlineKeyboardButton(text="✖ Cancel", callback_data="ax:x"),
        ]
    )
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


_RENDERERS = {
    AddExpenseFlow.participants.state: _render_participants,
    AddExpenseFlow.split_mode.state: _render_split_mode,
    AddExpenseFlow.share_entry.state: _render_share_entry,
}


# ---------------------------------------------------------------------------
# The one-liner teaching tip (appended to the wizard receipt)
# ---------------------------------------------------------------------------

# Characters in a description the one-liner grammar could mis-parse — mention
# markers, the #-flag namespace, any quote opener/closer, the escape char, a
# newline. The tip is skipped rather than risk teaching a command that wouldn't
# round-trip.
_TIP_UNSAFE = re.compile(r"[@#\\\"'`«»“”‘’\n]")


def _fmt_plain_amount(cents: int) -> str:
    """An amount as the user would type it: ``50`` or ``50.25`` (no currency)."""
    return f"{cents // 100}.{cents % 100:02d}" if cents % 100 else str(cents // 100)


def _one_liner_tip(data: WizardDraft) -> str | None:
    """The one-liner equivalent of a submitted draft, or ``None`` when it can't
    be reconstructed faithfully (uneven split, a selected member without a
    public @username, a description the grammar would mis-parse).

    Appended to the wizard receipt as a teaching footer: the wizard is the
    recognition path and the one-liner the recall path — showing the exact
    command the wizard just performed is how users graduate from taps to the
    one-message fast path.
    """
    if data["split_mode"] != "equal":
        return None
    description = data.get("description")
    if description and _TIP_UNSAFE.search(description):
        return None
    amount = _fmt_plain_amount(data["amount_cents"])
    if data["currency"] != data["currency_default"]:
        amount = f"{data['currency']}{amount}"
    parts = ["/addexpense", amount]
    if description:
        parts.append(description)
    selected = data.get("selected", [])
    roster = data.get("roster", [])
    # Everyone selected ⇄ no mentions (the inline "split everyone" default);
    # a subset is spelled out as @handles.
    if len(selected) != len(roster):
        for i in selected:
            handle = roster[i]["username"]
            if handle is None:
                return None
            parts.append(f"@{handle}")
    if data.get("force_general") and data.get("active_event_id"):
        parts.append(f"#{GENERAL_KEYWORD}")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Prompt & anchor chrome
# ---------------------------------------------------------------------------


def _mention_prefix(user: User | None) -> str:
    """A leading @mention so ForceReply(selective=True) targets the initiator and
    the reply box pops on a callback-triggered prompt (which has no message to
    reply to). Empty when they have no public @username — then the box won't
    auto-pop and they swipe-reply (the opening/re-ask prompts pop via reply)."""
    return f"@{user.username} " if user and user.username else ""


async def _ask(message: Message, text: str) -> Message:
    """Send a ForceReply prompt as a *reply* to the user's message, so
    ForceReply(selective=True) targets them and the box pops. Returns the sent
    message so a caller can record its id for deletion."""
    return await message.reply(
        f"{text} Or send /cancel to abort.", reply_markup=ForceReply(selective=True)
    )


async def _amount_reprompt(message: Message, state: FSMContext, text: str) -> None:
    """Re-ask for the amount, making the fresh prompt the new reply target. Nothing
    is deleted — the kept opening prompt stays, and the ForceReply box points the
    user at this latest ask."""
    sent = await _ask(message, text)
    await state.update_data(prompt_id=sent.message_id)


async def _reask(bot: Bot, message: Message, state: FSMContext, text: str) -> None:
    """Re-ask after bad input on the share step, whose reply the caller has already
    dropped. We can't `message.reply` to a deleted message, so target the user with
    an @mention (as the share prompt itself does) to pop the box; replaces the live
    prompt."""
    await _clear_prompt(bot, message.chat.id, state)
    prompt = await bot.send_message(
        message.chat.id,
        f"{_mention_prefix(message.from_user)}{text} Or send /cancel to abort.",
        reply_markup=ForceReply(selective=True),
    )
    await state.update_data(prompt_id=prompt.message_id)


async def _render_current(
    state: FSMContext,
) -> tuple[WizardDraft, str, InlineKeyboardMarkup] | None:
    """Render the anchor for the current FSM state, or ``None`` when no renderer is
    registered. Returns the state data alongside the text/keyboard so callers that
    edit or resend the anchor don't re-read it."""
    data = await get_draft(state)
    renderer = _RENDERERS.get(await state.get_state() or "")
    if renderer is None:
        return None
    text, kb = renderer(data)
    return data, text, kb


async def _repaint(bot: Bot, chat_id: int, state: FSMContext) -> None:
    rendered = await _render_current(state)
    if rendered is None:
        return
    data, text, kb = rendered
    await _edit_anchor(bot, chat_id, data, text, kb)


async def _edit_anchor(
    bot: Bot,
    chat_id: int,
    data: WizardDraft,
    text: str,
    kb: InlineKeyboardMarkup | None,
) -> None:
    anchor_id = data.get("anchor_id")
    if anchor_id is None:
        return
    try:
        await bot.edit_message_text(
            text=text, chat_id=chat_id, message_id=anchor_id, reply_markup=kb
        )
    except TelegramBadRequest:
        # "message is not modified" (e.g. a re-tap on the same toggle). Harmless.
        logger.debug("wizard anchor edit skipped (not modified)")


async def _safe_delete(bot: Bot, chat_id: int, message_id: int) -> None:
    """Delete a message, ignoring failures. Own messages always delete; deleting a
    *user's* message needs the bot's 'Delete messages' admin right and raises
    TelegramBadRequest without it — best-effort, so a clean thread degrades to a
    tidy-enough one rather than an error."""
    try:
        await bot.delete_message(chat_id, message_id)
    except TelegramBadRequest:
        logger.debug("wizard message delete skipped (%s)", message_id)


async def _drop_user_reply(bot: Bot, message: Message) -> None:
    """Best-effort remove the user's own reply, so a processed step leaves only the
    refreshed anchor (needs the bot's 'Delete messages' right; skipped otherwise)."""
    await _safe_delete(bot, message.chat.id, message.message_id)


async def _clear_prompt(bot: Bot, chat_id: int, state: FSMContext) -> None:
    """Remove the ForceReply prompt the user just answered (the bot's own message)."""
    prompt_id = (await get_draft(state)).get("prompt_id")
    if prompt_id is not None:
        await _safe_delete(bot, chat_id, prompt_id)
        await state.update_data(prompt_id=None)


async def _resend_anchor(bot: Bot, chat_id: int, state: FSMContext) -> None:
    """Replace the anchor with a fresh copy at the bottom of the chat.

    Used after a *reply* step (amount / description / share) so the update lands
    as a NEW message the user can't miss, rather than a silent in-place edit
    above their reply. Button steps still edit in place (see ``_repaint``) so the
    keyboard doesn't jump on every tap. Only the bot's own messages are deleted —
    the user's reply is left untouched.
    """
    rendered = await _render_current(state)
    if rendered is None:
        return
    data, text, kb = rendered
    old_anchor = data.get("anchor_id")
    if old_anchor is not None:
        await _safe_delete(bot, chat_id, old_anchor)
    sent = await bot.send_message(chat_id, text, reply_markup=kb)
    await state.update_data(anchor_id=sent.message_id)
