"""aiogram middlewares for the bot adapter.

One module per middleware; this package re-exports them so call sites keep
`from countbeans.bot.middleware import ...`. Registration order is load-bearing
(see `bot/server.py`): LoggingContext → Transactional → AdminGate, so the
request_id is stamped before the "transaction opened" line and the admin gate
runs with `data["uow"]` already in place.
"""

from .admin_gate import AdminGateMiddleware
from .logging_context import LoggingContextMiddleware
from .transactional import TransactionalMiddleware

__all__ = [
    "AdminGateMiddleware",
    "LoggingContextMiddleware",
    "TransactionalMiddleware",
]
