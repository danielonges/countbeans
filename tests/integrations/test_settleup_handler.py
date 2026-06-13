"""Handler tests for /settleup — arg parsing, unknown handles, the @all admin
gate, and the bare-command tap-to-settle picker."""

from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from countbeans.db.models import Settlement
from countbeans.services.repositories import UserRepository

from ._bot_harness import MockedBot, feed, feed_callback, make_callback, make_message
from ._seed import seed_event, seed_expense, seed_group, seed_member


async def _settlement_count(session: AsyncSession) -> int:
    return (
        await session.execute(select(func.count()).select_from(Settlement))
    ).scalar_one()


def _buttons(bot: MockedBot) -> list[tuple[str, str]]:
    """(label, callback_data) pairs on the most recent reply's keyboard."""
    markup = bot.sent[-1].reply_markup
    assert isinstance(markup, InlineKeyboardMarkup), "reply carries no keyboard"
    return [
        (b.text, b.callback_data or "") for row in markup.inline_keyboard for b in row
    ]


async def _seed_caller_owes_bob(session: AsyncSession) -> None:
    group = await seed_group(session)
    caller = await seed_member(session, group, telegram_user_id=1001, username="caller")
    bob = await seed_member(session, group, telegram_user_id=2002, username="bob")
    await seed_expense(
        session, group, payer=bob, participants=[caller, bob], amount_cents=1000
    )


async def _seed_alice_owes_bob(session: AsyncSession) -> None:
    """Two members (neither the caller) with a debt: alice owes bob SGD 5 — the
    on-behalf shape, e.g. when alice has since left the group."""
    group = await seed_group(session)
    alice = await seed_member(session, group, telegram_user_id=3003, username="alice")
    bob = await seed_member(session, group, telegram_user_id=2002, username="bob")
    await seed_expense(
        session, group, payer=bob, participants=[alice, bob], amount_cents=1000
    )


async def test_settleup_usage_on_bad_args(dispatcher, session: AsyncSession) -> None:
    bot = MockedBot()
    await feed(
        dispatcher, bot, make_message("/settleup not-a-mention"), session=session
    )
    assert "Usage" in (bot.last_reply or "")


async def test_settleup_unknown_handle_rejected(
    dispatcher, session: AsyncSession
) -> None:
    await seed_group(session)
    bot = MockedBot()
    await feed(
        dispatcher,
        bot,
        make_message("/settleup @nobody 5", from_id=1001, username="caller"),
        session=session,
    )

    assert "don't know @nobody" in (bot.last_reply or "")
    # A typo must not spawn a placeholder for the unknown handle.
    assert await UserRepository(session).find_by_mention("nobody") is None


async def test_settleup_records_full_owed_amount(
    dispatcher, session: AsyncSession
) -> None:
    await _seed_caller_owes_bob(session)
    bot = MockedBot()
    # Omit the amount → settle the full suggested debt (SGD 5).
    await feed(
        dispatcher,
        bot,
        make_message("/settleup @bob", from_id=1001, username="caller"),
        session=session,
    )

    assert "Settled up" in (bot.last_reply or "")
    assert "5.00" in (bot.last_reply or "")
    assert await _settlement_count(session) == 1


async def test_settleup_all_refused_for_non_admin(
    dispatcher, session: AsyncSession
) -> None:
    await _seed_caller_owes_bob(session)
    bot = MockedBot(caller_is_admin=False)
    await feed(
        dispatcher,
        bot,
        make_message("/settleup @all", from_id=1001, username="caller"),
        session=session,
    )

    assert "admin" in (bot.last_reply or "").lower()
    assert await _settlement_count(session) == 0  # nothing recorded


async def test_settleup_all_by_admin_clears_group(
    dispatcher, session: AsyncSession
) -> None:
    await _seed_caller_owes_bob(session)
    bot = MockedBot(caller_is_admin=True)
    await feed(
        dispatcher,
        bot,
        make_message("/settleup @all", from_id=1001, username="caller"),
        session=session,
    )

    assert "whole group" in (bot.last_reply or "").lower()
    assert await _settlement_count(session) == 1  # the one outstanding transfer


async def test_settleup_pair_refused_for_non_admin(
    dispatcher, session: AsyncSession
) -> None:
    await _seed_alice_owes_bob(session)
    bot = MockedBot(caller_is_admin=False)
    await feed(
        dispatcher,
        bot,
        make_message("/settleup @alice @bob", from_id=1001, username="caller"),
        session=session,
    )

    assert "admin" in (bot.last_reply or "").lower()
    assert await _settlement_count(session) == 0  # nothing recorded


async def test_settleup_pair_by_admin_records_full_owed(
    dispatcher, session: AsyncSession
) -> None:
    """An admin clears a debt between two *other* members (the departed-member
    case) — amount omitted settles the full SGD 5 alice owes bob."""
    await _seed_alice_owes_bob(session)
    bot = MockedBot(caller_is_admin=True)
    await feed(
        dispatcher,
        bot,
        make_message("/settleup @alice @bob", from_id=1001, username="caller"),
        session=session,
    )

    reply = bot.last_reply or ""
    assert "Recorded" in reply
    assert "5.00" in reply
    # The recipient (still in the group) is pinged to confirm the logged payment.
    assert "@bob — flag it" in reply
    assert await _settlement_count(session) == 1


async def test_settleup_pair_records_explicit_amount(
    dispatcher, session: AsyncSession
) -> None:
    await _seed_alice_owes_bob(session)
    bot = MockedBot(caller_is_admin=True)
    await feed(
        dispatcher,
        bot,
        make_message("/settleup @alice @bob 3", from_id=1001, username="caller"),
        session=session,
    )

    assert "3.00" in (bot.last_reply or "")
    assert await _settlement_count(session) == 1


async def test_settleup_pair_rejects_all_keyword(
    dispatcher, session: AsyncSession
) -> None:
    await _seed_alice_owes_bob(session)
    bot = MockedBot(caller_is_admin=True)
    await feed(
        dispatcher,
        bot,
        make_message("/settleup @alice @all", from_id=1001, username="caller"),
        session=session,
    )

    assert "@all" in (bot.last_reply or "")
    assert await _settlement_count(session) == 0


async def test_settleup_pair_unknown_handle_rejected(
    dispatcher, session: AsyncSession
) -> None:
    await _seed_alice_owes_bob(session)
    bot = MockedBot(caller_is_admin=True)
    await feed(
        dispatcher,
        bot,
        make_message("/settleup @nobody @bob 3", from_id=1001, username="caller"),
        session=session,
    )

    assert "don't know @nobody" in (bot.last_reply or "")
    assert await _settlement_count(session) == 0


async def test_settleup_pair_wrong_direction_rejected(
    dispatcher, session: AsyncSession
) -> None:
    """alice owes bob, so recording bob→alice has no suggested payment."""
    await _seed_alice_owes_bob(session)
    bot = MockedBot(caller_is_admin=True)
    await feed(
        dispatcher,
        bot,
        make_message("/settleup @bob @alice 3", from_id=1001, username="caller"),
        session=session,
    )

    assert "debt runs in that direction" in (bot.last_reply or "")
    assert await _settlement_count(session) == 0


# --- the bare-command picker (tap-to-settle) --------------------------------


async def test_bare_settleup_all_settled(dispatcher, session: AsyncSession) -> None:
    """No debts → a reassuring empty state, not the usage block."""
    await seed_group(session)
    bot = MockedBot()
    await feed(
        dispatcher,
        bot,
        make_message("/settleup", from_id=1001, username="caller"),
        session=session,
    )

    reply = bot.last_reply or ""
    assert "all settled up" in reply.lower()
    assert "Usage" not in reply
    assert bot.sent[-1].reply_markup is None


async def test_bare_settleup_tap_settles_in_full(
    dispatcher, session: AsyncSession
) -> None:
    """The picker lists the caller's payment as a button; one tap records it in
    full, announces it to the chat, and repaints the picker to all-settled."""
    await _seed_caller_owes_bob(session)
    bot = MockedBot()
    await feed(
        dispatcher,
        bot,
        make_message("/settleup", from_id=1001, username="caller"),
        session=session,
    )

    reply = bot.last_reply or ""
    assert "tap one" in reply
    labels = _buttons(bot)
    pay = next((data for text, data in labels if "Pay @bob" in text), None)
    assert pay is not None and pay.startswith("st:p:k-:")
    assert any(data.startswith("st:x:") for _, data in labels)  # Close button

    await feed_callback(
        dispatcher,
        bot,
        make_callback(pay, from_id=1001, username="caller"),
        session=session,
    )

    assert await _settlement_count(session) == 1
    confirmation = bot.sent[-1].text or ""
    assert "Settled up" in confirmation and "5.00" in confirmation
    assert "all settled up" in (bot.last_edit or "").lower()  # picker repainted
    assert (
        "recorded" in ((bot.last_answer.text if bot.last_answer else "") or "").lower()
    )


async def test_pay_button_bound_to_debtor(dispatcher, session: AsyncSession) -> None:
    """Someone else tapping the caller's pay button gets an alert; no write."""
    await _seed_caller_owes_bob(session)
    bot = MockedBot()
    await feed(
        dispatcher,
        bot,
        make_message("/settleup", from_id=1001, username="caller"),
        session=session,
    )
    pay = next(data for text, data in _buttons(bot) if data.startswith("st:p:"))

    await feed_callback(
        dispatcher,
        bot,
        make_callback(pay, from_id=2002, username="bob"),
        session=session,
    )

    answer = bot.last_answer
    assert answer is not None and answer.show_alert
    assert "someone else's payment" in (answer.text or "")
    assert await _settlement_count(session) == 0


async def test_stale_pay_button_alerts_without_writing(
    dispatcher, session: AsyncSession
) -> None:
    """A button whose debt was settled meanwhile alerts and repaints — it never
    records a second settlement."""
    await _seed_caller_owes_bob(session)
    bot = MockedBot()
    await feed(
        dispatcher,
        bot,
        make_message("/settleup", from_id=1001, username="caller"),
        session=session,
    )
    pay = next(data for text, data in _buttons(bot) if data.startswith("st:p:"))

    # The debt gets settled the typed way first…
    await feed(
        dispatcher,
        bot,
        make_message("/settleup @bob", from_id=1001, username="caller"),
        session=session,
    )
    assert await _settlement_count(session) == 1

    # …then the old button is tapped.
    await feed_callback(
        dispatcher,
        bot,
        make_callback(pay, from_id=1001, username="caller"),
        session=session,
    )

    answer = bot.last_answer
    assert answer is not None and answer.show_alert
    assert "no longer suggested" in (answer.text or "")
    assert await _settlement_count(session) == 1  # still just the typed one
    assert "all settled up" in (bot.last_edit or "").lower()  # repainted


async def test_picker_close_is_owner_bound(dispatcher, session: AsyncSession) -> None:
    await _seed_caller_owes_bob(session)
    bot = MockedBot()
    await feed(
        dispatcher,
        bot,
        make_message("/settleup", from_id=1001, username="caller"),
        session=session,
    )
    close = next(data for _, data in _buttons(bot) if data.startswith("st:x:"))

    # Someone else can't close it…
    await feed_callback(
        dispatcher,
        bot,
        make_callback(close, from_id=2002, username="bob"),
        session=session,
    )
    answer = bot.last_answer
    assert answer is not None and answer.show_alert
    assert not bot.edits

    # …the owner can.
    await feed_callback(
        dispatcher,
        bot,
        make_callback(close, from_id=1001, username="caller"),
        session=session,
    )
    assert "closed" in (bot.last_edit or "").lower()
    assert await _settlement_count(session) == 0


async def test_picker_in_event_scope_settles_event(
    dispatcher, session: AsyncSession
) -> None:
    """With an event active, the picker shows and settles that event's debts —
    the recorded settlement carries the event id."""
    group = await seed_group(session)
    caller = await seed_member(session, group, telegram_user_id=1001, username="caller")
    bob = await seed_member(session, group, telegram_user_id=2002, username="bob")
    event = await seed_event(session, group, creator=caller, name="Bali")
    await seed_expense(
        session,
        group,
        payer=bob,
        participants=[caller, bob],
        amount_cents=1000,
        event_id=event.event_id,
    )

    bot = MockedBot()
    await feed(
        dispatcher,
        bot,
        make_message("/settleup", from_id=1001, username="caller"),
        session=session,
    )
    assert 'in "Bali"' in (bot.last_reply or "")
    pay = next(data for text, data in _buttons(bot) if data.startswith("st:p:"))

    await feed_callback(
        dispatcher,
        bot,
        make_callback(pay, from_id=1001, username="caller"),
        session=session,
    )

    recorded_event_id = (
        await session.execute(select(Settlement.event_id))
    ).scalar_one()
    assert recorded_event_id == event.event_id
    assert 'in "Bali"' in (bot.sent[-1].text or "")


async def test_general_picker_mid_event_settles_general(
    dispatcher, session: AsyncSession
) -> None:
    """/settleup #general while an event is active: the picker shows general
    debts, and a tap settles the general scope — never the event."""
    group = await seed_group(session)
    caller = await seed_member(session, group, telegram_user_id=1001, username="caller")
    bob = await seed_member(session, group, telegram_user_id=2002, username="bob")
    # A general debt first, then an event opens (active, but holds no debt).
    await seed_expense(
        session, group, payer=bob, participants=[caller, bob], amount_cents=1000
    )
    await seed_event(session, group, creator=caller, name="Bali")

    bot = MockedBot()
    await feed(
        dispatcher,
        bot,
        make_message("/settleup #general", from_id=1001, username="caller"),
        session=session,
    )
    pay = next(data for text, data in _buttons(bot) if data.startswith("st:p:"))
    assert pay.startswith("st:p:kg:")  # the general override travels in the button

    await feed_callback(
        dispatcher,
        bot,
        make_callback(pay, from_id=1001, username="caller"),
        session=session,
    )

    recorded_event_id = (
        await session.execute(select(Settlement.event_id))
    ).scalar_one()
    assert recorded_event_id is None
    confirmation = bot.sent[-1].text or ""
    assert "Recorded as general" in confirmation and '"Bali"' in confirmation
