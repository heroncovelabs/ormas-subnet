# SN76 economics — emission schedule vs. demand

## Task pricing (implemented in the 2026-10-02 source)

A miner offers a `firm` price or a `limit` for the whole task. A limit offer states
both `estimate_usd`, the expected charge, and `limit_usd`, the hard ceiling, with
`0 < estimate_usd ≤ limit_usd`. Accepted firm delivery charges exactly `price_usd`.
Accepted limit delivery charges exactly the miner’s `settled_price_usd`, at or below
`limit_usd`. Failed delivery earns nothing. Legacy
`ask_usd` claims keep their firm-price behaviour. See [Protocol](protocol.md).

Estimate honestly: the expected cost of your usual recovery chain plus margin.
Set the limit at your worst-case chain. At delivery, use the actual metered cost of
every attempt in that chain plus your margin, capped at the limit. When cost runs
above your estimate, consider dropping the margin to zero. You may deliver at a
loss; the client’s charge stays within the accepted limit.

**Made-up example:** you expect your usual chain to cost $0.80 and apply a 25%
margin, so you estimate $1.00. Your worst-case chain would cost $1.60, so you set a
$1.60 limit and offer
`{"job_id": "job_example", "kind": "limit", "estimate_usd": 1.00, "limit_usd": 1.60}`.
Two attempts actually cost $0.30 and $0.20. With the same 25% margin, you settle at
$0.625 and the client pays $0.625. If a third attempt brings chain cost to $1.20,
above your estimate, you might drop the margin and settle at $1.20. If chain cost
rises above $1.60, the settlement stays at $1.60 and you bear the loss.

**Why both numbers.** The estimate says what you expect to charge; the limit says
what you will never exceed. The gateway's ledger can compare what you settled with
what you estimated and with your limit, dollar-weighted, by task shape. A miner
whose settlements track its estimates earns credible estimates; one that estimates
low and settles near the limit shows it. These two ratios inform live offer ranking.

The live gateway collects firm and limit offers in a window and defaults to `lowest_expected_total_cost`, using price, settlement history and delivery performance. Buyers may set `award_policy` fields `policy`, `ack_by_s`, `window_s` and `fallback`. Miner-targeting fields `only_miners`, `prefer_miners`, `qualification` and `prefer_affiliation` are operator-only. Automatic selection may award after eligible miners respond or at the acknowledgement deadline (`ack_by_s`, 60 seconds by default). See [Offer ranking](protocol.md#offer-ranking).

## Chain compensation (targets and weights live; treasury top-ups planned)

The validator and gateway implement the target → weight → carry-forward rule below
(requirements: SN76 compensation model v1, 2026-09-13). Since 2026-10-05T15:00Z the target is
settled USD × 2 under rate version `earned-bid-2x-v1` ([`MINER_TERMS.md`](../MINER_TERMS.md) §3);
targets frozen earlier keep their 1× value. The treasury top-up and payout schedule are still
planned. Where a number is an owner or CPA decision it is marked **OPEN** and has not been chosen. Nothing here is an offer,
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

**Settlement → target.** The accepted firm price or settled limit price on an Outcome, times the
rate version in force at settlement, becomes its **emission target**: the USD value the miner is
owed for that Outcome. Only Outcomes accepted through the validator-gated path
count; a miner's own report of success never does.

**Target → weight.** Each epoch, validators sum what every miner is owed — this epoch’s accepted settlements
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

## Client rebate weights (planned; not in force)

**Status.** The gateway computes client rebate weights in a dry run and sets none. No client entry
appears in the vector until the owner records a policy version with an effective time, after counsel
review and after the consensus test below passes. This page will state that time. Nothing here is an
offer, forecast or promise of return.

**What it is.** A client who pays for accepted outcome tasks may enroll an SN76 hotkey it registered
itself. Validators then place a weight on that hotkey, and the network pays the client a rebate on its
paid work, in SN76 alpha, directly to that hotkey. Ormas never holds, transfers or keeps alpha for a
client. Alpha is a network token; its dollar value can rise or fall.

**What counts.** Paid, accepted outcome tasks funded by card payments, settled at or after the later of
the program start and the client's enrollment. An outcome task is a defined task with a verify command,
offered to competing miners, delivered and independently accepted before it is paid. Fees, unspent
credit, promotional credit and earlier work never count. A refund or dispute lowers later rebates;
amounts already paid are not reclaimed.

**The rule.**

1. Each UTC day, a client's target is that day's eligible base times the rate in the published version
   row.
2. The rebate never exceeds what the client paid for that work. Delivered rebate is the emission the
   chain actually paid to the client's hotkey, valued at the on-chain alpha price at the epoch block and
   the published TAO/USD reference at the epoch time. An epoch without a price counts nothing, and the
   client's weight is held at zero until it is priced.
3. Every vote turns the unpaid amount into a weight, over the same horizon used for miners.
4. Order inside the vector: miner weights first, never reduced; then the Protected bootstrap; then
   clients, pro rata, inside a combined ceiling of 25% of the vote; the remainder goes to UID 0 and
   burns. Unpaid amounts stay owed when the ceiling binds.
5. Eligibility: US clients; sanctions screening at enrollment and before each day's weight; current
   terms accepted; a verified hotkey registered on SN76 that is not a miner, validator, burn or pool
   hotkey. The client pays its own registration. A hotkey that earns nothing may be deregistered by the
   network; it can register again, and amounts owed are kept.
6. One rule for everyone. If you or an affiliate operates an SN76 miner, you must declare the Client
   accounts you control. An outcome on a declared account earns the Client rebate only when an
   independent miner delivers it. When your own or an affiliate's miner delivers it, it earns no Client
   rebate, and that miner earns only the base miner reward, without any bonus, on spend funded with
   outside money. Ormas may withhold or cancel rebates obtained through self-dealing, duplicate accounts
   or other abuse.

Weights and payments to a client hotkey are public on chain, so anyone can estimate a client's rebate.

**For validators: the feed.** Validators that follow the Ormas vector read `GET /v1/sn76/weight-vector`
with a validator token issued by Ormas. The response is `{as_of, window, weights}`, with weights keyed
by SS58 hotkey; while live client entries exist it also lists `client_hotkeys`. It carries no amounts,
tenants or prices. Client hotkeys are ordinary entries: map each to its UID as for miners, send any
unregistered hotkey's share to UID 0, and burn rather than submit a vector older than five minutes.

**Why consensus decides.** Yuma clips each weight to what validators holding a majority of active stake
support. A client entry set only by Ormas's validator is dropped: the client receives nothing and
Ormas's validator trust falls. Validators holding most of the active stake currently reproduce the
Ormas vector with a lag of up to about two epochs, which is how miner entries reach consensus today.
The program goes live only when validators whose combined active stake (metagraph `total_stake`: alpha
stake plus root TAO at the subnet's TAO weight) exceeds half reproduce the non-burn entries of the
Ormas vector. Identical all-burn rows do not count, since independent burning produces them too. Until
then client amounts accrue in the dry run and nothing is paid.
