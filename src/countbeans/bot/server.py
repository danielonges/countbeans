from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from aiogram import Bot, Dispatcher

from countbeans.bot.handlers import addexpense, balance, settleup, simplify, start
from countbeans.bot.middleware import TransactionalMiddleware
from countbeans.config import get_settings
from countbeans.services.uow import UnitOfWork


async def run(token: str) -> None:
    settings = get_settings()
    engine = create_async_engine(str(settings.database_url), echo=False)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    def uow_factory() -> UnitOfWork:
        return UnitOfWork(session_factory)

    dp = Dispatcher()
    dp.message.middleware(TransactionalMiddleware(uow_factory))
    dp.include_router(start.router)
    dp.include_router(settleup.router)
    dp.include_router(addexpense.router)
    dp.include_router(balance.router)
    dp.include_router(simplify.router)

    bot = Bot(token=token)
    try:
        await dp.start_polling(bot)
    finally:
        await engine.dispose()
