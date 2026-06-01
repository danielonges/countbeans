from aiogram import Bot, Dispatcher

from countbeans.bot.handlers import start


async def run(token: str) -> None:
    dp = Dispatcher()
    dp.include_router(start.router)

    bot = Bot(token=token)
    await dp.start_polling(bot)
