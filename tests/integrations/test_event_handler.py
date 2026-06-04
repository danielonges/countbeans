"""Handler tests for /event and active-event wiring in /addexpense, /settleup, /balance."""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from countbeans.db.models import Expense, Settlement
from countbeans.services.repositories import EventRepository

from ._bot_harness import MockedBot, feed, make_message
from ._seed import read_group, seed_event, seed_expense, seed_group, seed_member


async def test_event_new_sets_active(dispatcher, session: AsyncSession) -> None:
    await seed_group(session)
    bot = MockedBot()
    await feed(
        dispatcher,
        bot,
        make_message('/event new "Bali Trip"', from_id=1001, username="creator"),
        session=session,
    )
    assert "Started event" in (bot.last_reply or "")
    assert "Bali Trip" in (bot.last_reply or "")
    assert (await read_group(session)).active_event_id is not None


async def test_event_new_rejected_when_one_open(dispatcher, session: AsyncSession) -> None:
    group = await seed_group(session)
    creator = await seed_member(session, group, telegram_user_id=1001, username="creator")
    first = await seed_event(session, group, creator=creator, name="One")

    bot = MockedBot()
    await feed(
        dispatcher,
        bot,
        make_message('/event new "Two"', from_id=1001, username="creator"),
        session=session,
    )
    assert "already open" in (bot.last_reply or "")
    # The active pointer still references the first event.
    assert (await read_group(session)).active_event_id == first.event_id


async def test_event_pause_resume_close_flow(dispatcher, session: AsyncSession) -> None:
    group = await seed_group(session)
    creator = await seed_member(session, group, telegram_user_id=1001, username="creator")
    ev = await seed_event(session, group, creator=creator, name="Trip")
    bot = MockedBot()

    await feed(dispatcher, bot, make_message("/event pause", from_id=1001), session=session)
    assert "Paused" in (bot.last_reply or "")
    assert (await read_group(session)).active_event_id is None

    await feed(dispatcher, bot, make_message("/event resume", from_id=1001), session=session)
    assert "Resumed" in (bot.last_reply or "")
    assert (await read_group(session)).active_event_id == ev.event_id

    await feed(dispatcher, bot, make_message("/event close", from_id=1001), session=session)
    assert "Closed" in (bot.last_reply or "")
    assert (await read_group(session)).active_event_id is None
    closed = await EventRepository(session).get(ev.event_id)
    assert closed is not None and closed.status == "closed"


async def test_event_add_remove_roster(dispatcher, session: AsyncSession) -> None:
    group = await seed_group(session)
    creator = await seed_member(session, group, telegram_user_id=1001, username="creator")
    ev = await seed_event(session, group, creator=creator, name="Trip")
    bot = MockedBot()

    await feed(dispatcher, bot, make_message("/event add @bob", from_id=1001), session=session)
    assert "Added @bob" in (bot.last_reply or "")
    roster = await EventRepository(session).list_members(ev.event_id)
    assert any(m.username == "bob" for m in roster)

    await feed(dispatcher, bot, make_message("/event remove @bob", from_id=1001), session=session)
    assert "Removed @bob" in (bot.last_reply or "")
    roster = await EventRepository(session).list_members(ev.event_id)
    assert not any(m.username == "bob" for m in roster)


async def test_addexpense_auto_tags_active_event(dispatcher, session: AsyncSession) -> None:
    group = await seed_group(session)
    creator = await seed_member(session, group, telegram_user_id=1001, username="creator")
    await seed_event(session, group, creator=creator, name="Bali")

    bot = MockedBot()
    await feed(
        dispatcher,
        bot,
        make_message('/addexpense 30 "Lunch" @bob', from_id=1001, username="creator"),
        session=session,
    )
    assert 'Added to "Bali"' in (bot.last_reply or "")
    expense = (await session.execute(select(Expense))).scalar_one()
    assert expense.event_id is not None


async def test_event_all_splits_roster_without_coverage_warning(
    dispatcher, session: AsyncSession
) -> None:
    group = await seed_group(session)
    creator = await seed_member(session, group, telegram_user_id=1001, username="creator")
    ev = await seed_event(session, group, creator=creator, name="Trip")
    # Roster = {creator, bob}; the chat claims many more members.
    bot = MockedBot(member_count=10)
    await feed(dispatcher, bot, make_message("/event add @bob", from_id=1001), session=session)

    await feed(
        dispatcher, bot, make_message("/addexpense 30", from_id=1001, username="creator"), session=session
    )
    reply = bot.last_reply or ""
    # @all means the roster (an intentional subset) — no group coverage warning.
    assert "haven't interacted" not in reply
    assert 'Added to "Trip"' in reply
    expense = (await session.execute(select(Expense))).scalar_one()
    assert expense.event_id == ev.event_id


async def test_settleup_auto_tags_active_event(dispatcher, session: AsyncSession) -> None:
    group = await seed_group(session)
    caller = await seed_member(session, group, telegram_user_id=1001, username="caller")
    bob = await seed_member(session, group, telegram_user_id=2002, username="bob")
    ev = await seed_event(session, group, creator=caller, name="Trip")
    # Event debt: bob fronts 10 split caller+bob → caller owes bob 5 in the event.
    await seed_expense(
        session, group, payer=bob, participants=[caller, bob], amount_cents=1000, event_id=ev.event_id
    )

    bot = MockedBot()
    await feed(dispatcher, bot, make_message("/settleup @bob", from_id=1001, username="caller"), session=session)
    reply = bot.last_reply or ""
    assert "Settled up" in reply
    assert 'Trip' in reply  # scope echoed
    settlement = (await session.execute(select(Settlement))).scalar_one()
    assert settlement.event_id == ev.event_id


async def test_balance_scoped_to_active_event(dispatcher, session: AsyncSession) -> None:
    group = await seed_group(session)
    caller = await seed_member(session, group, telegram_user_id=1001, username="caller")
    bob = await seed_member(session, group, telegram_user_id=2002, username="bob")
    # General debt of 5.00 (excluded while the event is active).
    await seed_expense(session, group, payer=caller, participants=[caller, bob], amount_cents=1000)
    # Event debt of 15.00.
    ev = await seed_event(session, group, creator=caller, name="Trip")
    await seed_expense(
        session, group, payer=bob, participants=[caller, bob], amount_cents=3000, event_id=ev.event_id
    )

    bot = MockedBot()
    await feed(dispatcher, bot, make_message("/balance all", from_id=1001, username="caller"), session=session)
    reply = bot.last_reply or ""
    assert 'Balances for "Trip"' in reply
    assert "SGD 15.00" in reply       # the event balance
    assert "SGD 5.00" not in reply    # the general balance is out of scope
