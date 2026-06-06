"""Unit tests for AdminGateMiddleware's command-bypass logic.

The full gate (refuse-until-bot-is-admin) is integration territory — it reads
the DB and calls getChatMember. But the /help bypass is checked *before* any of
that, so it's exercisable in isolation: the parser is pure, and the bypass path
returns the inner handler's result without ever touching data["uow"]/["bot"].
"""

from datetime import datetime, timezone

from aiogram.types import Chat, Message, User

from countbeans.bot.middleware.admin_gate import AdminGateMiddleware, _command_name


def test_command_name_parsing() -> None:
    assert _command_name("/help") == "help"
    assert _command_name("/help@countbeans_bot") == "help"
    assert _command_name("/help me please") == "help"
    assert _command_name("/HELP") == "help"  # case-folded
    assert _command_name("/addexpense 20 lunch") == "addexpense"
    assert _command_name("not a command") is None
    assert _command_name("") is None
    assert _command_name(None) is None
    assert _command_name("/") is None


def _group_message(text: str, chat_type: str = "supergroup") -> Message:
    return Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=-100, type=chat_type, title="Test Group"),
        from_user=User(id=1, is_bot=False, first_name="Caller"),
        text=text,
    )


async def test_help_bypasses_the_gate_without_touching_uow_or_bot() -> None:
    """/help in a group reaches the handler even with no uow/bot in data — proving
    the bypass happens before the admin check (so it works pre-promotion)."""
    gate = AdminGateMiddleware()
    seen: dict[str, object] = {}

    async def handler(event: object, data: dict[str, object]) -> str:
        seen["called"] = True
        return "handled"

    # data deliberately omits "uow"/"bot": the non-bypass path would KeyError on
    # them, so reaching the handler proves the bypass fired first.
    result = await gate(handler, _group_message("/help@countbeans_bot extra"), {})

    assert seen.get("called") is True
    assert result == "handled"


async def test_non_bypassed_command_does_not_short_circuit() -> None:
    """A normal command falls through to the gate's real logic, which needs the
    uow — so an empty data dict raises KeyError rather than reaching the handler
    (i.e. the bypass is scoped to the allowlist, not everything)."""
    gate = AdminGateMiddleware()

    async def handler(event: object, data: dict[str, object]) -> str:
        return "handled"

    try:
        await gate(handler, _group_message("/addexpense 20 lunch"), {})
    except KeyError:
        pass  # expected: the gate reached `data["uow"]`
    else:
        raise AssertionError("non-bypassed command should not skip the admin gate")
