# SN76 rewards for accepted work

**Policy approved September 24, 2026; implementation candidate, not activated.**
SN76 already emits alpha. Existing chain weights do not yet prove that this policy
is in effect. Activation requires a matched gateway and validator release,
finalized applied-weight evidence, and an observed epoch. Independent validators
must adopt the policy too; one operator cannot impose a subnet-wide payout cap.

## Client price and miner reward

The client pays the accepted task price in USD when the delivery passes the
agreed acceptance checks. Customer USD stays with Heron Cove. It is not passed
through to the miner, converted for that miner, or held in a miner reserve.

The miner's reward target is **1× the eligible accepted bid**, converted once to
gross miner alpha at the reference price. Put your costs, margin and token-price
risk into your bid. There is no extra incentive multiplier, escrow reserve,
treasury top-up, USD miner payout, or guaranteed dollar realization.

Gross miner alpha means the protocol's miner allocation before distribution to
the miner's owners or stakers. It is not necessarily liquid alpha received by
the operator's coldkey. Validator dividends are separate compensation.

## Which deliveries count

A qualifying delivery must have its exact accepted bid, job, miner identity and
required validator acceptances bound together. A `paid` label or the miner's own
success report is insufficient. The full accepted bid must be settled and funded
at the ledger's nine-decimal USD precision. Partial or decayed underpayments do
not qualify under this first policy.

Outside clients require confirmed full prepaid debit evidence. Prepaid balances
may include administrator-funded credit; this is not proof of a particular card
purchase or new external cash. Uncharged promotional usage does not qualify.
Company-funded work may qualify when an independent miner delivers it.

Known same-controller demand and delivery do not earn miner emissions. In
particular, company jobs served by `miner:ormas-operator-1` or `miner:nc-arm64` earn
zero. They were UID 102 and UID 120 respectively on September 24; eligibility
follows the stable miner identities, not a permanent assumption about UID numbers.
Genuine outside-client jobs served by either remain eligible. The rule also
applies to known relationships involving other miners. It is not a claim that
every hidden relationship or sham task can be detected.

Open disputes are excluded. Refunds reduce available prepaid balance, but this
candidate does not trace a later refund back through every previously completed
task or reverse alpha already emitted.

## Conversion, accounting and unused allocation

The candidate freezes each eligible receipt's target in integer alpha units
using a finalized SN76 pool ratio (TAO per alpha) and a Coinbase Exchange TAO/USD
quote. A spending vector requires both observations to be no more than five
minutes old. A frozen target is not repriced whenever alpha moves.

The private gateway projection retains targets and subtracts finalized gross
miner emissions since policy activation, including emissions caused by other
validators' votes. Restarting, rereading a window or aging a receipt out of the
diagnostic window does not create another target. There is no retroactive award
for receipts predating activation.

Remaining targets determine a conservative requested share of the projected
miner pool over the commit/reveal horizon. If demand exceeds that capacity,
targets remain outstanding in alpha units. This is accounting for future votes,
not a guaranteed payment date or a USD debt promised by Heron Cove.

**Unused miner allocation goes to a chain-verified owner burn sink.** The runtime
must be qualified and configured to burn. Empty demand, unmapped shares and
rounding dust are not redistributed to active miners. Owner and validator
allocations are outside this rule.

When current accounting or prices are unavailable, validators attempt an explicit
all-burn replacement after independently verifying the sink. Stopping a service
or submitting a commit does not clear an old paid vector immediately. Rate limits,
commit/reveal delay, chain failures and other validators can still cause excess
or delayed awards. Later observations net those awards against targets; the
software cannot claw emitted tokens back.

## Validator responsibilities and limits

Task checkers independently rerun the client's verifier and enforce scope in the
declared environment. The chain component reads the gateway's restricted weight
vector, verifies the burn destination against finalized chain state, submits its
vote and records the later applied-state readback. A submission acknowledgment
is not proof that the vote has been revealed or applied.

The operator's separate observation credential can publish public chain and
reference-price evidence. It cannot read the task ledger. Other validators receive
only `as_of`, `window` and `weights`; per-task amounts, client identities and model
choices remain private. Public weights and chain facts can still reveal aggregate
allocation. This is an operator-accounted projection, not an independent public
reconstruction of the private ledger.

The initial runtime qualification is Finney/SN76 spec 469. Unknown runtimes pause
fresh allocations. Miner hotkeys must be separate from validator hotkeys: a
nonimmune epoch row with both incentive and dividends cannot be split reliably
from the available total-emission field, so observation stops pending reviewed
accounting qualification. Changing a miner's registered hotkey likewise pauses
its old immutable targets pending an explicit reviewed migration.

The first rollout must prove these controls on the installed services and inspect
actual finalized votes and epoch emissions. This document records the approved
design; it does not certify that rollout has happened.
