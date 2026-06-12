"""The wizard's FSM states and its typed draft (the (chat, user)-scoped data).

callback_data is 64 bytes — far too small for an expense draft — so the draft
lives in aiogram FSM state (``MemoryStorage``) keyed by ``(chat, user)``, and
buttons reference roster members by **index** into the stored roster, never by
UUID. This module owns the draft's shape (``WizardDraft``), the state machine,
and the pure scope/share helpers over it; rendering and Telegram I/O live in
the sibling modules.
"""

import uuid
from typing import Literal, TypedDict, cast

from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from countbeans.bot.utils.parsing import parse_amount_cents
from countbeans.dto.domain import MemberInfo
from countbeans.services.uow import UnitOfWork


class AddExpenseFlow(StatesGroup):
    amount = State()  # awaiting a ForceReply with the amount (entry or ✏️ edit)
    description = State()  # awaiting a ForceReply with the description
    participants = State()  # anchor showing the member-toggle roster
    split_mode = State()  # anchor showing the three uneven split-mode buttons
    share_entry = State()  # anchor collecting per-person shares (non-equal)
    # No confirm state: an equal split commits straight from the roster anchor
    # (which already previews the draft; /void is the undo, as inline), and an
    # uneven split commits from the share screen's reconciliation-gated Confirm.


class RosterMember(TypedDict):
    """One roster entry as stored in FSM data — a MemberInfo flattened to plain
    JSON-able types (see _member_to_dict / _dict_to_member)."""

    user_id: str
    username: str | None
    first_name: str | None
    is_pending: bool


class WizardDraft(TypedDict):
    """Every key the wizard keeps in FSM data.

    A typing veneer over aiogram's plain dict: the draft is only ever read
    through the ``cast`` in :func:`get_draft`, never constructed as a literal,
    so declaring all keys required costs nothing at runtime while making every
    access typed (a key typo is now a pyright error). Keys still *arrive*
    progressively as steps complete — read through ``.get`` where a step may
    not have run yet, exactly as the handlers already do.
    """

    initiator_id: int  # telegram user id whose taps the anchor accepts
    group_id: str  # UUIDs stored as str (FSM data stays JSON-able)
    payer_id: str
    payer_username: str | None
    payer_first_name: str | None
    # Both scope defaults are kept so the effective one can be re-derived when
    # the #general toggle flips the scope mid-draft (see _default_currency).
    currency_general: str  # the group default (general-scope fallback)
    currency_event: str | None  # the event scope's default; None = no event
    currency_explicit: bool  # the typed amount pinned a currency (EUR50/€50)
    active_event_id: str | None
    active_event_name: str | None
    force_general: bool  # the #general toggle (one-off scope override)
    # Mirrors AddExpenseCommand.split_mode; _set_mode validates against
    # _MODE_DISPLAY before storing, so the draft never holds anything else.
    split_mode: Literal["equal", "weighted", "percent", "exact"]
    shares: dict[str, int]  # roster index (as str) -> entered share value
    page: int  # current roster page
    prompt_id: int | None  # the live ForceReply prompt replies must target
    amount_cents: int  # arrives with the amount step
    currency: str
    description: str | None
    roster: list[RosterMember]  # arrives when the participants step opens
    selected: list[int]  # indices into roster
    pending_share_idx: int | None  # roster index awaiting a share reply
    anchor_id: int  # the single in-place-edited anchor message
    submitting: bool  # claimed write slot — a double-tapped Confirm bails


async def get_draft(state: FSMContext) -> WizardDraft:
    """The current FSM data as a typed draft (aiogram stores a plain dict)."""
    return cast(WizardDraft, await state.get_data())


def _parse_share(raw: str, mode: str) -> int:
    """Parse a typed per-person share for the given split mode (raises ValueError
    with a user-facing message)."""
    if mode == "exact":
        try:
            return parse_amount_cents(raw)
        except ValueError:
            raise ValueError("Enter an amount like 30 or 30.50.") from None
    if not raw.isdigit() or int(raw) <= 0:
        unit = "a percentage like 60" if mode == "percent" else "a weight like 2"
        raise ValueError(f"Enter {unit}.")
    return int(raw)


def _is_reconciled(data: WizardDraft) -> bool:
    """Whether a non-equal split's shares add up, so Confirm can appear."""
    mode = data["split_mode"]
    selected = data.get("selected", [])
    shares = data.get("shares", {})
    if not selected or any(str(i) not in shares for i in selected):
        return False
    total = sum(shares[str(i)] for i in selected)
    if mode == "percent":
        return total == 100
    if mode == "exact":
        return total == data["amount_cents"]
    return total > 0  # weighted: any positive total (each share is > 0)


def _use_event_scope(data: WizardDraft) -> bool:
    """Whether this draft writes to the active event: there is one, and #general
    hasn't overridden it for this expense. The single source of the scope rule."""
    return bool(data.get("active_event_id")) and not data.get("force_general")


def _default_currency(data: WizardDraft) -> str:
    """The currency a bare amount means under the draft's *effective* scope —
    the event's when writing to the event, the group's otherwise. The single
    place the wizard resolves a default currency (parallels ChatContext.currency
    on the inline path, which resolves scope before parsing)."""
    if _use_event_scope(data):
        event_currency = data["currency_event"]
        assert event_currency is not None  # stored whenever an event is active
        return event_currency
    return data["currency_general"]


def _scope_label(data: WizardDraft) -> str:
    if _use_event_scope(data):
        return data.get("active_event_name") or "event"
    if data.get("active_event_id"):
        return "General (#general)"
    return "General"


def _effective_event_id(data: WizardDraft) -> uuid.UUID | None:
    if _use_event_scope(data):
        event_id = data["active_event_id"]
        assert event_id is not None  # guaranteed by _use_event_scope
        return uuid.UUID(event_id)
    return None


def _member_to_dict(member: MemberInfo) -> RosterMember:
    return {
        "user_id": str(member.user_id),
        "username": member.username,
        "first_name": member.first_name,
        "is_pending": member.is_pending,
    }


def _dict_to_member(data: RosterMember) -> MemberInfo:
    return MemberInfo(
        user_id=uuid.UUID(data["user_id"]),
        username=data["username"],
        first_name=data["first_name"],
        is_pending=data["is_pending"],
    )


async def _reload_roster(state: FSMContext, uow: UnitOfWork) -> None:
    """(Re)load the roster for the current scope and default-select everyone.
    Called at first open and whenever the #general toggle swaps the scope."""
    data = await get_draft(state)
    members = await _load_roster(uow, data)
    await state.update_data(
        roster=[_member_to_dict(m) for m in members],
        selected=list(range(len(members))),
        page=0,
    )


async def _load_roster(uow: UnitOfWork, data: WizardDraft) -> list[MemberInfo]:
    if _use_event_scope(data):
        event_id = data["active_event_id"]
        assert event_id is not None  # guaranteed by _use_event_scope
        return await uow.events.list_members(uuid.UUID(event_id))
    return await uow.group_members.list_members(uuid.UUID(data["group_id"]))
