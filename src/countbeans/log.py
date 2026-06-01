"""
Logging configuration for countbeans.

Call setup() once at application startup (in main()). Every other module
should obtain its logger via get_logger(__name__) rather than calling
logging.getLogger directly, so all loggers flow through the same config.
"""

import logging

# Third-party loggers that are too noisy at INFO — capped at WARNING.
_NOISY_LOGGERS = [
    "httpx",
    "aiohttp.access",
    "aiogram.event",
]


def setup(level: str = "INFO") -> None:
    """Configure logging for the whole application.

    Args:
        level: Root log level as a string (e.g. "DEBUG", "INFO", "WARNING").
               Driven by settings.log_level so it can be set via
               COUNTBEANS_LOG_LEVEL in the environment.
    """
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s.%(msecs)03d %(name)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Return a logger for the given name.

    Usage in any module:
        from countbeans.log import get_logger
        logger = get_logger(__name__)
    """
    return logging.getLogger(name)
