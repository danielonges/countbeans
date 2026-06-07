"""Handler tests for /event and active-event wiring in /addexpense, /settleup, /balance."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from countbeans.db.models import Expense, Settlement
from countbeans.services.repositories import EventRepository

from ._bot_harness import MockedBot, feed, make_message
from ._seed import read_group, seed_event, seed_expense, seed_group, seed_member


async def test_event_info_shows_status_roster_outstanding(
    dispatcher, session: AsyncSession
) -> None:
    group = await seed_group(session)
    caller = await seed_member(session, group, telegram_user_id=1001, username="caller")
    bob = await seed_member(session, group, telegram_user_id=2002, username="bob")
    ev = await seed_event(
        session, group, creator=caller, name="Trip", default_currency="IDR"
    )
    await seed_expense(
        session,
        group,
        payer=caller,
        participants=[caller, bob],
        amount_cents=200000,
        currency="IDR",
        event_id=ev.event_id,
    )

    bot = MockedBot()
    await feed(
        dispatcher, bot, make_message("/event info", from_id=1001), session=session
    )
    reply = bot.last_reply or ""
    assert "Trip" in reply
    assert "[IDR]" in reply
    assert "active" in reply
    assert "caller" in reply  # roster
    assert "IDR 1000.00" in reply  # bob owes caller half of 2000.00
    assert "#general" in reply  # active event surfaces the one-off general path


async def test_event_info_paused_state(dispatcher, session: AsyncSession) -> None:
    group = await seed_group(session)
    creator = await seed_member(
        session, group, telegram_user_id=1001, username="creator"
    )
    await seed_event(session, group, creator=creator, name="Trip")
    bot = MockedBot(caller_is_admin=True)

    await feed(
        dispatcher, bot, make_message("/event pause", from_id=1001), session=session
    )
    await feed(
        dispatcher, bot, make_message("/event info", from_id=1001), session=session
    )
    assert "paused" in (bot.last_reply or "")


async def test_event_info_no_open_event(dispatcher, session: AsyncSession) -> None:
    await seed_group(session)
    bot = MockedBot()
    await feed(
        dispatcher, bot, make_message("/event info", from_id=1001), session=session
    )
    assert "No event is open" in (bot.last_reply or "")


async def test_event_management_refused_for_non_admin(
    dispatcher, session: AsyncSession
) -> None:
    """Managing an event is admin-only (mirrors /simplify, /currency): a non-admin
    is refused and no event is created — none of the shared state is touched."""
    await seed_group(session)
    bot = MockedBot(caller_is_admin=False)
    await feed(
        dispatcher,
        bot,
        make_message('/event new "Trip"', from_id=1001, username="member"),
        session=session,
    )
    assert "admins" in (bot.last_reply or "").lower()
    assert (await read_group(session)).active_event_id is None


async def test_event_info_open_to_non_admin(dispatcher, session: AsyncSession) -> None:
    """Viewing an event stays open to any member, even one the bot reports as a
    non-admin — only the mutating subcommands are gated."""
    group = await seed_group(session)
    creator = await seed_member(
        session, group, telegram_user_id=1001, username="creator"
    )
    await seed_event(session, group, creator=creator, name="Trip")
    bot = MockedBot(caller_is_admin=False)
    await feed(
        dispatcher, bot, make_message("/event info", from_id=2002), session=session
    )
    assert "Trip" in (bot.last_reply or "")


async def test_event_new_with_currency_stores_and_echoes(
    dispatcher, session: AsyncSession
) -> None:
    await seed_group(session)
    bot = MockedBot(caller_is_admin=True)
    await feed(
        dispatcher,
        bot,
        make_message('/event new "Bali Trip" IDR', from_id=1001, username="creator"),
        session=session,
    )
    reply = bot.last_reply or ""
    assert "Started event" in reply
    assert "[IDR]" in reply
    group = await read_group(session)
    assert group.active_event_id is not None
    ev = await EventRepository(session).get(group.active_event_id)
    assert ev is not None and ev.default_currency == "IDR"


async def test_event_new_unquoted_name_with_currency(
    dispatcher, session: AsyncSession
) -> None:
    """Unquoted `/event new Bali IDR` — the trailing 3-letter token is the
    currency, not part of the name."""
    await seed_group(session)
    bot = MockedBot(caller_is_admin=True)
    await feed(
        dispatcher,
        bot,
        make_message("/event new Bali IDR", from_id=1001, username="creator"),
        session=session,
    )
    reply = bot.last_reply or ""
    assert "Started event" in reply
    assert "[IDR]" in reply
    group = await read_group(session)
    assert group.active_event_id is not None
    ev = await EventRepository(session).get(group.active_event_id)
    assert ev is not None
    assert ev.default_currency == "IDR"
    assert ev.name == "Bali"  # currency not folded into the name


async def test_addexpense_uses_event_default_currency(
    dispatcher, session: AsyncSession
) -> None:
    group = await seed_group(session)
    creator = await seed_member(
        session, group, telegram_user_id=1001, username="creator"
    )
    await seed_event(
        session, group, creator=creator, name="Bali", default_currency="IDR"
    )

    bot = MockedBot()
    await feed(
        dispatcher,
        bot,
        make_message(
            '/addexpense 30000 "Lunch" @bob', from_id=1001, username="creator"
        ),
        session=session,
    )
    assert 'Added to "Bali"' in (bot.last_reply or "")
    # No per-reply pause nudge on an ordinary event expense — the scope echo above
    # is the signal, and #general is the one-step opt-out (CLAUDE.md "Events").
    assert "/event pause" not in (bot.last_reply or "")
    expense = (await session.execute(select(Expense))).scalar_one()
    assert expense.currency == "IDR"


async def test_settleup_uses_event_default_currency(
    dispatcher, session: AsyncSession
) -> None:
    group = await seed_group(session)
    caller = await seed_member(session, group, telegram_user_id=1001, username="caller")
    bob = await seed_member(session, group, telegram_user_id=2002, username="bob")
    ev = await seed_event(
        session, group, creator=caller, name="Bali", default_currency="IDR"
    )
    await seed_expense(
        session,
        group,
        payer=bob,
        participants=[caller, bob],
        amount_cents=200000,
        currency="IDR",
        event_id=ev.event_id,
    )

    bot = MockedBot()
    await feed(
        dispatcher,
        bot,
        make_message("/settleup @bob", from_id=1001, username="caller"),
        session=session,
    )
    assert "Settled up" in (bot.last_reply or "")
    settlement = (await session.execute(select(Settlement))).scalar_one()
    assert settlement.currency == "IDR"


async def test_event_new_sets_active(dispatcher, session: AsyncSession) -> None:
    await seed_group(session)
    bot = MockedBot(caller_is_admin=True)
    await feed(
        dispatcher,
        bot,
        make_message('/event new "Bali Trip"', from_id=1001, username="creator"),
        session=session,
    )
    assert "Started event" in (bot.last_reply or "")
    assert "Bali Trip" in (bot.last_reply or "")
    assert (await read_group(session)).active_event_id is not None


async def test_event_new_rejected_when_one_open(
    dispatcher, session: AsyncSession
) -> None:
    group = await seed_group(session)
    creator = await seed_member(
        session, group, telegram_user_id=1001, username="creator"
    )
    first = await seed_event(session, group, creator=creator, name="One")

    bot = MockedBot(caller_is_admin=True)
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
    creator = await seed_member(
        session, group, telegram_user_id=1001, username="creator"
    )
    ev = await seed_event(session, group, creator=creator, name="Trip")
    bot = MockedBot(caller_is_admin=True)

    await feed(
        dispatcher, bot, make_message("/event pause", from_id=1001), session=session
    )
    assert "Paused" in (bot.last_reply or "")
    assert (await read_group(session)).active_event_id is None

    await feed(
        dispatcher, bot, make_message("/event resume", from_id=1001), session=session
    )
    assert "Resumed" in (bot.last_reply or "")
    assert (await read_group(session)).active_event_id == ev.event_id

    await feed(
        dispatcher, bot, make_message("/event close", from_id=1001), session=session
    )
    assert "Closed" in (bot.last_reply or "")
    assert (await read_group(session)).active_event_id is None
    closed = await EventRepository(session).get(ev.event_id)
    assert closed is not None and closed.status == "closed"


async def test_event_add_remove_roster(dispatcher, session: AsyncSession) -> None:
    group = await seed_group(session)
    creator = await seed_member(
        session, group, telegram_user_id=1001, username="creator"
    )
    ev = await seed_event(session, group, creator=creator, name="Trip")
    bot = MockedBot(caller_is_admin=True)

    await feed(
        dispatcher, bot, make_message("/event add @bob", from_id=1001), session=session
    )
    assert "Added @bob" in (bot.last_reply or "")
    roster = await EventRepository(session).list_members(ev.event_id)
    assert any(m.username == "bob" for m in roster)

    await feed(
        dispatcher,
        bot,
        make_message("/event remove @bob", from_id=1001),
        session=session,
    )
    assert "Removed @bob" in (bot.last_reply or "")
    roster = await EventRepository(session).list_members(ev.event_id)
    assert not any(m.username == "bob" for m in roster)


async def test_event_add_all_folds_group_onto_roster(
    dispatcher, session: AsyncSession
) -> None:
    group = await seed_group(session)
    creator = await seed_member(
        session, group, telegram_user_id=1001, username="creator"
    )
    await seed_member(session, group, telegram_user_id=1002, username="bob")
    await seed_member(session, group, telegram_user_id=1003, username="carol")
    # Event starts with only the creator on the roster (a deliberate subset).
    ev = await seed_event(session, group, creator=creator, name="Trip")
    bot = MockedBot(caller_is_admin=True)

    await feed(
        dispatcher,
        bot,
        make_message("/event add @all", from_id=1001, username="creator"),
        session=session,
    )
    assert "2 group member(s)" in (bot.last_reply or "")  # bob + carol (not creator)
    roster = await EventRepository(session).list_members(ev.event_id)
    assert {m.username for m in roster} == {"creator", "bob", "carol"}

    # Idempotent: a second @all adds nobody new.
    await feed(
        dispatcher,
        bot,
        make_message("/event add @all", from_id=1001, username="creator"),
        session=session,
    )
    assert "already on" in (bot.last_reply or "").lower()


async def test_event_remove_all_refused(dispatcher, session: AsyncSession) -> None:
    group = await seed_group(session)
    creator = await seed_member(
        session, group, telegram_user_id=1001, username="creator"
    )
    await seed_event(session, group, creator=creator, name="Trip")
    bot = MockedBot(caller_is_admin=True)

    await feed(
        dispatcher,
        bot,
        make_message("/event remove @all", from_id=1001),
        session=session,
    )
    assert "isn't removable" in (bot.last_reply or "")


async def test_addexpense_auto_tags_active_event(
    dispatcher, session: AsyncSession
) -> None:
    group = await seed_group(session)
    creator = await seed_member(
        session, group, telegram_user_id=1001, username="creator"
    )
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
    creator = await seed_member(
        session, group, telegram_user_id=1001, username="creator"
    )
    ev = await seed_event(session, group, creator=creator, name="Trip")
    # Roster = {creator, bob}; the chat claims many more members.
    bot = MockedBot(member_count=10, caller_is_admin=True)
    await feed(
        dispatcher, bot, make_message("/event add @bob", from_id=1001), session=session
    )

    await feed(
        dispatcher,
        bot,
        make_message("/addexpense 30", from_id=1001, username="creator"),
        session=session,
    )
    reply = bot.last_reply or ""
    # @all means the roster (an intentional subset) — no group coverage warning.
    assert "haven't interacted" not in reply
    assert 'Added to "Trip"' in reply
    expense = (await session.execute(select(Expense))).scalar_one()
    assert expense.event_id == ev.event_id


async def test_settleup_auto_tags_active_event(
    dispatcher, session: AsyncSession
) -> None:
    group = await seed_group(session)
    caller = await seed_member(session, group, telegram_user_id=1001, username="caller")
    bob = await seed_member(session, group, telegram_user_id=2002, username="bob")
    ev = await seed_event(session, group, creator=caller, name="Trip")
    # Event debt: bob fronts 10 split caller+bob → caller owes bob 5 in the event.
    await seed_expense(
        session,
        group,
        payer=bob,
        participants=[caller, bob],
        amount_cents=1000,
        event_id=ev.event_id,
    )

    bot = MockedBot()
    await feed(
        dispatcher,
        bot,
        make_message("/settleup @bob", from_id=1001, username="caller"),
        session=session,
    )
    reply = bot.last_reply or ""
    assert "Settled up" in reply
    assert "Trip" in reply  # scope echoed
    settlement = (await session.execute(select(Settlement))).scalar_one()
    assert settlement.event_id == ev.event_id


async def test_addexpense_general_override_tags_general(
    dispatcher, session: AsyncSession
) -> None:
    """#general forces a single expense to general scope while an event is active —
    no event tag, general reply wording, and a note confirming it stayed general."""
    group = await seed_group(session)
    creator = await seed_member(
        session, group, telegram_user_id=1001, username="creator"
    )
    await seed_event(session, group, creator=creator, name="Bali")

    bot = MockedBot()
    await feed(
        dispatcher,
        bot,
        make_message(
            '/addexpense 30 "Lunch" #general @bob', from_id=1001, username="creator"
        ),
        session=session,
    )
    reply = bot.last_reply or ""
    assert 'Added to "Bali"' not in reply  # not tagged to the active event
    assert "Added expense" in reply  # general wording
    assert "Logged as general" in reply  # override confirmed
    expense = (await session.execute(select(Expense))).scalar_one()
    assert expense.event_id is None


async def test_addexpense_general_flag_without_event_is_noop(
    dispatcher, session: AsyncSession
) -> None:
    """#general with no active event: a harmless flag — the token is stripped from
    the description and no 'logged as general' note appears (nothing to opt out of)."""
    group = await seed_group(session)
    await seed_member(session, group, telegram_user_id=1001, username="creator")

    bot = MockedBot()
    await feed(
        dispatcher,
        bot,
        make_message(
            "/addexpense 30 Lunch #general @bob", from_id=1001, username="creator"
        ),
        session=session,
    )
    reply = bot.last_reply or ""
    assert "Added expense" in reply
    assert "#general" not in reply  # stripped from the description
    assert "Logged as general" not in reply
    expense = (await session.execute(select(Expense))).scalar_one()
    assert expense.event_id is None


async def test_settleup_general_override_settles_general(
    dispatcher, session: AsyncSession
) -> None:
    """#general settles a general debt while an event is active — the settlement is
    tagged general, not to the event."""
    group = await seed_group(session)
    caller = await seed_member(session, group, telegram_user_id=1001, username="caller")
    bob = await seed_member(session, group, telegram_user_id=2002, username="bob")
    # General debt: bob fronts 10 split caller+bob → caller owes bob 5 in general.
    await seed_expense(
        session, group, payer=bob, participants=[caller, bob], amount_cents=1000
    )
    # An active event whose (empty) scope must NOT capture the settlement.
    await seed_event(session, group, creator=caller, name="Trip")

    bot = MockedBot()
    await feed(
        dispatcher,
        bot,
        make_message("/settleup @bob #general", from_id=1001, username="caller"),
        session=session,
    )
    reply = bot.last_reply or ""
    assert "Settled up" in reply
    assert "Recorded as general" in reply  # override confirmed
    settlement = (await session.execute(select(Settlement))).scalar_one()
    assert settlement.event_id is None


async def test_balance_scoped_to_active_event(
    dispatcher, session: AsyncSession
) -> None:
    group = await seed_group(session)
    caller = await seed_member(session, group, telegram_user_id=1001, username="caller")
    bob = await seed_member(session, group, telegram_user_id=2002, username="bob")
    # General debt of 5.00 (excluded while the event is active).
    await seed_expense(
        session, group, payer=caller, participants=[caller, bob], amount_cents=1000
    )
    # Event debt of 15.00.
    ev = await seed_event(session, group, creator=caller, name="Trip")
    await seed_expense(
        session,
        group,
        payer=bob,
        participants=[caller, bob],
        amount_cents=3000,
        event_id=ev.event_id,
    )

    bot = MockedBot()
    await feed(
        dispatcher,
        bot,
        make_message("/balance all", from_id=1001, username="caller"),
        session=session,
    )
    reply = bot.last_reply or ""
    assert 'Balances for "Trip"' in reply
    assert "SGD 15.00" in reply  # the event balance
    assert "SGD 5.00" not in reply  # the general balance is out of scope
