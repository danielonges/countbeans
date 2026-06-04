"""Handler tests for /currency — read by anyone, change is admin-gated."""

from sqlalchemy.ext.asyncio import AsyncSession

from ._bot_harness import MockedBot, feed, make_message
from ._seed import read_group


async def test_currency_reports_default(dispatcher, session: AsyncSession) -> None:
    bot = MockedBot()
    await feed(dispatcher, bot, make_message("/currency"), session=session)
    assert "SGD" in (bot.last_reply or "")


async def test_currency_change_refused_for_non_admin(
    dispatcher, session: AsyncSession
) -> None:
    bot = MockedBot(caller_is_admin=False)
    await feed(dispatcher, bot, make_message("/currency EUR"), session=session)

    assert "admin" in (bot.last_reply or "").lower()
    assert (await read_group(session)).default_currency == "SGD"  # unchanged


async def test_currency_changed_by_admin(dispatcher, session: AsyncSession) -> None:
    bot = MockedBot(caller_is_admin=True)
    await feed(dispatcher, bot, make_message("/currency EUR"), session=session)

    assert "EUR" in (bot.last_reply or "")
    assert (await read_group(session)).default_currency == "EUR"


async def test_currency_rejects_bad_code(dispatcher, session: AsyncSession) -> None:
    bot = MockedBot(caller_is_admin=True)
    await feed(dispatcher, bot, make_message("/currency EUROS"), session=session)

    assert "ISO 4217" in (bot.last_reply or "")
    assert (await read_group(session)).default_currency == "SGD"
