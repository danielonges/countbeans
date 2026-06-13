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

**Severity** (NN/g standard): **1** cosmetic · **2** minor · **3** major
(high priority) · **4** usability catastrophe (must fix).

**Status badges** (tracked in place): ✅ shipped · ◑ partial · ☐ open.
Every shipped item below landed on **2026-06-13** (see git history for
commits); the date is omitted on individual items to keep them readable.

---

## Status at a glance

| # | Finding | Sev | Status |
|---|---|---|---|
| 1 | Bare `/void` wrote instantly; `/help` invited it | 4 | ✅ |
| 2 | Suggested transfers were dead text (no tap-to-settle) | 3 | ✅ |
| 3 | Settlements couldn't be undone | 3 | ✅ |
| 4 | Only the most recent expense was correctable | 3 | ✅ |
| 5 | Mode (active-event) visibility inconsistent on reads | 2–3 | ☐ |
| 6 | `/event` recall-heavy, zero buttons | 3 | ◑ |
| 7 | Read commands silently swallow bad arguments | 2 | ☐ |

The four headline findings (1–4) and the `/event` buttonization (6) shipped;
what remains is scope-labeling on reads (5), unrecognized-arg notes (7), the
`/event` toggle-roster editor, a void entry point from `/statements`, and the
onboarding/wording polish. Details per command below.

---

## Executive summary — top findings, ranked

1. ✅ **Bare `/void` wrote instantly, and `/help` told users to send bare
   commands to explore.** The help text said *"send any command with no
   arguments to see exactly how it works"* — a user who followed that advice
   with `/void` silently voided the group's most recent expense. One command
   violated the otherwise-universal "bare = safe" contract, and the
   documentation actively steered people into it. (H4 + H5, sev 4 in
   combination.)
   **Fixed:** `/void` now previews the expense (description, amount, payer,
   time, scope) with confirm/keep buttons bound to the caller; the confirm is
   pinned to the previewed entry's id, so a write landing in between can't
   redirect it, and a double-tap is a no-op. The `/help` tip now truthfully
   promises bare commands never act unasked.

2. ✅ **Suggested transfers were dead text.** `/balance all` computes exactly
   who should pay whom and how much — then required the user to hand-transcribe
   that into a `/settleup @user amount` command. The textbook H6 violation
   (information from one screen retyped on another) and the single largest
   remaining friction in the core loop. (Sev 3.)
   **Fixed:** bare `/settleup` is now a picker — your suggested payments as
   tap-to-pay buttons, one tap settles in full; `/balance` and `/balance all`
   carry the same buttons under their transfer lists. Every button is bound to
   its debtor, the amount is re-derived at tap time (a stale button alerts
   instead of overpaying), and the view repaints after the payment lands.

3. ✅ **Settlements couldn't be undone.** `/void` covered expenses only — a
   mis-typed settlement amount or direction was permanent. (H3, sev 3.)
   **Fixed:** settlements now void exactly like expenses (append-only stamp;
   balances re-derive; statements show them struck out). Either party to the
   settlement — or an admin — may void it.

4. ✅ **Only the most recent expense was correctable.** Discovering
   yesterday's error led to a dead end. (H3 + H6, sev 3.)
   **Fixed:** the `/void` preview steps back through the last 10 entries in
   scope (⬅ Older / Newer ➡). An entry the caller can't void still previews —
   naming who can — so they can step on to their own.
   **Still open:** a void entry point directly from `/statements`.

5. ☐ **Mode visibility is inconsistent.** Active-event mode changes what every
   write and read means, but `/statements` doesn't say which scope it's
   showing, and `/group` — the "group info" command — doesn't mention the
   active event at all. (H1, sev 2–3.)

6. ◑ **`/event` is the most recall-heavy surface left** — eight text
   subcommands, admin semantics, a pause-vs-close model users predictably trip
   on, and zero buttons even on replies that describe exactly which actions are
   available next. (H6 + H5, sev 3.)
   **Fixed:** `/event info` and bare `/event` now carry the legal transitions
   as buttons (Pause/Close while active, Resume/Close while paused) —
   admin-gated at tap, announced to the chat, stale-safe. Removing an unsettled
   member warns; tap-mentioned members can be removed by tap (the
   unremovable-member edge is gone).
   **Still open:** the toggle-roster editor.

7. ☐ **Read commands silently swallow bad arguments.** `/balance al` (typo)
   shows your *personal* balance with no hint that the argument was ignored —
   the user may believe they're looking at the group view. (H9, sev 2.)

The overarching theme: **the bot already has a strong interaction pattern —
state-aware inline buttons with per-user ownership gates** (the wizard roster,
`/statements` pagination). The biggest wins came from extending that pattern to
replies that *described* available actions in prose instead of *offering* them
as buttons. Very few commands need a wizard; most need one or two buttons on
replies they already send.

---

## Cross-cutting themes

### T1 — The bare-command contract is incoherent ✅ (H4, H5 · sev 4)

What a bare command used to do varied by command, with no discernible rule:

| Bare command | Behavior (before) |
|---|---|
| `/addexpense` | launches the wizard |
| `/balance`, `/statements`, `/group`, `/simplify`, `/currency` | safe read |
| `/event` | usage text + status |
| `/settleup` | usage text |
| `/void` | **executed a write immediately** |

Users build a mental model from repetition: "sending the bare command is safe —
it shows me the state or teaches me the syntax." `/help` explicitly endorsed
this model. `/void` broke it with the worst possible payload: an unprompted
ledger mutation.

**Fixed:** the contract is now explicit and uniform — *a bare command never
writes*. `/void` previews with a confirm step, and the `/help` tip was
rewritten to describe the real, now-uniform behavior (it was also stale for
`/addexpense`, which starts the wizard rather than showing usage).

### T2 — Computed knowledge should be tappable, not transcribable ◑ (H6, H5, H7 · sev 3)

The bot frequently *knows* the next action and used to render it as prose the
user had to re-type:

- `/balance all` listed `@bob → @alice: SGD 5.00` → user typed
  `/settleup @alice 5` (and could mistype the amount, the handle, or the
  direction — every error `/settleup` guards against was an error this flow
  *invited*).
- `/event info` printed `/event pause • /event close` as text → admin retyped
  them.
- The welcome message says "run /join" → every member types `/join`.

The ownership problem (group-chat buttons are visible to everyone) was already
solved in this product: `/statements` personal pagination rejects non-owners
with a clear alert, and the wizard anchor is owner-bound. The same gate makes
"Pay @alice SGD 5.00" safe to show in a group: only the named debtor can tap it.

**Fixed (partial):** the settle-up half shipped (tap-to-pay on bare
`/settleup`, `/balance`, and `/balance all`, debtor-gated), and `/event info`'s
action hints became real Pause/Resume/Close buttons.
**Still open:** the welcome's "run /join".

### T3 — Mode (active event) needs consistent signaling ☐ (H1 · sev 2–3)

Active-event mode is a classic mode in the NN/g sense: identical input means
different things depending on invisible state. The product handles this well on
*writes* (receipts echo the scope; `#general` overrides are confirmed) but
inconsistently on *reads*: `/balance` headers name the event, `/statements`
headers don't, and `/group` omits the active event entirely. Anywhere money is
displayed, the scope it was filtered by should be named. (Note: *named
cross-scope reads* — e.g. reading general while an event is active — are a
separate, deliberately deferred feature; this finding is only about labeling
what is already shown.)

### T4 — Forgiving parsing has tipped into silent misdirection ☐ (H9 · sev 2)

`/balance` and `/statements` ignore unrecognized arguments and fall back to the
personal view. Forgiveness is right; *silence* is not. A typo'd selector should
still answer, but with a one-line note: "I didn't recognize 'al' — showing your
own balance. For everyone's, use /balance all." Cost: one line. Benefit: the
user never mistakes the personal view for the group view.

### T5 — Admin refusals are a strength — keep the formula (positive · H9)

Almost every refusal in the product names *who can* do the thing and *what the
caller can do instead* ("Only group admins can manage events… Anyone can view
the current event with /event info"). This is exemplary error-message design and
should remain the template for any new feature.

---

## `/addexpense` (+ `/add`, wizard) — the baseline

**Cost today:** equal split with everyone = command + 1 reply + 1 tap. One-liner
accelerator for experts, with a receipt-embedded teaching tip bridging novice →
expert. Uneven splits keep a reconciliation-gated confirm.

**Verdict:** post-redesign, this is the reference experience — fast path for the
common case, guided path for everything, undo via `/void`, scope echoed on every
receipt. Remaining nits only:

- ☐ **(H6, sev 1)** The wizard's uneven-split share entry still requires one
  typed reply per person. Acceptable (buttons can't carry arbitrary numbers),
  and uneven splits are rare; no change recommended now.
- ☐ **(H4, sev 1)** The payer-excluded nudge wording differs between inline
  ("@mention your own handle") and wizard ("re-run and tap yourself in") —
  correct in context, just worth keeping intentional.

**Wizard verdict:** has one; it's the model the rest of this review measures
against.

---

## `/balance`

**Cost today:** 1 message → 1 reply. Personal by default; `all` for the group
view.

**What works:** instant, glanceable, names the event scope in its header, shows
direction in plain words ("you owe" / "you're owed").

**Findings:**

- ☐ **(H6/H7, sev 2)** Pivoting between "my balance" and "everyone's" means
  retyping the command with a remembered selector. The most common follow-up to
  one view is the other.
- ✅ **(H6/H5, sev 3)** The suggested-transfers block was the product's
  highest-value computation rendered as inert text (theme T2).
  **Fixed:** both `/balance` views now carry a debtor-gated tap-to-settle button
  per transfer, and the view repaints after a payment lands.
- ☐ **(H2, sev 1)** "To settle up (simplified)" / "(raw)" is system vocabulary.
  "Raw" means nothing to a non-technical user; the distinction that matters to
  them is "fewest payments" vs. "exact pairwise debts."
- ☐ **(H9, sev 2)** Unrecognized arguments are silently ignored (theme T4).
- ☐ **(H4, sev 1)** `/statements` accepts `me` as a selector; `/balance`
  accepts only bare-or-`all`. The two selector families should be identical —
  anyone who learns `me` on one will try it on the other.

**Recommendations (feature level):**

- ☐ Add a single pivot button to each view ("👥 Everyone's balances" on the
  personal view, "🙋 Just mine" on the group view) that edits the message in
  place — same pattern as statement pagination.
- ✅ Where a suggested transfer involves the viewer, make it actionable: a
  "Pay @alice SGD 5.00" button, tappable only by the named debtor, recording
  the settlement with a confirmation reply. Collapses read-suggestion → settle
  from two commands plus transcription into one tap.
- ☐ Replace "simplified"/"raw" with plain words; keep the toggle's behavior
  untouched.
- ☐ Accept `me` for symmetry, and add the gentle unrecognized-argument note.

**Wizard verdict:** **no wizard.** It's a read; a wizard would add steps. Buttons
on the reply are the right tool.

---

## `/statements`

**Cost today:** 1 message → 1 reply; ◀/▶ pagination at 8 entries per page,
personal pages owner-gated with a clear alert. `me` / `all` selectors.

**What works:** pagination is the house pattern done right — in-place edits, page
indicator ("page 2/4, 29 total"), buttons only when meaningful, and the
cross-user rejection message tells the intruder the right command to run. Voided
entries are visibly struck (❌ + "(voided)") — now for settlements too.

**Findings:**

- ☐ **(H1, sev 2)** The header never names the active event. During a trip,
  "📋 Your statement" is silently event-only — a user checking whether an old
  general expense was recorded will conclude it's missing. `/balance` already
  names the scope; this is the inconsistency (theme T3).
- ◑ **(H3/H6, sev 3 → 2)** The statement is where users *discover* mistakes —
  and it offers no path to correct any of them.
  **Mitigated:** `/void` now steps back through the last 10 entries (both
  kinds), so anything recently visible in a statement is reachable without IDs.
  **Still open:** acting on an entry directly from the statement page itself.
- ☐ **(H9, sev 2)** Silent argument swallowing, as with `/balance` (theme T4).
- ☐ **(H1, sev 1)** Timestamps carry no timezone hint. For a travel-oriented
  product, "Jun 03 12:30" in an unstated zone occasionally misleads ("that
  dinner was at 8pm"). A one-time footnote or localized times would close it.

**Recommendations (feature level):**

- ☐ Name the scope in the header whenever an event is active ("📋 Your
  statement — "Bali Trip"").
- ☐ Treat the statement as the entry point for corrections: from a statement
  view, a member should be able to initiate voiding one of *their* visible
  entries (selection by tapping, never by typing an ID). Same ownership and
  admin rules as `/void` today.
- ☐ Add the unrecognized-argument note (shared with `/balance`).

**Wizard verdict:** **no wizard.** Extend the existing button surface (scope
label + entry-level actions), don't add steps.

---

## `/settleup`

**Cost today:** bare `/settleup` opens a tap-to-pay picker (see below). The typed
forms still require the user to assemble, from memory or a separate `/balance
all` screen: the direction convention (caller is always the payer; the mention
is the recipient), the counterparty's exact handle, optionally the exact amount,
and the rule that omitting the amount settles the full suggested debt in the
group's default currency.

**What works:** the error messages are genuinely diagnostic — they name the owed
amount, the currency, and the corrective command ("Only SGD 50.00 is owed in
that direction — settle that or less, or omit the amount to settle in full"). The
admin forms (`@from @to`, `@all`) are appropriately gated with explanatory
refusals, and the on-behalf form notifies the affected member ("@bob — flag it
if that's not right") — a nice accountability touch.

**Findings:**

- ✅ **(H6, sev 3)** This was the recall-heaviest *everyday* command once
  `/addexpense` had a guided path. The information it demanded is exactly what
  the bot already computed elsewhere (theme T2).
  **Fixed:** bare `/settleup` shows your suggested payments as tap-to-pay buttons
  (empty state: "you're all settled up"); direction, counterparty, currency, and
  amount all come from the suggestion — nothing to transcribe. The `#general`
  override carries through to the buttons. Typed forms unchanged as the
  accelerator and the only path to partial amounts.
- ☐ **(H2, sev 2)** The direction convention ("`/settleup @alice` means *I pay
  Alice*") is invisible in the typed command and only learnable from the usage
  block. Settling is the moment of highest anxiety in an expense-splitting
  product — users double-check direction precisely because the syntax doesn't
  state it. *(The picker sidesteps this for the common case; the typed form
  still carries it.)*
- ✅ **(H3, sev 3)** **No undo.** A settlement recorded with the wrong amount or
  counterparty was permanent.
  **Fixed:** settlements are voidable via `/void` like expenses — by either party
  or an admin.
- ☐ **(H9, sev 1)** The currency-mismatch error explains itself well but makes
  the user re-issue the whole command with an explicit amount; it could carry
  the corrected command in copy-paste form.

**Recommendations (feature level):**

- ✅ **One-screen suggestion picker on bare invocation** — not a multi-step
  wizard. Bare `/settleup` shows the caller *their own* suggested payments as
  buttons ("Pay @alice SGD 25.50", "Pay @dana EUR 10.00") plus a cancel; one tap
  settles in full. Partial amounts remain the typed accelerator's job. The empty
  state ("you owe nobody") replaces the old usage-block response with something
  far more reassuring.
- ✅ The same buttons surfaced under `/balance` / `/balance all` (debtor-gated)
  make the read view actionable without even issuing `/settleup`.
- ✅ Extend undo to settlements (see `/void`).
- ☐ Keep the typed forms exactly as they are for experts and admins —
  consistent with the `/addexpense` philosophy of wizard-plus-accelerator.

**Wizard verdict:** **yes — a single-screen suggestion picker** on bare
invocation. Highest projected friction reduction of any change in this review.

---

## `/simplify`

**Cost today:** 1 message → 1 reply. Read open to all; toggle admin-gated with a
clear refusal. Idempotent calls answer "already ON/OFF."

**Findings:**

- ☐ **(H2, sev 1)** "Debt simplification" is named but never explained at the
  point of use. The toggle reply ("Debt simplification is now OFF.") doesn't say
  what just changed for the reader of `/balance all`.
- ☐ **(H1, sev 1)** The state-change reply could show its effect ("suggested
  transfers will now show every pairwise debt") so the admin can confirm they
  got what they intended without running `/balance all` to check.

**Recommendations:** ☐ append one plain-language clause to the read and toggle
replies explaining the visible effect (fewer payments vs. exact pairwise debts;
balances never change). Nothing else — the command is appropriately tiny. (The
ON-by-default decision is settled and not re-examined here.)

**Wizard verdict:** **no.** Two states, one argument, clear refusals.

---

## `/void`

**Cost today:** 1 bare message → preview of the target entry → 1 confirm tap.
*(Originally: the bare message immediately voided, sight unseen.)* Permissions
are sensible (payer/recorder always; others need admin, with a refusal that names
who *can*).

**Findings:**

- ✅ **(H5, sev 4 — with the `/help` tip)** A bare command that wrote, in a
  product where every other bare command reads or teaches, and whose own help
  text invited bare-command exploration (theme T1). The slip was invisible until
  after it happened.
  **Fixed:** bare `/void` now shows, not does — preview + caller-bound
  confirm/keep buttons.
- ✅ **(H5, sev 3)** Even for intentional use, there was no preview: "the most
  recent expense in scope" was a *guess* at message-send time. In an active
  group, someone else's expense may have landed in between — the caller voided
  (or, if admin, silently voided someone else's) entry they never saw.
  **Fixed:** the preview names the entry, and the confirm voids exactly the
  previewed id — a write landing in between can never redirect it; a stale
  confirm reports "already voided or gone" instead of acting.
- ✅ **(H3, sev 3)** Only the most recent expense was reachable.
  **Fixed:** the preview steps through the last 10 active entries in scope with
  ⬅ Older / Newer ➡; permission is evaluated per entry, so a non-owner can browse
  past someone else's entry to their own.
- ✅ **(H3, sev 3)** Settlements were out of scope entirely.
  **Fixed:** `/void` now browses and voids settlements too (either party or an
  admin); voided settlements stay in `/statements`, struck out.
- ✅ **(H9, sev 1)** "Nothing to void — no expenses recorded yet" was slightly
  wrong when expenses exist but are all voided.
  **Fixed:** the empty state now reads "no active expenses or settlements here."

**Recommendations (feature level):**

- ✅ **Bare `/void` should show, not do:** display the entry it *would* void with
  a confirm button and a cancel, tappable only by the caller. One extra tap
  converts an invisible slip into a reviewable action — exactly the
  "confirmation friction on high-cost errors" budget the `/addexpense` redesign
  spent correctly.
- ✅ From that same preview, allow stepping to slightly older entries so the
  discovered-later mistake has a recovery path without IDs or new syntax. *(Pairs
  with the still-open `/statements` entry-point recommendation.)*
- ✅ Extend voiding to settlements under the same permission model (a
  settlement's sender or recipient stands in for payer/recorder — no recorder is
  stored).
- ☐ Keep a typed accelerator for the power case if desired — but the *default*
  path must preview first.

**Wizard verdict:** **a one-screen confirm, not a wizard** — confirmation friction
spent at exactly the spot where Nielsen says to spend it.

---

## `/event`

**Cost today:** every operation is one typed message; the status views also carry
action buttons (below). Choosing *which* typed message still requires recalling
eight subcommands, quoting rules for names, currency-code placement, the
pause-vs-close distinction, and the one-open-event rule. All mutations admin-only
(deliberate; not re-examined), reads open to all.

**What works:** bare `/event` and `/event info` are genuinely good status
surfaces — name, currency, active/paused state, roster, outstanding balances, and
the state-appropriate next actions. The refusal and error messages consistently
say what to do instead. The `#general` hint lives exactly where the relevant mode
confusion arises.

**Findings:**

- ✅ **(H6, sev 3)** The family's own replies proved the point: `/event info`
  *printed* "/event pause • /event close" as text the admin had to retype
  (theme T2).
  **Fixed:** the status views carry the legal transitions as buttons; the prose
  hint is gone.
- ✅ **(H5/H2, sev 3)** The pause-vs-close model is the family's recurring trap.
  **Fixed:** the state and its actions are now one surface — a paused event shows
  Resume/Close buttons, so the forgotten-pause dead end resolves in one tap. A
  stale button (someone else already closed) answers gracefully and repaints.
- ✅ **(H4, sev 2)** A member added by tapped mention (no public username) could
  never be removed — removal only accepted a typed `@handle`.
  **Fixed:** `/event remove` accepts a tapped mention too, resolved by Telegram
  id like the add path.
- ✅ **(H5, sev 2)** Removing someone with outstanding event balances was silent
  and unconditional.
  **Fixed:** removal now appends a non-blocking warning naming the unsettled
  amounts — their ledger entries survive and still count.
- ☐ **(H6, sev 2)** Editing a roster by typing one handle per message is the
  exact task the `/addexpense` wizard already solved with a paged toggle roster.
- ☐ **(H10, sev 1)** After `/event close`, a user who wants the event back
  discovers reopening isn't supported only by trying syntax that fails into the
  generic usage block. Until reopen exists (deferred), the close confirmation
  could set the expectation ("closed events stay closed").

**Recommendations (feature level):**

- ✅ **State-aware action buttons on the status replies** (not a wizard).
  `/event info` and bare `/event` carry the legal transitions as buttons for
  admins (Pause/Close while active; Resume/Close while paused), with the existing
  admin gate deciding tappability. Removes most of the subcommand recall at zero
  added steps, and turns the paused-event trap into a visible, one-tap recovery.
- ☐ Roster editing should offer the toggle-roster pattern from the `/addexpense`
  wizard (tap members in/out), reachable from `/event info`. Typed add/remove
  stays as the accelerator. *(The unremovable-member asymmetry this would have
  fixed was closed directly — `/event remove` now accepts tapped mentions.)*
- ✅ Add the non-blocking warning when removing a roster member with outstanding
  balances in the event.
- ☐ Event creation can stay typed (a name is keyboard-natural); the only guided
  piece worth considering is a currency suggestion after creation, and only if
  real groups show currency-setting mistakes.

**Wizard verdict:** **no wizard — buttonize the status replies.** The information
architecture is already right; the actions were just trapped in prose.

---

## `/group`

**Cost today:** 1 message → 1 reply: name, default currency, simplify setting,
claimed members, pending placeholders, a coverage-gap nudge with the corrective
command, and per-currency activity totals.

**Findings:**

- ☐ **(H1, sev 2)** The "group info" command omits the single most
  action-relevant piece of group state: whether an event is active and where new
  expenses will land (theme T3). A user checking "what's the state here?" gets
  currency and member info but not the mode.
- ☐ **(H6, sev 1)** The reply is a good dashboard with no exits — it names no
  related commands (`/balance all`, `/event info`) even where its own content
  begs the follow-up (activity totals → balances).

**Recommendations:** ☐ add an "Active event" line (or "No active event — new
expenses are general") to the snapshot; optionally close with one line of
related-command pointers. Nothing structural.

**Wizard verdict:** **no.** It's a status read and a good one.

---

## `/currency`

**Cost today:** 1 message → 1 reply. Read open to all and self-documenting ("Set
it with /currency <CODE>"); change admin-gated; the change confirmation
proactively kills the scariest misconception ("This applies to new expenses —
past entries keep their currency" — excellent H1).

**Findings:**

- ☐ **(H5, sev 1)** Any 3-letter alphabetic token is accepted as a currency
  (by-shape trust is a settled design decision). A typo like `/currency USE`
  succeeds silently and every future expense displays it. Given the command is
  admin-only and rare, severity is low — but the confirmation carrying the code
  prominently is what makes the slip catchable, so that wording must stay.

**Recommendations:** none required. If polish budget exists: ☐ confirm unfamiliar
codes ("USE isn't a currency I recognize — set it anyway?") while letting
real-but-obscure codes through.

**Wizard verdict:** **no.**

---

## Onboarding & discovery (`/start`, `/join`, `/help`, welcome, command menu)

**Cost today (cold start):** add bot → promote to admin (nudged with exact menu
path — good) → welcome wall of text → each member types `/join` → first
`/addexpense`. Every refusal names the alternative path (`/start` non-admin →
"use /join"). Placeholder claiming on `/join` is communicated well ("You'd
already been mentioned in expenses here — I've linked those to you").

**Findings:**

- ✅ **(H10/H5, sev 3)** The `/help` tip — "send any command with no arguments
  to see exactly how it works" — was wrong twice: bare `/addexpense` launches the
  wizard (better than the tip promised, but different), and bare `/void`
  *performed a write* (theme T1). Documentation that actively misleads about
  safety is worse than no documentation.
  **Fixed:** the tip now reads "every command is safe to send with no arguments —
  it shows its status or usage instead of acting", true the moment `/void` gained
  its preview.
- ☐ **(H8, sev 2)** The welcome message is a ten-bullet command reference
  delivered at the moment the group's task is "get set up," not "learn
  everything." The two actions that matter at that moment — members join, someone
  records the first expense — are buried as the last line.
- ☐ **(H6/H7, sev 2)** Joining requires every member to type `/join`. This is
  the highest-volume onboarding action and it's a typed command with a well-known
  button alternative: a "✋ Count me in" button on the welcome message (tappable
  by anyone, onboarding the tapper) would make joining literally one tap and
  double as social proof in the chat.
- ☐ **(H1, sev 1)** Silent auto-onboarding via the membership stream means
  `/join` often answers "you're already part of this group's ledger" to someone
  who never joined — harmless, but the wording could acknowledge it ("you were
  added automatically when you joined the chat").
- ☐ **(H4, sev 1)** Menu descriptions are good and admin-gating is flagged inline
  ("(admin)") — keep this convention for any new command.

**Recommendations (feature level):**

- ◑ Rewrite the `/help` tip to describe per-command reality (**done**), or —
  better — make the bare-command contract uniform (**done**, T1) so the tip can
  be true.
- ☐ Restructure the welcome into a short "two steps to start" message (join +
  first expense), with a "Count me in" join button, and point to `/help` for the
  full reference.
- ☐ Keep the refusal-message formula exactly as is.

**Wizard verdict:** **no wizards.** One button and shorter words.

---

## Wizard / button verdict — summary

| Command | Wizard? | Status & what it still needs |
|---|---|---|
| `/addexpense` | Has one (baseline) | — |
| `/settleup` | One-screen picker | ✅ tap-to-pay shipped; typed form stays as accelerator |
| `/void` | One-screen preview + confirm | ✅ shipped (preview, browse, settlements) |
| `/event` | No (buttonize status) | ✅ action buttons shipped; ☐ toggle-roster editor |
| `/balance` | No | ✅ tap-to-settle shipped; ☐ me⇄all pivot button |
| `/statements` | No | ☐ scope label in header; ☐ void-from-statement entry point |
| `/group` | No | ☐ active-event line |
| `/simplify` | No | ☐ one explanatory clause in replies |
| `/currency` | No | — |
| `/start` `/join` `/help` | No | ✅ truthful help tip; ☐ join button; ☐ shorter welcome |

---

## Priorities

1. ✅ **Bare-`/void` write + `/help` tip** (T1). Smallest change, removed the
   only severity-4 interaction in the product. Shipped: preview + caller-bound
   confirm/keep buttons, id-pinned confirm, rewritten help tip and `/void`
   reference line.
2. ✅ **Tap-to-settle** (picker on bare `/settleup`, plus debtor-gated buttons
   under `/balance` and `/balance all`). Biggest friction reduction in the core
   loop. Shipped: one tap records the payment in full, announces it to the chat,
   and repaints the view; stale buttons alert instead of writing.
3. ◑ **Undo coverage** — settlements voidable; older entries reachable from the
   `/void` preview. Shipped: settlement voiding (schema + derivation + struck-out
   statements) and ⬅ Older / Newer ➡ stepping through the last 10 entries.
   **Open:** a void entry point directly from `/statements` pages.
4. ◑ **Buttonize `/event` status replies + toggle-roster editing.** Shipped:
   Pause/Resume/Close buttons (admin-gated at tap, chat-announced, stale-safe),
   tapped-mention removal, and the unsettled-removal warning. **Open:** the
   toggle-roster editor.
5. ☐ **Scope labeling sweep** (`/statements` header, `/group` active-event line)
   and the unrecognized-argument notes on read commands.
6. ☐ **Onboarding polish** (welcome restructure + join button) and
   plain-language wording for simplified/raw.

Priorities 1–2 are complete; 3–4 are mostly done with one follow-up each; 5–6 are
each a small batch of individually tiny changes that can ride along with adjacent
work.
