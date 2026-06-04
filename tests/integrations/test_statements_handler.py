"""Handler tests for /statements — the message view and inline paging callbacks.

Exercises the callback path the service tests can't: the owner-binding check
(only your own taps page your statement) and that a page tap repaints via
edit_text.
"""
from sqlalchemy.ext.asyncio import AsyncSession

from ._bot_harness import MockedBot, feed, feed_callback, make_callback, make_message
from ._seed import seed_group


async def test_statements_personal_view(dispatcher, session: AsyncSession) -> None:
    await seed_group(session)
    bot = MockedBot()
    await feed(dispatcher, bot, make_message("/statements", from_id=1001, username="caller"), session=session)

    reply = bot.last_reply or ""
    assert "Your statement" in reply
    assert "No transactions yet" in reply


async def test_statements_group_view(dispatcher, session: AsyncSession) -> None:
    await seed_group(session)
    bot = MockedBot()
    await feed(dispatcher, bot, make_message("/statements all"), session=session)
    assert "Group statement" in (bot.last_reply or "")


async def test_statements_group_page_callback_repaints(dispatcher, session: AsyncSession) -> None:
    await seed_group(session)
    bot = MockedBot()
    await feed_callback(dispatcher, bot, make_callback("stmt:g:0", from_id=1001), session=session)

    assert "Group statement" in (bot.last_edit or "")  # repainted via edit_text
    assert bot.last_answer is not None  # the tap was acknowledged


async def test_statements_other_users_page_is_rejected(dispatcher, session: AsyncSession) -> None:
    await seed_group(session)
    bot = MockedBot()
    # Caller 1001 taps a page button bound to subject 9999 — not theirs.
    await feed_callback(dispatcher, bot, make_callback("stmt:u:9999:0", from_id=1001), session=session)

    assert bot.last_answer is not None
    assert "not your statement" in (bot.last_answer.text or "").lower()
    assert bot.edits == []  # nothing repainted
