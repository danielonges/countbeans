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


def _pay_buttons(bot: MockedBot) -> list[tuple[str, str]]:
    markup = bot.sent[-1].reply_markup
    assert isinstance(markup, InlineKeyboardMarkup), "reply carries no keyboard"
    return [
        (b.text, b.callback_data or "") for row in markup.inline_keyboard for b in row
    ]


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
    assert bot.sent[-1].reply_markup is None
