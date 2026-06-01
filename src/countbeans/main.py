import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message

from countbeans.config.core import get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

dp = Dispatcher()


@dp.message(Command("start"), F.chat.type.in_({"group", "supergroup"}))
async def start_group(message: Message) -> None:
    logger.info("Effective chat: %s", message.chat)
    await message.answer(f"Have some dirt on yall: {message.chat.username}")


@dp.message(Command("start"))
async def start_private(message: Message) -> None:
    await message.answer("I'm a bot, please talk to me!")


def main() -> int:
    settings = get_settings()
    bot = Bot(token=settings.bot_token)

    async def run() -> None:
        await dp.start_polling(bot)

    asyncio.run(run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
