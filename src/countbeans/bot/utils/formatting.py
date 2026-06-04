"""Shared display-formatting helpers for the bot layer.

The @-prefix, first-name fallback, and generic placeholder are presentation
concerns and live here, never in DTOs or the service core (CLAUDE.md: display
strings are formatted in the bot layer).
"""


def display_name(username: str | None, first_name: str | None) -> str:
    """Render a user for display: @handle, else first name, else a generic — but
    never a raw UUID.

    Unreachable-in-practice last branch: a claimed user always has a first name
    (Telegram requires it) and a placeholder always has a @handle, so one of the
    first two always fires; the fallback only guards the degenerate case.
    """
    if username:
        return f"@{username}"
    if first_name:
        return first_name
    return "someone"
