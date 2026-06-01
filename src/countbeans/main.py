import asyncio
import sys

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message

from countbeans.config import get_settings
from countbeans.logging import get_logger, setup as setup_logging

logger = get_logger(__name__)

dp = Dispatcher()


async def start_group(message: Message) -> None:
    logger.info("Effective chat: %s", message.chat)
    await message.answer(f"Have some dirt on yall: {message.chat.username}")


async def start_private(message: Message) -> None:
    await message.answer("I'm a bot, please talk to me!")


def main() -> int:
    settings = get_settings()
    setup_logging(level=settings.log_level, fmt=settings.log_format)

    dp.message.register(start_group, Command("start"), F.chat.type.in_({"group", "supergroup"}))
    dp.message.register(start_private, Command("start"))

    bot = Bot(token=settings.bot_token)

    async def run() -> None:
        await dp.start_polling(bot)

    asyncio.run(run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
