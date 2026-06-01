import logging
from collections.abc import Generator

import pytest
from pythonjsonlogger.json import JsonFormatter

from countbeans.logging.core import setup


@pytest.fixture(autouse=True)
def restore_root_logger() -> Generator[None, None, None]:
    root = logging.getLogger()
    original_level = root.level
    original_handlers = root.handlers[:]
    yield
    root.handlers[:] = original_handlers
    root.setLevel(original_level)


def test_setup_sets_level() -> None:
    setup("DEBUG")
    assert logging.getLogger().level == logging.DEBUG


def test_setup_sets_level_case_insensitive() -> None:
    setup("warning")
    assert logging.getLogger().level == logging.WARNING


def test_setup_single_handler() -> None:
    setup("INFO")
    setup("INFO")
    assert len(logging.getLogger().handlers) == 1


def test_setup_handler_uses_json_formatter() -> None:
    setup("INFO")
    handler = logging.getLogger().handlers[0]
    assert isinstance(handler.formatter, JsonFormatter)
