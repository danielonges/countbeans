"""Bot handler for the /settleup command.

Parses: /settleup @username [amount]   |   /settleup @all   (admin-only)

"What you owe a person" is the amount of the suggested ``you -> them`` transfer
that ``/balance all`` shows (honoring the group's simplify toggle). Omitting the
amount auto-fills that suggested amount in the group's default currency; an
explicit amount may not exceed it (settlements only ever happen along a
suggested transfer, so balances can never flip). The cap and the
no-suggested-payment cases are enforced in the settle_up service.

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

from countbeans.bot.formatting import display_name
from countbeans.bot.parsing import parse_amount_cents
from countbeans.bot.permissions import is_admin
from countbeans.dto.commands import SettleUpCommand
from countbeans.services.settlement import owed_by_currency, settle_all, settle_up
from countbeans.services.uow import UnitOfWork

logger = logging.getLogger(__name__)

router = Router()

# The command args (CommandObject strips "/settleup" and any "@botname"):
#   @handle 12.50  |  @handle 12  |  @handle  (amount omitted → auto)  |  @all
_ARGS_RE = re.compile(r"^@([\w.]+)(?:\s+(\d+(?:\.\d{1,2})?))?$")


def _money(cents: int, currency: str) -> str:
    return f"{currency} {cents // 100}.{cents % 100:02d}"


@router.message(Command("settleup"))
async def cmd_settleup(
    message: Message, command: CommandObject, uow: UnitOfWork, bot: Bot
) -> None:
    if message.from_user is None:
        return

    match = _ARGS_RE.match((command.args or "").strip())
    if not match:
        await message.reply(
            "Usage: /settleup @username [amount]   or   /settleup @all (admins)\n"
            "Example: /settleup @alice 25.50\n"
            "Omit the amount to settle your full outstanding debt to that person."
        )
        return

    target_username = match.group(1)
    amount_str = match.group(2)  # None if omitted

    from_user = await uow.users.upsert(
        telegram_user_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
    )
    group = await uow.groups.upsert(
        telegram_chat_id=message.chat.id,
        group_name=getattr(message.chat, "title", None),
    )

    # Active-event mode: settle within the active event's scope (writes are
    # strictly active-scoped — /event pause to settle a general debt). CLAUDE.md.
    active = await uow.events.get(group.active_event_id) if group.active_event_id else None
    event_id = active.id if active else None
    scope_note = f' in "{active.name}"' if active else ""

    # @all — settle the whole scope at once. Admin-only and amount-less.
    if target_username.lower() == "all":
        await _settle_whole_group(
            message, bot, uow, group.id, group.simplify_debts, event_id, scope_note
        )
        return

    # A normal mention must resolve to someone already known — never create a
    # placeholder here (a typo'd /settleup @foo would otherwise leave a stray).
    to_user = await uow.users.find_by_mention(target_username)
    if to_user is None:
        await message.reply(
            f"I don't know @{target_username} yet — they need to send a message "
            "or appear in an expense before you can settle with them."
        )
        return

    currency = group.default_currency

    if amount_str is not None:
        try:
            amount_cents = parse_amount_cents(amount_str)
        except ValueError:
            await message.reply("Invalid amount. Please use a positive number, e.g. 25.50")
            return
    else:
        # Auto-fill: settle the full suggested you→them transfer in the default
        # currency. owed_by_currency reflects the same set /balance all shows.
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
                detail = ", ".join(
                    f"{c} {owed[c] // 100}.{owed[c] % 100:02d}" for c in others
                )
                await message.reply(
                    f"The suggested settlement has you paying @{target_username} in "
                    f"{detail}, not {currency}. Settle with an explicit amount in that currency."
                )
            else:
                await message.reply(
                    f"The suggested settlement doesn't have you paying @{target_username}. "
                    "Run /balance all to see who to pay."
                )
            return
        amount_cents = owed[currency]

    try:
        cmd = SettleUpCommand(
            group_id=group.id,
            from_user_id=from_user.id,
            to_user_id=to_user.id,
            amount_cents=amount_cents,
            currency=currency,
            event_id=event_id,
            created_by=from_user.id,
        )
    except Exception as exc:
        logger.warning("Invalid /settleup command: %s", exc)
        await message.reply(f"Invalid command: {exc}")
        return

    # settle_up raises ValueError with a user-facing message when the settlement
    # breaks a ledger rule (no suggested payment to this person, or the amount
    # exceeds what's owed).
    try:
        result = await settle_up(uow, cmd, simplify_debts=group.simplify_debts)
    except ValueError as exc:
        await message.reply(str(exc))
        return

    auto_note = " (full amount owed)" if amount_str is None else ""
    await message.reply(
        f"Settled up{scope_note}: {display_name(from_user.username, from_user.first_name)} paid "
        f"{display_name(to_user.username, to_user.first_name)} "
        f"{_money(result.amount_cents, result.currency)}{auto_note}"
    )
    logger.info(
        "Settlement recorded: settlement_id=%s from=%s to=%s amount_cents=%d currency=%s",
        result.settlement_id,
        result.from_user_id,
        result.to_user_id,
        result.amount_cents,
        result.currency,
    )


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
        await message.reply("Only group admins can settle up the whole group (/settleup @all).")
        return

    results = await settle_all(uow, group_id, simplify_debts=simplify_debts, event_id=event_id)
    if not results:
        await message.reply(f"Everyone's already settled up{scope_note} — nothing to record.")
        return

    ids = {r.from_user_id for r in results} | {r.to_user_id for r in results}
    names = await uow.balances.get_display_names(ids)

    def name(uid: uuid.UUID) -> str:
        username, first_name = names.get(uid, (None, None))
        return display_name(username, first_name)

    lines = [f"✅ Settled up the whole group{scope_note} — {len(results)} transfer(s) recorded:"]
    for r in results:
        lines.append(f"  {name(r.from_user_id)} → {name(r.to_user_id)}: {_money(r.amount_cents, r.currency)}")
    await message.reply("\n".join(lines))
    logger.info(
        "Group settle-up by user=%s in group=%s: %d settlements",
        message.from_user.id,
        group_id,
        len(results),
    )
