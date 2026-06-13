# Command-by-command UX review — Nielsen heuristic evaluation

**Date:** 2026-06-13
**Method:** Heuristic evaluation against Nielsen's ten usability heuristics
(NN/g), applied to every user-facing command. The evaluation criteria mirror
the ones used for the `/addexpense` redesign:

1. **Fewest interactions** — taps + typed messages to complete the task.
2. **Zero recall** — the user should never have to remember exact syntax,
   keywords, or state to get something done; the interface should show them.

**Scope:** `/addexpense` (baseline — just redesigned), `/balance`,
`/statements`, `/settleup`, `/simplify`, `/void`, `/event`, `/group`,
`/currency`, and the onboarding surface (`/start`, `/join`, `/help`, welcome
messages). This document critiques *behavior as a user experiences it* and
recommends changes at the feature level only — no implementation notes.

**Heuristic shorthand** (used in finding tags):

| Tag | Heuristic |
|---|---|
| H1 | Visibility of system status |
| H2 | Match between system and the real world |
| H3 | User control and freedom (undo/redo, emergency exit) |
| H4 | Consistency and standards |
| H5 | Error prevention (slips vs. mistakes) |
| H6 | Recognition rather than recall |
| H7 | Flexibility and efficiency of use (accelerators) |
| H8 | Aesthetic and minimalist design |
| H9 | Help users recognize, diagnose, and recover from errors |
| H10 | Help and documentation |

**Severity scale** (NN/g standard): **1** cosmetic · **2** minor ·
**3** major (high priority) · **4** usability catastrophe (must fix).

> **Implementation status is tracked in place:** items are tagged
> ✅ **FIXED** (with date) once shipped; anything untagged is still
> outstanding. Last updated: 2026-06-13.

---

## Executive summary — top findings, ranked

1. ✅ **FIXED 2026-06-13** — **Bare `/void` executes a write instantly, and
   `/help` tells users to send bare commands to explore.** The help text said
   *"send any command with no arguments to see exactly how it works"* — a user
   who followed that advice with `/void` silently voided the group's most
   recent expense. One command violated the otherwise-universal "bare = safe"
   contract, and the documentation actively steered people into it. (H4 + H5,
   severity 4 in combination.) *Fix: `/void` now previews the expense
   (description, amount, payer, time, scope) with confirm/keep buttons bound
   to the caller; the confirm is pinned to the previewed entry's id, so a
   write landing in between can't redirect it, and a double-tap is a no-op.
   The `/help` tip now truthfully promises bare commands never act unasked.*
2. ✅ **FIXED 2026-06-13** — **Suggested transfers are dead text.** `/balance
   all` computes exactly who should pay whom and how much — then required the
   user to hand-transcribe that into a `/settleup @user amount` command. The
   textbook H6 violation (information from one screen retyped on another) and
   the single largest remaining friction in the core loop. (Severity 3.)
   *Fix: bare `/settleup` is now a picker — your suggested payments as
   tap-to-pay buttons, one tap settles in full; `/balance` and `/balance all`
   carry the same buttons under their transfer lists. Every button is bound to
   its debtor, the amount is re-derived at tap time (a stale button alerts
   instead of overpaying), and the view repaints after the payment lands.*
3. ✅ **FIXED 2026-06-13** — **Settlements cannot be undone.** `/void` covered
   expenses only. A mis-typed settlement amount or direction was permanent.
   (H3, severity 3.) *Fix: settlements now void exactly like expenses
   (append-only stamp; balances re-derive; statements show them struck out).
   Either party to the settlement — or an admin — may void it.*
4. ✅ **FIXED 2026-06-13** — **Only the most recent expense is correctable.**
   Discovering yesterday's error led to a dead end. (H3 + H6, severity 3.)
   *Fix: the `/void` preview steps back through the last 10 entries in scope
   (⬅ Older / Newer ➡). An entry the caller can't void still previews — naming
   who can — so they can step on to their own. A void entry point directly
   from `/statements` remains outstanding.*
5. **Mode visibility is inconsistent.** Active-event mode changes what every
   write and read means, but `/statements` doesn't say which scope it's
   showing, and `/group` — the "group info" command — doesn't mention the
   active event at all. (H1, severity 2–3.)
6. **`/event` is the most recall-heavy surface left** — eight text
   subcommands, admin semantics, a pause-vs-close model users predictably
   trip on, and zero buttons even on replies that describe exactly which
   actions are available next. (H6 + H5, severity 3.)
7. **Read commands silently swallow bad arguments.** `/balance al` (typo)
   shows your *personal* balance with no hint that the argument was ignored —
   the user may believe they're looking at the group view. (H9, severity 2.)

The overarching theme: **the bot already has a strong interaction pattern —
state-aware inline buttons with per-user ownership gates** (the wizard
roster, `/statements` pagination). The biggest wins come from extending that
pattern to replies that currently *describe* available actions in prose
instead of *offering* them as buttons. Very few commands need a wizard;
most need one or two buttons on replies they already send.

---

## Cross-cutting themes

### T1 — The bare-command contract is incoherent (H4, H5 · severity 4)

What a bare command does today varies by command, with no discernible rule:

| Bare command | Behavior |
|---|---|
| `/addexpense` | launches the wizard |
| `/balance`, `/statements`, `/group`, `/simplify`, `/currency` | safe read |
| `/event` | usage text + status |
| `/settleup` | usage text |
| `/void` | **executes a write immediately** |

Users build a mental model from repetition: "sending the bare command is
safe — it shows me the state or teaches me the syntax." `/help` explicitly
endorses this model. `/void` breaks it with the worst possible payload: an
unprompted ledger mutation. **Recommendation:** adopt an explicit contract —
*a bare command never writes* — and bring `/void` into compliance (see its
section). Separately, update the `/help` tip, which is also stale for
`/addexpense` (bare no longer shows usage; it starts the wizard — a good
outcome, but the tip should say so).

> ✅ **FIXED 2026-06-13** — `/void` now previews with a confirm step (the
> contract holds everywhere), and the `/help` tip was rewritten to describe
> the real, now-uniform behavior.

### T2 — Computed knowledge should be tappable, not transcribable (H6, H5, H7 · severity 3)

The bot frequently *knows* the next action and renders it as prose the user
must re-type:

- `/balance all` lists `@bob → @alice: SGD 5.00` → user types
  `/settleup @alice 5` (and can mistype the amount, the handle, or the
  direction — every error `/settleup` guards against is an error this flow
  *invites*).
- `/event info` prints `/event pause • /event close` as text → admin retypes
  them.
- The welcome message says "run /join" → every member types `/join`.

Each of these is one button away from zero recall and one tap. The ownership
problem (group-chat buttons are visible to everyone) is already solved in
this product: `/statements` personal pagination rejects non-owners with a
clear alert, and the wizard anchor is owner-bound. The same gate makes
"Pay @alice SGD 5.00" safe to show in a group: only the named debtor can
tap it.

> ✅ **PARTIALLY FIXED 2026-06-13** — the settle-up half shipped (tap-to-pay
> on bare `/settleup`, `/balance`, and `/balance all`, debtor-gated). Still
> prose-only: `/event info`'s action hints and the welcome's "run /join".

### T3 — Mode (active event) needs consistent signaling (H1 · severity 2–3)

Active-event mode is a classic mode in the NN/g sense: identical input means
different things depending on invisible state. The product handles this well
on *writes* (receipts echo the scope; `#general` overrides are confirmed)
but inconsistently on *reads*: `/balance` headers name the event,
`/statements` headers don't, and `/group` omits the active event entirely.
Anywhere money is displayed, the scope it was filtered by should be named.
(Note: *named cross-scope reads* — e.g. reading general while an event is
active — are a separate, deliberately deferred feature; this finding is only
about labeling what is already shown.)

### T4 — Forgiving parsing has tipped into silent misdirection (H9 · severity 2)

`/balance` and `/statements` ignore unrecognized arguments and fall back to
the personal view. Forgiveness is right; *silence* is not. A typo'd selector
should still answer, but with a one-line note: "I didn't recognize 'al' —
showing your own balance. For everyone's, use /balance all." Cost: one line.
Benefit: the user never mistakes the personal view for the group view.

### T5 — Admin refusals are a strength — keep the formula (H9 · positive)

Almost every refusal in the product names *who can* do the thing and *what
the caller can do instead* ("Only group admins can manage events… Anyone can
view the current event with /event info"). This is exemplary error-message
design and should remain the template for any new feature.

---

## `/addexpense` (+ `/add`, wizard) — the baseline

**Cost today:** equal split with everyone = command + 1 reply + 1 tap.
One-liner accelerator for experts, with a receipt-embedded teaching tip
bridging novice → expert. Uneven splits keep a reconciliation-gated confirm.

**Verdict:** post-redesign, this is the reference experience — fast path for
the common case, guided path for everything, undo via `/void`, scope echoed
on every receipt. Remaining nits only:

- **(H6, sev 1)** The wizard's uneven-split share entry still requires one
  typed reply per person. Acceptable (buttons can't carry arbitrary
  numbers), and uneven splits are rare; no change recommended now.
- **(H4, sev 1)** The payer-excluded nudge wording differs between inline
  ("@mention your own handle") and wizard ("re-run and tap yourself in") —
  correct in context, just worth keeping intentional.

**Wizard verdict:** has one; it's the model the rest of this review measures
against.

---

## `/balance`

**Cost today:** 1 message → 1 reply. Personal by default; `all` for the
group view. No buttons.

**What works:** instant, glanceable, names the event scope in its header,
shows direction in plain words ("you owe" / "you're owed").

**Findings:**

1. **(H6/H7, sev 2)** Pivoting between "my balance" and "everyone's" means
   retyping the command with a remembered selector. The most common
   follow-up to one view is the other.
2. ✅ **FIXED 2026-06-13** — **(H6/H5, sev 3)** The suggested-transfers block
   was the product's highest-value computation rendered as inert text (theme
   T2). *Fix: both `/balance` views now carry a debtor-gated tap-to-settle
   button per transfer, and the view repaints after a payment lands.*
3. **(H2, sev 1)** "To settle up (simplified)" / "(raw)" is system
   vocabulary. "Raw" means nothing to a non-technical user; the distinction
   that matters to them is "fewest payments" vs. "exact pairwise debts."
4. **(H9, sev 2)** Unrecognized arguments are silently ignored (theme T4).
5. **(H4, sev 1)** `/statements` accepts `me` as a selector; `/balance`
   accepts only bare-or-`all`. The two selector families should be
   identical — anyone who learns `me` on one will try it on the other.

**Recommendations (feature level):**

- Add a single pivot button to each view ("👥 Everyone's balances" on the
  personal view, "🙋 Just mine" on the group view) that edits the message in
  place — same pattern as statement pagination.
- ✅ **FIXED 2026-06-13** — Where a suggested transfer involves the viewer,
  make it actionable: a "Pay @alice SGD 5.00" button, tappable only by the
  named debtor (owner gate, as in `/statements`), recording the settlement
  with a confirmation reply. This collapses read-suggestion → settle from two
  commands plus transcription into one tap.
- Replace "simplified"/"raw" with plain words; keep the toggle's behavior
  untouched.
- Accept `me` for symmetry, and add the gentle unrecognized-argument note.

**Wizard verdict:** **no wizard.** It's a read; a wizard would add steps.
Buttons on the reply are the right tool.

---

## `/statements`

**Cost today:** 1 message → 1 reply; ◀/▶ pagination at 8 entries per page,
personal pages owner-gated with a clear alert. `me` / `all` selectors.

**What works:** pagination is the house pattern done right — in-place edits,
page indicator ("page 2/4, 29 total"), buttons only when meaningful, and the
cross-user rejection message tells the intruder the right command to run.
Voided entries are visibly struck (❌ + "(voided)").

**Findings:**

1. **(H1, sev 2)** The header never names the active event. During a trip,
   "📋 Your statement" is silently event-only — a user checking whether an
   old general expense was recorded will conclude it's missing. `/balance`
   already names the scope; this is the inconsistency (theme T3).
2. **(H3/H6, sev 3 → 2)** The statement is where users *discover* mistakes —
   and it offers no path to correct any of them. *Largely mitigated
   2026-06-13: `/void` now steps back through the last 10 entries (both
   kinds), so anything recently visible in a statement is reachable without
   IDs.* Still outstanding: acting on an entry directly from the statement
   page itself.
3. **(H9, sev 2)** Silent argument swallowing, as with `/balance` (theme T4).
4. **(H1, sev 1)** Timestamps carry no timezone hint. For a travel-oriented
   product, "Jun 03 12:30" in an unstated zone occasionally misleads
   ("that dinner was at 8pm"). A one-time footnote or localized times would
   close it.

**Recommendations (feature level):**

- Name the scope in the header whenever an event is active ("📋 Your
  statement — "Bali Trip"").
- Treat the statement as the entry point for corrections: from a statement
  view, a member should be able to initiate voiding one of *their* visible
  entries (selection by tapping, never by typing an ID). Same ownership and
  admin rules as `/void` today.
- Add the unrecognized-argument note (shared with `/balance`).

**Wizard verdict:** **no wizard.** Extend the existing button surface
(scope label + entry-level actions), don't add steps.

---

## `/settleup`

**Cost today:** one typed message — but only after the user has assembled,
from memory or from a separate `/balance all` screen: the direction
convention (caller is always the payer; the mention is the recipient), the
counterparty's exact handle, optionally the exact amount, and the rule that
omitting the amount settles the full suggested debt in the group's default
currency. Bad input lands on a four-line usage block.

**What works:** the error messages are genuinely diagnostic — they name the
owed amount, the currency, and the corrective command ("Only SGD 50.00 is
owed in that direction — settle that or less, or omit the amount to settle
in full"). The admin forms (`@from @to`, `@all`) are appropriately gated
with explanatory refusals, and the on-behalf form notifies the affected
member ("@bob — flag it if that's not right") — a nice accountability touch.

**Findings:**

1. ✅ **FIXED 2026-06-13** — **(H6, sev 3)** This was the recall-heaviest
   *everyday* command once `/addexpense` had a guided path. The information it
   demanded is exactly what the bot already computed elsewhere (theme T2).
   *Fix: bare `/settleup` shows your suggested payments as tap-to-pay buttons
   (empty state: "you're all settled up"); direction, counterparty, currency,
   and amount all come from the suggestion — nothing to transcribe. The
   `#general` override carries through to the buttons. Typed forms unchanged
   as the accelerator and the only path to partial amounts.*
2. **(H2, sev 2)** The direction convention ("`/settleup @alice` means *I pay
   Alice*") is invisible in the command itself and only learnable from the
   usage block. Settling is also the moment of highest anxiety in an
   expense-splitting product — users double-check direction precisely
   because the syntax doesn't state it.
3. ✅ **FIXED 2026-06-13** — **(H3, sev 3)** **No undo.** A settlement
   recorded with the wrong amount or counterparty was permanent. *Fix:
   settlements are voidable via `/void` like expenses — by either party or
   an admin.*
4. **(H9, sev 1)** The currency-mismatch error explains itself well but
   makes the user re-issue the whole command with an explicit amount; it
   could carry the corrected command in copy-paste form.

**Recommendations (feature level):**

- ✅ **FIXED 2026-06-13** — **This is the strongest wizard/picker candidate in
  the product — but as a one-screen picker, not a multi-step wizard.** Bare
  `/settleup` should show the caller *their own* suggested payments as
  buttons ("Pay @alice SGD 25.50", "Pay @dana EUR 10.00") plus a cancel. One
  tap settles in full — the dominant case. Partial amounts remain the typed
  accelerator's job (the usage text already teaches it). The empty state
  ("you owe nobody") replaces today's usage-block response with something far
  more reassuring.
- ✅ **FIXED 2026-06-13** — The same buttons surfaced under `/balance all`
  (debtor-gated) make the read view actionable without even issuing
  `/settleup`.
- ✅ **FIXED 2026-06-13** — Extend undo to settlements (see `/void`).
- Keep the typed forms exactly as they are for experts and admins —
  consistent with the `/addexpense` philosophy of wizard-plus-accelerator.

**Wizard verdict:** **yes — a single-screen suggestion picker** on bare
invocation. Highest projected friction reduction of any change in this
review.

---

## `/simplify`

**Cost today:** 1 message → 1 reply. Read open to all; toggle admin-gated
with a clear refusal. Idempotent calls answer "already ON/OFF."

**Findings:**

1. **(H2, sev 1)** "Debt simplification" is named but never explained at the
   point of use. The toggle reply ("Debt simplification is now OFF.")
   doesn't say what just changed for the reader of `/balance all`.
2. **(H1, sev 1)** The state-change reply could show its effect ("suggested
   transfers will now show every pairwise debt") so the admin can confirm
   they got what they intended without running `/balance all` to check.

**Recommendations:** append one plain-language clause to the read and toggle
replies explaining the visible effect (fewer payments vs. exact pairwise
debts; balances never change). Nothing else — the command is appropriately
tiny. (The ON-by-default decision is settled and not re-examined here.)

**Wizard verdict:** **no.** Two states, one argument, clear refusals.

---

## `/void`

**Cost today:** 1 bare message → preview of the target expense → 1 confirm
tap. *(Originally: the bare message immediately voided, sight unseen — fixed
2026-06-13.)* Permissions are sensible (payer/recorder always; others need
admin, with a refusal that names who *can*).

**Findings:**

1. ✅ **FIXED 2026-06-13** — **(H5, sev 4 — in combination with the `/help`
   tip)** A bare command that writes, in a product where every other bare
   command reads or teaches, and whose own help text invites bare-command
   exploration (theme T1). The slip is invisible until after it happens.
   *Fix: bare `/void` now shows, not does — preview + caller-bound
   confirm/keep buttons.*
2. ✅ **FIXED 2026-06-13** — **(H5, sev 3)** Even for intentional use, there
   was no preview: "the most recent expense in scope" was a *guess* at
   message-send time. In an active group, someone else's expense may have
   landed in between — the caller voided (or, if admin, silently voided
   *someone else's*) entry they never saw. *Fix: the preview names the entry,
   and the confirm voids exactly the previewed expense id — a write landing
   in between can never redirect it; a stale confirm reports "already voided
   or gone" instead of acting.*
3. ✅ **FIXED 2026-06-13** — **(H3, sev 3)** Only the most recent expense was
   reachable. *Fix: the preview steps through the last 10 active entries in
   scope with ⬅ Older / Newer ➡; permission is evaluated per entry, so a
   non-owner can browse past someone else's entry to their own.*
4. ✅ **FIXED 2026-06-13** — **(H3, sev 3)** Settlements were out of scope
   entirely. *Fix: `/void` now browses and voids settlements too (either
   party or an admin); voided settlements stay in `/statements`, struck out.*
5. ✅ **FIXED 2026-06-13** — **(H9, sev 1)** "Nothing to void — no expenses
   recorded yet" was slightly wrong when expenses exist but are all voided.
   *Fix: the empty state now reads "no active expenses or settlements here."*

**Recommendations (feature level):**

- ✅ **FIXED 2026-06-13** — **Bare `/void` should show, not do:** display the
  entry it *would* void (description, amount, who paid, when) with a confirm
  button and a cancel. One extra tap converts an invisible slip into a
  reviewable action — this is exactly the "confirmation friction on
  high-cost errors" budget the `/addexpense` redesign spent correctly. The
  confirm must be tappable only by the caller.
- ✅ **FIXED 2026-06-13** — From that same preview, allow stepping to slightly
  older entries (the caller's own recent expenses; admins see all) so the
  discovered-later mistake has a recovery path without IDs or new syntax.
  This pairs with the `/statements` entry-point recommendation (which is
  still outstanding).
- ✅ **FIXED 2026-06-13** — Extend voiding to settlements under the same
  permission model (a settlement's sender or recipient stands in for
  payer/recorder — no recorder is stored). The ledger is append-only either
  way; this was purely an exposure question.
- Keep a typed accelerator for the power case if desired — but the
  *default* path must preview first.

**Wizard verdict:** **a one-screen confirm, not a wizard.** The current
zero-confirmation design optimizes taps at the exact spot where Nielsen
says to spend them.

---

## `/event`

**Cost today:** every operation is one typed message — but choosing *which*
message requires recalling eight subcommands, quoting rules for names,
currency-code placement, the pause-vs-close distinction, and the
one-open-event rule. Zero buttons anywhere in the family. All mutations
admin-only (deliberate; not re-examined), reads open to all.

**What works:** bare `/event` and `/event info` are genuinely good status
surfaces — name, currency, active/paused state, roster, outstanding
balances, and *state-appropriate next commands* listed in the reply. The
refusal and error messages consistently say what to do instead. The
`#general` hint lives exactly where the relevant mode confusion arises.

**Findings:**

1. **(H6, sev 3)** The family's own replies prove the point: `/event info`
   *prints* "/event pause • /event close" as text the admin must retype.
   The system knows the state and the legal transitions; the user supplies
   keystrokes (theme T2).
2. **(H5/H2, sev 3)** The pause-vs-close model is the family's recurring
   trap, and the product knows it (the "already open — close it first"
   error exists because people pause an event, forget it, and weeks later
   can't start a new one). The error recovers well, but prevention is
   available: the state is always shown — the *actions* on it aren't.
3. **(H4, sev 2)** A member added to the roster by tapped mention (no public
   username) can never be removed — removal only accepts a typed `@handle`.
   The add path and remove path accept different identifier families, and
   the asymmetry creates an unremovable roster entry.
4. **(H5, sev 2)** Removing someone with outstanding event balances is
   silent and unconditional. Their debts persist in the ledger (correct) but
   vanish from the visible roster (confusing at settle-up time). A
   non-blocking warning at removal — mirroring the coverage-warning
   philosophy — would prevent the later "who is this debt for?" moment.
5. **(H6, sev 2)** Editing a roster by typing one handle per message is the
   exact task the `/addexpense` wizard already solved with a paged toggle
   roster.
6. **(H10, sev 1)** After `/event close`, a user who wants the event back
   discovers reopening isn't supported only by trying syntax that fails
   into the generic usage block. Until reopen exists (deferred), the close
   confirmation could set the expectation ("closed events stay closed").

**Recommendations (feature level):**

- **Not a wizard — state-aware action buttons on the status replies.**
  `/event info` and bare `/event` should carry the legal transitions as
  buttons for admins (Pause/Close while active; Resume/Close while paused),
  with the existing admin gate deciding tappability. This removes most of
  the subcommand recall at zero added steps, and turns the paused-event
  trap into a visible, one-tap recovery.
- Roster editing should offer the toggle-roster pattern from the
  `/addexpense` wizard (tap members in/out), reachable from `/event info`.
  Typed add/remove stays as the accelerator. This also resolves the
  unremovable-member asymmetry, since tapping doesn't need a handle.
- Add the non-blocking warning when removing a roster member with
  outstanding balances in the event.
- Event creation can stay typed (a name is keyboard-natural); the only
  guided piece worth considering is a currency suggestion after creation,
  and only if real groups show currency-setting mistakes.

**Wizard verdict:** **no wizard — buttonize the status replies.** The
information architecture is already right; the actions are just trapped in
prose.

---

## `/group`

**Cost today:** 1 message → 1 reply: name, default currency, simplify
setting, claimed members, pending placeholders, a coverage-gap nudge with
the corrective command, and per-currency activity totals.

**Findings:**

1. **(H1, sev 2)** The "group info" command omits the single most
   action-relevant piece of group state: whether an event is active and
   where new expenses will land (theme T3). A user checking "what's the
   state here?" gets currency and member info but not the mode.
2. **(H6, sev 1)** The reply is a good dashboard with no exits — it names
   no related commands (`/balance all`, `/event info`) even where its own
   content begs the follow-up (activity totals → balances).

**Recommendations:** add an "Active event" line (or "No active event — new
expenses are general") to the snapshot; optionally close with one line of
related-command pointers. Nothing structural.

**Wizard verdict:** **no.** It's a status read and a good one.

---

## `/currency`

**Cost today:** 1 message → 1 reply. Read open to all and self-documenting
("Set it with /currency <CODE>"); change admin-gated; the change
confirmation proactively kills the scariest misconception ("This applies to
new expenses — past entries keep their currency" — excellent H1).

**Findings:**

1. **(H5, sev 1)** Any 3-letter alphabetic token is accepted as a currency
   (by-shape trust is a settled design decision). A typo like `/currency
   USE` succeeds silently and every future expense displays it. Given the
   command is admin-only and rare, severity is low — but the confirmation
   carrying the code prominently is what makes the slip catchable, so that
   wording must stay.

**Recommendations:** none required. If polish budget exists: confirm
unfamiliar codes ("USE isn't a currency I recognize — set it anyway?") while
letting real-but-obscure codes through.

**Wizard verdict:** **no.**

---

## Onboarding & discovery (`/start`, `/join`, `/help`, welcome, command menu)

**Cost today (cold start):** add bot → promote to admin (nudged with exact
menu path — good) → welcome wall of text → each member types `/join` → first
`/addexpense`. Every refusal names the alternative path (`/start` non-admin →
"use /join"). Placeholder claiming on `/join` is communicated well ("You'd
already been mentioned in expenses here — I've linked those to you").

**Findings:**

1. ✅ **FIXED 2026-06-13** — **(H10/H5, sev 3)** The `/help` tip — "send any
   command with no arguments to see exactly how it works" — was wrong twice:
   bare `/addexpense` launches the wizard (better than the tip promised, but
   different), and bare `/void` *performed a write* (theme T1). Documentation
   that actively misleads about safety is worse than no documentation.
   *Fix: the tip now reads "every command is safe to send with no arguments —
   it shows its status or usage instead of acting", which became true the
   moment `/void` gained its preview.*
2. **(H8, sev 2)** The welcome message is a ten-bullet command reference
   delivered at the moment the group's task is "get set up," not "learn
   everything." The two actions that matter at that moment — members join,
   someone records the first expense — are buried as the last line.
3. **(H6/H7, sev 2)** Joining requires every member to type `/join`. This is
   the highest-volume onboarding action and it's a typed command with a
   well-known button alternative: a "✋ Count me in" button on the welcome
   message (tappable by anyone, onboarding the tapper) would make joining
   literally one tap and double as social proof in the chat.
4. **(H1, sev 1)** Silent auto-onboarding via the membership stream means
   `/join` often answers "you're already part of this group's ledger" to
   someone who never joined — harmless, but the wording could acknowledge
   it ("you were added automatically when you joined the chat").
5. **(H4, sev 1)** Menu descriptions are good and admin-gating is flagged
   inline ("(admin)") — keep this convention for any new command.

**Recommendations (feature level):**

- Rewrite the `/help` tip to describe per-command reality, or — better —
  make the bare-command contract uniform (T1) so the tip can be true.
- Restructure the welcome into a short "two steps to start" message
  (join + first expense), with a "Count me in" join button, and point to
  `/help` for the full reference.
- Keep the refusal-message formula exactly as is.

**Wizard verdict:** **no wizards.** One button and shorter words.

---

## Wizard/button verdict — summary

| Command | Needs a wizard? | What it actually needs |
|---|---|---|
| `/addexpense` | Has one (baseline) | — |
| `/settleup` | **Yes — one-screen suggestion picker** ✅ shipped 2026-06-13 | ~~Tap-to-pay buttons~~ ✅; typed form stays as accelerator |
| `/void` | One-screen **preview + confirm** ✅ shipped 2026-06-13 | ~~Bare must show, not do~~ ✅; ~~reach older entries~~ ✅; ~~cover settlements~~ ✅ |
| `/event` | No | State-aware action buttons on `/event info` / bare `/event`; toggle-roster for add/remove |
| `/balance` | No | me⇄all pivot button; debtor-gated tap-to-settle on suggestions |
| `/statements` | No | Scope label in header; entry-level void entry point |
| `/group` | No | Active-event line |
| `/simplify` | No | One explanatory clause in replies |
| `/currency` | No | Nothing required |
| `/start` `/join` `/help` | No | Join button on welcome; shorter welcome; truthful help tip |

---

## Priorities

1. ✅ **FIXED 2026-06-13** — **Fix the bare-`/void` write and the `/help` tip
   together** (T1). Smallest change, removes the only severity-4 interaction
   in the product. *Shipped: preview + caller-bound confirm/keep buttons,
   id-pinned confirm, rewritten help tip and `/void` reference line.*
2. ✅ **FIXED 2026-06-13** — **Tap-to-settle** (suggestion picker on bare
   `/settleup`, plus debtor-gated buttons under `/balance` and `/balance
   all`). Biggest friction reduction in the core loop, and it reuses two
   patterns the product already trusts. *Shipped: one tap records the
   payment in full, announces it to the chat, and repaints the view; stale
   buttons alert instead of writing.*
3. ✅ **MOSTLY FIXED 2026-06-13** — **Undo coverage: settlements voidable;
   older entries reachable** from the `/void` preview and `/statements`.
   Closes the H3 gap at the highest-anxiety moments. *Shipped: settlement
   voiding (schema + derivation + struck-out statements) and ⬅ Older / Newer ➡
   stepping through the last 10 entries in the `/void` preview. Outstanding:
   a void entry point directly from `/statements` pages.*
4. **Buttonize `/event` status replies + toggle-roster editing.** Converts
   the most recall-heavy remaining command family; also fixes the
   unremovable-member edge.
5. **Scope labeling sweep** (`/statements` header, `/group` active-event
   line) and the unrecognized-argument notes on read commands.
6. **Onboarding polish** (welcome restructure + join button), plain-language
   wording for simplified/raw.

Items 5–6 are individually small and could ride along with any adjacent
work; items 1–4 are each a self-contained feature.
