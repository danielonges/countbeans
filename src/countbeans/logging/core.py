"""
Logging configuration for countbeans.

Call setup() once at application startup (in main()). Every other module
should obtain its logger via get_logger(__name__) rather than calling
logging.getLogger directly, so all loggers flow through the same config.
"""

import logging

from pythonjsonlogger.json import JsonFormatter


_TEXT_FORMAT = "%(asctime)s.%(msecs)03d %(levelname)-8s %(name)s  %(message)s"
_TEXT_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"

_JSON_FIELDS = "%(asctime)s %(levelname)s %(name)s %(message)s"


def _text_handler() -> logging.StreamHandler:
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter(fmt=_TEXT_FORMAT, datefmt=_TEXT_DATE_FORMAT)
    )
    return handler


def _json_handler() -> logging.StreamHandler:
    handler = logging.StreamHandler()
    handler.setFormatter(
        JsonFormatter(
            fmt=_JSON_FIELDS,
            datefmt=_TEXT_DATE_FORMAT,
            rename_fields={"asctime": "time", "levelname": "level", "name": "logger"},
        )
    )
    return handler


def setup(level: str = "INFO", fmt: str = "text") -> None:
    """Configure logging for the whole application.

    Args:
        level:  Root log level (e.g. "DEBUG", "INFO", "WARNING").
                Driven by settings.log_level / COUNTBEANS_LOG_LEVEL.
        fmt:    "text" for human-readable output (dev),
                "json" for structured output (prod / Docker).
                Driven by settings.log_format / COUNTBEANS_LOG_FORMAT.
    """
    handler = _json_handler() if fmt.lower() == "json" else _text_handler()
    root = logging.getLogger()
    root.setLevel(level.upper())
    root.handlers.clear()
    root.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    """Return a logger for the given name.

    Usage in any module:
        from countbeans.logging import get_logger
        logger = get_logger(__name__)
    """
    return logging.getLogger(name)
