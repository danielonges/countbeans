import logging

from pythonjsonlogger.json import JsonFormatter

_JSON_FIELDS = "%(asctime)s %(levelname)s %(name)s %(message)s"
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"


def _json_handler() -> logging.StreamHandler:
    handler = logging.StreamHandler()
    handler.setFormatter(
        JsonFormatter(
            fmt=_JSON_FIELDS,
            datefmt=_DATE_FORMAT,
            rename_fields={"asctime": "time", "levelname": "level", "name": "logger"},
        )
    )
    return handler


def setup(level: str = "INFO") -> None:
    root = logging.getLogger()
    root.setLevel(level.upper())
    root.handlers.clear()
    root.addHandler(_json_handler())
