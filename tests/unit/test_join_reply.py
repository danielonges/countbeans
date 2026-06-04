"""Unit tests for /join's status-aware reply composition.

`_compose_join_reply` is a pure function of the OnboardResult flags, so the
branch selection is tested here without a bot or DB. Claiming takes precedence
over newly-added (it's the more informative outcome).
"""

import uuid

from countbeans.bot.handlers.join import _compose_join_reply
from countbeans.dto.results import OnboardResult


def _result(*, claimed: bool, newly_added: bool) -> OnboardResult:
    return OnboardResult(
        user_id=uuid.uuid4(),
        username="bob",
        first_name="Bob",
        claimed_placeholder=claimed,
        newly_added=newly_added,
    )


def test_claimed_placeholder_message() -> None:
    reply = _compose_join_reply(_result(claimed=True, newly_added=True))
    assert "linked" in reply.lower()


def test_claimed_takes_precedence_over_newly_added() -> None:
    # Even when both flags are set, the claim message wins.
    claimed_only = _compose_join_reply(_result(claimed=True, newly_added=False))
    both = _compose_join_reply(_result(claimed=True, newly_added=True))
    assert claimed_only == both


def test_newly_added_message() -> None:
    reply = _compose_join_reply(_result(claimed=False, newly_added=True))
    assert "you're in" in reply.lower()


def test_already_member_message() -> None:
    reply = _compose_join_reply(_result(claimed=False, newly_added=False))
    assert "already" in reply.lower()
