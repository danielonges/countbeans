"""Service for /group — assembles a read-only snapshot of a group."""

import uuid

from countbeans.dto.domain import GroupInfo

from .uow import UnitOfWork


async def get_group_info(
    uow: UnitOfWork,
    group_id: uuid.UUID,
    group_name: str | None,
    default_currency: str,
    simplify_debts: bool,
    actual_count: int | None,
) -> GroupInfo:
    members = await uow.group_members.list_members(group_id)
    activity = await uow.expenses.activity_summary(group_id)
    return GroupInfo(
        group_id=group_id,
        group_name=group_name,
        default_currency=default_currency,
        simplify_debts=simplify_debts,
        members=members,
        known_count=len(members),
        actual_count=actual_count,
        activity=activity,
    )
