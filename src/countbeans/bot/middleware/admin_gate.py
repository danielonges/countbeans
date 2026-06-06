import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot
from aiogram.types import Message, TelegramObject

from countbeans.bot.handlers._welcome import PROMOTE_REQUEST
from countbeans.bot.utils.permissions import ADMIN_STATUSES, GROUP_TYPES
from countbeans.services.uow import UnitOfWork

logger = logging.getLogger(__name__)

# Commands that must work even before the bot is an administrator: a confused
# installer in the added-but-not-promoted window should still be able to get
# oriented. /help is pure, read-only info (touches no state), so letting it
# through the gate is safe.
_GATE_BYPASS_COMMANDS = {"help"}


def _command_name(text: str | None) -> str | None:
    """The command keyword of a message, lower-cased — ``/Help@bot foo`` -> ``help``.
    Returns None when the text isn't a slash-command."""
    if not text or not text.startswith("/"):
        return None
    token = text.split(maxsplit=1)[0]  # drop args
    return token[1:].split("@", 1)[0].lower() or None  # drop leading / and @botname


class AdminGateMiddleware(BaseMiddleware):
    """Refuses to process group commands until the bot is an administrator.

    Reads the durable `groups.bot_is_admin` flag (kept current by the
    my_chat_member stream). When it reads false, self-heals with a single
    getChatMember(bot) check — covering groups that predate the flag or a missed
    update — and persists the result. Private chats, non-Message updates, and
    groups the bot has not been added to yet pass straight through (so /start can
    run). Must be registered after TransactionalMiddleware so data["uow"] exists.
    """

    def __init__(self) -> None:
        self._bot_id: int | None = None

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message) or event.chat.type not in GROUP_TYPES:
            return await handler(event, data)

        # Pure info commands (/help) bypass the gate — checked before touching the
        # uow/bot so an un-onboarded or unpromoted group can still get oriented.
        if _command_name(event.text) in _GATE_BYPASS_COMMANDS:
            return await handler(event, data)

        uow: UnitOfWork = data["uow"]
        group = await uow.groups.get_by_telegram_chat_id(event.chat.id)
        if group is None or group.bot_is_admin:
            return await handler(event, data)

        # Stored flag is false — confirm the bot's current status and persist it.
        bot: Bot = data["bot"]
        if self._bot_id is None:
            self._bot_id = (await bot.me()).id
        member = await bot.get_chat_member(event.chat.id, self._bot_id)
        is_admin = member.status in ADMIN_STATUSES
        await uow.groups.set_bot_admin(group.id, is_admin)
        logger.debug(
            "admin-gate self-heal: chat=%s bot_is_admin=%s (stored flag was false)",
            event.chat.id,
            is_admin,
        )
        if is_admin:
            return await handler(event, data)

        await event.reply(PROMOTE_REQUEST)
        logger.info("blocked command in chat=%s — bot is not an admin", event.chat.id)
        return None
