"""Shared group-chat copy used by /start (manual setup), /help (on-demand), and
the my_chat_member handler (the bot being added/promoted). The command list lives
in one constant — COMMAND_REFERENCE — so it never drifts between these entry
points (and stays in step with server.py's terse command-menu descriptions)."""

# Single source of truth for the human-readable command list (richer one-liners
# than the constrained Telegram command-menu descriptions in server.py). Both the
# welcome and /help render from this, so adding a command updates every entry
# point at once.
COMMAND_REFERENCE = (
    "Commands:\n"
    '• /addexpense <amount> "<desc>" [@user …] — record an expense '
    "(/add works too; send it bare for a guided, button-based flow)\n"
    "• /balance [all] — your net position, or every member's with /balance all\n"
    "• /settleup @user [amount] — record a payment; omit amount to settle in full\n"
    "• /void — undo your most recent expense (asks before voiding)\n"
    "• /statements [all] — your transactions, or the whole group's with /statements all\n"
    "• /event … — track a trip or dinner as its own scope (new/pause/resume/close/add/remove)\n"
    "• /simplify [on|off] — view or (admins) toggle simplified settle-up suggestions\n"
    "• /currency [CODE] — view or (admins) set the group's default currency\n"
    "• /group — group info, members, and activity"
)

GROUP_WELCOME = (
    "👋 Hi! I'm countbeans — I track shared expenses for this group so nobody "
    "has to do the mental math.\n"
    "\n"
    f"{COMMAND_REFERENCE}\n"
    "\n"
    "Everyone: run /join to be added to this group's ledger."
)

# /help — the on-demand command reference. The payoff line is the tip: a bare
# command is always safe to try (it shows status/usage or opens a guided flow,
# and never writes without asking), so /help just surfaces that rather than
# duplicating each command's grammar.
GROUP_HELP = (
    "🫘 countbeans — I track and split shared expenses for this group.\n"
    "\n"
    f"{COMMAND_REFERENCE}\n"
    "\n"
    "Tip: every command is safe to send with no arguments — it shows its "
    "status or usage instead of acting (/addexpense opens a guided flow; "
    "/void previews and asks first).\n"
    "New here? Run /join to be added to the ledger."
)

# /help in a private chat — the bot is group-only, so point the user to a group.
PRIVATE_HELP = (
    "🫘 I'm countbeans, a shared-expense tracker for Telegram groups.\n"
    "\n"
    "I only work inside a group chat — add me to one, then send /help there to "
    "see everything I can do."
)

# Shown when the bot is in the group but not an administrator. It needs admin
# rights to keep membership accurate from the join/leave stream, so it refuses to
# process commands until promoted (CLAUDE.md "Onboarding & membership").
PROMOTE_REQUEST = (
    "🔒 I need to be a group administrator before I can track expenses here.\n"
    "Please promote me (Manage group → Administrators), then I'll be ready — "
    "no special permissions needed beyond the default admin role."
)
