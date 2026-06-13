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
import math
import re
import uuid

from aiogram import Bot, F, Router
from aiogram.enums import MessageEntityType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    User,
)

from countbeans.bot.utils.context import resolve_chat_context
from countbeans.bot.utils.formatting import display_name, format_money
from countbeans.bot.utils.parsing import extract_quoted_description, is_all
from countbeans.bot.utils.permissions import is_admin
from countbeans.db.models import Event, Group
from countbeans.db.models import User as DbUser
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
        f'✅ Closed "{open_event.name}". Closed events stay closed — start a fresh '
        'one with /event new "<name>".'
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
    # placeholder (security review #1). Supported on both edits — without it on
    # `remove`, a member added by tap could never be taken off again.
    text_mention = next(
        (
            e
            for e in (message.entities or [])
            if e.type == MessageEntityType.TEXT_MENTION
        ),
        None,
    )
    if text_mention is not None and text_mention.user is not None:
        if action == "add":
            await _roster_add_text_mention(
                message, uow, group, open_event, text_mention.user
            )
        else:
            await _roster_remove_text_mention(
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
        await _roster_remove_handle(message, uow, group, open_event, handle)


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
    message: Message, uow: UnitOfWork, group: Group, open_event: Event, handle: str
) -> None:
    user = await uow.users.find_by_mention(handle)
    if user is None:
        await message.reply(f"I don't know @{handle} — nothing to remove.")
        return
    await _remove_user(message, uow, group, open_event, user, f"@{handle}")


async def _roster_remove_text_mention(
    message: Message, uow: UnitOfWork, group: Group, open_event: Event, tu: User
) -> None:
    """Remove a tap-mentioned member (no public @handle needed) — the mirror of
    the text_mention add path, resolved by telegram id, never by username."""
    user = await uow.users.get_by_telegram_id(tu.id)
    if user is None:
        await message.reply(f"I don't know {tu.first_name} — nothing to remove.")
        return
    await _remove_user(
        message,
        uow,
        group,
        open_event,
        user,
        display_name(user.username, user.first_name),
    )


async def _remove_user(
    message: Message,
    uow: UnitOfWork,
    group: Group,
    open_event: Event,
    user: DbUser,
    label: str,
) -> None:
    """The shared tail of both remove grammars: drop the roster row and, when
    the member leaves unsettled, append a non-blocking warning (the ledger keeps
    their entries either way — removal is roster-only)."""
    changed = await edit_event_roster(
        uow,
        EditEventRosterCommand(
            event_id=open_event.id, user_id=user.id, action="remove"
        ),
    )
    if not changed:
        await message.reply(f'{label} isn\'t on the "{open_event.name}" roster.')
        return
    logger.info(
        "Event roster remove: event_id=%s user_id=%s changed=%s",
        open_event.id,
        user.id,
        changed,
    )
    note = f'Removed {label} from "{open_event.name}".'
    note += await _unsettled_warning(uow, group, open_event, user.id, label)
    await _reply_with_roster(message, uow, open_event, note)


async def _unsettled_warning(
    uow: UnitOfWork, group: Group, open_event: Event, user_id: uuid.UUID, label: str
) -> str:
    """Warn (non-blocking, like the @all coverage warning) when a removed member
    still has a balance in the event — so the later "whose debt is this?" moment
    at settle-up time never happens. Empty string when they're settled."""
    balances = await compute_balances(uow, group.id, event_id=open_event.id)
    parts = [
        f"{format_money(abs(cents), key.currency)} "
        f"{'owed to them' if cents > 0 else 'they owe'}"
        for key, cents in balances.items()
        if key.user_id == user_id and cents != 0
    ]
    if not parts:
        return ""
    return (
        f'\n⚠️ {label} isn\'t settled in "{open_event.name}" yet '
        f"({', '.join(parts)}) — their entries stay on the ledger and still count."
    )


async def _reply_with_roster(
    message: Message, uow: UnitOfWork, open_event: Event, note: str
) -> None:
    """The shared reply tail of every roster edit: re-fetch the members and echo
    the updated roster under the outcome note."""
    roster = await uow.events.list_members(open_event.id)
    await message.reply(f"{note}\nRoster: {_roster_str(roster)}")


def _event_keyboard(is_active: bool) -> InlineKeyboardMarkup:
    """The open event's legal transitions as buttons, plus a roster editor entry.
    Shown to everyone (the callbacks re-check the admin gate at tap and alert
    non-admins), so the state machine is visible — no recalling which subcommand
    fits which state."""
    transition = (
        InlineKeyboardButton(text="⏸ Pause", callback_data="ev:pause")
        if is_active
        else InlineKeyboardButton(text="▶️ Resume", callback_data="ev:resume")
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                transition,
                InlineKeyboardButton(text="✅ Close", callback_data="ev:close"),
            ],
            [InlineKeyboardButton(text="✏️ Edit roster", callback_data="er:open")],
        ]
    )


_ROSTER_PAGE_SIZE = 8


async def _roster_editor_view(
    uow: UnitOfWork, group: Group, page: int
) -> tuple[str, InlineKeyboardMarkup] | None:
    """The tap-to-toggle roster editor: every known group member with a ✅/⬜
    marker for on/off the event roster, paged 8 at a time — the same pattern the
    /addexpense wizard uses, instead of typing one @handle per message. None when
    no event is open."""
    event = await uow.events.get_open(group.id)
    if event is None:
        return None
    members = await uow.group_members.list_members(group.id)
    roster_ids = {m.user_id for m in await uow.events.list_members(event.id)}

    pages = max(1, math.ceil(len(members) / _ROSTER_PAGE_SIZE))
    page = max(0, min(page, pages - 1))
    start = page * _ROSTER_PAGE_SIZE

    rows: list[list[InlineKeyboardButton]] = []
    for idx in range(start, min(start + _ROSTER_PAGE_SIZE, len(members))):
        m = members[idx]
        mark = "✅" if m.user_id in roster_ids else "⬜"
        name = display_name(m.username, m.first_name)
        if m.is_pending:
            name += " ⏳"
        rows.append(
            [InlineKeyboardButton(text=f"{mark} {name}", callback_data=f"er:t:{idx}")]
        )
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀", callback_data=f"er:p:{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="▶", callback_data=f"er:p:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="✅ Done", callback_data="er:done")])

    text = (
        f'Edit "{event.name}" roster — tap a name to add or remove it.\n'
        f"On the roster: {len(roster_ids)} of {len(members)} known member(s)."
    )
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def _info_view(
    uow: UnitOfWork, group: Group
) -> tuple[str, InlineKeyboardMarkup | None]:
    """The /event info text + action keyboard — also what the ev: callbacks
    repaint after a transition, so the view comes from one place."""
    open_event = (
        await uow.events.get(group.active_event_id) if group.active_event_id else None
    )
    if open_event is None:
        open_event = await uow.events.get_open(group.id)
    if open_event is None:
        return 'No event is open. Start one with /event new "<name>".', None

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
    # One-off general expense mid-event: #general beats pausing (no admin, no
    # forgotten resume). Surfaced here rather than nudged on every expense reply.
    if is_active:
        lines.append(
            'General (non-event) item? Add #general, e.g. /addexpense 12 "taxi" #general.'
        )
    return "\n".join(lines), _event_keyboard(is_active)


async def _info(message: Message, uow: UnitOfWork, group: Group) -> None:
    text, keyboard = await _info_view(uow, group)
    await message.reply(text, reply_markup=keyboard)


async def _status(message: Message, uow: UnitOfWork, group: Group) -> None:
    lines = [_USAGE]
    keyboard: InlineKeyboardMarkup | None = None
    if group.active_event_id is not None:
        active = await uow.events.get(group.active_event_id)
        if active is not None:
            roster = await uow.events.list_members(active.id)
            cur = f" [{active.default_currency}]" if active.default_currency else ""
            lines.append(
                f'\nActive event: "{active.name}"{cur} — roster: {_roster_str(roster)}'
            )
            keyboard = _event_keyboard(is_active=True)
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
            keyboard = _event_keyboard(is_active=False)
    await message.reply("\n".join(lines), reply_markup=keyboard)


@router.callback_query(F.data.startswith("ev:"))
async def on_event_action(callback: CallbackQuery, uow: UnitOfWork, bot: Bot) -> None:
    """The status views' transition buttons: ev:pause / ev:resume / ev:close.

    Admin-gated at tap (same rule and wording as the typed subcommands — the
    buttons are visible to everyone so the state machine is, too). State is
    re-checked against the group's *current* open event, so a stale button after
    someone else already paused/closed answers gracefully instead of acting.
    A successful transition is announced as a fresh chat message — a mode flip
    everyone should see — and the tapped view repaints."""
    parts = (callback.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    if action not in ("pause", "resume", "close"):
        await callback.answer()
        return
    if not isinstance(callback.message, Message):
        await callback.answer()
        return
    chat = callback.message.chat

    if not await is_admin(bot, chat.id, callback.from_user.id):
        await callback.answer(
            "Only group admins can manage events. Anyone can view the current "
            "event with /event info.",
            show_alert=True,
        )
        return

    group = await uow.groups.upsert(
        telegram_chat_id=chat.id, group_name=getattr(chat, "title", None)
    )
    open_event = await uow.events.get_open(group.id)
    if open_event is None:
        await _repaint_event_view(callback.message, uow, group)
        await callback.answer("No open event any more.")
        return

    is_active = group.active_event_id == open_event.id
    note: str | None = None
    if action == "pause":
        if not is_active:
            await callback.answer(f'"{open_event.name}" is already paused.')
        else:
            await set_active_event(
                uow, SetActiveEventCommand(group_id=group.id, event_id=None)
            )
            note = (
                f'⏸ Paused "{open_event.name}". New expenses are general '
                "until it's resumed."
            )
            await callback.answer("Paused")
    elif action == "resume":
        if is_active:
            await callback.answer(f'"{open_event.name}" is already active.')
        else:
            await set_active_event(
                uow, SetActiveEventCommand(group_id=group.id, event_id=open_event.id)
            )
            note = f'▶️ Resumed "{open_event.name}". New expenses tag to it again.'
            await callback.answer("Resumed")
    else:  # close
        await close_event(
            uow,
            group.id,
            SetEventStatusCommand(event_id=open_event.id, status="closed"),
        )
        note = (
            f'✅ Closed "{open_event.name}". Closed events stay closed — start a '
            'fresh one with /event new "<name>".'
        )
        await callback.answer("Closed")

    if note is not None:
        # The mode flip is shared state — announce it like the typed command
        # would, not just a quiet edit.
        await callback.message.answer(note)
        logger.info(
            "Event %s via button: event_id=%s group_id=%s by=%s",
            action,
            open_event.id,
            group.id,
            callback.from_user.id,
        )
    await _repaint_event_view(callback.message, uow, group)


async def _repaint_event_view(message: Message, uow: UnitOfWork, group: Group) -> None:
    """Repaint the tapped status message to the current event state so its
    buttons always match reality (a stale Pause after a close would mislead)."""
    text, keyboard = await _info_view(uow, group)
    try:
        await message.edit_text(text, reply_markup=keyboard)
    except TelegramBadRequest:
        # "message is not modified" — e.g. a double-tap on an already-handled state.
        logger.debug("event view repaint skipped (not modified)")


@router.callback_query(F.data.startswith("er:"))
async def on_roster_edit(callback: CallbackQuery, uow: UnitOfWork, bot: Bot) -> None:
    """The roster editor: `er:open` opens it, `er:t:<idx>` toggles a member on/off
    the roster, `er:p:<page>` pages, `er:done` returns to /event info. Admin-gated
    at tap (same rule as the typed roster edits), and every action repaints from
    the current DB state so a stale index/page can't corrupt anything."""
    parts = (callback.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    if not isinstance(callback.message, Message):
        await callback.answer()
        return
    chat = callback.message.chat

    if not await is_admin(bot, chat.id, callback.from_user.id):
        await callback.answer(
            "Only group admins can edit the roster. Anyone can view it with "
            "/event info.",
            show_alert=True,
        )
        return

    group = await uow.groups.upsert(
        telegram_chat_id=chat.id, group_name=getattr(chat, "title", None)
    )

    if action == "done":
        await _repaint_event_view(callback.message, uow, group)
        await callback.answer("Roster saved.")
        return

    if action == "open":
        await _repaint_roster_editor(callback, uow, group, page=0)
        return

    if action == "p" and len(parts) == 3:
        try:
            page = int(parts[2])
        except ValueError:
            await callback.answer()
            return
        await _repaint_roster_editor(callback, uow, group, page=page)
        return

    if action == "t" and len(parts) == 3:
        try:
            idx = int(parts[2])
        except ValueError:
            await callback.answer()
            return
        event = await uow.events.get_open(group.id)
        if event is None:
            await _repaint_event_view(callback.message, uow, group)
            await callback.answer("No open event any more.")
            return
        members = await uow.group_members.list_members(group.id)
        if not 0 <= idx < len(members):
            await callback.answer()
            return
        member = members[idx]
        on_roster = await uow.events.list_members(event.id)
        is_on = any(m.user_id == member.user_id for m in on_roster)
        await edit_event_roster(
            uow,
            EditEventRosterCommand(
                event_id=event.id,
                user_id=member.user_id,
                action="remove" if is_on else "add",
            ),
        )
        label = display_name(member.username, member.first_name)
        toast = f"Removed {label}" if is_on else f"Added {label}"
        await _repaint_roster_editor(
            callback, uow, group, page=idx // _ROSTER_PAGE_SIZE, toast=toast
        )
        return

    await callback.answer()


async def _repaint_roster_editor(
    callback: CallbackQuery,
    uow: UnitOfWork,
    group: Group,
    *,
    page: int,
    toast: str | None = None,
) -> None:
    """Render the roster editor at one page and edit the tapped message into it.
    Falls back to the /event info view if the event was closed meanwhile."""
    assert isinstance(callback.message, Message)
    view = await _roster_editor_view(uow, group, page)
    if view is None:
        await _repaint_event_view(callback.message, uow, group)
        await callback.answer("No open event any more.")
        return
    text, keyboard = view
    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
    except TelegramBadRequest:
        logger.debug("roster editor repaint skipped (not modified)")
    await callback.answer(toast) if toast else await callback.answer()
