"""Bot handler for /help — an on-demand command reference.

Static by design: Telegram's command menu already gives a browsable index, and
every command shows its own detailed usage when sent with no arguments — so
/help just lists what's available and points at that, rather than duplicating
each command's grammar. It works even before the bot is promoted to admin
(AdminGateMiddleware lets /help through), so a confused installer can always get
oriented. Reads nothing and writes nothing.
"""

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

from countbeans.bot.handlers._welcome import GROUP_HELP, PRIVATE_HELP

router = Router()


@router.message(Command("help"), F.chat.type.in_({"group", "supergroup"}))
async def help_group(message: Message) -> None:
    await message.reply(GROUP_HELP)


@router.message(Command("help"))
async def help_private(message: Message) -> None:
    await message.answer(PRIVATE_HELP)
