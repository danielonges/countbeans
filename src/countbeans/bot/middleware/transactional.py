import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from countbeans.services.uow import UnitOfWork

logger = logging.getLogger(__name__)


class TransactionalMiddleware(BaseMiddleware):
    def __init__(self, uow_factory: Callable[[], UnitOfWork]) -> None:
        self._uow_factory = uow_factory

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        try:
            async with self._uow_factory() as uow:
                data["uow"] = uow
                return await handler(event, data)
        except Exception:
            logger.warning("unhandled exception in handler", exc_info=True)
            raise
