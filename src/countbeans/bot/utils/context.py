"""Per-command chat context — the shared "who / where / which scope" resolution.

Every group command opens the same way: upsert the group (first — the
placeholder claim in ``users.upsert`` is group-scoped), upsert the calling
user, ensure their membership, and fetch the active event. ``ChatContext``
bundles those rows plus the derived scope values the handlers used to
recompute by hand (effective event, currency fallback, the scope-note reply
fragment).
"""

import uuid
from dataclasses import dataclass, replace

from aiogram.types import Message

from countbeans.db.models import Event, Group, User
from countbeans.services.uow import UnitOfWork


@dataclass(frozen=True)
class ChatContext:
    """One command's identity rows and effective write scope.

    ``group`` / ``caller`` / ``active_event`` are read-only ORM context rows —
    the documented read-side exception to the DTO boundary (CLAUDE.md
    "Database sessions"); all mutations still flow through commands and
    repository methods. ``active_event`` is the group's active event regardless
    of any override; ``scoped_event`` is what writes should target — ``None``
    when ``force_general`` opted this one command out (CLAUDE.md "The #general
    write-scope override").
    """

    group: Group
    caller: User
    active_event: Event | None
    force_general: bool = False

    def scoped(self, *, force_general: bool) -> "ChatContext":
        """A copy with the #general override applied — parsed from the command
        args *after* context resolution in /addexpense and /settleup."""
        return replace(self, force_general=force_general)

    @property
    def scoped_event(self) -> Event | None:
        return None if self.force_general else self.active_event

    @property
    def event_id(self) -> uuid.UUID | None:
        return self.scoped_event.id if self.scoped_event else None

    @property
    def currency(self) -> str:
        """The effective scope's currency: the scoped event's default (when set),
        else the group default."""
        return (
            self.scoped_event.default_currency if self.scoped_event else None
        ) or self.group.default_currency

    @property
    def scope_note(self) -> str:
        """The ``' in "<event>"'`` reply fragment; empty in general scope."""
        return f' in "{self.scoped_event.name}"' if self.scoped_event else ""


async def resolve_chat_context(uow: UnitOfWork, message: Message) -> ChatContext:
    """Resolve the calling user's context for one group command.

    Group upsert first (the placeholder claim in ``users.upsert`` is
    group-scoped), then the caller upsert and membership, then the active
    event. Precondition: ``message.from_user`` is not None — handlers bail on
    authorless messages before resolving context.
    """
    assert message.from_user is not None
    group = await uow.groups.upsert(
        telegram_chat_id=message.chat.id,
        group_name=getattr(message.chat, "title", None),
    )
    caller = await uow.users.upsert(
        telegram_user_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
        claim_in_group=group.id,
    )
    await uow.group_members.ensure_member(group.id, caller.id)
    active_event = (
        await uow.events.get(group.active_event_id) if group.active_event_id else None
    )
    return ChatContext(group=group, caller=caller, active_event=active_event)
