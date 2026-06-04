"""A lightweight in-process harness for driving aiogram handlers end-to-end.

Why this exists: the service-core tests cover business logic, but the *handler*
paths — command routing, chat-type filters, the admin gate, which reply text is
sent — were previously only exercised by hand. This harness feeds a constructed
`Update` through a real `Dispatcher` (so filters, routing, and middleware all
run) with:

  * `MockedBot` — a `Bot` whose every API call is intercepted: `get_chat_member`
    returns a configurable status (drives the admin gate), `send_message` /
    replies are *recorded* instead of sent, and `get_chat_member_count` returns a
    set number. Nothing touches Telegram.
  * a `uow` bound to the conftest `session` fixture (rolled back per test) and
    passed straight into handler data via `feed_update`'s kwargs — so handler
    writes hit the real schema but never persist, and no per-test middleware (or
    per-test Dispatcher) is needed. The Dispatcher is built **once** because an
    aiogram Router can only attach to a single Dispatcher per process.

It needs Postgres like the other integration tests (handlers onboard, which
writes), so it lives under tests/integrations/.
"""
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.methods import (
    GetChatMember,
    GetChatMemberCount,
    GetMe,
    SendMessage,
    TelegramMethod,
)
from aiogram.types import (
    Chat,
    ChatMemberMember,
    ChatMemberOwner,
    Message,
    Update,
    User,
)
from sqlalchemy.ext.asyncio import AsyncSession

from countbeans.services.uow import UnitOfWork

_BOT_ID = 42
_BOT_TOKEN = f"{_BOT_ID}:TEST-TOKEN-FOR-HARNESS-ONLY"


class MockedBot(Bot):
    """A Bot that intercepts every API call instead of hitting Telegram.

    `caller_is_admin` decides what `get_chat_member` reports for the caller (the
    admin gate's only input); `sent` records the outgoing `send_message` calls so
    a test can assert on reply text.
    """

    def __init__(self, *, caller_is_admin: bool = False, member_count: int = 2) -> None:
        super().__init__(token=_BOT_TOKEN, default=DefaultBotProperties())
        self.caller_is_admin = caller_is_admin
        self.member_count = member_count
        self.sent: list[SendMessage] = []

    async def __call__(self, method: TelegramMethod, request_timeout: int | None = None):  # type: ignore[override]
        if isinstance(method, GetMe):
            return User(id=_BOT_ID, is_bot=True, first_name="countbeans", username="countbeans_bot")
        if isinstance(method, GetChatMember):
            user = User(id=method.user_id, is_bot=False, first_name="Caller")
            if self.caller_is_admin:
                return ChatMemberOwner(user=user, is_anonymous=False)
            return ChatMemberMember(user=user)
        if isinstance(method, GetChatMemberCount):
            return self.member_count
        if isinstance(method, SendMessage):
            self.sent.append(method)
            return _fake_sent_message(method)
        raise NotImplementedError(f"MockedBot got an un-stubbed call: {type(method).__name__}")

    @property
    def last_reply(self) -> str | None:
        """Text of the most recent send_message, if any."""
        return self.sent[-1].text if self.sent else None


def _fake_sent_message(method: SendMessage) -> Message:
    return Message(
        message_id=999,
        date=datetime.now(timezone.utc),
        chat=Chat(id=method.chat_id if isinstance(method.chat_id, int) else 0, type="supergroup"),
        text=method.text,
    )


class HarnessUoW(UnitOfWork):
    """A UoW bound to the pre-opened test session that never commits (the
    conftest `session` fixture owns the transaction and rolls it back)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._attach(session)


def build_dispatcher(*routers: Router) -> Dispatcher:
    """A Dispatcher wired with the routers under test. Build it **once** per
    process (a Router cannot attach to two Dispatchers) — share it across tests
    via a module-scoped fixture; the per-test `uow` is passed in `feed`."""
    dp = Dispatcher()
    for router in routers:
        dp.include_router(router)
    return dp


def make_message(
    text: str,
    *,
    from_id: int = 1001,
    username: str | None = "caller",
    first_name: str = "Caller",
    chat_id: int = -1000000000001,
    chat_type: str = "supergroup",
) -> Message:
    """Construct a group (default) or private message carrying `text`."""
    return Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=chat_id, type=chat_type, title="Test Group"),
        from_user=User(id=from_id, is_bot=False, first_name=first_name, username=username),
        text=text,
    )


async def feed(
    dp: Dispatcher,
    bot: MockedBot,
    message: Message,
    *,
    session: AsyncSession | None = None,
) -> None:
    """Send one message-update through the dispatcher (filters + routing run).

    When `session` is given, a `HarnessUoW` over it is passed into handler data —
    aiogram forwards `feed_update` kwargs straight to the handler, so no
    middleware is needed. Handlers that don't declare `uow` simply ignore it.
    """
    extra = {"uow": HarnessUoW(session)} if session is not None else {}
    await dp.feed_update(bot, Update(update_id=1, message=message), **extra)
