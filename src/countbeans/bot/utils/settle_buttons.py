"""Tap-to-settle buttons — the shared keyboard under /settleup and /balance.

The bot computes suggested transfers and shows them as text; these helpers make
the viewer's own payments *tappable* so settling never means re-typing what the
bot just said (the `/settleup @user amount` transcription step). One callback
namespace (`st:`) is handled in handlers/settleup.py; this module only builds
the buttons, so handlers/balance.py can import it without importing the
settleup handler (no handler→handler cycle).

callback_data layout (Telegram caps it at 64 bytes, so UUIDs travel as 22-char
urlsafe-base64):

  st:p:<origin><scope>:<from22>:<to22>:<currency>   — record this payment in full
  st:x:<from22>                                     — close the /settleup picker

`origin` says which message to repaint after a settle ('k' picker, 'a'
/balance all, 'm' personal /balance); `scope` is 'g' when the picker was opened
with #general (the tap must settle the general scope even mid-event) and '-'
to follow the group's current active scope, exactly like a typed /settleup.
Buttons are *debtor-bound*: the handler resolves the tapper and acts only when
they are the `from` user — like /statements' owner-bound paging.
"""

import base64
import uuid

from aiogram.types import InlineKeyboardButton

from countbeans.dto.domain import Transfer

from .formatting import display_name, format_money

# Most transfers a single message renders as buttons — a keyboard taller than
# this stops being a shortcut. The text above it always lists every transfer.
MAX_PAY_BUTTONS = 10


def encode_id(uid: uuid.UUID) -> str:
    """A UUID as 22 urlsafe-base64 chars — the only encoding that fits two ids
    plus routing inside the 64-byte callback_data cap."""
    return base64.urlsafe_b64encode(uid.bytes).rstrip(b"=").decode()


def decode_id(token: str) -> uuid.UUID:
    """Inverse of encode_id. Raises ValueError on garbage (crafted callbacks)."""
    try:
        raw = base64.urlsafe_b64decode(token + "==")
    except Exception as exc:  # binascii.Error subclasses ValueError, but be safe
        raise ValueError(f"bad id token: {token!r}") from exc
    if len(raw) != 16:
        raise ValueError(f"bad id token length: {token!r}")
    return uuid.UUID(bytes=raw)


def pay_callback_data(transfer: Transfer, *, origin: str, force_general: bool) -> str:
    scope = "g" if force_general else "-"
    return (
        f"st:p:{origin}{scope}:{encode_id(transfer.from_user_id)}:"
        f"{encode_id(transfer.to_user_id)}:{transfer.currency}"
    )


def payment_buttons(
    transfers: list[Transfer],
    names: dict[uuid.UUID, tuple[str | None, str | None]],
    *,
    origin: str,
    force_general: bool = False,
    viewer_is_payer: bool = False,
) -> list[list[InlineKeyboardButton]]:
    """One row per suggested transfer (capped at MAX_PAY_BUTTONS), each tappable
    only by its debtor. `viewer_is_payer` shortens the label to "Pay @x …" when
    every listed transfer is the viewer's own (picker / personal balance)."""

    def label(uid: uuid.UUID) -> str:
        return display_name(*names.get(uid, (None, None)))

    rows: list[list[InlineKeyboardButton]] = []
    for t in transfers[:MAX_PAY_BUTTONS]:
        money = format_money(t.amount_cents, t.currency)
        text = (
            f"💸 Pay {label(t.to_user_id)} {money}"
            if viewer_is_payer
            else f"💸 {label(t.from_user_id)} → {label(t.to_user_id)} {money}"
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text=text,
                    callback_data=pay_callback_data(
                        t, origin=origin, force_general=force_general
                    ),
                )
            ]
        )
    return rows
