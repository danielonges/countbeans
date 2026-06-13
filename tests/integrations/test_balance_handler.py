"""Handler tests for /balance and /balance all over a seeded debt."""

from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from countbeans.db.models import Settlement

from ._bot_harness import MockedBot, feed, feed_callback, make_callback, make_message
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
    # Plain words, not the system vocabulary "simplified"/"raw" (default is ON).
    assert "fewest payments" in reply
    assert "simplified" not in reply and "(raw)" not in reply


def _all_buttons(bot: MockedBot) -> list[tuple[str, str]]:
    markup = bot.sent[-1].reply_markup
    assert isinstance(markup, InlineKeyboardMarkup), "reply carries no keyboard"
    return [
        (b.text, b.callback_data or "") for row in markup.inline_keyboard for b in row
    ]


def _pay_buttons(bot: MockedBot) -> list[tuple[str, str]]:
    """Only the tap-to-settle buttons (every view also carries a me⇄all pivot)."""
    return [(t, d) for t, d in _all_buttons(bot) if d.startswith("st:p:")]


def _pivot_data(bot: MockedBot) -> str | None:
    return next((d for _, d in _all_buttons(bot) if d.startswith("bal:")), None)


async def test_balance_all_transfer_button_settles(
    dispatcher, session: AsyncSession
) -> None:
    """/balance all carries a tap-to-settle button per suggested transfer; the
    debtor's tap records the payment and repaints the view."""
    await _seed_caller_owes_bob(session)
    bot = MockedBot()
    await feed(
        dispatcher,
        bot,
        make_message("/balance all", from_id=1001, username="caller"),
        session=session,
    )

    buttons = _pay_buttons(bot)
    assert len(buttons) == 1
    label, data = buttons[0]
    assert "@caller" in label and "@bob" in label and "5.00" in label
    assert data.startswith("st:p:a-:")

    await feed_callback(
        dispatcher,
        bot,
        make_callback(data, from_id=1001, username="caller"),
        session=session,
    )

    count = (
        await session.execute(select(func.count()).select_from(Settlement))
    ).scalar_one()
    assert count == 1
    assert "Settled up" in (bot.sent[-1].text or "")
    # The balance view repaints to the post-settle state.
    assert "no outstanding balances" in (bot.last_edit or "").lower()


async def test_personal_balance_has_pay_button(
    dispatcher, session: AsyncSession
) -> None:
    """The personal view offers the caller's own payments as buttons (and only
    those — money owed *to* the caller isn't theirs to move)."""
    await _seed_caller_owes_bob(session)
    bot = MockedBot()
    await feed(
        dispatcher,
        bot,
        make_message("/balance", from_id=1001, username="caller"),
        session=session,
    )

    buttons = _pay_buttons(bot)
    assert len(buttons) == 1
    label, data = buttons[0]
    assert "Pay @bob" in label and "5.00" in label
    assert data.startswith("st:p:m-:")


async def test_creditor_personal_balance_has_no_pay_button(
    dispatcher, session: AsyncSession
) -> None:
    """bob is owed money — nothing for him to pay, so no buttons."""
    await _seed_caller_owes_bob(session)
    bot = MockedBot()
    await feed(
        dispatcher,
        bot,
        make_message("/balance", from_id=2002, username="bob"),
        session=session,
    )

    assert "you're owed" in (bot.last_reply or "")
    assert _pay_buttons(bot) == []  # nothing for bob to pay
    assert _pivot_data(bot) == "bal:all"  # but the pivot to everyone is still there


async def test_balance_pivot_flips_views_in_place(
    dispatcher, session: AsyncSession
) -> None:
    """Personal view carries '👥 Everyone's balances'; tapping it repaints to the
    group view in place, which carries '🙋 Just mine' to flip back."""
    await _seed_caller_owes_bob(session)
    bot = MockedBot()
    await feed(
        dispatcher,
        bot,
        make_message("/balance", from_id=1001, username="caller"),
        session=session,
    )
    assert _pivot_data(bot) == "bal:all"  # personal view → pivot to everyone

    await feed_callback(
        dispatcher,
        bot,
        make_callback("bal:all", from_id=1001, username="caller"),
        session=session,
    )
    edited = bot.last_edit or ""
    assert "Group balances" in edited
    # The repainted group view carries the reverse pivot.
    markup = bot.edits[-1].reply_markup
    data = [b.callback_data for row in markup.inline_keyboard for b in row]  # type: ignore[union-attr]
    assert "bal:me" in data


async def test_balance_pivot_me_shows_tappers_own(
    dispatcher, session: AsyncSession
) -> None:
    """'🙋 Just mine' shows the *tapper's* balance, not the original caller's —
    and reveals nothing /balance all wouldn't."""
    await _seed_caller_owes_bob(session)
    bot = MockedBot()
    # caller opens the group view…
    await feed(
        dispatcher,
        bot,
        make_message("/balance all", from_id=1001, username="caller"),
        session=session,
    )
    # …bob taps "Just mine" → bob's own personal view (he's owed SGD 5).
    await feed_callback(
        dispatcher,
        bot,
        make_callback("bal:me", from_id=2002, username="bob"),
        session=session,
    )
    assert "you're owed" in (bot.last_edit or "")


async def test_balance_me_is_personal_without_note(
    dispatcher, session: AsyncSession
) -> None:
    """`/balance me` mirrors `/statements me` — personal view, no typo note."""
    await _seed_caller_owes_bob(session)
    bot = MockedBot()
    await feed(
        dispatcher,
        bot,
        make_message("/balance me", from_id=1001, username="caller"),
        session=session,
    )

    reply = bot.last_reply or ""
    assert "you owe" in reply.lower()
    assert "didn't recognize" not in reply


async def test_balance_unrecognized_arg_notes_and_shows_personal(
    dispatcher, session: AsyncSession
) -> None:
    """A typo'd selector still answers (personal view) but says so, so the caller
    can't mistake it for the group view."""
    await _seed_caller_owes_bob(session)
    bot = MockedBot()
    await feed(
        dispatcher,
        bot,
        make_message("/balance al", from_id=1001, username="caller"),
        session=session,
    )

    reply = bot.last_reply or ""
    assert 'didn\'t recognize "al"' in reply
    assert "/balance all" in reply
    assert "you owe" in reply.lower()  # personal view still rendered
