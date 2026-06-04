"""Bot handler for the /settleup command.

Parses: /settleup @username [amount]

"What you owe a person" is the amount of the suggested ``you -> them`` transfer
that ``/balance all`` shows (honoring the group's simplify toggle). Omitting the
amount auto-fills that suggested amount in the group's default currency; an
explicit amount may not exceed it (settlements only ever happen along a
suggested transfer, so balances can never flip). The cap and the
no-suggested-payment cases are enforced in the settle_up service.
"""
import logging
import re

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from countbeans.bot.parsing import parse_amount_cents
from countbeans.dto.commands import SettleUpCommand
from countbeans.services.settlement import owed_by_currency, settle_up
from countbeans.services.uow import UnitOfWork

logger = logging.getLogger(__name__)

router = Router()

# The command args (CommandObject strips "/settleup" and any "@botname"):
#   @handle 12.50  |  @handle 12  |  @handle  (amount omitted → auto)
_ARGS_RE = re.compile(r"^@([\w.]+)(?:\s+(\d+(?:\.\d{1,2})?))?$")


@router.message(Command("settleup"))
async def cmd_settleup(message: Message, command: CommandObject, uow: UnitOfWork) -> None:
    if message.from_user is None:
        return

    match = _ARGS_RE.match((command.args or "").strip())
    if not match:
        await message.reply(
            "Usage: /settleup @username [amount]\n"
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
    to_user = await uow.users.resolve_mention(target_username)
    group = await uow.groups.upsert(
        telegram_chat_id=message.chat.id,
        group_name=getattr(message.chat, "title", None),
    )

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
            uow, group.id, from_user.id, to_user.id, simplify_debts=group.simplify_debts
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

    major, minor = result.amount_cents // 100, result.amount_cents % 100
    auto_note = " (full amount owed)" if amount_str is None else ""
    await message.reply(
        f"Settled up: @{message.from_user.username or 'you'} paid "
        f"@{target_username} {result.currency} {major}.{minor:02d}{auto_note}"
    )
    logger.info(
        "Settlement recorded: settlement_id=%s from=%s to=%s amount_cents=%d currency=%s",
        result.settlement_id,
        result.from_user_id,
        result.to_user_id,
        result.amount_cents,
        result.currency,
    )
