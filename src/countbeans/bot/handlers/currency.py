"""Bot handler for /currency [CODE] — the per-group default currency.

Reading the setting (no argument) is open to any member; changing it is
admin-only — the bot checks the caller via getChatMember and proceeds only if
their status is `creator` or `administrator` (mirrors /simplify, since the
default currency is likewise a shared, group-wide setting).

Setting the default only affects *future* expenses and settlements — past
ledger rows keep whatever currency they were recorded with (the ledger is
append-only and balances derive per-currency, so a group can hold mixed
currencies).
"""

import logging

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import Message

from countbeans.bot.utils.permissions import is_admin
from countbeans.services.uow import UnitOfWork

logger = logging.getLogger(__name__)

router = Router()


@router.message(Command("currency"))
async def cmd_currency(message: Message, bot: Bot, uow: UnitOfWork) -> None:
    if message.from_user is None:
        return

    group = await uow.groups.upsert(
        telegram_chat_id=message.chat.id,
        group_name=getattr(message.chat, "title", None),
    )

    parts = (message.text or "").split()
    arg = parts[1].upper() if len(parts) > 1 else None

    # No argument → report the current default (any member may read).
    if arg is None:
        await message.reply(
            f"Default currency is {group.default_currency}.\n"
            "Set it with /currency <CODE> (e.g. /currency USD)."
        )
        return

    if not (len(arg) == 3 and arg.isalpha()):
        await message.reply(
            "Usage: /currency <CODE> — a 3-letter ISO 4217 code, e.g. /currency EUR"
        )
        return

    # Changing the setting is admin-only.
    if not await is_admin(bot, message.chat.id, message.from_user.id):
        await message.reply("Only group admins can change the default currency.")
        return

    if arg == group.default_currency:
        await message.reply(f"Default currency is already {arg}.")
        return

    await uow.groups.set_default_currency(group.id, arg)
    await message.reply(
        f"Default currency is now {arg}. "
        "This applies to new expenses — past entries keep their currency."
    )
    logger.info(
        "default_currency set to %s for group_id=%s by user=%s",
        arg,
        group.id,
        message.from_user.id,
    )
