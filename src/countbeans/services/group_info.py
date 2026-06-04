"""Service for /group — assembles a read-only snapshot of a group."""

import logging
import uuid

logger = logging.getLogger(__name__)

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
    logger.debug("get_group_info: group=%s", group_id)
    members = await uow.group_members.list_members(group_id)
    activity = await uow.expenses.activity_summary(group_id)
    logger.debug(
        "get_group_info: known_members=%d actual_count=%s", len(members), actual_count
    )
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
