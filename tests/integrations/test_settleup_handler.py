"""Handler tests for /settleup — arg parsing, unknown handles, the @all admin gate."""
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from countbeans.db.models import Settlement
from countbeans.services.repositories import UserRepository

from ._bot_harness import MockedBot, feed, make_message
from ._seed import seed_expense, seed_group, seed_member


async def _settlement_count(session: AsyncSession) -> int:
    return (await session.execute(select(func.count()).select_from(Settlement))).scalar_one()


async def _seed_caller_owes_bob(session: AsyncSession) -> None:
    group = await seed_group(session)
    caller = await seed_member(session, group, telegram_user_id=1001, username="caller")
    bob = await seed_member(session, group, telegram_user_id=2002, username="bob")
    await seed_expense(session, group, payer=bob, participants=[caller, bob], amount_cents=1000)


async def test_settleup_usage_on_bad_args(dispatcher, session: AsyncSession) -> None:
    bot = MockedBot()
    await feed(dispatcher, bot, make_message("/settleup"), session=session)
    assert "Usage" in (bot.last_reply or "")


async def test_settleup_unknown_handle_rejected(dispatcher, session: AsyncSession) -> None:
    await seed_group(session)
    bot = MockedBot()
    await feed(dispatcher, bot, make_message("/settleup @nobody 5", from_id=1001, username="caller"), session=session)

    assert "don't know @nobody" in (bot.last_reply or "")
    # A typo must not spawn a placeholder for the unknown handle.
    assert await UserRepository(session).find_by_mention("nobody") is None


async def test_settleup_records_full_owed_amount(dispatcher, session: AsyncSession) -> None:
    await _seed_caller_owes_bob(session)
    bot = MockedBot()
    # Omit the amount → settle the full suggested debt (SGD 5).
    await feed(dispatcher, bot, make_message("/settleup @bob", from_id=1001, username="caller"), session=session)

    assert "Settled up" in (bot.last_reply or "")
    assert "5.00" in (bot.last_reply or "")
    assert await _settlement_count(session) == 1


async def test_settleup_all_refused_for_non_admin(dispatcher, session: AsyncSession) -> None:
    await _seed_caller_owes_bob(session)
    bot = MockedBot(caller_is_admin=False)
    await feed(dispatcher, bot, make_message("/settleup @all", from_id=1001, username="caller"), session=session)

    assert "admin" in (bot.last_reply or "").lower()
    assert await _settlement_count(session) == 0  # nothing recorded


async def test_settleup_all_by_admin_clears_group(dispatcher, session: AsyncSession) -> None:
    await _seed_caller_owes_bob(session)
    bot = MockedBot(caller_is_admin=True)
    await feed(dispatcher, bot, make_message("/settleup @all", from_id=1001, username="caller"), session=session)

    assert "whole group" in (bot.last_reply or "").lower()
    assert await _settlement_count(session) == 1  # the one outstanding transfer
