"""Unit tests for /statements rendering and pagination-keyboard logic."""

from datetime import datetime, timezone
from typing import Any

from countbeans.bot.handlers.statements import _entry_lines, _keyboard, _render
from countbeans.dto.domain import StatementEntry, StatementPage

_WHEN = datetime(2026, 6, 3, 12, 30, tzinfo=timezone.utc)

_EXPENSE = StatementEntry(
    kind="expense",
    created_at=_WHEN,
    amount_cents=2550,
    currency="SGD",
    description="Dinner",
    actor_username="alice",
    counterparty_username=None,
    participant_count=3,
    voided=False,
)

_SETTLEMENT = StatementEntry(
    kind="settlement",
    created_at=_WHEN,
    amount_cents=1000,
    currency="SGD",
    description=None,
    actor_username="bob",
    counterparty_username="alice",
    participant_count=None,
    voided=False,
)


def _expense(**over: Any) -> StatementEntry:
    return _EXPENSE.model_copy(update=over)


def _settlement(**over: Any) -> StatementEntry:
    return _SETTLEMENT.model_copy(update=over)


def _page(entries, page=0, page_size=8, total=None) -> StatementPage:
    return StatementPage(
        entries=entries,
        page=page,
        page_size=page_size,
        total=len(entries) if total is None else total,
    )


def test_expense_line_shows_amount_payer_and_split():
    out = _entry_lines(_expense())
    assert "Dinner — SGD 25.50" in out
    assert "paid by @alice" in out
    assert "split 3-way" in out


def test_voided_expense_is_flagged():
    out = _entry_lines(_expense(voided=True))
    assert "❌" in out
    assert "(voided)" in out


def test_settlement_line_shows_direction():
    out = _entry_lines(_settlement())
    assert "@bob → @alice: SGD 10.00" in out


def test_missing_username_falls_back_to_someone():
    out = _entry_lines(_expense(actor_username=None))
    assert "paid by someone" in out


def test_render_empty_page():
    text = _render(_page([], total=0), "📋 Group statement")
    assert "No transactions yet." in text


def test_render_header_has_page_and_total():
    # 9 entries, page size 8 → 2 pages; viewing page 0.
    text = _render(
        _page([_expense()], page=0, page_size=8, total=9), "📋 Group statement"
    )
    assert "page 1/2" in text
    assert "9 total" in text


def test_keyboard_first_page_has_only_next():
    kb = _keyboard(_page([_expense()], page=0, page_size=8, total=20), "stmt:g")
    assert kb is not None
    buttons = kb.inline_keyboard[0]
    assert [b.text for b in buttons] == ["Next ▶"]
    assert buttons[0].callback_data == "stmt:g:1"


def test_keyboard_last_page_has_only_prev():
    kb = _keyboard(_page([_expense()], page=2, page_size=8, total=20), "stmt:g")
    assert kb is not None
    buttons = kb.inline_keyboard[0]
    assert [b.text for b in buttons] == ["◀ Prev"]
    assert buttons[0].callback_data == "stmt:g:1"


def test_keyboard_middle_page_has_both():
    kb = _keyboard(_page([_expense()], page=1, page_size=8, total=20), "stmt:u:42")
    assert kb is not None
    buttons = kb.inline_keyboard[0]
    assert [b.text for b in buttons] == ["◀ Prev", "Next ▶"]
    assert buttons[0].callback_data == "stmt:u:42:0"
    assert buttons[1].callback_data == "stmt:u:42:2"


def test_keyboard_single_page_is_none():
    assert (
        _keyboard(_page([_expense()], page=0, page_size=8, total=3), "stmt:g") is None
    )
