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
    AnswerCallbackQuery,
    EditMessageReplyMarkup,
    EditMessageText,
    GetChatMember,
    GetChatMemberCount,
    GetMe,
    SendMessage,
    TelegramMethod,
)
from aiogram.types import (
    CallbackQuery,
    Chat,
    ChatMemberAdministrator,
    ChatMemberLeft,
    ChatMemberMember,
    ChatMemberOwner,
    ChatMemberUpdated,
    Message,
    Update,
    User,
)
from sqlalchemy.ext.asyncio import AsyncSession

from countbeans.services.uow import UnitOfWork

_BOT_ID = 42
_BOT_TOKEN = f"{_BOT_ID}:TEST-TOKEN-FOR-HARNESS-ONLY"

# The chat every harness message/callback defaults to — seed helpers use this so
# the group a handler upserts matches the one a test pre-seeded.
DEFAULT_CHAT_ID = -1000000000001


class MockedBot(Bot):
    """A Bot that intercepts every API call instead of hitting Telegram.

    `caller_is_admin` decides what `get_chat_member` reports for the caller (the
    admin gate's only input); `sent` records the outgoing `send_message` calls so
    a test can assert on reply text.
    """

    def __init__(
        self,
        *,
        caller_is_admin: bool = False,
        member_count: int = 2,
        bot_is_admin: bool = True,
    ) -> None:
        super().__init__(token=_BOT_TOKEN, default=DefaultBotProperties())
        self.caller_is_admin = caller_is_admin
        self.member_count = member_count
        # The bot's *own* admin status, reported by get_chat_member for _BOT_ID —
        # what AdminGateMiddleware's self-heal reads. Defaults True so handler
        # tests aren't gated unless they opt in.
        self.bot_is_admin = bot_is_admin
        self.sent: list[SendMessage] = []
        self.edits: list[EditMessageText] = []
        self.answers: list[AnswerCallbackQuery] = []

    async def __call__(self, method: TelegramMethod, request_timeout: int | None = None):  # type: ignore[override]
        if isinstance(method, GetMe):
            return User(
                id=_BOT_ID,
                is_bot=True,
                first_name="countbeans",
                username="countbeans_bot",
            )
        if isinstance(method, GetChatMember):
            if method.user_id == _BOT_ID:
                bot_user = User(id=_BOT_ID, is_bot=True, first_name="countbeans")
                if self.bot_is_admin:
                    return _admin_member(bot_user)
                return ChatMemberMember(user=bot_user)
            user = User(id=method.user_id, is_bot=False, first_name="Caller")
            if self.caller_is_admin:
                return ChatMemberOwner(user=user, is_anonymous=False)
            return ChatMemberMember(user=user)
        if isinstance(method, GetChatMemberCount):
            return self.member_count
        if isinstance(method, SendMessage):
            self.sent.append(method)
            return _fake_sent_message(method)
        if isinstance(method, EditMessageText):
            self.edits.append(method)
            return True
        if isinstance(method, AnswerCallbackQuery):
            self.answers.append(method)
            return True
        if isinstance(method, EditMessageReplyMarkup):
            return True
        raise NotImplementedError(
            f"MockedBot got an un-stubbed call: {type(method).__name__}"
        )

    @property
    def last_reply(self) -> str | None:
        """Text of the most recent send_message, if any."""
        return self.sent[-1].text if self.sent else None

    @property
    def last_edit(self) -> str | None:
        """Text of the most recent edit_text (statements paging), if any."""
        return self.edits[-1].text if self.edits else None

    @property
    def last_answer(self) -> AnswerCallbackQuery | None:
        """The most recent answerCallbackQuery (toast/alert), if any."""
        return self.answers[-1] if self.answers else None


def _fake_sent_message(method: SendMessage) -> Message:
    return Message(
        message_id=999,
        date=datetime.now(timezone.utc),
        chat=Chat(
            id=method.chat_id if isinstance(method.chat_id, int) else 0,
            type="supergroup",
        ),
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
    chat_id: int = DEFAULT_CHAT_ID,
    chat_type: str = "supergroup",
) -> Message:
    """Construct a group (default) or private message carrying `text`."""
    return Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=chat_id, type=chat_type, title="Test Group"),
        from_user=User(
            id=from_id, is_bot=False, first_name=first_name, username=username
        ),
        text=text,
    )


def make_callback(
    data: str,
    *,
    from_id: int = 1001,
    username: str | None = "caller",
    chat_id: int = DEFAULT_CHAT_ID,
) -> CallbackQuery:
    """An inline-button tap carrying `data` (e.g. `stmt:g:1`). Its `.message` is
    the bot's own message being repainted — the handler only reads its chat and
    calls edit_text."""
    inline_msg = Message(
        message_id=500,
        date=datetime.now(timezone.utc),
        chat=Chat(id=chat_id, type="supergroup", title="Test Group"),
        from_user=User(id=_BOT_ID, is_bot=True, first_name="countbeans"),
        text="(statement)",
    )
    return CallbackQuery(
        id="cb-1",
        from_user=User(
            id=from_id, is_bot=False, first_name="Caller", username=username
        ),
        chat_instance="ci-1",
        message=inline_msg,
        data=data,
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


async def feed_callback(
    dp: Dispatcher,
    bot: MockedBot,
    callback: CallbackQuery,
    *,
    session: AsyncSession | None = None,
) -> None:
    """Send one callback-query update (an inline-button tap) through the dispatcher."""
    extra = {"uow": HarnessUoW(session)} if session is not None else {}
    await dp.feed_update(bot, Update(update_id=2, callback_query=callback), **extra)


def _admin_member(user: User) -> ChatMemberAdministrator:
    """An `administrator` ChatMember with all granular rights off — enough to
    exercise the admin-status check (status == administrator)."""
    return ChatMemberAdministrator(
        user=user,
        can_be_edited=False,
        is_anonymous=False,
        can_manage_chat=True,
        can_delete_messages=False,
        can_manage_video_chats=False,
        can_restrict_members=False,
        can_promote_members=False,
        can_change_info=False,
        can_invite_users=False,
        can_post_stories=False,
        can_edit_stories=False,
        can_delete_stories=False,
    )


# The three ChatMember states the membership handlers care about, by keyword.
_STATE_BUILDERS = {
    "left": lambda u: ChatMemberLeft(user=u),
    "member": lambda u: ChatMemberMember(user=u),
    "administrator": _admin_member,
}


def make_chat_member_updated(
    *,
    old: str,
    new: str,
    target_id: int = _BOT_ID,
    username: str | None = None,
    first_name: str = "Member",
    is_bot: bool | None = None,
    actor_id: int = 1001,
    chat_id: int = DEFAULT_CHAT_ID,
    chat_type: str = "supergroup",
) -> ChatMemberUpdated:
    """Build a ChatMemberUpdated for either my_chat_member (target is the bot,
    `target_id=_BOT_ID`) or chat_member (target is another user). `old`/`new` are
    one of 'left', 'member', 'administrator'."""
    target = User(
        id=target_id,
        is_bot=is_bot if is_bot is not None else target_id == _BOT_ID,
        first_name=first_name,
        username=username,
    )
    return ChatMemberUpdated(
        chat=Chat(id=chat_id, type=chat_type, title="Test Group"),
        from_user=User(id=actor_id, is_bot=False, first_name="Actor"),
        date=datetime.now(timezone.utc),
        old_chat_member=_STATE_BUILDERS[old](target),
        new_chat_member=_STATE_BUILDERS[new](target),
    )


async def feed_my_chat_member(
    dp: Dispatcher,
    bot: MockedBot,
    event: ChatMemberUpdated,
    *,
    session: AsyncSession | None = None,
) -> None:
    extra = {"uow": HarnessUoW(session)} if session is not None else {}
    await dp.feed_update(bot, Update(update_id=3, my_chat_member=event), **extra)


async def feed_chat_member(
    dp: Dispatcher,
    bot: MockedBot,
    event: ChatMemberUpdated,
    *,
    session: AsyncSession | None = None,
) -> None:
    extra = {"uow": HarnessUoW(session)} if session is not None else {}
    await dp.feed_update(bot, Update(update_id=4, chat_member=event), **extra)
