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

from aiogram import Bot, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from countbeans.bot.utils.formatting import display_name, format_money
from countbeans.bot.utils.parsing import is_all, parse_amount_cents
from countbeans.bot.utils.permissions import is_admin
from countbeans.db.models import Group, User
from countbeans.dto.commands import SettleUpCommand
from countbeans.dto.results import SettlementCreatedResult
from countbeans.services.settlement import owed_by_currency, settle_all, settle_up
from countbeans.services.uow import UnitOfWork

logger = logging.getLogger(__name__)

router = Router()

# The command args (CommandObject strips "/settleup" and any "@botname"):
#   @handle 12.50  |  @handle 12  |  @handle  (amount omitted → auto)  |  @all
_ARGS_RE = re.compile(r"^@([\w.]+)(?:\s+(\d+(?:\.\d{1,2})?))?$")
# The two-handle (admin on-behalf) form, tried first since it is more specific:
#   @from @to 12.50  |  @from @to  (amount omitted → auto)
_PAIR_ARGS_RE = re.compile(r"^@([\w.]+)\s+@([\w.]+)(?:\s+(\d+(?:\.\d{1,2})?))?$")

_USAGE = (
    "Usage: /settleup @username [amount]   (you pay someone)\n"
    "       /settleup @from @to [amount]   (admins — record a pair's payment)\n"
    "       /settleup @all                 (admins — settle the whole group)\n"
    "Example: /settleup @alice 25.50\n"
    "Omit the amount to settle the full outstanding debt."
)


@router.message(Command("settleup"))
async def cmd_settleup(
    message: Message, command: CommandObject, uow: UnitOfWork, bot: Bot
) -> None:
    if message.from_user is None:
        return

    args = (command.args or "").strip()

    group = await uow.groups.upsert(
        telegram_chat_id=message.chat.id,
        group_name=getattr(message.chat, "title", None),
    )

    # Active-event mode: settle within the active event's scope (writes are
    # strictly active-scoped — /event pause to settle a general debt). CLAUDE.md.
    active = (
        await uow.events.get(group.active_event_id) if group.active_event_id else None
    )
    event_id = active.id if active else None
    scope_note = f' in "{active.name}"' if active else ""
    currency = (active.default_currency if active else None) or group.default_currency

    # Two handles → admin records a settlement for a pair (e.g. a departed
    # member). Tried before the single-handle form, which it would also match.
    pair = _PAIR_ARGS_RE.match(args)
    if pair:
        await _settle_on_behalf(
            message, bot, uow, group, event_id, scope_note, currency, *pair.groups()
        )
        return

    match = _ARGS_RE.match(args)
    if not match:
        await message.reply(_USAGE)
        return

    target_username = match.group(1)
    amount_str = match.group(2)  # None if omitted

    # @all — settle the whole scope at once. Admin-only and amount-less.
    if is_all(target_username):
        await _settle_whole_group(
            message, bot, uow, group.id, group.simplify_debts, event_id, scope_note
        )
        return

    from_user = await uow.users.upsert(
        telegram_user_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
        claim_in_group=group.id,
    )
    await uow.group_members.ensure_member(group.id, from_user.id)

    # A normal mention must resolve to someone already known — never create a
    # placeholder here (a typo'd /settleup @foo would otherwise leave a stray).
    to_user = await uow.users.find_by_mention(target_username)
    if to_user is None:
        await message.reply(
            f"I don't know @{target_username} yet — they need to send a message "
            "or appear in an expense before you can settle with them."
        )
        return

    amount_cents = await _resolve_amount(
        message,
        uow,
        group,
        from_user=from_user,
        to_user=to_user,
        amount_str=amount_str,
        event_id=event_id,
        currency=currency,
        from_label="you",
        to_label=f"@{target_username}",
    )
    if amount_cents is None:
        return

    result = await _record(
        message,
        uow,
        group=group,
        from_user=from_user,
        to_user=to_user,
        amount_cents=amount_cents,
        currency=currency,
        event_id=event_id,
        created_by=from_user.id,
    )
    if result is None:
        return

    auto_note = " (full amount owed)" if amount_str is None else ""
    await message.reply(
        f"Settled up{scope_note}: {display_name(from_user.username, from_user.first_name)} paid "
        f"{display_name(to_user.username, to_user.first_name)} "
        f"{format_money(result.amount_cents, result.currency)}{auto_note}"
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
    group: Group,
    event_id: uuid.UUID | None,
    scope_note: str,
    currency: str,
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

    amount_cents = await _resolve_amount(
        message,
        uow,
        group,
        from_user=from_user,
        to_user=to_user,
        amount_str=amount_str,
        event_id=event_id,
        currency=currency,
        from_label=f"@{from_handle}",
        to_label=f"@{to_handle}",
    )
    if amount_cents is None:
        return

    # The recording admin is the author (created_by) — an audit trail of who
    # logged a payment they weren't a party to.
    actor = await uow.users.upsert(
        telegram_user_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
        claim_in_group=group.id,
    )
    await uow.group_members.ensure_member(group.id, actor.id)
    result = await _record(
        message,
        uow,
        group=group,
        from_user=from_user,
        to_user=to_user,
        amount_cents=amount_cents,
        currency=currency,
        event_id=event_id,
        created_by=actor.id,
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
        f"Recorded{scope_note}: {display_name(from_user.username, from_user.first_name)} paid "
        f"{display_name(to_user.username, to_user.first_name)} "
        f"{format_money(result.amount_cents, result.currency)}{auto_note}.{confirm}"
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
    group: Group,
    *,
    from_user: User,
    to_user: User,
    amount_str: str | None,
    event_id: uuid.UUID | None,
    currency: str,
    from_label: str,
    to_label: str,
) -> int | None:
    """Resolve the settlement amount in cents, or reply with an error and return
    None. An explicit amount is parsed; an omitted one auto-fills the full
    suggested from→to transfer in the default currency (the set /balance all
    shows). `from_label`/`to_label` adapt the copy to self vs. on-behalf."""
    if amount_str is not None:
        try:
            return parse_amount_cents(amount_str)
        except ValueError:
            await message.reply(
                "Invalid amount. Please use a positive number, e.g. 25.50"
            )
            return None

    owed = await owed_by_currency(
        uow,
        group.id,
        from_user.id,
        to_user.id,
        simplify_debts=group.simplify_debts,
        event_id=event_id,
    )
    if currency not in owed:
        others = [c for c in owed if c != currency]
        if others:
            detail = ", ".join(format_money(owed[c], c) for c in others)
            await message.reply(
                f"The suggested settlement has {from_label} paying {to_label} in "
                f"{detail}, not {currency}. Settle with an explicit amount in that currency."
            )
        else:
            await message.reply(
                f"The suggested settlement doesn't have {from_label} paying {to_label}. "
                "Run /balance all to see who to pay."
            )
        return None
    return owed[currency]


async def _record(
    message: Message,
    uow: UnitOfWork,
    *,
    group: Group,
    from_user: User,
    to_user: User,
    amount_cents: int,
    currency: str,
    event_id: uuid.UUID | None,
    created_by: uuid.UUID,
) -> SettlementCreatedResult | None:
    """Build the command and record the settlement, replying with the error and
    returning None on a validation/ledger failure. settle_up raises ValueError
    with a user-facing message when the settlement breaks a ledger rule (no
    suggested payment in that direction, or the amount exceeds what's owed)."""
    try:
        cmd = SettleUpCommand(
            group_id=group.id,
            from_user_id=from_user.id,
            to_user_id=to_user.id,
            amount_cents=amount_cents,
            currency=currency,
            event_id=event_id,
            created_by=created_by,
        )
    except Exception as exc:
        logger.warning("Invalid /settleup command: %s", exc)
        await message.reply(f"Invalid command: {exc}")
        return None

    try:
        return await settle_up(uow, cmd, simplify_debts=group.simplify_debts)
    except ValueError as exc:
        await message.reply(str(exc))
        return None


async def _settle_whole_group(
    message: Message,
    bot: Bot,
    uow: UnitOfWork,
    group_id: uuid.UUID,
    simplify_debts: bool,
    event_id: uuid.UUID | None,
    scope_note: str,
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
        uow, group_id, simplify_debts=simplify_debts, event_id=event_id
    )
    if not results:
        await message.reply(
            f"Everyone's already settled up{scope_note} — nothing to record."
        )
        return

    ids = {r.from_user_id for r in results} | {r.to_user_id for r in results}
    names = await uow.balances.get_display_names(ids)

    def name(uid: uuid.UUID) -> str:
        username, first_name = names.get(uid, (None, None))
        return display_name(username, first_name)

    lines = [
        f"✅ Settled up the whole group{scope_note} — {len(results)} transfer(s) recorded:"
    ]
    for r in results:
        lines.append(
            f"  {name(r.from_user_id)} → {name(r.to_user_id)}: {format_money(r.amount_cents, r.currency)}"
        )
    await message.reply("\n".join(lines))
    logger.info(
        "Group settle-up by user=%s in group=%s: %d settlements",
        message.from_user.id,
        group_id,
        len(results),
    )
