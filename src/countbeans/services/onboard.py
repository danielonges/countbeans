"""Service for onboarding a caller into a group's ledger.

Shared by `/start` (admin setup) and `/join` (anyone's opt-in): upsert the user
and group, ensure an active membership row, and claim a pending placeholder if
one waits under the caller's @handle. Returns an OnboardResult carrying the two
status flags the bot needs to compose a status-aware reply.
"""
from countbeans.dto.commands import OnboardUserCommand
from countbeans.dto.results import OnboardResult

from .uow import UnitOfWork


async def onboard_member(uow: UnitOfWork, cmd: OnboardUserCommand) -> OnboardResult:
    # Detect a placeholder claim BEFORE upsert mutates state: the caller is
    # unknown by telegram_id but a pending placeholder waits under their @handle.
    # upsert() performs the actual claim; this read just reports that it happened.
    existing = await uow.users.get_by_telegram_id(cmd.telegram_user_id)
    claimed_placeholder = (
        existing is None
        and cmd.username is not None
        and await uow.users.pending_placeholder(cmd.username) is not None
    )

    user = await uow.users.upsert(
        telegram_user_id=cmd.telegram_user_id,
        username=cmd.username,
        first_name=cmd.first_name,
        last_name=cmd.last_name,
    )
    group = await uow.groups.upsert(
        telegram_chat_id=cmd.telegram_chat_id,
        group_name=cmd.group_name,
    )
    newly_added = await uow.group_members.ensure_member(group.id, user.id)

    return OnboardResult(
        user_id=user.id,
        username=user.username,
        first_name=user.first_name,
        claimed_placeholder=claimed_placeholder,
        newly_added=newly_added,
    )
