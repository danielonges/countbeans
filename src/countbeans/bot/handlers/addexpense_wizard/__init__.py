"""Interactive, button-driven `/addexpense` wizard.

A *bare* ``/addexpense`` (no args) starts this guided flow instead of showing
inline usage; the one-liner grammar in ``addexpense.py`` is untouched and still
serves power users. The wizard collects the same fields and calls the same
``add_expense`` service path — it is a bot-layer *entry path* only.

Two platform facts drive the shape (see CLAUDE.md / the plan):

* **Group privacy mode** means the bot only receives commands, @mentions, and
  *replies to its own messages*. So every free-text step (amount, description, a
  per-person share) is prompted with ``ForceReply`` and the answer is matched
  back to the prompt's ``message_id``. Choice steps use inline keyboards, whose
  callbacks always arrive.
* **callback_data is 64 bytes**, too small for an expense draft, so the draft
  lives in aiogram FSM state (``MemoryStorage``) keyed by ``(chat, user)`` —
  which also isolates two users' concurrent wizards for free. Buttons reference
  roster members by **index** into the stored roster, never by UUID.

All steps after the amount edit a single **anchor** message in place (like
``/statements`` paging), keeping the group thread clean. Anchor buttons are
bound to the initiator and reject other users' taps.

The package splits by concern: ``states`` (the FSM states + typed draft and
pure helpers over it), ``render`` (anchor/prompt painting), ``steps`` (the
ForceReply free-text steps + /cancel), and ``actions`` (inline-button callbacks
+ submit).
"""

from aiogram import Router

from . import actions, steps
from .steps import start_wizard

router = Router()
router.include_router(steps.router)
router.include_router(actions.router)

__all__ = ["router", "start_wizard"]
