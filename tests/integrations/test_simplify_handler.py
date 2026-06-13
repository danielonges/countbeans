"""Handler tests for /simplify — read by anyone, toggle is admin-gated."""

from sqlalchemy.ext.asyncio import AsyncSession

from ._bot_harness import MockedBot, feed, make_message
from ._seed import read_group


async def test_simplify_reports_default_on(dispatcher, session: AsyncSession) -> None:
    bot = MockedBot()
    await feed(dispatcher, bot, make_message("/simplify"), session=session)
    reply = bot.last_reply or ""
    assert "ON" in reply
    # The read explains the visible effect, not just the state name.
    assert "fewest payments" in reply
    assert "same either way" in reply


async def test_simplify_toggle_refused_for_non_admin(
    dispatcher, session: AsyncSession
) -> None:
    bot = MockedBot(caller_is_admin=False)
    await feed(dispatcher, bot, make_message("/simplify off"), session=session)

    assert "admin" in (bot.last_reply or "").lower()
    assert (await read_group(session)).simplify_debts is True  # unchanged


async def test_simplify_toggled_off_by_admin(dispatcher, session: AsyncSession) -> None:
    bot = MockedBot(caller_is_admin=True)
    await feed(dispatcher, bot, make_message("/simplify off"), session=session)

    reply = bot.last_reply or ""
    assert "OFF" in reply
    assert "every pairwise debt" in reply  # the toggle reply states the effect
    assert (await read_group(session)).simplify_debts is False
