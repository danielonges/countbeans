"""Unit tests for /balance's _display fallback: @handle → first name → generic."""
import uuid

from countbeans.bot.handlers.balance import _display
from countbeans.dto.domain import MemberBalance


def _member(username: str | None, first_name: str | None) -> MemberBalance:
    return MemberBalance(
        user_id=uuid.uuid4(),
        username=username,
        first_name=first_name,
        balance_cents=100,
        currency="SGD",
    )


def test_prefers_username() -> None:
    assert _display(_member("alice", "Alice")) == "@alice"


def test_falls_back_to_first_name() -> None:
    assert _display(_member(None, "Bob")) == "Bob"


def test_falls_back_to_generic_when_nameless() -> None:
    # Near-unreachable in practice (claimed users always have a first name,
    # placeholders always have a @handle) — but never a raw UUID.
    assert _display(_member(None, None)) == "someone"
