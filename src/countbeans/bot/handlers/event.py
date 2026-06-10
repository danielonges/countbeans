"""Bot handler for /event — manage ad-hoc event scopes within a group.

Managing an event — new/pause/resume/close/add/remove — is **admin-only** (like
/simplify and /currency): it changes how *subsequent* expenses split for the
whole group, so it isn't a single member's call. Viewing (info, or bare /event)
is open to any member.
  /event new "<name>"    (admin) start an event (one open at a time); new
                         /addexpense & /settleup auto-tag to it until paused/closed
  /event pause           (admin) stop auto-tagging for a run of general expenses; stays
                         open. For just one, use /addexpense … #general (no pause)
  /event resume          (admin) resume auto-tagging to the open event
  /event close           (admin) finish the open event, freeing the slot
  /event add @user       (admin) add someone to the roster (@all adds the whole group)
  /event remove @user    (admin) remove someone from the roster
  /event info            show the open event's status, roster, and outstanding balance
  /event                 show the active event + usage

Scope is durable, shared group state (`groups.active_event_id`), not aiogram FSM
(CLAUDE.md "Events"). The full /event list & info command is deferred.
"""

import logging
import re
import uuid

from aiogram import Bot, Router
from aiogram.enums import MessageEntityType
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, User

from countbeans.bot.utils.context import resolve_chat_context
from countbeans.bot.utils.formatting import display_name, format_money
from countbeans.bot.utils.parsing import extract_quoted_description, is_all
from countbeans.bot.utils.permissions import is_admin
from countbeans.db.models import Event, Group
from countbeans.dto.commands import (
    CreateEventCommand,
    EditEventRosterCommand,
    SetActiveEventCommand,
    SetEventStatusCommand,
)
from countbeans.dto.domain import MemberInfo
from countbeans.services.balance import compute_balances
from countbeans.services.errors import DomainError
from countbeans.services.events import (
    add_group_to_roster,
    close_event,
    create_event,
    edit_event_roster,
    set_active_event,
)
from countbeans.services.uow import UnitOfWork

logger = logging.getLogger(__name__)

router = Router()

_MENTION_RE = re.compile(r"@([\w.]+)")

_USAGE = (
    "Manage an event scope:\n"
    '• /event new "<name>" [CUR] — start an event (new expenses tag to it)\n'
    "• /event info — show the open event's status, roster, and outstanding balance\n"
    "• /event pause — stop tagging so new expenses are general (for a run of them)\n"
    "• /event resume — resume tagging to the open event\n"
    "• /event close — finish the open event\n"
    "• /event add @user / remove @user — edit the roster\n"
    "Inside an event, @all (or no mentions) splits the roster, not the whole group.\n"
    "Just one general expense? Add #general to /addexpense — no pause needed.\n"
    "Managing an event is admin-only; anyone can view it."
)

# Subcommands that change shared event state — and thus how later expenses split
# for the whole group — are admin-gated. Reads (info, the bare-status fallback)
# stay open to any member.
_MUTATING_SUBCOMMANDS = frozenset({"new", "pause", "resume", "close", "add", "remove"})


def _roster_str(members: list[MemberInfo]) -> str:
    return (
        ", ".join(display_name(m.username, m.first_name) for m in members) or "(empty)"
    )


@router.message(Command("event"))
async def cmd_event(
    message: Message, command: CommandObject, uow: UnitOfWork, bot: Bot
) -> None:
    if message.from_user is None:
        return

    tokens = (command.args or "").split(maxsplit=1)
    sub = tokens[0].lower() if tokens else ""
    rest = tokens[1] if len(tokens) > 1 else ""

    # Managing an event is admin-only (mirrors /simplify, /currency): it changes
    # how subsequent expenses split for the whole group, so refuse non-admins
    # before any state is touched. Reads (info, bare status) fall through.
    if sub in _MUTATING_SUBCOMMANDS and not await is_admin(
        bot, message.chat.id, message.from_user.id
    ):
        logger.info(
            "Refused /event %s: user=%s is not an admin in chat=%s",
            sub,
            message.from_user.id,
            message.chat.id,
        )
        await message.reply(
            "Only group admins can manage events (start, pause, resume, close, or "
            "edit the roster). Anyone can view the current event with /event info."
        )
        return

    ctx = await resolve_chat_context(uow, message)
    group = ctx.group

    if sub == "new":
        await _new(message, uow, group, ctx.caller.id, rest)
    elif sub == "pause":
        await _pause(message, uow, group)
    elif sub == "resume":
        await _resume(message, uow, group)
    elif sub == "close":
        await _close(message, uow, group)
    elif sub in ("add", "remove"):
        await _roster(message, uow, group, sub, rest)
    elif sub == "info":
        await _info(message, uow, group)
    else:
        await _status(message, uow, group)


async def _new(
    message: Message, uow: UnitOfWork, group: Group, caller_id: uuid.UUID, rest: str
) -> None:
    # Name may be quoted (handles @ or spaces) or the bare remaining text.
    # An optional 3-letter ISO 4217 code sets the event currency — it must follow
    # a quoted name (e.g. "Bali" IDR) or be the last whitespace-separated token
    # of an unquoted name (e.g. Bali IDR).
    name, after_name = extract_quoted_description(rest)
    if name is None:
        tokens = rest.strip().split()
        if len(tokens) >= 2 and len(tokens[-1]) == 3 and tokens[-1].isalpha():
            name = " ".join(tokens[:-1])
            after_name = tokens[-1]
        else:
            name = rest.strip()
            after_name = ""
    if not name:
        await message.reply('Usage: /event new "<name>" [CUR]')
        return

    currency: str | None = None
    if after_name:
        token = after_name.strip().upper()
        if len(token) == 3 and token.isalpha():
            currency = token

    try:
        result = await create_event(
            uow,
            CreateEventCommand(
                group_id=group.id,
                name=name,
                created_by=caller_id,
                default_currency=currency,
            ),
        )
    except DomainError as exc:
        await message.reply(str(exc))
        return

    currency_note = f" [{result.currency}]" if result.currency else ""
    await message.reply(
        f'✅ Started event "{result.name}"{currency_note}. New expenses and settlements '
        "tag to it until /event pause or /event close."
    )
    logger.info("Event opened: event_id=%s group_id=%s", result.event_id, group.id)


async def _pause(message: Message, uow: UnitOfWork, group: Group) -> None:
    open_event = await uow.events.get_open(group.id)
    if open_event is None:
        await message.reply('No event is open. Start one with /event new "<name>".')
        return
    if group.active_event_id is None:
        await message.reply(
            f'"{open_event.name}" is already paused — new expenses are general. '
            "/event resume to continue."
        )
        return
    await set_active_event(uow, SetActiveEventCommand(group_id=group.id, event_id=None))
    await message.reply(
        f'⏸ Paused "{open_event.name}". New expenses are general until /event resume.'
    )
    logger.info("Event paused: event_id=%s group_id=%s", open_event.id, group.id)


async def _resume(message: Message, uow: UnitOfWork, group: Group) -> None:
    open_event = await uow.events.get_open(group.id)
    if open_event is None:
        await message.reply(
            'No open event to resume. Start one with /event new "<name>".'
        )
        return
    if group.active_event_id == open_event.id:
        await message.reply(f'"{open_event.name}" is already active.')
        return
    await set_active_event(
        uow, SetActiveEventCommand(group_id=group.id, event_id=open_event.id)
    )
    await message.reply(
        f'▶️ Resumed "{open_event.name}". New expenses tag to it again.'
    )
    logger.info("Event resumed: event_id=%s group_id=%s", open_event.id, group.id)


async def _close(message: Message, uow: UnitOfWork, group: Group) -> None:
    open_event = await uow.events.get_open(group.id)
    if open_event is None:
        await message.reply("No open event to close.")
        return
    await close_event(
        uow, group.id, SetEventStatusCommand(event_id=open_event.id, status="closed")
    )
    await message.reply(
        f'✅ Closed "{open_event.name}". Start a new event with /event new "<name>".'
    )
    logger.info("Event closed: event_id=%s group_id=%s", open_event.id, group.id)


async def _roster(
    message: Message, uow: UnitOfWork, group: Group, action: str, rest: str
) -> None:
    """Dispatch a roster edit to its grammar: a text_mention (add only), the
    reserved @all keyword (add only), or a single typed @handle."""
    open_event = await uow.events.get_open(group.id)
    if open_event is None:
        await message.reply('No open event. Start one with /event new "<name>" first.')
        return

    # A text_mention (a user without a public @handle, or tap-selected) carries a
    # real telegram_user_id → resolve to a claimed user, never a username
    # placeholder (security review #1). Supported for `add`; removal is by @handle.
    text_mention = next(
        (
            e
            for e in (message.entities or [])
            if e.type == MessageEntityType.TEXT_MENTION
        ),
        None,
    )
    if action == "add" and text_mention is not None and text_mention.user is not None:
        await _roster_add_text_mention(
            message, uow, group, open_event, text_mention.user
        )
        return

    mention = _MENTION_RE.search(rest)
    if mention is None:
        await message.reply(f"Usage: /event {action} @user")
        return
    handle = mention.group(1)

    # @all is the reserved "everyone" keyword, never a username (so it can't spawn
    # a literal "all" placeholder). On `add` it folds the whole known group onto
    # the roster; on `remove` it has no meaning — name a specific member.
    if is_all(handle):
        if action == "add":
            await _roster_add_all(message, uow, group, open_event)
        else:
            await message.reply(
                "@all isn't removable — name a specific @user to take off the roster."
            )
        return

    if action == "add":
        await _roster_add_handle(message, uow, group, open_event, handle)
    else:
        await _roster_remove_handle(message, uow, open_event, handle)


async def _roster_add_text_mention(
    message: Message, uow: UnitOfWork, group: Group, open_event: Event, tu: User
) -> None:
    user = await uow.users.upsert(
        telegram_user_id=tu.id,
        username=tu.username,
        first_name=tu.first_name,
        last_name=tu.last_name,
        claim_in_group=group.id,
    )
    await uow.group_members.ensure_member(group.id, user.id)
    changed = await edit_event_roster(
        uow,
        EditEventRosterCommand(event_id=open_event.id, user_id=user.id, action="add"),
    )
    logger.info(
        "Event roster add (text_mention): event_id=%s user_id=%s changed=%s",
        open_event.id,
        user.id,
        changed,
    )
    label = display_name(user.username, user.first_name)
    note = (
        f'Added {label} to "{open_event.name}".'
        if changed
        else f"{label} is already on the roster."
    )
    await _reply_with_roster(message, uow, open_event, note)


async def _roster_add_all(
    message: Message, uow: UnitOfWork, group: Group, open_event: Event
) -> None:
    added = await add_group_to_roster(uow, group.id, open_event.id)
    logger.info("Event roster add @all: event_id=%s added=%d", open_event.id, added)
    note = (
        f'Added {added} group member(s) to "{open_event.name}".'
        if added
        else f'Everyone I know is already on the "{open_event.name}" roster.'
    )
    await _reply_with_roster(message, uow, open_event, note)


async def _roster_add_handle(
    message: Message, uow: UnitOfWork, group: Group, open_event: Event, handle: str
) -> None:
    # Naming someone unseen is fine here — it tracks them as a placeholder,
    # exactly like /addexpense.
    user = await uow.users.resolve_mention(handle)
    await uow.group_members.ensure_member(group.id, user.id)
    changed = await edit_event_roster(
        uow,
        EditEventRosterCommand(event_id=open_event.id, user_id=user.id, action="add"),
    )
    logger.info(
        "Event roster add: event_id=%s user_id=%s changed=%s",
        open_event.id,
        user.id,
        changed,
    )
    note = (
        f'Added @{handle} to "{open_event.name}".'
        if changed
        else f"@{handle} is already on the roster."
    )
    await _reply_with_roster(message, uow, open_event, note)


async def _roster_remove_handle(
    message: Message, uow: UnitOfWork, open_event: Event, handle: str
) -> None:
    user = await uow.users.find_by_mention(handle)
    if user is None:
        await message.reply(f"I don't know @{handle} — nothing to remove.")
        return
    changed = await edit_event_roster(
        uow,
        EditEventRosterCommand(
            event_id=open_event.id, user_id=user.id, action="remove"
        ),
    )
    if not changed:
        await message.reply(f'@{handle} isn\'t on the "{open_event.name}" roster.')
        return
    logger.info(
        "Event roster remove: event_id=%s user_id=%s changed=%s",
        open_event.id,
        user.id,
        changed,
    )
    await _reply_with_roster(
        message, uow, open_event, f'Removed @{handle} from "{open_event.name}".'
    )


async def _reply_with_roster(
    message: Message, uow: UnitOfWork, open_event: Event, note: str
) -> None:
    """The shared reply tail of every roster edit: re-fetch the members and echo
    the updated roster under the outcome note."""
    roster = await uow.events.list_members(open_event.id)
    await message.reply(f"{note}\nRoster: {_roster_str(roster)}")


async def _info(message: Message, uow: UnitOfWork, group: Group) -> None:
    open_event = (
        await uow.events.get(group.active_event_id) if group.active_event_id else None
    )
    if open_event is None:
        open_event = await uow.events.get_open(group.id)
    if open_event is None:
        await message.reply('No event is open. Start one with /event new "<name>".')
        return

    is_active = group.active_event_id == open_event.id
    state = "active" if is_active else "paused"
    cur = f" [{open_event.default_currency}]" if open_event.default_currency else ""
    roster = await uow.events.list_members(open_event.id)

    balances = await compute_balances(uow, group.id, event_id=open_event.id)
    outstanding: dict[str, int] = {}
    for key, cents in balances.items():
        if cents > 0:
            outstanding[key.currency] = outstanding.get(key.currency, 0) + cents

    lines = [f'Event: "{open_event.name}"{cur} — {state}']
    lines.append(f"Roster: {_roster_str(roster)}")
    if outstanding:
        parts = [format_money(v, c) for c, v in outstanding.items()]
        lines.append(f"Outstanding: {', '.join(parts)}")
    else:
        lines.append("All settled up.")
    hints = (
        "/event resume • /event close"
        if not is_active
        else "/event pause • /event close"
    )
    lines.append(hints)
    # One-off general expense mid-event: #general beats pausing (no admin, no
    # forgotten resume). Surfaced here rather than nudged on every expense reply.
    if is_active:
        lines.append(
            'General (non-event) item? Add #general, e.g. /addexpense 12 "taxi" #general.'
        )
    await message.reply("\n".join(lines))


async def _status(message: Message, uow: UnitOfWork, group: Group) -> None:
    lines = [_USAGE]
    if group.active_event_id is not None:
        active = await uow.events.get(group.active_event_id)
        if active is not None:
            roster = await uow.events.list_members(active.id)
            cur = f" [{active.default_currency}]" if active.default_currency else ""
            lines.append(
                f'\nActive event: "{active.name}"{cur} — roster: {_roster_str(roster)}'
            )
    else:
        open_event = await uow.events.get_open(group.id)
        if open_event is not None:
            cur = (
                f" [{open_event.default_currency}]"
                if open_event.default_currency
                else ""
            )
            lines.append(
                f'\nOpen but paused: "{open_event.name}"{cur} (/event resume to continue).'
            )
    await message.reply("\n".join(lines))
