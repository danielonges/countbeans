"""Unit tests for the event command DTOs — no database needed."""

import uuid

import pytest
from pydantic import ValidationError

from countbeans.dto.commands import (
    CreateEventCommand,
    EditEventRosterCommand,
    SetActiveEventCommand,
    SetEventStatusCommand,
)


def test_create_event_valid() -> None:
    cmd = CreateEventCommand(
        group_id=uuid.uuid4(), name="Bali Trip", created_by=uuid.uuid4()
    )
    assert cmd.name == "Bali Trip"
    assert cmd.default_currency is None


def test_create_event_with_currency() -> None:
    cmd = CreateEventCommand(
        group_id=uuid.uuid4(),
        name="Bali",
        default_currency="IDR",
        created_by=uuid.uuid4(),
    )
    assert cmd.default_currency == "IDR"


def test_create_event_rejects_blank_name() -> None:
    with pytest.raises(ValidationError, match="name"):
        CreateEventCommand(group_id=uuid.uuid4(), name="   ", created_by=uuid.uuid4())


def test_create_event_rejects_bad_currency() -> None:
    with pytest.raises(ValidationError, match="currency"):
        CreateEventCommand(
            group_id=uuid.uuid4(),
            name="Trip",
            default_currency="US",
            created_by=uuid.uuid4(),
        )


def test_create_event_is_frozen() -> None:
    cmd = CreateEventCommand(
        group_id=uuid.uuid4(), name="Trip", created_by=uuid.uuid4()
    )
    with pytest.raises(Exception):  # noqa: B017
        cmd.name = "Other"  # type: ignore[misc]


def test_set_active_event_allows_none() -> None:
    cmd = SetActiveEventCommand(group_id=uuid.uuid4(), event_id=None)
    assert cmd.event_id is None


def test_set_active_event_allows_id() -> None:
    eid = uuid.uuid4()
    cmd = SetActiveEventCommand(group_id=uuid.uuid4(), event_id=eid)
    assert cmd.event_id == eid


def test_set_event_status_rejects_bad_status() -> None:
    with pytest.raises(ValidationError):
        SetEventStatusCommand(event_id=uuid.uuid4(), status="paused")  # type: ignore[arg-type]


def test_edit_event_roster_valid() -> None:
    cmd = EditEventRosterCommand(
        event_id=uuid.uuid4(), user_id=uuid.uuid4(), action="add"
    )
    assert cmd.action == "add"


def test_edit_event_roster_rejects_bad_action() -> None:
    with pytest.raises(ValidationError):
        EditEventRosterCommand(
            event_id=uuid.uuid4(), user_id=uuid.uuid4(), action="drop"  # type: ignore[arg-type]
        )
