"""Bot handler for the /settleup command.

Parses:
  /settleup @username [amount]        — you pay someone (self-settle)
  /settleup @from @to [amount]        — admin records a settlement for a pair
  /settleup @all                      — admin settles the whole group

"What you owe a person" is the amount of the suggested ``you -> them`` transfer
that ``/balance all`` shows (honoring the group's simplify toggle). Omitting the
amount auto-fills that suggested amount in the group's default currency; an
explicit amount may not exceed it (settlements only ever happen along a
suggested transfer, so balances can never flip). The cap and the
no-suggested-payment cases are enforced in the settle_up service.

The **two-handle form** (`@from @to`) lets a group admin record a settlement on
behalf of *two other people* — the way to clear a debt with a member who has
left the group (and so can never run `/settleup` themselves), or to log a
payment that happened offline. It's admin-gated like `@all`, since recording a
payment rewrites standing for people who aren't the caller; both handles must
resolve to someone already known, and `@all` is not accepted in either slot.

``@all`` is a reserved keyword, never a username: it records every suggested
transfer at once to zero the whole group, and is restricted to group admins.
A normal mention must resolve to *someone already known* — a typo'd handle is
rejected rather than spawning a stray placeholder.
"""

import logging
import re
import uuid

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from pydantic import ValidationError

from countbeans.bot.handlers.balance import (
    render_group_balances,
    render_personal_balance,
)
from countbeans.bot.utils.context import ChatContext, resolve_chat_context
from countbeans.bot.utils.formatting import (
    display_name,
    format_money,
    humanize_validation_error,
)
from countbeans.bot.utils.parsing import (
    extract_general_flag,
    is_all,
    parse_money,
)
from countbeans.bot.utils.permissions import is_admin
from countbeans.bot.utils.settle_buttons import (
    decode_id,
    encode_id,
    payment_buttons,
)
from countbeans.db.models import Event, Group, User
from countbeans.dto.commands import SettleUpCommand
from countbeans.dto.results import SettlementCreatedResult
from countbeans.services.balance import suggested_transfers
from countbeans.services.errors import DomainError
from countbeans.services.settlement import owed_by_currency, settle_all, settle_up
from countbeans.services.uow import UnitOfWork

logger = logging.getLogger(__name__)

router = Router()

# The command args (CommandObject strips "/settleup" and any "@botname"). The
# amount slot is a whole non-space token so it can carry a currency prefix
# (`EUR50`, `€50`, `$50`, `50`) — parse_money validates it, just like /addexpense.
#   @handle EUR12.50  |  @handle 12  |  @handle  (amount omitted → auto)  |  @all
_ARGS_RE = re.compile(r"^@([\w.]+)(?:\s+(\S+))?$")
# The two-handle (admin on-behalf) form, tried first since it is more specific:
#   @from @to EUR12.50  |  @from @to  (amount omitted → auto)
_PAIR_ARGS_RE = re.compile(r"^@([\w.]+)\s+@([\w.]+)(?:\s+(\S+))?$")

_USAGE = (
    "Usage: /settleup                      (see what you owe — tap to settle)\n"
    "       /settleup @username [amount]   (you pay someone)\n"
    "       /settleup @from @to [amount]   (admins — record a pair's payment)\n"
    "       /settleup @all                 (admins — settle the whole group)\n"
    "Example: /settleup @alice 25.50   (or @alice EUR25.50 for another currency)\n"
    "Omit the amount to settle the full outstanding debt.\n"
    "While an event is active, add #general to settle a general (non-event) debt "
    "instead (no /event pause needed)."
)


def _general_note(ctx: ChatContext) -> str:
    """Shown after a successful general override mid-event, so a deliberate
    escape-hatch use is visible (ctx.scope_note is empty in that case)."""
    return (
        f'\nℹ️ Recorded as general — not in "{ctx.active_event.name}".'
        if ctx.force_general and ctx.active_event is not None
        else ""
    )


@router.message(Command("settleup"))
async def cmd_settleup(
    message: Message, command: CommandObject, uow: UnitOfWork, bot: Bot
) -> None:
    if message.from_user is None:
        return

    args = (command.args or "").strip()
    # #general forces THIS settlement to general (no-event) scope even while an
    # event is active — the one-off counterpart to /event pause, with no mode flip
    # (CLAUDE.md "The #general write-scope override"). Strip it before the grammar
    # regexes (which anchor on the whole args string) try to match.
    args, force_general = extract_general_flag(args)

    # Active-event mode: settle within the active event's scope unless #general
    # overrode it for this command (writes are otherwise strictly active-scoped).
    # event_id, scope_note, and the currency fallback all key off ctx.scoped_event.
    ctx = (await resolve_chat_context(uow, message)).scoped(force_general=force_general)

    # Bare /settleup — the picker: the caller's suggested payments as buttons,
    # one tap each. The typed forms below stay as the accelerator (and the only
    # way to settle a partial amount).
    if not args:
        text, keyboard = await _picker_view(
            uow,
            ctx.group,
            ctx.caller.id,
            event=ctx.scoped_event,
            force_general=ctx.force_general,
        )
        await message.reply(text, reply_markup=keyboard)
        return

    # Two handles → admin records a settlement for a pair (e.g. a departed
    # member). Tried before the single-handle form, which it would also match.
    pair = _PAIR_ARGS_RE.match(args)
    if pair:
        await _settle_on_behalf(message, bot, uow, ctx, *pair.groups())
        return

    match = _ARGS_RE.match(args)
    if not match:
        await message.reply(_USAGE)
        return

    target_username = match.group(1)
    amount_str = match.group(2)  # None if omitted

    # @all — settle the whole scope at once. Admin-only and amount-less.
    if is_all(target_username):
        await _settle_whole_group(message, bot, uow, ctx)
        return

    from_user = ctx.caller

    # A normal mention must resolve to someone already known — never create a
    # placeholder here (a typo'd /settleup @foo would otherwise leave a stray).
    to_user = await uow.users.find_by_mention(target_username)
    if to_user is None:
        await message.reply(
            f"I don't know @{target_username} yet — they need to send a message "
            "or appear in an expense before you can settle with them."
        )
        return

    resolved = await _resolve_amount(
        message,
        uow,
        ctx,
        from_user=from_user,
        to_user=to_user,
        amount_str=amount_str,
        from_label="you",
        to_label=f"@{target_username}",
        retry_prefix=f"/settleup @{target_username}",
    )
    if resolved is None:
        return
    amount_cents, currency = resolved

    result = await _record(
        message,
        uow,
        ctx,
        from_user=from_user,
        to_user=to_user,
        amount_cents=amount_cents,
        currency=currency,
        created_by=from_user.id,
    )
    if result is None:
        return

    auto_note = " (full amount owed)" if amount_str is None else ""
    # Second person ("you paid …") states the direction the bare command doesn't:
    # this is the caller settling their own debt, threaded as a reply to them.
    await message.reply(
        f"Settled up{ctx.scope_note}: you paid "
        f"{display_name(to_user.username, to_user.first_name)} "
        f"{format_money(result.amount_cents, result.currency)}{auto_note}{_general_note(ctx)}"
    )
    logger.info(
        "Settlement recorded: settlement_id=%s from=%s to=%s amount_cents=%d currency=%s",
        result.settlement_id,
        result.from_user_id,
        result.to_user_id,
        result.amount_cents,
        result.currency,
    )


async def _settle_on_behalf(
    message: Message,
    bot: Bot,
    uow: UnitOfWork,
    ctx: ChatContext,
    from_handle: str,
    to_handle: str,
    amount_str: str | None,
) -> None:
    """/settleup @from @to [amount] — an admin records a settlement between two
    other members. The way to clear a debt with someone who has left the group
    (they can never run /settleup themselves) or to log an offline payment.

    Admin-gated (it rewrites standing for people who aren't the caller). Both
    handles must resolve to known users — no placeholders — and @all is rejected
    in either slot (use /settleup @all for the whole group)."""
    assert message.from_user is not None
    if not await is_admin(bot, message.chat.id, message.from_user.id):
        await message.reply(
            "Only group admins can record a settlement on behalf of others "
            "(/settleup @from @to [amount])."
        )
        return

    if is_all(from_handle) or is_all(to_handle):
        await message.reply(
            "Use /settleup @all to settle the whole group — the two-handle form "
            "is for a specific pair."
        )
        return

    from_user = await uow.users.find_by_mention(from_handle)
    if from_user is None:
        await message.reply(
            f"I don't know @{from_handle} yet — they need to send a message or "
            "appear in an expense first."
        )
        return
    to_user = await uow.users.find_by_mention(to_handle)
    if to_user is None:
        await message.reply(
            f"I don't know @{to_handle} yet — they need to send a message or "
            "appear in an expense first."
        )
        return
    if from_user.id == to_user.id:
        await message.reply("The payer and recipient must be different people.")
        return

    resolved = await _resolve_amount(
        message,
        uow,
        ctx,
        from_user=from_user,
        to_user=to_user,
        amount_str=amount_str,
        from_label=f"@{from_handle}",
        to_label=f"@{to_handle}",
        retry_prefix=f"/settleup @{from_handle} @{to_handle}",
    )
    if resolved is None:
        return
    amount_cents, currency = resolved

    # The recording admin is the author (created_by) — an audit trail of who
    # logged a payment they weren't a party to.
    result = await _record(
        message,
        uow,
        ctx,
        from_user=from_user,
        to_user=to_user,
        amount_cents=amount_cents,
        currency=currency,
        created_by=ctx.caller.id,
    )
    if result is None:
        return

    auto_note = " (full amount owed)" if amount_str is None else ""
    # The payment was logged without the recipient's own action, so ping them
    # (when they have a handle to notify) to confirm or flag it.
    confirm = (
        f"\n@{to_user.username} — flag it if that's not right."
        if to_user.username
        else ""
    )
    await message.reply(
        f"Recorded{ctx.scope_note}: {display_name(from_user.username, from_user.first_name)} paid "
        f"{display_name(to_user.username, to_user.first_name)} "
        f"{format_money(result.amount_cents, result.currency)}{auto_note}.{confirm}{_general_note(ctx)}"
    )
    logger.info(
        "On-behalf settlement by admin=%s: settlement_id=%s from=%s to=%s amount_cents=%d currency=%s",
        message.from_user.id,
        result.settlement_id,
        result.from_user_id,
        result.to_user_id,
        result.amount_cents,
        result.currency,
    )


async def _resolve_amount(
    message: Message,
    uow: UnitOfWork,
    ctx: ChatContext,
    *,
    from_user: User,
    to_user: User,
    amount_str: str | None,
    from_label: str,
    to_label: str,
    retry_prefix: str,
) -> tuple[int, str] | None:
    """Resolve the settlement ``(amount_cents, currency)``, or reply with an error
    and return None. An explicit amount is parsed with a currency-aware grammar
    (`EUR50`, `$50`, `50`) so a non-default-currency debt is settleable by typing;
    an omitted one auto-fills the full suggested from→to transfer in the scope's
    currency (the set /balance all shows). `from_label`/`to_label` adapt the copy
    to self vs. on-behalf; `retry_prefix` is the command minus the amount, so the
    wrong-currency error can hand back a ready-to-send correction."""
    if amount_str is not None:
        try:
            currency, cents = parse_money(amount_str, ctx.currency)
        except ValueError:
            await message.reply(
                "Invalid amount. Use a positive number, optionally with a "
                "currency — e.g. 25.50 or EUR25.50."
            )
            return None
        return cents, currency

    owed = await owed_by_currency(
        uow,
        ctx.group.id,
        from_user.id,
        to_user.id,
        simplify_debts=ctx.group.simplify_debts,
        event_id=ctx.event_id,
    )
    currency = ctx.currency
    if currency not in owed:
        others = [c for c in owed if c != currency]
        if others:
            detail = ", ".join(format_money(owed[c], c) for c in others)
            c0 = others[0]
            example = f"{retry_prefix} {c0}{owed[c0] // 100}.{owed[c0] % 100:02d}"
            await message.reply(
                f"The suggested settlement has {from_label} paying {to_label} in "
                f"{detail}, not {currency}. Settle in that currency — e.g. {example}"
            )
        else:
            await message.reply(
                f"The suggested settlement doesn't have {from_label} paying {to_label}. "
                "Run /balance all to see who to pay."
            )
        return None
    return owed[currency], currency


async def _record(
    message: Message,
    uow: UnitOfWork,
    ctx: ChatContext,
    *,
    from_user: User,
    to_user: User,
    amount_cents: int,
    currency: str,
    created_by: uuid.UUID,
) -> SettlementCreatedResult | None:
    """Build the command and record the settlement, replying with the error and
    returning None on a validation/ledger failure. ``currency`` is the resolved
    settlement currency (the scope default, or whatever the amount's prefix
    named). settle_up raises DomainError with a user-facing message when the
    settlement breaks a ledger rule (no suggested payment in that direction, or
    the amount exceeds what's owed in that currency)."""
    try:
        cmd = SettleUpCommand(
            group_id=ctx.group.id,
            from_user_id=from_user.id,
            to_user_id=to_user.id,
            amount_cents=amount_cents,
            currency=currency,
            event_id=ctx.event_id,
            created_by=created_by,
        )
    except ValidationError as exc:
        logger.warning("Invalid /settleup command: %s", exc)
        await message.reply(f"Invalid command: {humanize_validation_error(exc)}")
        return None

    try:
        return await settle_up(uow, cmd, simplify_debts=ctx.group.simplify_debts)
    except DomainError as exc:
        await message.reply(str(exc))
        return None


async def _settle_whole_group(
    message: Message, bot: Bot, uow: UnitOfWork, ctx: ChatContext
) -> None:
    """/settleup @all — record every suggested transfer to zero the scope.

    Admin-gated (the bot checks getChatMember, like /simplify): it rewrites
    everyone's standing, so it isn't a single member's call. Amount-less by
    definition — it settles whatever is outstanding. When an event is active it
    zeroes that event's scope, not the general ledger."""
    assert message.from_user is not None
    if not await is_admin(bot, message.chat.id, message.from_user.id):
        await message.reply(
            "Only group admins can settle up the whole group (/settleup @all)."
        )
        return

    results = await settle_all(
        uow,
        ctx.group.id,
        simplify_debts=ctx.group.simplify_debts,
        event_id=ctx.event_id,
    )
    if not results:
        await message.reply(
            f"Everyone's already settled up{ctx.scope_note} — nothing to record."
        )
        return

    ids = {r.from_user_id for r in results} | {r.to_user_id for r in results}
    names = await uow.balances.get_display_names(ids)

    def name(uid: uuid.UUID) -> str:
        username, first_name = names.get(uid, (None, None))
        return display_name(username, first_name)

    lines = [
        f"✅ Settled up the whole group{ctx.scope_note} — {len(results)} transfer(s) recorded:"
    ]
    for r in results:
        lines.append(
            f"  {name(r.from_user_id)} → {name(r.to_user_id)}: {format_money(r.amount_cents, r.currency)}"
        )
    await message.reply("\n".join(lines) + _general_note(ctx))
    logger.info(
        "Group settle-up by user=%s in group=%s: %d settlements",
        message.from_user.id,
        ctx.group.id,
        len(results),
    )


async def _picker_view(
    uow: UnitOfWork,
    group: Group,
    viewer_id: uuid.UUID,
    *,
    event: Event | None,
    force_general: bool,
) -> tuple[str, InlineKeyboardMarkup | None]:
    """The bare-/settleup picker: the viewer's suggested payments as tap-to-pay
    buttons (origin 'k'). Also repainted after a tap so settled rows vanish."""
    event_id = event.id if event else None
    scope_note = f' in "{event.name}"' if event else ""
    balances = await uow.balances.compute_for_group(group.id, event_id=event_id)
    mine = [
        t
        for t in suggested_transfers(balances, simplify_debts=group.simplify_debts)
        if t.from_user_id == viewer_id
    ]
    if not mine:
        return (
            f"✅ You're all settled up{scope_note} — you don't owe anyone right now.\n"
            "See everyone's balances with /balance all.",
            None,
        )
    names = await uow.balances.get_display_names({t.to_user_id for t in mine})
    rows = payment_buttons(
        mine, names, origin="k", force_general=force_general, viewer_is_payer=True
    )
    rows.append(
        [
            InlineKeyboardButton(
                text="✖ Close", callback_data=f"st:x:{encode_id(viewer_id)}"
            )
        ]
    )
    text = (
        f"💸 You have {len(mine)} payment(s) to make{scope_note} — tap one to "
        "record it as paid in full.\n"
        "(Partial payment? /settleup @user <amount>.)"
    )
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("st:"))
async def on_settle_tap(callback: CallbackQuery, uow: UnitOfWork) -> None:
    """Tap-to-settle: `st:p:<origin><scope>:<from>:<to>:<cur>` records the full
    suggested payment between that pair, `st:x:<owner>` closes the picker. Every
    pay button is bound to its *debtor* — the tapper must be the `from` user —
    so a button shown in the group can't move anyone else's money. The amount is
    re-derived at tap time ("settle in full" semantics, like the amount-less
    typed form), so a stale button can never overpay; if the suggestion is gone
    the tap alerts and the message repaints instead of writing."""
    parts = (callback.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""

    # Narrow to a live Message: an InaccessibleMessage (too old to edit) has no
    # edit_text, and there's nothing to repaint anyway.
    if not isinstance(callback.message, Message):
        await callback.answer()
        return

    chat = callback.message.chat
    group = await uow.groups.upsert(
        telegram_chat_id=chat.id, group_name=getattr(chat, "title", None)
    )
    tapper = await uow.users.upsert(
        telegram_user_id=callback.from_user.id,
        username=callback.from_user.username,
        first_name=callback.from_user.first_name,
        last_name=callback.from_user.last_name,
        claim_in_group=group.id,
    )

    if action == "x" and len(parts) == 3:
        try:
            owner_id = decode_id(parts[2])
        except ValueError:
            await callback.answer()
            return
        if tapper.id != owner_id:
            await callback.answer(
                "Only the person who ran /settleup can close this.", show_alert=True
            )
            return
        await callback.message.edit_text("Settle-up closed. Run /settleup any time.")
        await callback.answer()
        return

    if action != "p" or len(parts) != 6 or len(parts[2]) != 2:
        await callback.answer()
        return
    origin, scope_flag = parts[2][0], parts[2][1]
    force_general = scope_flag == "g"
    try:
        from_id = decode_id(parts[3])
        to_id = decode_id(parts[4])
    except ValueError:
        await callback.answer()
        return
    currency = parts[5]

    if tapper.id != from_id:
        await callback.answer(
            "This button records someone else's payment — only they can tap it. "
            "Run /settleup to see yours.",
            show_alert=True,
        )
        return

    # Scope at tap time: the group's current active event, unless the button was
    # minted under a #general override — then it stays general, as promised.
    active_event = (
        await uow.events.get(group.active_event_id) if group.active_event_id else None
    )
    event = None if force_general else active_event
    event_id = event.id if event else None
    scope_note = f' in "{event.name}"' if event else ""

    owed = await owed_by_currency(
        uow,
        group.id,
        from_id,
        to_id,
        simplify_debts=group.simplify_debts,
        event_id=event_id,
    )
    if currency not in owed:
        await callback.answer(
            "That payment is no longer suggested — balances have changed.",
            show_alert=True,
        )
        await _repaint_origin(
            callback.message, uow, group, origin, from_id, force_general, active_event
        )
        return

    try:
        cmd = SettleUpCommand(
            group_id=group.id,
            from_user_id=from_id,
            to_user_id=to_id,
            amount_cents=owed[currency],
            currency=currency,
            event_id=event_id,
            created_by=from_id,
        )
        result = await settle_up(uow, cmd, simplify_debts=group.simplify_debts)
    except ValidationError as exc:
        await callback.answer(humanize_validation_error(exc)[:200], show_alert=True)
        return
    except DomainError as exc:
        # The suggestion shifted between the owed read and the write — rare.
        await callback.answer(str(exc)[:200], show_alert=True)
        return

    names = await uow.balances.get_display_names({from_id, to_id})

    def name(uid: uuid.UUID) -> str:
        username, first_name = names.get(uid, (None, None))
        return display_name(username, first_name)

    general_note = (
        f'\nℹ️ Recorded as general — not in "{active_event.name}".'
        if force_general and active_event is not None
        else ""
    )
    # The settlement is a ledger event the group should see — a fresh message,
    # like a typed /settleup's reply; the tapped view repaints separately.
    await callback.message.answer(
        f"Settled up{scope_note}: {name(from_id)} paid {name(to_id)} "
        f"{format_money(result.amount_cents, result.currency)} (full amount owed)"
        f"{general_note}"
    )
    await _repaint_origin(
        callback.message, uow, group, origin, from_id, force_general, active_event
    )
    await callback.answer("Payment recorded ✅")
    logger.info(
        "Settlement recorded via tap: settlement_id=%s from=%s to=%s "
        "amount_cents=%d currency=%s",
        result.settlement_id,
        result.from_user_id,
        result.to_user_id,
        result.amount_cents,
        result.currency,
    )


async def _repaint_origin(
    message: Message,
    uow: UnitOfWork,
    group: Group,
    origin: str,
    viewer_id: uuid.UUID,
    force_general: bool,
    active_event: Event | None,
) -> None:
    """Repaint the message the tapped button lives on so its buttons reflect the
    post-settle state — a stale keyboard would alert on every further tap."""
    if origin == "a":
        text, keyboard = await render_group_balances(uow, group, active_event)
    elif origin == "m":
        text, keyboard = await render_personal_balance(
            uow, group, viewer_id, active_event
        )
    elif origin == "k":
        event = None if force_general else active_event
        text, keyboard = await _picker_view(
            uow, group, viewer_id, event=event, force_general=force_general
        )
    else:
        return
    try:
        await message.edit_text(text, reply_markup=keyboard)
    except TelegramBadRequest:
        # "message is not modified" — e.g. a stale tap after an identical repaint.
        logger.debug("settle repaint skipped (not modified)")
