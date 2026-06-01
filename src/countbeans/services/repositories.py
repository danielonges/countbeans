"""Repository classes."""
import uuid

import uuid_utils
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from countbeans.db.models import Expense, ExpenseShare, Group, GroupMember, User


class ExpenseRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, expense: Expense, shares: dict[uuid.UUID, int]) -> None:
        self._session.add(expense)
        await self._session.flush()
        self._session.add_all([
            ExpenseShare(expense_id=expense.id, user_id=uid, share_cents=cents)
            for uid, cents in shares.items()
        ])
        await self._session.flush()


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(
        self,
        telegram_user_id: int,
        username: str | None,
        first_name: str | None,
        last_name: str | None,
    ) -> User:
        stmt = (
            pg_insert(User)
            .values(
                id=uuid_utils.uuid7(),
                telegram_user_id=telegram_user_id,
                username=username,
                first_name=first_name,
                last_name=last_name,
            )
            .on_conflict_do_update(
                index_elements=["telegram_user_id"],
                set_={"username": username, "first_name": first_name, "last_name": last_name},
            )
            .returning(User)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one()

    async def get_or_create_placeholder(self, username: str) -> User:
        result = await self._session.execute(
            select(User).where(User.username == username, User.telegram_user_id.is_(None))
        )
        existing = result.scalar_one_or_none()
        if existing:
            return existing
        user = User(id=uuid_utils.uuid7(), username=username)
        self._session.add(user)
        await self._session.flush()
        return user


class GroupRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(self, telegram_chat_id: int, group_name: str | None) -> Group:
        stmt = (
            pg_insert(Group)
            .values(
                id=uuid_utils.uuid7(),
                telegram_chat_id=telegram_chat_id,
                group_name=group_name,
            )
            .on_conflict_do_update(
                index_elements=["telegram_chat_id"],
                set_={"group_name": group_name},
            )
            .returning(Group)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one()


class GroupMemberRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def ensure_member(self, group_id: uuid.UUID, user_id: uuid.UUID) -> None:
        result = await self._session.execute(
            select(GroupMember).where(
                GroupMember.group_id == group_id,
                GroupMember.user_id == user_id,
                GroupMember.left_at.is_(None),
            )
        )
        if result.scalar_one_or_none() is None:
            self._session.add(GroupMember(group_id=group_id, user_id=user_id))
            await self._session.flush()
