"""Interactive, button-driven `/addexpense` wizard.

A *bare* ``/addexpense`` (no args) starts this guided flow instead of showing
inline usage; the one-liner grammar in ``addexpense.py`` is untouched and still
serves power users. The wizard collects the same fields and calls the same
``add_expense`` service path — it is a bot-layer *entry path* only.

Two platform facts drive the shape (see CLAUDE.md / the plan):

* **Group privacy mode** means the bot only receives commands, @mentions, and
  *replies to its own messages*. So every free-text step (amount, description, a
  per-person share) is prompted with ``ForceReply`` and the answer is matched
  back to the prompt's ``message_id``. Choice steps use inline keyboards, whose
  callbacks always arrive.
* **callback_data is 64 bytes**, too small for an expense draft, so the draft
  lives in aiogram FSM state (``MemoryStorage``) keyed by ``(chat, user)`` —
  which also isolates two users' concurrent wizards for free. Buttons reference
  roster members by **index** into the stored roster, never by UUID.

All steps after the amount edit a single **anchor** message in place (like
``/statements`` paging), keeping the group thread clean. Anchor buttons are
bound to the initiator and reject other users' taps.
"""

import logging
import uuid
from typing import Any

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    User,
)
from pydantic import ValidationError

from countbeans.bot.utils.context import resolve_chat_context
from countbeans.bot.utils.formatting import (
    VOID_HINT,
    coverage_gap_warning,
    display_name,
    format_expense_receipt,
    format_money,
    humanize_validation_error,
)
from countbeans.bot.utils.parsing import (
    extract_quoted_description,
    parse_amount_cents,
    parse_money,
)
from countbeans.dto.commands import AddExpenseCommand
from countbeans.dto.domain import MemberInfo
from countbeans.services.add_expense import add_expense
from countbeans.services.errors import DomainError
from countbeans.services.uow import UnitOfWork

logger = logging.getLogger(__name__)

router = Router()

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


class AddExpenseFlow(StatesGroup):
    amount = State()  # awaiting a ForceReply with the amount
    description = State()  # awaiting a ForceReply with the description
    participants = State()  # anchor showing the member-toggle roster
    split_mode = State()  # anchor showing the four split-mode buttons
    share_entry = State()  # anchor collecting per-person shares (non-equal)
    confirm = State()  # anchor showing the final preview


# ---------------------------------------------------------------------------
# Entry point (called from the bare-command branch of cmd_addexpense)
# ---------------------------------------------------------------------------


async def start_wizard(message: Message, state: FSMContext, uow: UnitOfWork) -> None:
    """Begin the guided flow: onboard the caller, seed the draft, and ask for the
    amount with a ForceReply. Everything else flows from the reply / button taps."""
    if message.from_user is None:
        return

    ctx = await resolve_chat_context(uow, message)
    payer, active = ctx.caller, ctx.active_event

    await state.set_state(AddExpenseFlow.amount)
    await state.set_data(
        {
            "initiator_id": message.from_user.id,
            "group_id": str(ctx.group.id),
            "payer_id": str(payer.id),
            "payer_username": payer.username,
            "payer_first_name": payer.first_name,
            "currency_default": ctx.currency,
            "active_event_id": str(active.id) if active else None,
            "active_event_name": active.name if active else None,
            "force_general": False,
            "split_mode": "equal",
            "shares": {},
            "page": 0,
        }
    )
    # Sent as a *reply* to /addexpense so ForceReply(selective=True) targets the
    # caller and the reply box pops (a plain send targets nobody). The prompt is
    # kept, but its id is tracked as the reply target the amount step accepts.
    prompt = await message.reply(
        "💰 New expense — how much?\n"
        "Reply to this message with the amount, optionally a description — e.g.\n"
        "    50.25 1 night at Domino's\n"
        "Prefix a currency to override: $50, €50, USD50. Or /cancel to abort.",
        reply_markup=ForceReply(selective=True),
    )
    await state.update_data(prompt_id=prompt.message_id)


# ---------------------------------------------------------------------------
# Free-text steps (ForceReply): amount, description, per-person share
# ---------------------------------------------------------------------------


# Free-text steps only fire on a *direct reply* to the bot's prompt, so ordinary
# group chatter (this bot can read all group messages) never interrupts the
# wizard. The handler then checks the reply targets the *current* prompt
# (_is_reply_to_prompt). The command exclusion lets /cancel & co. through; FSM
# state is keyed per (chat, user), so only the initiator's reply lands here.
_TEXT_INPUT = F.text & ~F.text.startswith("/") & F.reply_to_message


@router.message(AddExpenseFlow.amount, _TEXT_INPUT)
async def on_amount(
    message: Message, state: FSMContext, uow: UnitOfWork, bot: Bot
) -> None:
    data = await state.get_data()
    if not _is_reply_to_prompt(message, data):
        return
    text = (message.text or "").strip()
    tokens = text.split()
    if not tokens:
        await _amount_reprompt(message, state, "Send an amount like 25.50.")
        return
    try:
        currency, amount_cents = parse_money(tokens[0], data["currency_default"])
    except ValueError:
        await _amount_reprompt(
            message, state, "Invalid amount. Send a positive number like 25.50."
        )
        return
    # Anything after the amount is an optional description — `50.25 dinner at
    # Domino's`. The description can be edited later via the 📝 button.
    description = _description_from_rest(text[len(tokens[0]) :].strip())
    await state.update_data(
        amount_cents=amount_cents, currency=currency, description=description
    )
    # The opening prompt is kept, so the reply to it is kept too (symmetry); just
    # post the anchor below. Later steps (description/share) do drop the reply.
    await _open_participants(bot, message, state, uow)


@router.message(AddExpenseFlow.description, _TEXT_INPUT)
async def on_description(message: Message, state: FSMContext, bot: Bot) -> None:
    if not _is_reply_to_prompt(message, await state.get_data()):
        return
    await state.update_data(description=(message.text or "").strip() or None)
    await state.set_state(AddExpenseFlow.participants)
    await _drop_user_reply(bot, message)
    await _clear_prompt(bot, message.chat.id, state)
    await _resend_anchor(bot, message.chat.id, state)


@router.message(AddExpenseFlow.share_entry, _TEXT_INPUT)
async def on_share(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    if not _is_reply_to_prompt(message, data):
        return
    idx = data.get("pending_share_idx")
    if idx is None:
        return  # no member is awaiting a share — ignore stray text
    try:
        value = _parse_share((message.text or "").strip(), data["split_mode"])
    except ValueError as exc:
        # Re-ask the same member (pending_share_idx stays); drop the bad reply.
        await _drop_user_reply(bot, message)
        await _reask(bot, message, state, str(exc))
        return
    shares = dict(data.get("shares", {}))
    shares[str(idx)] = value
    await state.update_data(shares=shares, pending_share_idx=None)
    await _drop_user_reply(bot, message)
    await _clear_prompt(bot, message.chat.id, state)
    await _resend_anchor(bot, message.chat.id, state)


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


@router.message(Command("cancel"), StateFilter(AddExpenseFlow))
async def on_cancel(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    await state.clear()
    anchor_id = data.get("anchor_id")
    if anchor_id is not None:
        try:
            await bot.edit_message_text(
                text="✖ Expense cancelled.",
                chat_id=message.chat.id,
                message_id=anchor_id,
                reply_markup=None,
            )
        except TelegramBadRequest:
            pass
    await message.reply("Cancelled. Start again any time with /addexpense.")


# ---------------------------------------------------------------------------
# Button taps (callback queries) — one entry point, ownership-checked
# ---------------------------------------------------------------------------


@router.callback_query(F.data.startswith("ax:"))
async def on_wizard_action(
    callback: CallbackQuery, state: FSMContext, uow: UnitOfWork, bot: Bot
) -> None:
    if not isinstance(callback.message, Message):
        await callback.answer()
        return
    # FSM state is keyed by (chat, user), so the draft only exists under the
    # initiator's context — a different user tapping the anchor has no draft here.
    # Bind to the *anchor message* too, so a user who happens to have their own
    # draft can't drive someone else's anchor (no state filter on the handler, so
    # the off-owner gets a clear rejection rather than a silent dropped tap).
    data = await state.get_data()
    if "initiator_id" not in data or callback.message.message_id != data.get(
        "anchor_id"
    ):
        await callback.answer(
            "This isn't your expense draft — start your own with /addexpense.",
            show_alert=True,
        )
        return

    chat_id = callback.message.chat.id
    action = (callback.data or "")[len("ax:") :]

    if action == "x":
        await state.clear()
        await _edit_anchor(bot, chat_id, data, "✖ Expense cancelled.", None)
        await callback.answer("Cancelled")
        return

    if action == "desc":
        prompt = await bot.send_message(
            chat_id,
            f"{_mention_prefix(callback.from_user)}📝 Send a short description "
            "for this expense.",
            reply_markup=ForceReply(selective=True),
        )
        await state.update_data(prompt_id=prompt.message_id)
        await state.set_state(AddExpenseFlow.description)
        await callback.answer()
        return

    if action == "gen":
        if not data.get("active_event_id"):
            await callback.answer()
            return
        force = not data.get("force_general")
        await state.update_data(force_general=force)
        await _reload_roster(state, uow)
        await state.set_state(AddExpenseFlow.participants)
        await _repaint(bot, chat_id, state)
        await callback.answer(
            "Now logging to general." if force else "Back to the event."
        )
        return

    if action == "all":
        await _roster_repaint(
            callback,
            bot,
            chat_id,
            state,
            selected=list(range(len(data.get("roster", [])))),
        )
        return

    if action == "clear":
        await _roster_repaint(callback, bot, chat_id, state, selected=[])
        return

    if action.startswith("p:"):
        idx = _action_index(action, "p:")
        # Reject a malformed or out-of-range index from a crafted callback, so a
        # bad toggle can't seed `selected` with an index that later IndexErrors in
        # _submit / the renderers.
        if idx is None or not 0 <= idx < len(data.get("roster", [])):
            await callback.answer()
            return
        selected = list(data.get("selected", []))
        if idx in selected:
            selected.remove(idx)
        else:
            selected.append(idx)
        await _roster_repaint(callback, bot, chat_id, state, selected=sorted(selected))
        return

    if action.startswith("pg:"):
        page = _action_index(action, "pg:")
        if page is None or page < 0:
            await callback.answer()
            return
        await _roster_repaint(callback, bot, chat_id, state, page=page)
        return

    if action == "pdone":
        if not data.get("selected"):
            await callback.answer("Pick at least one person.", show_alert=True)
            return
        await state.set_state(AddExpenseFlow.split_mode)
        await _repaint(bot, chat_id, state)
        await callback.answer()
        return

    if action == "back":
        target = (
            AddExpenseFlow.participants
            if await state.get_state() == AddExpenseFlow.split_mode.state
            else AddExpenseFlow.split_mode
        )
        await state.set_state(target)
        await _repaint(bot, chat_id, state)
        await callback.answer()
        return

    if action.startswith("m:"):
        mode = action[2:]
        if mode not in _MODE_DISPLAY:
            await callback.answer()
            return
        await state.update_data(split_mode=mode, shares={})
        await state.set_state(
            AddExpenseFlow.confirm if mode == "equal" else AddExpenseFlow.share_entry
        )
        await _repaint(bot, chat_id, state)
        await callback.answer()
        return

    if action.startswith("s:"):
        idx = _action_index(action, "s:")
        # `idx in selected` also bounds it to a valid roster index (selected only
        # ever holds in-range indices — see the p: guard above).
        if idx is None or idx not in data.get("selected", []):
            await callback.answer()
            return
        member = data["roster"][idx]
        hint = _SHARE_HINTS[data["split_mode"]]
        prompt = await bot.send_message(
            chat_id,
            f"{_mention_prefix(callback.from_user)}Share for "
            f"{display_name(member['username'], member['first_name'])} "
            f"— send a number ({hint}).",
            reply_markup=ForceReply(selective=True),
        )
        await state.update_data(prompt_id=prompt.message_id, pending_share_idx=idx)
        await callback.answer()
        return

    if action == "ok":
        await _submit(callback, state, uow, bot, chat_id)
        return

    await callback.answer()


# ---------------------------------------------------------------------------
# Submit — reuse the unchanged add_expense service path
# ---------------------------------------------------------------------------


async def _submit(
    callback: CallbackQuery,
    state: FSMContext,
    uow: UnitOfWork,
    bot: Bot,
    chat_id: int,
) -> None:
    data = await state.get_data()
    # One write per draft: a double-tapped Confirm must not record the expense
    # twice in the append-only ledger (the slot is claimed before the write below).
    if data.get("submitting"):
        await callback.answer()
        return
    selected = data.get("selected", [])
    roster = data["roster"]
    if not selected:
        await callback.answer("Pick at least one person.", show_alert=True)
        return

    mode = data["split_mode"]
    amount_cents = data["amount_cents"]
    currency = data["currency"]
    shares = data.get("shares", {})

    if mode == "equal":
        split_params = None
    else:
        if any(str(i) not in shares for i in selected):
            await callback.answer("Enter a share for everyone first.", show_alert=True)
            return
        total = sum(shares[str(i)] for i in selected)
        if mode == "percent" and total != 100:
            await callback.answer(
                f"Percentages must total 100% — now {total}%.", show_alert=True
            )
            return
        if mode == "exact" and total != amount_cents:
            await callback.answer(
                f"Exact amounts must total {format_money(amount_cents, currency)}.",
                show_alert=True,
            )
            return
        split_params = {
            uuid.UUID(roster[i]["user_id"]): shares[str(i)] for i in selected
        }

    payer_id = uuid.UUID(data["payer_id"])
    event_id = _effective_event_id(data)
    participants = [uuid.UUID(roster[i]["user_id"]) for i in selected]

    # Claim the write slot. MemoryStorage's get_data/update_data touch no real I/O,
    # so nothing yields the event loop between the `submitting` guard read at the
    # top and this set — a double-tapped Confirm's second callback is scheduled only
    # once the first reaches `await add_expense`, by which point it sees the flag and
    # bails. Reset on the pre-write failure paths so the user can fix and retry.
    await state.update_data(submitting=True)
    try:
        cmd = AddExpenseCommand(
            group_id=uuid.UUID(data["group_id"]),
            payer_id=payer_id,
            amount_cents=amount_cents,
            currency=currency,
            description=data.get("description"),
            participants=participants,
            split_mode=mode,
            split_params=split_params,
            event_id=event_id,
            created_by=payer_id,
        )
    except ValidationError as exc:
        await state.update_data(submitting=False)
        await callback.answer(
            f"Invalid: {humanize_validation_error(exc)}", show_alert=True
        )
        return

    try:
        result = await add_expense(uow, cmd)
    except DomainError as exc:
        await state.update_data(submitting=False)
        await callback.answer(str(exc), show_alert=True)
        return

    await state.clear()

    sel_members = [_dict_to_member(roster[i]) for i in selected]
    lines = format_expense_receipt(
        scoped_event_name=(
            data.get("active_event_name") if event_id is not None else None
        ),
        description=data.get("description"),
        amount_cents=result.amount_cents,
        currency=result.currency,
        payer_username=data.get("payer_username"),
        payer_first_name=data.get("payer_first_name"),
        participants=sel_members,
        shares=result.shares,
        split_mode=mode,
    )
    # A subset that leaves the payer out is the everyday "I paid, forgot to add
    # myself" footgun — nudge (the expense is still recorded as chosen).
    if payer_id not in participants:
        lines.append(
            "\nℹ️ You're not included in this split. If you shared it too, re-run "
            "and tap yourself in."
        )
    # A #general override while an event is active: confirm it stayed general.
    if event_id is None and data.get("active_event_id"):
        lines.append(
            f'\nℹ️ Logged as general — not tagged to "{data.get("active_event_name")}".'
        )
    # Splitting the whole known group in general scope: warn if the bot can't see
    # everyone yet (mirrors the inline coverage check — independent of the override
    # nudge above, so a #general whole-group split gets both, exactly as inline).
    if event_id is None and len(selected) == len(roster):
        try:
            actual = await bot.get_chat_member_count(chat_id) - 1  # minus the bot
            if len(selected) < actual:
                lines.append(
                    coverage_gap_warning(len(selected), actual - len(selected))
                )
        except Exception:
            logger.warning(
                "could not fetch chat member count for %s", chat_id, exc_info=True
            )

    lines.append(f"\n{VOID_HINT}")
    await _edit_anchor(bot, chat_id, data, "\n".join(lines), None)
    await callback.answer("Added ✅")
    logger.info(
        "Expense recorded via wizard: expense_id=%s amount_cents=%d participants=%d",
        result.expense_id,
        result.amount_cents,
        len(participants),
    )


# ---------------------------------------------------------------------------
# Rendering — each step paints the one anchor message
# ---------------------------------------------------------------------------


def _summary_lines(data: dict[str, Any]) -> list[str]:
    lines = [
        "🧾 New expense",
        f"Amount: {format_money(data['amount_cents'], data['currency'])}",
    ]
    if data.get("description"):
        lines.append(f"For: {data['description']}")
    lines.append(f"Scope: {_scope_label(data)}")
    return lines


def _render_participants(data: dict[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
    selected = set(data.get("selected", []))
    lines = _summary_lines(data)
    lines.append(f"Split: {_MODE_DISPLAY[data['split_mode']]}")
    lines.append("")
    lines.append(f"Who's in? ({len(selected)} selected) — tap a name to toggle:")
    return "\n".join(lines), _participants_keyboard(data, selected)


def _participants_keyboard(
    data: dict[str, Any], selected: set[int]
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
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{mark} {display_name(member['username'], member['first_name'])}",
                    callback_data=f"ax:p:{idx}",
                )
            ]
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

    util = [InlineKeyboardButton(text="📝 Description", callback_data="ax:desc")]
    if data.get("active_event_id"):
        util.append(
            InlineKeyboardButton(
                text="📂 Use event" if data.get("force_general") else "📂 #general",
                callback_data="ax:gen",
            )
        )
    rows.append(util)

    rows.append(
        [
            InlineKeyboardButton(text="Split ▶", callback_data="ax:pdone"),
            InlineKeyboardButton(text="✖ Cancel", callback_data="ax:x"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _render_split_mode(data: dict[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
    lines = _summary_lines(data)
    lines.append("")
    lines.append(f"How should it split among {len(data.get('selected', []))}?")
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Equal", callback_data="ax:m:equal"),
                InlineKeyboardButton(text="Exact", callback_data="ax:m:exact"),
            ],
            [
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


def _render_share_entry(data: dict[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
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


def _render_confirm(data: dict[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
    mode = data["split_mode"]
    currency = data["currency"]
    selected = data.get("selected", [])
    roster = data["roster"]
    shares = data.get("shares", {})

    lines = [
        "🧾 Confirm expense",
        f"Amount: {format_money(data['amount_cents'], currency)}",
        f"For: {data['description']}" if data.get("description") else "For: —",
        f"Scope: {_scope_label(data)}",
        f"Paid by: {display_name(data.get('payer_username'), data.get('payer_first_name'))}",
        "",
    ]
    names = ", ".join(
        display_name(roster[i]["username"], roster[i]["first_name"]) for i in selected
    )
    if mode == "equal":
        lines.append(f"Split equally among {len(selected)}: {names}")
    else:
        lines.append(f"Split by {_MODE_DISPLAY[mode].lower()}:")
        for i in selected:
            member = roster[i]
            lines.append(
                f"  {display_name(member['username'], member['first_name'])}: "
                f"{_fmt_share(shares.get(str(i)), mode, currency)}"
            )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Confirm", callback_data="ax:ok")],
            [
                InlineKeyboardButton(text="◀ Back", callback_data="ax:back"),
                InlineKeyboardButton(text="✖ Cancel", callback_data="ax:x"),
            ],
        ]
    )
    return "\n".join(lines), kb


_RENDERERS = {
    AddExpenseFlow.participants.state: _render_participants,
    AddExpenseFlow.split_mode.state: _render_split_mode,
    AddExpenseFlow.share_entry.state: _render_share_entry,
    AddExpenseFlow.confirm.state: _render_confirm,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_reply_to_prompt(message: Message, data: dict[str, Any]) -> bool:
    """True only when the message replies to the bot's *current* prompt, so a reply
    to some other message (or a stale prompt) doesn't advance the step."""
    rtm = message.reply_to_message
    return rtm is not None and rtm.message_id == data.get("prompt_id")


def _action_index(action: str, prefix: str) -> int | None:
    """The integer index from a prefixed callback action (``p:3``, ``pg:2``,
    ``s:1``), or ``None`` when it's malformed. callback_data normally comes from
    bot-rendered buttons, but a crafted callback shouldn't raise — parse defensively
    and let the caller bounds-check."""
    try:
        return int(action[len(prefix) :])
    except ValueError:
        return None


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


def _description_from_rest(rest: str) -> str | None:
    """The optional description typed after the amount on the first line.

    Quotes are optional. A quoted run is honoured only when it wraps the *whole*
    remainder (``leftover`` empty), so an apostrophe (``domino's``), a stray
    ``@`` used as "at", or text like ``it's mike's`` is kept verbatim rather than
    mis-parsed as a partial quote. Returns ``None`` when nothing follows.
    """
    if not rest:
        return None
    quoted, leftover = extract_quoted_description(rest)
    if quoted is not None and not leftover.strip():
        return quoted
    return rest


async def _open_participants(
    bot: Bot, message: Message, state: FSMContext, uow: UnitOfWork
) -> None:
    """Leave the amount step: load the roster (everyone selected) and post the
    first anchor below the (kept) opening prompt. The opening prompt isn't deleted;
    we just stop tracking it as a reply target."""
    await _reload_roster(state, uow)
    await state.set_state(AddExpenseFlow.participants)
    await state.update_data(prompt_id=None)
    await _resend_anchor(bot, message.chat.id, state)


async def _reload_roster(state: FSMContext, uow: UnitOfWork) -> None:
    """(Re)load the roster for the current scope and default-select everyone.
    Called at first open and whenever the #general toggle swaps the scope."""
    data = await state.get_data()
    members = await _load_roster(uow, data)
    await state.update_data(
        roster=[_member_to_dict(m) for m in members],
        selected=list(range(len(members))),
        page=0,
    )


async def _load_roster(uow: UnitOfWork, data: dict[str, Any]) -> list[MemberInfo]:
    if _use_event_scope(data):
        return await uow.events.list_members(uuid.UUID(data["active_event_id"]))
    return await uow.group_members.list_members(uuid.UUID(data["group_id"]))


async def _roster_repaint(
    callback: CallbackQuery,
    bot: Bot,
    chat_id: int,
    state: FSMContext,
    **update: Any,
) -> None:
    """Apply a roster-screen edit (toggle / select-all / clear / page) and repaint
    the anchor in place. These buttons only render on the roster screen, so the
    state is re-asserted to ``participants`` defensively before the repaint."""
    await state.update_data(**update)
    await state.set_state(AddExpenseFlow.participants)
    await _repaint(bot, chat_id, state)
    await callback.answer()


async def _render_current(
    state: FSMContext,
) -> tuple[dict[str, Any], str, InlineKeyboardMarkup] | None:
    """Render the anchor for the current FSM state, or ``None`` when no renderer is
    registered. Returns the state data alongside the text/keyboard so callers that
    edit or resend the anchor don't re-read it."""
    data = await state.get_data()
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
    data: dict[str, Any],
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
    prompt_id = (await state.get_data()).get("prompt_id")
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


def _parse_share(raw: str, mode: str) -> int:
    """Parse a typed per-person share for the given split mode (raises ValueError
    with a user-facing message)."""
    if mode == "exact":
        try:
            return parse_amount_cents(raw)
        except ValueError:
            raise ValueError("Enter an amount like 30 or 30.50.") from None
    if not raw.isdigit() or int(raw) <= 0:
        unit = "a percentage like 60" if mode == "percent" else "a weight like 2"
        raise ValueError(f"Enter {unit}.")
    return int(raw)


def _is_reconciled(data: dict[str, Any]) -> bool:
    """Whether a non-equal split's shares add up, so Confirm can appear."""
    mode = data["split_mode"]
    selected = data.get("selected", [])
    shares = data.get("shares", {})
    if not selected or any(str(i) not in shares for i in selected):
        return False
    total = sum(shares[str(i)] for i in selected)
    if mode == "percent":
        return total == 100
    if mode == "exact":
        return total == data["amount_cents"]
    return total > 0  # weighted: any positive total (each share is > 0)


def _fmt_share(value: int | None, mode: str, currency: str) -> str:
    if value is None:
        return "—"
    if mode == "exact":
        return format_money(value, currency)
    return f"{value}%" if mode == "percent" else f"{value}x"


def _use_event_scope(data: dict[str, Any]) -> bool:
    """Whether this draft writes to the active event: there is one, and #general
    hasn't overridden it for this expense. The single source of the scope rule."""
    return bool(data.get("active_event_id")) and not data.get("force_general")


def _scope_label(data: dict[str, Any]) -> str:
    if _use_event_scope(data):
        return data.get("active_event_name") or "event"
    if data.get("active_event_id"):
        return "General (#general)"
    return "General"


def _effective_event_id(data: dict[str, Any]) -> uuid.UUID | None:
    if _use_event_scope(data):
        return uuid.UUID(data["active_event_id"])
    return None


def _member_to_dict(member: MemberInfo) -> dict[str, Any]:
    return {
        "user_id": str(member.user_id),
        "username": member.username,
        "first_name": member.first_name,
        "is_pending": member.is_pending,
    }


def _dict_to_member(data: dict[str, Any]) -> MemberInfo:
    return MemberInfo(
        user_id=uuid.UUID(data["user_id"]),
        username=data["username"],
        first_name=data["first_name"],
        is_pending=data["is_pending"],
    )
