"""Shared group-chat copy used by both /start (manual setup) and the
my_chat_member handler (the bot being added/promoted). Kept in one place so the
command list never drifts between the two entry points."""

GROUP_WELCOME = (
    "👋 Hi! I'm countbeans — I track shared expenses for this group so nobody "
    "has to do the mental math.\n"
    "\n"
    "Commands:\n"
    '• /addexpense <amount> "<desc>" [@user …] — record an expense\n'
    "• /balance [all] — your net position, or every member's with /balance all\n"
    "• /settleup @user [amount] — record a payment; omit amount to settle in full\n"
    "• /statements [all] — your transactions, or the whole group's with /statements all\n"
    "• /simplify [on|off] — view or (admins) toggle simplified settle-up suggestions\n"
    "• /currency [CODE] — view or (admins) set the group's default currency\n"
    "• /group — group info, members, and activity\n"
    "\n"
    "Everyone: run /join to be added to this group's ledger."
)

# Shown when the bot is in the group but not an administrator. It needs admin
# rights to keep membership accurate from the join/leave stream, so it refuses to
# process commands until promoted (CLAUDE.md "Onboarding & membership").
PROMOTE_REQUEST = (
    "🔒 I need to be a group administrator before I can track expenses here.\n"
    "Please promote me (Manage group → Administrators), then I'll be ready — "
    "no special permissions needed beyond the default admin role."
)
