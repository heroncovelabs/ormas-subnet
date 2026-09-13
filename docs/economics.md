# SN76 economics — emission schedule vs. demand

**Status: planned — not yet live.** This page explains the compensation model the validator and
gateway will implement (requirements: SN76 compensation model v1, 2026-09-13). Where a number is an
owner or CPA decision it is marked **OPEN** and has not been chosen. Nothing here is an offer,
forecast, or promise of return. Authority order: [`DECISIONS.md`](DECISIONS.md) → [`CONTRACT.md`](CONTRACT.md) → this page.

## Two quantities that do not match

1. **Emission is fixed in alpha.** The chain emits a fixed amount of SN76 alpha per block; the miner
   share of each epoch is a fixed number of alpha, independent of how much work was done.
2. **Demand is variable in USD.** Customers buy Outcomes in dollars. In an epoch the accepted
   Outcomes sum to some USD figure — zero on a quiet day, large on a busy one.

The USD capacity of an epoch's emission is `alpha_emitted × (TAO per alpha at the pool) × (USD per
TAO)`. It moves with both prices. The model below exists to reconcile a fixed alpha supply with a
variable dollar demand without anyone converting a customer's dollars for a miner.

## The rule, step by step

**Bid → target.** A miner's accepted `ask_usd` on an Outcome becomes its **emission target**: the USD
value the miner is owed for that Outcome. Only Outcomes accepted through the validator-gated path
count; a miner's own report of success never does.

**Target → weight.** Each epoch, validators sum what every miner is owed — this epoch's accepted bids
plus any balance carried from earlier epochs — and set weights **proportional to owed USD**. The
conversion uses two prices, both recorded in the epoch artifact with their source ids: the SN76 pool
price (TAO per alpha) read from chain at the epoch's reference block, and a **published TAO/USD
reference** with a documented fallback order and a staleness cap (**OPEN:** which sources, the cap).
If the price cannot be established within the cap, the epoch is marked degraded and weights are
held, not guessed.

**Excess → owner UID.** When total owed USD is less than the epoch's emission can pay, the remainder
of the miner weight goes to the subnet owner UID, where it burns or recycles per the subnet's
`recycle_or_burn` setting. Unearned emission is not distributed to miners.

**Shortfall → carry-forward.** When total owed USD exceeds what the epoch's emission can pay, every
miner is paid pro rata in alpha and the unpaid remainder **carries forward**:

```
owed_open(miner, epoch) = owed_open(miner, epoch−1)
                        + accepted_usd(miner, epoch)
                        − usd_value(emission_received(miner, epoch))
```

where `emission_received` is read from chain after the epoch settles and valued at the same epoch
reference prices. The ledger is a pure function of chain weights, chain emissions, the published
prices, and the accepted set; two validators running the same code on the same inputs produce the
same ledger byte for byte.

**Persistent balance → treasury top-up.** A balance that stays open past an aging threshold
(**OPEN**) is netted daily at a fixed UTC cutoff (**OPEN**) and paid **in alpha from Heron Cove's
treasury** to the miner's registered payout coldkey, T+N after cutoff (**OPEN:** N and the dispute
window), at one price reference per batch. The batch file is computed from the validators' published
artifacts, not from Ormas's own books, and is itself reproducible. A miner without a completed
supplier record (W-9/W-8, screening) earns emissions normally but receives no top-up; its balance
shows as `unpaid_kyc_pending` until the record is complete. Top-ups are counsel-gated and ship behind a
default-off flag; a USD payout via a payment provider is the designed fallback if they are not cleared.

**Buybacks.** Heron Cove may buy alpha with **earned** revenue on its own account, which raises the
USD capacity of future epochs. That is a manual treasury operation, never wired to customer payments,
and never funded from unearned credits.

## Why each piece exists

| Piece | Without it |
|---|---|
| USD targets | a miner cannot price a task; emission value swings with the market, not with the work |
| Validator conversion | the operator would set weights from its own books — no independent acceptance |
| Owner-UID remainder | quiet epochs would overpay whoever happened to be active |
| Carry-forward | busy epochs would silently underpay; the shortfall would vanish |
| Treasury top-up | a carried balance could grow without bound and never clear |
| Public artifacts | none of the above could be checked by anyone but us |
| KYC gate | a payment in property to an unidentified supplier |

## What the model does not do

- Pay miners in USD by default, or convert a specific customer's dollars for a specific miner.
- Hold alpha or USD on a miner's or customer's behalf.
- Adjust any weight outside the acceptance path — there are no quality or reliability multipliers;
  an Outcome is accepted or it is not.
- Top up the operator's own miner. Its balance is published like any other and never paid from
  treasury.
- Accept delegation to the company validator.

## Verify it yourself

Every epoch artifact (epoch id, reference block, prices and source ids, per-miner accepted USD,
emission received, open balance, weight set, input hash) is signed by the validator hotkey and
retrievable without authentication. A script in this repository (**planned**, requirement M4)
recomputes any miner's published balance from the artifact and chain data. If the numbers do not
match, the artifact is wrong and you should say so publicly.

## Open parameters

Owner or CPA decisions, recorded here only so nobody invents them: UTC cutoff; T+N payout delay;
dispute window; carry-forward aging before a top-up is due; TAO/USD reference sources, fallback order
and staleness cap; buyback share of earned revenue; foreign-miner policy; KYC tier contents; whether
the existing 40% per-hotkey weight cap stays alongside proportional weighting and, if so, how its
shortfall is treated.
