import logging

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
)

from countbeans.bot.handlers import (
    addexpense,
    addexpense_wizard,
    balance,
    currency,
    event,
    group,
    help,
    join,
    membership,
    settleup,
    simplify,
    start,
    statements,
    void,
)
from countbeans.bot.middleware import (
    AdminGateMiddleware,
    LoggingContextMiddleware,
    TransactionalMiddleware,
)
from countbeans.config import get_settings
from countbeans.services.uow import UnitOfWork

logger = logging.getLogger(__name__)

_COMMANDS = [
    BotCommand(command="start", description="Set up the bot here (admin)"),
    BotCommand(command="join", description="Add yourself to expense tracking"),
    BotCommand(command="help", description="List the commands and how to use them"),
    BotCommand(command="addexpense", description="Record an expense"),
    BotCommand(
        command="balance", description="View your balance (or 'all' for everyone)"
    ),
    BotCommand(
        command="settleup", description="See what you owe and settle with a tap"
    ),
    BotCommand(command="void", description="Undo your most recent expense"),
    BotCommand(
        command="simplify", description="View or toggle debt simplification (admin)"
    ),
    BotCommand(
        command="currency",
        description="View or set the group's default currency (admin)",
    ),
    BotCommand(
        command="statements",
        description="Your transactions ('all' for the whole group)",
    ),
    BotCommand(
        command="event",
        description="Manage an event scope (new/pause/resume/close/add/remove)",
    ),
    BotCommand(command="group", description="Show group info and member list"),
]


async def run(token: str) -> None:
    settings = get_settings()
    engine = create_async_engine(
        str(settings.database_url),
        echo=False,
        # Resilience for the always-on poller — see Settings for the rationale.
        pool_pre_ping=settings.db_pool_pre_ping,
        pool_recycle=settings.db_pool_recycle_seconds,
    )
    logger.info(
        "database engine created (pool_pre_ping=%s pool_recycle=%ss)",
        settings.db_pool_pre_ping,
        settings.db_pool_recycle_seconds,
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    def uow_factory() -> UnitOfWork:
        return UnitOfWork(session_factory)

    # MemoryStorage backs the interactive /addexpense wizard's FSM state (one
    # in-flight draft per chat+user). In-memory is right for this single-instance
    # long-poll bot — a half-finished wizard is simply abandoned on restart. It is
    # aiogram's default, but pinned here because the wizard depends on it.
    dp = Dispatcher(storage=MemoryStorage())
    # /statements paging and the /addexpense wizard arrive as callback queries, so
    # the UoW middleware must cover both update types — the callback handlers read.
    dp.message.middleware(LoggingContextMiddleware())
    dp.callback_query.middleware(LoggingContextMiddleware())
    # my_chat_member / chat_member updates also write (onboarding, bot-admin flag).
    dp.my_chat_member.middleware(LoggingContextMiddleware())
    dp.chat_member.middleware(LoggingContextMiddleware())
    dp.message.middleware(TransactionalMiddleware(uow_factory))
    dp.callback_query.middleware(TransactionalMiddleware(uow_factory))
    dp.my_chat_member.middleware(TransactionalMiddleware(uow_factory))
    dp.chat_member.middleware(TransactionalMiddleware(uow_factory))
    # The admin gate runs after the UoW is opened (it reads/writes bot_is_admin)
    # and only on messages — membership updates must always be processed.
    dp.message.middleware(AdminGateMiddleware())
    dp.include_router(start.router)
    dp.include_router(join.router)
    dp.include_router(help.router)
    dp.include_router(settleup.router)
    dp.include_router(addexpense.router)
    dp.include_router(addexpense_wizard.router)
    dp.include_router(void.router)
    dp.include_router(balance.router)
    dp.include_router(simplify.router)
    dp.include_router(currency.router)
    dp.include_router(statements.router)
    dp.include_router(event.router)
    dp.include_router(group.router)
    dp.include_router(membership.router)

    # Every handler replies in PLAIN TEXT: user-controlled strings (expense
    # descriptions, event names, @handles) are echoed back verbatim and escaped
    # nowhere. Pin parse_mode to None at the composition root so a future default
    # can't silently turn those echoes into a Markdown/HTML injection vector. If
    # formatting is ever enabled, every echoed user string must first be escaped
    # (html.escape / aiogram's quote helpers).
    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=None))
    try:
        await bot.set_my_commands(
            [
                BotCommand(
                    command="start",
                    description="Add me to a group to start tracking expenses",
                ),
                BotCommand(command="help", description="How to use countbeans"),
            ],
            scope=BotCommandScopeAllPrivateChats(),
        )
        await bot.set_my_commands(_COMMANDS, scope=BotCommandScopeAllGroupChats())
        # chat_member is not delivered unless explicitly requested; resolving the
        # used update types from the registered handlers opts us into both the
        # my_chat_member and chat_member streams.
        logger.info("command menus published; starting long-polling")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await engine.dispose()
        logger.info("polling stopped; database engine disposed")
