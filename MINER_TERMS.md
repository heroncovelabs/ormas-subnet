# Ormas miner terms — DRAFT, pending counsel review

> **This is a draft.** It has not been reviewed by counsel and is not yet in force. It is published
> so miners can read what we intend before it binds anyone. The terms that apply to you today are the
> protocol mechanics in [`docs/CONTRACT.md`](docs/CONTRACT.md) and the operator's gateway terms at
> ormas.ai. Where this draft and a signed agreement differ, the signed agreement controls.

## 1. Who the parties are

These terms are between **Heron Cove LLC**, a Wyoming limited liability company ("Heron Cove",
"Ormas", "we"), and you, the operator of a miner connected to the Ormas gateway on Bittensor subnet
76 ("miner", "you").

Heron Cove sells verified coding Outcomes to its own customers and is responsible to them for
delivery. You are Heron Cove's **supplier**. You have no contract with, and no obligation to, any
Heron Cove customer, and no customer has any obligation to you. Which miner receives a task is Heron
Cove's routing decision, informed by your bid.

## 2. What you agree to deliver

For each task you claim you agree to attempt the whole task as specified in its work packet, within
its allowed paths, and to report truthfully: usage you do not know is reported as unknown, never as
zero; `verified` is never self-declared. A delivery outside the allowed paths, or one that fails the
packet's verification when validators re-run it, is not accepted and earns nothing.

You provision your own runtimes, tools, models, and hardware. Heron Cove supplies none of them and
does not pay for them.

## 3. How you are paid

**Your bid is a USD target, not a USD payment.** When your delivery is accepted, your bid (`ask_usd`)
becomes your emission target for that Outcome. Validators convert targets into SN76 emission weights
each epoch at the on-chain pool price and a published TAO/USD reference. **You are paid in alpha by
the chain.** The alpha you actually receive for a given target varies with those prices.

**Carry-forward.** If an epoch's emission does not cover your target, the unpaid USD balance carries
forward and is published in the validators' per-epoch artifacts.

**Treasury top-ups (not yet in force).** Balances that remain open past the published aging threshold
are netted daily and paid in alpha from Heron Cove's treasury to the payout coldkey you registered,
T+N after the published cutoff, at one price reference per batch, subject to §4. Until counsel clears
this path it is disabled; if it is not cleared, the designed fallback is a USD payout through a
payment provider on the same batch schedule, and we will say so here before it applies.

**No other payment.** Heron Cove does not pay for inference, tokens, uptime, or attempts, does not
hold alpha or dollars on your behalf, and does not convert any customer's payment for you.

## 4. Supplier record

Before any treasury top-up you must complete a supplier record: a W-9 (US) or W-8 (non-US), a payout
coldkey distinct from your hotkey, and sanctions/hotkey screening. Without it you earn emissions
normally but receive no top-up; your balance is shown as `unpaid_kyc_pending`. Payments in property
are reported as the law requires (for US suppliers, Form 1099-NEC).

## 5. Your identity and history

Accept/reject decisions accrue to your registered miner identity and, once live, to its mapped
hotkey. Reporting `verified` and being rejected by validators counts against you in selection. You
may not register more than one identity to evade history, and you may not collude with validators or
other miners on acceptance.

## 6. What Heron Cove may change

The published schedule parameters (cutoff, delay, aging threshold, price sources) and the margin of
the gateway-derived default ask may change with notice in this repository. Your bid on a task is
fixed once that task is leased to you. Changes never apply to a task already leased.

## 7. Disputes, law, and venue

Disagreements about a balance are settled first against the published artifacts: if a script from
this repository recomputes a different number from the same inputs, the artifact is corrected. These
terms are governed by the laws of the State of Wyoming, without regard to conflict-of-laws rules.
Venue: **OPEN — counsel.**

## 8. Disclosure

Heron Cove operates a miner and a validator on SN76 and the gateway that settles work. Its miner is
labelled `operator-run` on every public surface and is never paid a treasury top-up.

---

*Draft of 2026-09-13. Open items for counsel: venue; whether §3 treasury top-ups are cleared under the
seller posture; §4 foreign-supplier treatment; any required arbitration clause. Nothing in this draft
is legal advice to you; consult your own advisers on the tax treatment of alpha you receive.*
