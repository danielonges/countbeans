"""Bot handler for /simplify [on|off] — the per-group debt-simplification toggle.

Reading the setting (no argument) is open to any member; changing it is
admin-only — the bot checks the caller via getChatMember and proceeds only if
their status is `creator` or `administrator`. The setting is purely
presentational (see CLAUDE.md "Debt simplification").
"""

import logging

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import Message

from countbeans.bot.permissions import is_admin
from countbeans.services.uow import UnitOfWork

logger = logging.getLogger(__name__)

router = Router()


def _state(on: bool) -> str:
    return "ON" if on else "OFF"


@router.message(Command("simplify"))
async def cmd_simplify(message: Message, bot: Bot, uow: UnitOfWork) -> None:
    if message.from_user is None:
        return

    group = await uow.groups.upsert(
        telegram_chat_id=message.chat.id,
        group_name=getattr(message.chat, "title", None),
    )

    parts = (message.text or "").split()
    arg = parts[1].lower() if len(parts) > 1 else None

    # No argument → report the current state (any member may read).
    if arg is None:
        await message.reply(
            f"Debt simplification is currently {_state(group.simplify_debts)}."
        )
        return

    if arg not in ("on", "off"):
        await message.reply("Usage: /simplify [on|off]")
        return

    # Changing the setting is admin-only.
    if not await is_admin(bot, message.chat.id, message.from_user.id):
        await message.reply("Only group admins can change debt simplification.")
        return

    new_value = arg == "on"
    if new_value == group.simplify_debts:
        await message.reply(f"Debt simplification is already {_state(new_value)}.")
        return

    await uow.groups.set_simplify_debts(group.id, new_value)
    await message.reply(f"Debt simplification is now {_state(new_value)}.")
    logger.info(
        "simplify_debts set to %s for group_id=%s by user=%s",
        new_value,
        group.id,
        message.from_user.id,
    )
