import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

logger = logging.getLogger(__name__)

router = Router()


@router.message(Command("start"), F.chat.type.in_({"group", "supergroup"}))
async def start_group(message: Message) -> None:
    logger.info("Effective chat: %s", message.chat)
    await message.answer(f"Have some dirt on yall: {message.chat.username}")


@router.message(Command("start"))
async def start_private(message: Message) -> None:
    await message.answer("I'm a bot, please talk to me!")
