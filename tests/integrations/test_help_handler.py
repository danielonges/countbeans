"""End-to-end /help handler tests via the aiogram harness.

/help reads and writes nothing, so these don't need the `session` fixture — they
just drive a real Update through the dispatcher and assert the reply text.
"""

from countbeans.bot.handlers._welcome import COMMAND_REFERENCE

from ._bot_harness import MockedBot, feed, make_message


async def test_help_in_group_lists_commands_and_the_tip(dispatcher) -> None:
    bot = MockedBot()
    await feed(dispatcher, bot, make_message("/help"))

    reply = bot.last_reply or ""
    # Renders the single command reference (so /void and /event are present too).
    assert COMMAND_REFERENCE in reply
    assert "/addexpense" in reply and "/void" in reply and "/event" in reply
    # The payoff line: every command self-documents when sent bare.
    assert "no arguments" in reply


async def test_help_with_botname_suffix_still_routes(dispatcher) -> None:
    """In groups Telegram appends @botname; the Command filter strips it."""
    bot = MockedBot()
    await feed(dispatcher, bot, make_message("/help@countbeans_bot"))

    assert "/addexpense" in (bot.last_reply or "")


async def test_help_in_private_chat_points_to_a_group(dispatcher) -> None:
    bot = MockedBot()
    await feed(dispatcher, bot, make_message("/help", chat_type="private", chat_id=777))

    reply = (bot.last_reply or "").lower()
    assert "group" in reply
    # The full command list is group-only copy, so it isn't dumped in private.
    assert "/addexpense" not in reply
