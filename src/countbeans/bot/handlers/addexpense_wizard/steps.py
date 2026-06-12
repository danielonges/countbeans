"""The wizard's free-text steps (ForceReply): entry, amount, description, share.

**Group privacy mode** means the bot only receives commands, @mentions, and
*replies to its own messages*. So every free-text step is prompted with
``ForceReply`` and the answer is matched back to the prompt's ``message_id``;
choice steps use inline keyboards (see ``actions``), whose callbacks always
arrive.
"""

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import ForceReply, Message

from countbeans.bot.utils.context import resolve_chat_context
from countbeans.bot.utils.parsing import (
    extract_quoted_description,
    looks_like_money,
    parse_money,
)
from countbeans.services.uow import UnitOfWork

from .render import (
    _amount_reprompt,
    _clear_prompt,
    _drop_user_reply,
    _reask,
    _resend_anchor,
)
from .states import (
    AddExpenseFlow,
    WizardDraft,
    _parse_share,
    _reload_roster,
    get_draft,
)

router = Router()


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
    data = await get_draft(state)
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
    rest = text[len(tokens[0]) :].strip()
    if "roster" in data:
        # ✏️ Amount re-entry from the roster (the roster only exists once the
        # participants step has opened): update the money — and the description
        # only when new text follows the amount — keep the selection, and return
        # to the roster with the 📝 description step's chrome.
        await state.update_data(amount_cents=amount_cents, currency=currency)
        if rest:
            await state.update_data(description=_description_from_rest(rest))
        await state.set_state(AddExpenseFlow.participants)
        await _drop_user_reply(bot, message)
        await _clear_prompt(bot, message.chat.id, state)
        await _resend_anchor(bot, message.chat.id, state)
        return
    # Anything after the amount is an optional description — `50.25 dinner at
    # Domino's`. The description can be edited later via the 📝 button.
    description = _description_from_rest(rest)
    await state.update_data(
        amount_cents=amount_cents, currency=currency, description=description
    )
    # The opening prompt is kept, so the reply to it is kept too (symmetry); just
    # post the anchor below. Later steps (description/share) do drop the reply.
    await _open_participants(bot, message, state, uow)


@router.message(
    AddExpenseFlow.amount, F.text & ~F.text.startswith("/") & ~F.reply_to_message
)
async def on_amount_not_reply(message: Message) -> None:
    """The initiator sent the amount as a plain message instead of a reply (the
    ForceReply box was dismissed) — without this, the wizard's silence reads as
    a dead bot. Nudge only when the text parses as an amount: anything else is
    ordinary chatter they're having mid-wizard, and stays ignored."""
    tokens = (message.text or "").split()
    if not tokens or not looks_like_money(tokens[0]):
        return
    await message.reply(
        "↩️ I only catch replies — tap reply on my prompt above and send the "
        "amount again."
    )


@router.message(AddExpenseFlow.description, _TEXT_INPUT)
async def on_description(message: Message, state: FSMContext, bot: Bot) -> None:
    if not _is_reply_to_prompt(message, await get_draft(state)):
        return
    await state.update_data(description=(message.text or "").strip() or None)
    await state.set_state(AddExpenseFlow.participants)
    await _drop_user_reply(bot, message)
    await _clear_prompt(bot, message.chat.id, state)
    await _resend_anchor(bot, message.chat.id, state)


@router.message(AddExpenseFlow.share_entry, _TEXT_INPUT)
async def on_share(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await get_draft(state)
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
    data = await get_draft(state)
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
# Helpers
# ---------------------------------------------------------------------------


def _is_reply_to_prompt(message: Message, data: WizardDraft) -> bool:
    """True only when the message replies to the bot's *current* prompt, so a reply
    to some other message (or a stale prompt) doesn't advance the step."""
    rtm = message.reply_to_message
    return rtm is not None and rtm.message_id == data.get("prompt_id")


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
