"""Bot handler for /simplify [on|off] — the per-group debt-simplification toggle.

Reading the setting (no argument) is open to any member; changing it is
admin-only — the bot checks the caller via getChatMember and proceeds only if
their status is `creator` or `administrator`. The setting is purely
presentational (see CLAUDE.md "Debt simplification").
"""

import logging

from aiogram import Bot, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from countbeans.bot.utils.permissions import is_admin
from countbeans.services.uow import UnitOfWork

logger = logging.getLogger(__name__)

router = Router()


def _state(on: bool) -> str:
    return "ON" if on else "OFF"


def _effect(on: bool) -> str:
    """What the setting actually does to /balance all — the name alone
    ("debt simplification") explains nothing at the point of use."""
    return (
        "/balance all suggests the fewest payments that clear everyone"
        if on
        else "/balance all lists every pairwise debt instead"
    )


@router.message(Command("simplify"))
async def cmd_simplify(
    message: Message, command: CommandObject, bot: Bot, uow: UnitOfWork
) -> None:
    if message.from_user is None:
        return

    group = await uow.groups.upsert(
        telegram_chat_id=message.chat.id,
        group_name=getattr(message.chat, "title", None),
    )

    args = (command.args or "").split()
    arg = args[0].lower() if args else None

    # No argument → report the current state (any member may read).
    if arg is None:
        on = group.simplify_debts
        await message.reply(
            f"Debt simplification is currently {_state(on)} — {_effect(on)} "
            "(your balances are the same either way)."
        )
        return

    if arg not in ("on", "off"):
        await message.reply("Usage: /simplify [on|off]")
        return

    # Changing the setting is admin-only.
    if not await is_admin(bot, message.chat.id, message.from_user.id):
        logger.info(
            "Refused /simplify: user=%s is not an admin in chat=%s",
            message.from_user.id,
            message.chat.id,
        )
        await message.reply("Only group admins can change debt simplification.")
        return

    new_value = arg == "on"
    if new_value == group.simplify_debts:
        await message.reply(f"Debt simplification is already {_state(new_value)}.")
        return

    await uow.groups.set_simplify_debts(group.id, new_value)
    await message.reply(
        f"Debt simplification is now {_state(new_value)} — {_effect(new_value)}."
    )
    logger.info(
        "simplify_debts set to %s for group_id=%s by user=%s",
        new_value,
        group.id,
        message.from_user.id,
    )
