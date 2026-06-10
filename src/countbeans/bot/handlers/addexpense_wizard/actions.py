"""The wizard's inline-button callbacks — one ``ax:*`` entry point,
ownership-checked, delegating to a named function per action — plus submit,
which reuses the unchanged ``add_expense`` service path.

Anchor buttons are bound to the initiator and reject other users' taps: FSM
state is keyed by ``(chat, user)`` *and* the tap must come from the draft's
own anchor message.
"""

import logging
import uuid
from typing import Any

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, ForceReply, Message
from pydantic import ValidationError

from countbeans.bot.utils.formatting import (
    VOID_HINT,
    display_name,
    format_expense_receipt,
    format_money,
    general_override_note,
    humanize_validation_error,
)
from countbeans.bot.utils.replies import whole_group_coverage_warning
from countbeans.dto.commands import AddExpenseCommand
from countbeans.services.add_expense import add_expense
from countbeans.services.errors import DomainError
from countbeans.services.uow import UnitOfWork

from .render import (
    _MODE_DISPLAY,
    _SHARE_HINTS,
    _edit_anchor,
    _mention_prefix,
    _repaint,
)
from .states import (
    AddExpenseFlow,
    WizardDraft,
    _dict_to_member,
    _effective_event_id,
    _reload_roster,
    get_draft,
)

logger = logging.getLogger(__name__)

router = Router()


@router.callback_query(F.data.startswith("ax:"))
async def on_wizard_action(
    callback: CallbackQuery, state: FSMContext, uow: UnitOfWork, bot: Bot
) -> None:
    if not isinstance(callback.message, Message):
        await callback.answer()
        return
    # FSM state is keyed by (chat, user), so the draft only exists under the
    # initiator's context — a different user tapping the anchor has no draft here.
    # Bind to the *anchor message* too, so a user who happens to have their own
    # draft can't drive someone else's anchor (no state filter on the handler, so
    # the off-owner gets a clear rejection rather than a silent dropped tap).
    data = await get_draft(state)
    if "initiator_id" not in data or callback.message.message_id != data.get(
        "anchor_id"
    ):
        await callback.answer(
            "This isn't your expense draft — start your own with /addexpense.",
            show_alert=True,
        )
        return

    chat_id = callback.message.chat.id
    action = (callback.data or "")[len("ax:") :]

    if action == "x":
        await _cancel(callback, bot, chat_id, state, data)
    elif action == "desc":
        await _prompt_description(callback, bot, chat_id, state)
    elif action == "gen":
        await _toggle_general(callback, bot, chat_id, state, uow, data)
    elif action == "all":
        await _select_all(callback, bot, chat_id, state, data)
    elif action == "clear":
        await _clear_selection(callback, bot, chat_id, state)
    elif action.startswith("p:"):
        await _toggle_participant(
            callback, bot, chat_id, state, data, _action_index(action, "p:")
        )
    elif action.startswith("pg:"):
        await _flip_page(callback, bot, chat_id, state, _action_index(action, "pg:"))
    elif action == "pdone":
        await _participants_done(callback, bot, chat_id, state, data)
    elif action == "back":
        await _back(callback, bot, chat_id, state)
    elif action.startswith("m:"):
        await _set_mode(callback, bot, chat_id, state, action[len("m:") :])
    elif action.startswith("s:"):
        await _prompt_share(
            callback, bot, chat_id, state, data, _action_index(action, "s:")
        )
    elif action == "ok":
        await _submit(callback, state, uow, bot, chat_id)
    else:
        await callback.answer()


# ---------------------------------------------------------------------------
# One function per action
# ---------------------------------------------------------------------------


async def _cancel(
    callback: CallbackQuery,
    bot: Bot,
    chat_id: int,
    state: FSMContext,
    data: WizardDraft,
) -> None:
    await state.clear()
    await _edit_anchor(bot, chat_id, data, "✖ Expense cancelled.", None)
    await callback.answer("Cancelled")


async def _prompt_description(
    callback: CallbackQuery, bot: Bot, chat_id: int, state: FSMContext
) -> None:
    prompt = await bot.send_message(
        chat_id,
        f"{_mention_prefix(callback.from_user)}📝 Send a short description "
        "for this expense.",
        reply_markup=ForceReply(selective=True),
    )
    await state.update_data(prompt_id=prompt.message_id)
    await state.set_state(AddExpenseFlow.description)
    await callback.answer()


async def _toggle_general(
    callback: CallbackQuery,
    bot: Bot,
    chat_id: int,
    state: FSMContext,
    uow: UnitOfWork,
    data: WizardDraft,
) -> None:
    if not data.get("active_event_id"):
        await callback.answer()
        return
    force = not data.get("force_general")
    await state.update_data(force_general=force)
    await _reload_roster(state, uow)
    await state.set_state(AddExpenseFlow.participants)
    await _repaint(bot, chat_id, state)
    await callback.answer("Now logging to general." if force else "Back to the event.")


async def _select_all(
    callback: CallbackQuery,
    bot: Bot,
    chat_id: int,
    state: FSMContext,
    data: WizardDraft,
) -> None:
    await _roster_repaint(
        callback,
        bot,
        chat_id,
        state,
        selected=list(range(len(data.get("roster", [])))),
    )


async def _clear_selection(
    callback: CallbackQuery, bot: Bot, chat_id: int, state: FSMContext
) -> None:
    await _roster_repaint(callback, bot, chat_id, state, selected=[])


async def _toggle_participant(
    callback: CallbackQuery,
    bot: Bot,
    chat_id: int,
    state: FSMContext,
    data: WizardDraft,
    idx: int | None,
) -> None:
    # Reject a malformed or out-of-range index from a crafted callback, so a
    # bad toggle can't seed `selected` with an index that later IndexErrors in
    # _submit / the renderers.
    if idx is None or not 0 <= idx < len(data.get("roster", [])):
        await callback.answer()
        return
    selected = list(data.get("selected", []))
    if idx in selected:
        selected.remove(idx)
    else:
        selected.append(idx)
    await _roster_repaint(callback, bot, chat_id, state, selected=sorted(selected))


async def _flip_page(
    callback: CallbackQuery,
    bot: Bot,
    chat_id: int,
    state: FSMContext,
    page: int | None,
) -> None:
    if page is None or page < 0:
        await callback.answer()
        return
    await _roster_repaint(callback, bot, chat_id, state, page=page)


async def _participants_done(
    callback: CallbackQuery,
    bot: Bot,
    chat_id: int,
    state: FSMContext,
    data: WizardDraft,
) -> None:
    if not data.get("selected"):
        await callback.answer("Pick at least one person.", show_alert=True)
        return
    await state.set_state(AddExpenseFlow.split_mode)
    await _repaint(bot, chat_id, state)
    await callback.answer()


async def _back(
    callback: CallbackQuery, bot: Bot, chat_id: int, state: FSMContext
) -> None:
    target = (
        AddExpenseFlow.participants
        if await state.get_state() == AddExpenseFlow.split_mode.state
        else AddExpenseFlow.split_mode
    )
    await state.set_state(target)
    await _repaint(bot, chat_id, state)
    await callback.answer()


async def _set_mode(
    callback: CallbackQuery, bot: Bot, chat_id: int, state: FSMContext, mode: str
) -> None:
    if mode not in _MODE_DISPLAY:
        await callback.answer()
        return
    await state.update_data(split_mode=mode, shares={})
    await state.set_state(
        AddExpenseFlow.confirm if mode == "equal" else AddExpenseFlow.share_entry
    )
    await _repaint(bot, chat_id, state)
    await callback.answer()


async def _prompt_share(
    callback: CallbackQuery,
    bot: Bot,
    chat_id: int,
    state: FSMContext,
    data: WizardDraft,
    idx: int | None,
) -> None:
    # `idx in selected` also bounds it to a valid roster index (selected only
    # ever holds in-range indices — see _toggle_participant's guard).
    if idx is None or idx not in data.get("selected", []):
        await callback.answer()
        return
    member = data["roster"][idx]
    hint = _SHARE_HINTS[data["split_mode"]]
    prompt = await bot.send_message(
        chat_id,
        f"{_mention_prefix(callback.from_user)}Share for "
        f"{display_name(member['username'], member['first_name'])} "
        f"— send a number ({hint}).",
        reply_markup=ForceReply(selective=True),
    )
    await state.update_data(prompt_id=prompt.message_id, pending_share_idx=idx)
    await callback.answer()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _action_index(action: str, prefix: str) -> int | None:
    """The integer index from a prefixed callback action (``p:3``, ``pg:2``,
    ``s:1``), or ``None`` when it's malformed. callback_data normally comes from
    bot-rendered buttons, but a crafted callback shouldn't raise — parse defensively
    and let the caller bounds-check."""
    try:
        return int(action[len(prefix) :])
    except ValueError:
        return None


async def _roster_repaint(
    callback: CallbackQuery,
    bot: Bot,
    chat_id: int,
    state: FSMContext,
    **update: Any,
) -> None:
    """Apply a roster-screen edit (toggle / select-all / clear / page) and repaint
    the anchor in place. These buttons only render on the roster screen, so the
    state is re-asserted to ``participants`` defensively before the repaint."""
    await state.update_data(**update)
    await state.set_state(AddExpenseFlow.participants)
    await _repaint(bot, chat_id, state)
    await callback.answer()


# ---------------------------------------------------------------------------
# Submit — reuse the unchanged add_expense service path
# ---------------------------------------------------------------------------


async def _submit(
    callback: CallbackQuery,
    state: FSMContext,
    uow: UnitOfWork,
    bot: Bot,
    chat_id: int,
) -> None:
    data = await get_draft(state)
    # One write per draft: a double-tapped Confirm must not record the expense
    # twice in the append-only ledger (the slot is claimed before the write below).
    if data.get("submitting"):
        await callback.answer()
        return
    selected = data.get("selected", [])
    roster = data["roster"]
    if not selected:
        await callback.answer("Pick at least one person.", show_alert=True)
        return

    mode = data["split_mode"]
    amount_cents = data["amount_cents"]
    currency = data["currency"]
    shares = data.get("shares", {})

    if mode == "equal":
        split_params = None
    else:
        if any(str(i) not in shares for i in selected):
            await callback.answer("Enter a share for everyone first.", show_alert=True)
            return
        total = sum(shares[str(i)] for i in selected)
        if mode == "percent" and total != 100:
            await callback.answer(
                f"Percentages must total 100% — now {total}%.", show_alert=True
            )
            return
        if mode == "exact" and total != amount_cents:
            await callback.answer(
                f"Exact amounts must total {format_money(amount_cents, currency)}.",
                show_alert=True,
            )
            return
        split_params = {
            uuid.UUID(roster[i]["user_id"]): shares[str(i)] for i in selected
        }

    payer_id = uuid.UUID(data["payer_id"])
    event_id = _effective_event_id(data)
    participants = [uuid.UUID(roster[i]["user_id"]) for i in selected]

    # Claim the write slot. MemoryStorage's get_data/update_data touch no real I/O,
    # so nothing yields the event loop between the `submitting` guard read at the
    # top and this set — a double-tapped Confirm's second callback is scheduled only
    # once the first reaches `await add_expense`, by which point it sees the flag and
    # bails. Reset on the pre-write failure paths so the user can fix and retry.
    await state.update_data(submitting=True)
    try:
        cmd = AddExpenseCommand(
            group_id=uuid.UUID(data["group_id"]),
            payer_id=payer_id,
            amount_cents=amount_cents,
            currency=currency,
            description=data.get("description"),
            participants=participants,
            split_mode=mode,
            split_params=split_params,
            event_id=event_id,
            created_by=payer_id,
        )
    except ValidationError as exc:
        logger.warning("Invalid wizard expense draft: %s", exc)
        await state.update_data(submitting=False)
        await callback.answer(
            f"Invalid: {humanize_validation_error(exc)}", show_alert=True
        )
        return

    try:
        result = await add_expense(uow, cmd)
    except DomainError as exc:
        await state.update_data(submitting=False)
        await callback.answer(str(exc), show_alert=True)
        return

    await state.clear()

    sel_members = [_dict_to_member(roster[i]) for i in selected]
    lines = format_expense_receipt(
        scoped_event_name=(
            data.get("active_event_name") if event_id is not None else None
        ),
        description=data.get("description"),
        amount_cents=result.amount_cents,
        currency=result.currency,
        payer_username=data.get("payer_username"),
        payer_first_name=data.get("payer_first_name"),
        participants=sel_members,
        shares=result.shares,
        split_mode=mode,
    )
    # A subset that leaves the payer out is the everyday "I paid, forgot to add
    # myself" footgun — nudge (the expense is still recorded as chosen).
    if payer_id not in participants:
        lines.append(
            "\nℹ️ You're not included in this split. If you shared it too, re-run "
            "and tap yourself in."
        )
    # A #general override while an event is active: confirm it stayed general.
    if event_id is None and data.get("active_event_id"):
        event_name = data.get("active_event_name")
        assert event_name is not None  # set together with active_event_id
        lines.append(general_override_note(event_name))
    # Splitting the whole known group in general scope: warn if the bot can't see
    # everyone yet (mirrors the inline coverage check — independent of the override
    # nudge above, so a #general whole-group split gets both, exactly as inline).
    if event_id is None and len(selected) == len(roster):
        warning = await whole_group_coverage_warning(bot, chat_id, len(selected))
        if warning is not None:
            lines.append(warning)

    lines.append(f"\n{VOID_HINT}")
    await _edit_anchor(bot, chat_id, data, "\n".join(lines), None)
    await callback.answer("Added ✅")
    logger.info(
        "Expense recorded via wizard: expense_id=%s amount_cents=%d participants=%d",
        result.expense_id,
        result.amount_cents,
        len(participants),
    )
