"""Integration tests for the onboard_member service (shared by /start and /join).

Needs Postgres — run `docker compose --profile test run --rm test` (or set
TEST_DATABASE_URL); skips otherwise (see conftest.py). Each test rolls back on
teardown, so rows never leak.

Covers the three reply-driving outcomes: a fresh member (newly added), claiming
a pending placeholder (the @mentioned-but-unseen case), and an idempotent repeat
(already a member).
"""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from countbeans.db.models import GroupMember, User
from countbeans.dto.commands import OnboardUserCommand
from countbeans.services.onboard import onboard_member
from countbeans.services.repositories import (
    GroupMemberRepository,
    GroupRepository,
    UserRepository,
)


class _SessionUoW:
    def __init__(self, session: AsyncSession) -> None:
        self.users = UserRepository(session)
        self.groups = GroupRepository(session)
        self.group_members = GroupMemberRepository(session)


def _cmd(**kw: object) -> OnboardUserCommand:
    base: dict[str, object] = {
        "telegram_user_id": 100,
        "telegram_chat_id": 1,
        "username": "bob",
        "first_name": "Bob",
        "last_name": None,
        "group_name": "G",
    }
    base.update(kw)
    return OnboardUserCommand(**base)  # type: ignore[arg-type]


async def _count_users(session: AsyncSession) -> int:
    return (await session.execute(select(func.count()).select_from(User))).scalar_one()


async def test_onboard_fresh_user(session: AsyncSession) -> None:
    uow = _SessionUoW(session)
    result = await onboard_member(uow, _cmd())  # type: ignore[arg-type]

    assert result.newly_added is True
    assert result.claimed_placeholder is False
    assert result.username == "bob"
    assert await _count_users(session) == 1


async def test_onboard_claims_pending_placeholder(session: AsyncSession) -> None:
    uow = _SessionUoW(session)
    # bob was @mentioned before ever interacting — the placeholder lives in this
    # group (every mention ensures group membership), so the group-scoped claim
    # gate (security review #1) lets the join claim it.
    group = await uow.groups.upsert(telegram_chat_id=1, group_name="G")
    placeholder = await uow.users.resolve_mention("bob")
    await uow.group_members.ensure_member(group.id, placeholder.id)
    assert placeholder.telegram_user_id is None

    result = await onboard_member(uow, _cmd(telegram_user_id=200))  # type: ignore[arg-type]

    assert result.claimed_placeholder is True
    assert result.newly_added is False  # already a member as the placeholder
    assert result.user_id == placeholder.id  # claimed in place, not duplicated
    assert await _count_users(session) == 1


async def test_onboard_is_idempotent(session: AsyncSession) -> None:
    uow = _SessionUoW(session)
    first = await onboard_member(uow, _cmd())  # type: ignore[arg-type]
    second = await onboard_member(uow, _cmd())  # type: ignore[arg-type]

    assert first.newly_added is True
    assert second.newly_added is False
    assert second.claimed_placeholder is False
    assert second.user_id == first.user_id
    # No duplicate membership row.
    count = (
        await session.execute(
            select(func.count())
            .select_from(GroupMember)
            .where(GroupMember.user_id == first.user_id)
        )
    ).scalar_one()
    assert count == 1


async def test_ensure_member_returns_false_when_already_present(
    session: AsyncSession,
) -> None:
    uow = _SessionUoW(session)
    result = await onboard_member(uow, _cmd())  # type: ignore[arg-type]
    group = await uow.groups.upsert(telegram_chat_id=1, group_name="G")

    assert await uow.group_members.ensure_member(group.id, result.user_id) is False
