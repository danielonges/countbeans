"""Repository classes — the only objects that hold SQLAlchemy models."""
import uuid

import uuid_utils
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from countbeans.db.models import Group, Settlement, User
from countbeans.dto.results import SettlementCreatedResult


class SettlementRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, settlement: Settlement) -> None:
        self._session.add(settlement)
        await self._session.flush()

    async def get(self, settlement_id: uuid.UUID) -> Settlement | None:
        result = await self._session.execute(
            select(Settlement).where(Settlement.id == settlement_id)
        )
        return result.scalar_one_or_none()

    def _to_dto(self, row: Settlement) -> SettlementCreatedResult:
        return SettlementCreatedResult(
            settlement_id=row.id,
            from_user_id=row.from_user_id,
            to_user_id=row.to_user_id,
            amount_cents=row.amount_cents,
            currency=row.currency,
            event_id=None,
        )


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
