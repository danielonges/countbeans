"""Event-scope service functions — open/activate/close events, edit rosters.

An event is a scope dimension on the one append-only ledger (CLAUDE.md "Events"),
never a second ledger: these functions manage the events / event_members tables
and the group's active-event pointer, but never net scopes or materialize a
balance. All SQL lives in the repositories; one transaction per command via the
caller-managed UoW.
"""

import logging
import uuid

import uuid_utils.compat as uuid_utils  # .compat yields stdlib uuid.UUID

logger = logging.getLogger(__name__)

from countbeans.db._mixins import _now
from countbeans.db.models import Event
from countbeans.dto.commands import (
    CreateEventCommand,
    EditEventRosterCommand,
    SetActiveEventCommand,
    SetEventStatusCommand,
)
from countbeans.dto.results import EventCreatedResult

from .uow import UnitOfWork


async def create_event(uow: UnitOfWork, cmd: CreateEventCommand) -> EventCreatedResult:
    """Open a new event: create it (status 'open'), add the creator to the roster,
    and point the group's active-event pointer at it.

    Rejects a second open event with a friendly error — at most one open event per
    group (the partial unique index would otherwise raise the raw violation), so
    close the current one first.
    """
    logger.debug(
        "create_event: group=%s name=%r currency=%s",
        cmd.group_id,
        cmd.name,
        cmd.default_currency,
    )
    if await uow.events.get_open(cmd.group_id) is not None:
        raise ValueError(
            "An event is already open — close it with /event close before starting another."
        )
    event = Event(
        id=uuid_utils.uuid7(),
        group_id=cmd.group_id,
        name=cmd.name,
        default_currency=cmd.default_currency,
        status="open",
        created_by=cmd.created_by,
    )
    await uow.events.create(event)
    await uow.events.ensure_member(event.id, cmd.created_by)
    await uow.groups.set_active_event(cmd.group_id, event.id)
    logger.debug("create_event: created event_id=%s", event.id)
    return uow.events._to_result(event)


async def set_active_event(uow: UnitOfWork, cmd: SetActiveEventCommand) -> None:
    """Pause (event_id=None) or resume (the open event's id) auto-tagging. The
    event's open/closed status is unchanged — only the pointer moves. The bot
    validates that an open event exists before resuming."""
    action = "resume" if cmd.event_id else "pause"
    logger.debug(
        "set_active_event: group=%s action=%s event=%s",
        cmd.group_id,
        action,
        cmd.event_id,
    )
    await uow.groups.set_active_event(cmd.group_id, cmd.event_id)


async def close_event(
    uow: UnitOfWork, group_id: uuid.UUID, cmd: SetEventStatusCommand
) -> None:
    """Finish an event: mark it closed and clear the group's active pointer.

    Only the single open event can be closed, and the active pointer can only ever
    reference that open event (or be NULL), so clearing it on close is always
    correct. Closing never rolls the event's debts into the general balance — that
    would be a materialization the spec forbids (CLAUDE.md "Events")."""
    logger.debug(
        "close_event: group=%s event=%s status=%s", group_id, cmd.event_id, cmd.status
    )
    closed_at = _now() if cmd.status == "closed" else None
    await uow.events.set_status(cmd.event_id, cmd.status, closed_at)
    if cmd.status == "closed":
        await uow.groups.set_active_event(group_id, None)


async def add_group_to_roster(
    uow: UnitOfWork, group_id: uuid.UUID, event_id: uuid.UUID
) -> int:
    """Add every known group member to the event's roster (the ``/event add @all``
    case). Returns how many were **newly** added. Idempotent: members already on
    the roster are skipped, so re-running only picks up newcomers. Like ``@all``
    on an expense, "everyone" means everyone the bot knows (placeholders
    included); it can't enumerate members it has never seen."""
    members = await uow.group_members.list_members(group_id)
    added = 0
    for m in members:
        if await uow.events.ensure_member(event_id, m.user_id):
            added += 1
    logger.debug("add_group_to_roster: event=%s added=%d", event_id, added)
    return added


async def edit_event_roster(uow: UnitOfWork, cmd: EditEventRosterCommand) -> bool:
    """Add or remove one user from an event's roster. Returns True when the roster
    actually changed (a no-op add/remove returns False)."""
    logger.debug(
        "edit_event_roster: event=%s action=%s user=%s",
        cmd.event_id,
        cmd.action,
        cmd.user_id,
    )
    if cmd.action == "add":
        changed = await uow.events.ensure_member(cmd.event_id, cmd.user_id)
    else:
        changed = await uow.events.remove_member(cmd.event_id, cmd.user_id)
    logger.debug("edit_event_roster: changed=%s", changed)
    return changed
