"""Handler tests for the interactive /addexpense wizard.

Drives the multi-step flow through the real Dispatcher (FSM state lives in the
harness's MemoryStorage, reset between tests by the conftest autouse fixture).
Free-text steps are fed as replies to the bot's ForceReply prompt — in the
harness every bot send is message_id 999, so a reply carries
``reply_to_message_id=999``; button taps are fed as callback queries.
"""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from countbeans.db.models import Expense, ExpenseShare, User

from ._bot_harness import (
    MockedBot,
    feed,
    feed_callback,
    make_callback,
    make_message,
)
from ._seed import seed_event, seed_group, seed_member

# Every bot send in the harness is message_id 999, so the wizard anchor is 999;
# the ownership gate binds taps to that anchor, so taps must carry the same id.
_ANCHOR_ID = 999


def make_tap(data: str, *, from_id: int = 1001):
    """A tap on the wizard anchor (message 999) from the initiator (default)."""
    return make_callback(data, from_id=from_id, message_id=_ANCHOR_ID)


async def _expense_count(session: AsyncSession) -> int:
    return (
        await session.execute(select(func.count()).select_from(Expense))
    ).scalar_one()


async def _shares_by_username(session: AsyncSession) -> dict[str | None, int]:
    rows = (
        await session.execute(
            select(User.username, ExpenseShare.share_cents).join(
                ExpenseShare, ExpenseShare.user_id == User.id
            )
        )
    ).all()
    return {username: cents for username, cents in rows}


async def _start_to_roster(
    dispatcher, bot: MockedBot, session: AsyncSession, *, amount: str
) -> None:
    """Drive the wizard from a bare /addexpense through the amount step, leaving
    the anchor on the participant roster. The amount is sent as a **plain typed
    message** (no reply) — the realistic path for a bot that can read group
    messages, and the regression that the reply-only filter used to drop."""
    await feed(
        dispatcher,
        bot,
        make_message("/addexpense", from_id=1001, username="alice"),
        session=session,
    )
    await feed(
        dispatcher,
        bot,
        make_message(amount, from_id=1001),
        session=session,
    )


# --- routing & entry --------------------------------------------------------


async def test_bare_command_starts_wizard(dispatcher, session: AsyncSession) -> None:
    await seed_group(session)
    bot = MockedBot()
    await feed(dispatcher, bot, make_message("/addexpense"), session=session)
    assert "how much" in (bot.last_reply or "").lower()
    # The first prompt has no inline keyboard (it's a ForceReply), so it must
    # surface the /cancel escape hatch in text.
    assert "/cancel" in (bot.last_reply or "")
    assert await _expense_count(session) == 0


# --- amount-line description ------------------------------------------------


async def test_amount_line_accepts_inline_description(
    dispatcher, session: AsyncSession
) -> None:
    group = await seed_group(session)
    await seed_member(session, group, telegram_user_id=2002, username="bob")
    bot = MockedBot(member_count=3)
    await feed(
        dispatcher,
        bot,
        make_message("/addexpense", from_id=1001, username="alice"),
        session=session,
    )
    # Amount + free-text description on one line (no quotes; the apostrophe stays).
    await feed(
        dispatcher,
        bot,
        make_message("50.25 1 night at domino's pizza", from_id=1001),
        session=session,
    )
    assert "For: 1 night at domino's pizza" in (bot.last_reply or "")
    assert "SGD 50.25" in (bot.last_reply or "")

    await feed_callback(dispatcher, bot, make_tap("ax:pdone"), session=session)
    await feed_callback(dispatcher, bot, make_tap("ax:m:equal"), session=session)
    await feed_callback(dispatcher, bot, make_tap("ax:ok"), session=session)
    assert "1 night at domino's pizza" in (bot.last_edit or "")
    assert await _expense_count(session) == 1


async def test_amount_line_strips_wrapping_quotes(
    dispatcher, session: AsyncSession
) -> None:
    await seed_group(session)
    bot = MockedBot()
    await feed(
        dispatcher,
        bot,
        make_message("/addexpense", from_id=1001, username="alice"),
        session=session,
    )
    await feed(
        dispatcher, bot, make_message('12 "team dinner"', from_id=1001), session=session
    )
    # The wrapping quotes are stripped — "For: team dinner", not 'For: "team dinner"'.
    assert "For: team dinner" in (bot.last_reply or "")


# --- happy path: everyone, equal -------------------------------------------


async def test_everyone_equal_records_expense(
    dispatcher, session: AsyncSession
) -> None:
    group = await seed_group(session)
    await seed_member(session, group, telegram_user_id=2002, username="bob")
    await seed_member(session, group, telegram_user_id=3003, username="carol")

    bot = MockedBot(member_count=4)  # alice + bob + carol + the bot
    await _start_to_roster(dispatcher, bot, session, amount="25.50")

    # The roster opens with everyone selected (matches inline "no mentions = all").
    assert "3 selected" in (bot.last_reply or "")

    await feed_callback(dispatcher, bot, make_tap("ax:pdone"), session=session)
    await feed_callback(dispatcher, bot, make_tap("ax:m:equal"), session=session)
    await feed_callback(dispatcher, bot, make_tap("ax:ok"), session=session)

    assert "Added expense" in (bot.last_edit or "")
    assert "SGD 25.50" in (bot.last_edit or "")
    assert await _expense_count(session) == 1
    assert await _shares_by_username(session) == {
        "alice": 850,
        "bob": 850,
        "carol": 850,
    }


async def test_everyone_split_warns_on_coverage_gap(
    dispatcher, session: AsyncSession
) -> None:
    group = await seed_group(session)
    await seed_member(session, group, telegram_user_id=2002, username="bob")

    # 5 real members in the chat (+ bot) but only alice & bob have interacted.
    bot = MockedBot(member_count=6)
    await _start_to_roster(dispatcher, bot, session, amount="10")

    await feed_callback(dispatcher, bot, make_tap("ax:pdone"), session=session)
    await feed_callback(dispatcher, bot, make_tap("ax:m:equal"), session=session)
    await feed_callback(dispatcher, bot, make_tap("ax:ok"), session=session)

    assert "haven't interacted" in (bot.last_edit or "")
    assert await _expense_count(session) == 1


# --- participant toggle -----------------------------------------------------


async def test_toggling_a_member_out_excludes_them(
    dispatcher, session: AsyncSession
) -> None:
    group = await seed_group(session)
    await seed_member(session, group, telegram_user_id=2002, username="bob")
    await seed_member(session, group, telegram_user_id=3003, username="carol")

    bot = MockedBot(member_count=4)
    await _start_to_roster(dispatcher, bot, session, amount="30")

    # Roster is ordered by username: alice(0), bob(1), carol(2). Drop carol.
    await feed_callback(dispatcher, bot, make_tap("ax:p:2"), session=session)
    assert "2 selected" in (bot.last_edit or "")

    await feed_callback(dispatcher, bot, make_tap("ax:pdone"), session=session)
    await feed_callback(dispatcher, bot, make_tap("ax:m:equal"), session=session)
    await feed_callback(dispatcher, bot, make_tap("ax:ok"), session=session)

    shares = await _shares_by_username(session)
    assert shares == {"alice": 1500, "bob": 1500}
    assert "carol" not in shares


# --- exact split: reconcile gating -----------------------------------------


async def test_exact_split_blocks_until_reconciled(
    dispatcher, session: AsyncSession
) -> None:
    group = await seed_group(session)
    await seed_member(session, group, telegram_user_id=2002, username="bob")
    await seed_member(session, group, telegram_user_id=3003, username="carol")

    bot = MockedBot(member_count=4)
    await _start_to_roster(dispatcher, bot, session, amount="30")  # 3000 cents

    await feed_callback(dispatcher, bot, make_tap("ax:pdone"), session=session)
    await feed_callback(dispatcher, bot, make_tap("ax:m:exact"), session=session)

    # Enter 10 + 10 + 5 = 2500 ≠ 3000 → Confirm must be refused.
    for idx, amount in (("ax:s:0", "10"), ("ax:s:1", "10"), ("ax:s:2", "5")):
        await feed_callback(dispatcher, bot, make_tap(idx), session=session)
        await feed(
            dispatcher,
            bot,
            make_message(amount, from_id=1001, reply_to_message_id=999),
            session=session,
        )

    await feed_callback(dispatcher, bot, make_tap("ax:ok"), session=session)
    assert "total" in ((bot.last_answer and bot.last_answer.text) or "").lower()
    assert await _expense_count(session) == 0

    # Fix carol's share to 10 → total 3000 → Confirm now writes.
    await feed_callback(dispatcher, bot, make_tap("ax:s:2"), session=session)
    await feed(
        dispatcher,
        bot,
        make_message("10", from_id=1001, reply_to_message_id=999),
        session=session,
    )
    await feed_callback(dispatcher, bot, make_tap("ax:ok"), session=session)

    assert await _expense_count(session) == 1
    assert await _shares_by_username(session) == {
        "alice": 1000,
        "bob": 1000,
        "carol": 1000,
    }


# --- cancellation -----------------------------------------------------------


async def test_cancel_button_aborts(dispatcher, session: AsyncSession) -> None:
    group = await seed_group(session)
    await seed_member(session, group, telegram_user_id=2002, username="bob")

    bot = MockedBot()
    await _start_to_roster(dispatcher, bot, session, amount="12")
    await feed_callback(dispatcher, bot, make_tap("ax:x"), session=session)

    assert "cancelled" in (bot.last_edit or "").lower()
    assert await _expense_count(session) == 0

    # State is cleared: a further tap matches no handler and records nothing.
    await feed_callback(dispatcher, bot, make_tap("ax:ok"), session=session)
    assert await _expense_count(session) == 0


async def test_command_at_amount_step_is_not_eaten(
    dispatcher, session: AsyncSession
) -> None:
    # A command typed mid-wizard must route to its own handler, not be parsed as
    # the amount — the free-text step filter excludes anything starting with "/".
    await seed_group(session)
    bot = MockedBot()
    await feed(
        dispatcher,
        bot,
        make_message("/addexpense", from_id=1001, username="alice"),
        session=session,
    )
    await feed(dispatcher, bot, make_message("/cancel", from_id=1001), session=session)
    assert "ancel" in (bot.last_reply or "")
    assert "Invalid amount" not in (bot.last_reply or "")


# --- ownership --------------------------------------------------------------


async def test_other_user_cannot_drive_draft(dispatcher, session: AsyncSession) -> None:
    group = await seed_group(session)
    await seed_member(session, group, telegram_user_id=2002, username="bob")

    bot = MockedBot()
    await _start_to_roster(dispatcher, bot, session, amount="40")

    # Bob (2002) taps the initiator's (alice, 1001) anchor — rejected.
    await feed_callback(
        dispatcher, bot, make_tap("ax:clear", from_id=2002), session=session
    )
    assert "isn't your" in ((bot.last_answer and bot.last_answer.text) or "").lower()

    # The draft is untouched: alice can still confirm everyone.
    await feed_callback(dispatcher, bot, make_tap("ax:pdone"), session=session)
    await feed_callback(dispatcher, bot, make_tap("ax:m:equal"), session=session)
    await feed_callback(dispatcher, bot, make_tap("ax:ok"), session=session)
    assert await _expense_count(session) == 1


# --- events / #general parity ----------------------------------------------


async def test_event_scope_defaults_and_general_toggle(
    dispatcher, session: AsyncSession
) -> None:
    group = await seed_group(session)
    alice = await seed_member(session, group, telegram_user_id=1001, username="alice")
    await seed_member(session, group, telegram_user_id=2002, username="bob")
    await seed_event(session, group, creator=alice, name="Trip")

    bot = MockedBot()
    await _start_to_roster(dispatcher, bot, session, amount="20")

    # An active event scopes the draft to the event roster (just alice so far).
    assert "Scope: Trip" in (bot.last_reply or "")
    assert "1 selected" in (bot.last_reply or "")

    # #general flips to the whole-group, no-event scope (alice + bob).
    await feed_callback(dispatcher, bot, make_tap("ax:gen"), session=session)
    assert "Scope: General" in (bot.last_edit or "")
    assert "2 selected" in (bot.last_edit or "")


async def test_event_scope_tags_expense_to_event(
    dispatcher, session: AsyncSession
) -> None:
    group = await seed_group(session)
    alice = await seed_member(session, group, telegram_user_id=1001, username="alice")
    event = await seed_event(session, group, creator=alice, name="Trip")

    bot = MockedBot()
    await _start_to_roster(dispatcher, bot, session, amount="20")
    await feed_callback(dispatcher, bot, make_tap("ax:pdone"), session=session)
    await feed_callback(dispatcher, bot, make_tap("ax:m:equal"), session=session)
    await feed_callback(dispatcher, bot, make_tap("ax:ok"), session=session)

    assert 'Added to "Trip"' in (bot.last_edit or "")
    row = (await session.execute(select(Expense))).scalar_one()
    assert row.event_id == event.event_id
