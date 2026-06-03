"""Bot handler for the /settleup command.

Parses: /settleup @username <amount>
"""
import logging
import re

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from countbeans.bot.parsing import parse_amount_cents
from countbeans.dto.commands import SettleUpCommand
from countbeans.services.settlement import settle_up
from countbeans.services.uow import UnitOfWork

logger = logging.getLogger(__name__)

router = Router()

# Matches:  /settleup @handle 12.50
#           /settleup @handle 12
_COMMAND_RE = re.compile(
    r"^/settleup\s+@([\w.]+)\s+(\d+(?:\.\d{1,2})?)$",
    re.IGNORECASE,
)


@router.message(Command("settleup"))
async def cmd_settleup(message: Message, uow: UnitOfWork) -> None:
    if message.text is None or message.from_user is None:
        return

    match = _COMMAND_RE.match(message.text.strip())
    if not match:
        await message.reply(
            "Usage: /settleup @username <amount>\n"
            "Example: /settleup @alice 25.50"
        )
        return

    target_username, amount_str = match.group(1), match.group(2)

    try:
        amount_cents = parse_amount_cents(amount_str)
    except ValueError:
        await message.reply("Invalid amount. Please use a positive number, e.g. 25.50")
        return

    from_user = await uow.users.upsert(
        telegram_user_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
    )
    to_user = await uow.users.get_or_create_placeholder(target_username)
    group = await uow.groups.upsert(
        telegram_chat_id=message.chat.id,
        group_name=getattr(message.chat, "title", None),
    )

    try:
        cmd = SettleUpCommand(
            group_id=group.id,
            from_user_id=from_user.id,
            to_user_id=to_user.id,
            amount_cents=amount_cents,
            currency=group.default_currency,
            created_by=from_user.id,
        )
    except Exception as exc:
        logger.warning("Invalid /settleup command: %s", exc)
        await message.reply(f"Invalid command: {exc}")
        return

    result = await settle_up(uow, cmd)

    major, minor = result.amount_cents // 100, result.amount_cents % 100
    await message.reply(
        f"Settled up: @{message.from_user.username or 'you'} paid "
        f"@{target_username} {result.currency} {major}.{minor:02d}"
    )
    logger.info(
        "Settlement recorded: settlement_id=%s from=%s to=%s amount_cents=%d currency=%s",
        result.settlement_id,
        result.from_user_id,
        result.to_user_id,
        result.amount_cents,
        result.currency,
    )
