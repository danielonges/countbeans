import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Generator

from pythonjsonlogger.json import JsonFormatter

_JSON_FIELDS = "%(asctime)s %(levelname)s %(name)s %(message)s"
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"

_log_ctx: ContextVar[dict[str, Any]] = ContextVar("log_ctx", default={})


class _ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        for k, v in _log_ctx.get().items():
            setattr(record, k, v)
        return True


@contextmanager
def log_context(**fields: Any) -> Generator[None, None, None]:
    """Attach structured fields to every log record emitted within this block.

    Merges with any enclosing context — nested calls accumulate fields.
    The ContextVar token guarantees the old context is restored on exit,
    even if the block raises.
    """
    token = _log_ctx.set({**_log_ctx.get(), **fields})
    try:
        yield
    finally:
        _log_ctx.reset(token)


def _json_handler() -> logging.StreamHandler:  # type: ignore[type-arg]
    handler = logging.StreamHandler()
    handler.setFormatter(
        JsonFormatter(
            fmt=_JSON_FIELDS,
            datefmt=_DATE_FORMAT,
            rename_fields={"asctime": "time", "levelname": "level", "name": "logger"},
        )
    )
    handler.addFilter(_ContextFilter())
    return handler


def setup(level: str = "INFO") -> None:
    root = logging.getLogger()
    root.setLevel(level.upper())
    root.handlers.clear()
    root.addHandler(_json_handler())
