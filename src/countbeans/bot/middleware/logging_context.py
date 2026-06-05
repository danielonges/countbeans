import logging
from typing import Any, Awaitable, Callable

import nanoid
from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from countbeans.logging.core import log_context

logger = logging.getLogger(__name__)


class LoggingContextMiddleware(BaseMiddleware):
    """Stamps every log record in a request with request_id, user_id, chat_id,
    and command. Must be registered before TransactionalMiddleware so the
    request_id is present on the "transaction opened" line."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        fields: dict[str, Any] = {"request_id": nanoid.generate()}
        if isinstance(event, Message) and event.from_user:
            fields["user_id"] = event.from_user.id
            fields["chat_id"] = event.chat.id
            # Log only the command NAME, never its arguments — the full text
            # carries user financial data (amounts, descriptions, @handles).
            # Strip the "@botname" suffix Telegram appends in groups. Non-command
            # messages leave "command" unset.
            head = (event.text or "").split(maxsplit=1)
            if head and head[0].startswith("/"):
                fields["command"] = head[0].split("@", 1)[0]
        elif isinstance(event, CallbackQuery) and event.from_user:
            fields["user_id"] = event.from_user.id
            fields["chat_id"] = event.message.chat.id if event.message else None
            fields["command"] = f"callback:{event.data}"
        with log_context(**fields):
            return await handler(event, data)
