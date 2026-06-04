"""Handler tests for /balance and /balance all over a seeded debt."""

from sqlalchemy.ext.asyncio import AsyncSession

from ._bot_harness import MockedBot, feed, make_message
from ._seed import seed_expense, seed_group, seed_member


async def _seed_caller_owes_bob(session: AsyncSession) -> None:
    """bob fronts SGD 10 split evenly with the caller → caller owes bob SGD 5."""
    group = await seed_group(session)
    caller = await seed_member(session, group, telegram_user_id=1001, username="caller")
    bob = await seed_member(session, group, telegram_user_id=2002, username="bob")
    await seed_expense(
        session, group, payer=bob, participants=[caller, bob], amount_cents=1000
    )


async def test_balance_empty_group(dispatcher, session: AsyncSession) -> None:
    await seed_group(session)
    bot = MockedBot()
    await feed(
        dispatcher,
        bot,
        make_message("/balance", from_id=1001, username="caller"),
        session=session,
    )
    assert "no outstanding balances" in (bot.last_reply or "").lower()


async def test_balance_personal_shows_what_you_owe(
    dispatcher, session: AsyncSession
) -> None:
    await _seed_caller_owes_bob(session)
    bot = MockedBot()
    await feed(
        dispatcher,
        bot,
        make_message("/balance", from_id=1001, username="caller"),
        session=session,
    )

    reply = bot.last_reply or ""
    assert "you owe" in reply.lower()
    assert "@bob" in reply
    assert "5.00" in reply


async def test_balance_all_lists_members_and_transfer(
    dispatcher, session: AsyncSession
) -> None:
    await _seed_caller_owes_bob(session)
    bot = MockedBot()
    await feed(
        dispatcher,
        bot,
        make_message("/balance all", from_id=1001, username="caller"),
        session=session,
    )

    reply = bot.last_reply or ""
    assert "Group balances" in reply
    assert "@bob" in reply and "@caller" in reply
    assert "→" in reply  # a suggested transfer is shown
