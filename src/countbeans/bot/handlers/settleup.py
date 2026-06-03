"""Bot handler for the /settleup command.

Parses: /settleup @username [amount]

Omitting the amount auto-fills the full outstanding debt to @username in
the group's default currency. If the caller owes @username in multiple
currencies the group default is picked; if they owe nothing in the default
currency they are told to specify an amount explicitly.
"""
import logging
import re

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from countbeans.bot.parsing import parse_amount_cents
from countbeans.dto.commands import SettleUpCommand
from countbeans.dto.domain import BalanceKey
from countbeans.services.settlement import settle_up
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

    if amount_str is not None:
        try:
            amount_cents = parse_amount_cents(amount_str)
        except ValueError:
            await message.reply("Invalid amount. Please use a positive number, e.g. 25.50")
            return
        currency = group.default_currency
    else:
        # Auto-fill: derive what the caller owes this specific person.
        # The balance formula gives net group positions, not pairwise debts, so we
        # use the group default currency and check the caller's net balance — if
        # negative, the full absolute value is the auto-amount. Multiple currencies
        # are resolved by picking the group default; if that's zero, tell the user
        # to specify an amount.
        balances = await uow.balances.compute_for_group(group.id)
        currency = group.default_currency
        payer_balance = balances.get(BalanceKey(from_user.id, currency), 0)

        # Check if the user owes in any other currencies too (for the hint).
        owed_currencies = [
            key.currency for key, cents in balances.items()
            if key.user_id == from_user.id and cents < 0
        ]

        if payer_balance >= 0:
            if owed_currencies:
                # Owes in other currencies but not the default.
                others = ", ".join(c for c in owed_currencies if c != currency)
                await message.reply(
                    f"You don't owe anything in {currency}. "
                    f"You have debts in: {others}. "
                    "Specify an amount to settle in a different currency."
                )
            else:
                await message.reply(
                    f"You don't owe @{target_username} anything — your balance is already settled."
                )
            return

        amount_cents = abs(payer_balance)

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
    # breaks a ledger rule (e.g. you owe nothing, or the recipient isn't owed).
    try:
        result = await settle_up(uow, cmd)
    except ValueError as exc:
        await message.reply(str(exc))
        return

    major, minor = result.amount_cents // 100, result.amount_cents % 100
    auto_note = " (full balance)" if amount_str is None else ""
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
