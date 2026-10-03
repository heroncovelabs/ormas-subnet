# Design decisions this repository implements

Recorded by the subnet owner (Heron Cove LLC) on 2026-09-10. Everything in this repository
follows these; where any document here disagrees with this page, this page wins.

1. **A miner delivers whole outcomes, never inference.** A miner clones the client's repository
   at a base commit, produces the change with its own agent, harness, and models, publishes a
   result branch, and reports evidence. It is never an inference endpoint, model supplier, or
   token vendor. Nobody is paid for tokens, completions, or uptime.
2. **Firm or limit offers; paid only on accepted delivery (amended 2026-10-02).** A firm offer is charged at
   its `price_usd`. A limit offer is charged at the miner's `settled_price_usd`, at or below
   its `limit_usd`. Failed deliveries are unpaid. Legacy asks remain firm offers.
3. **Everything that makes a miner good is the miner's business.** Model routing, hardware,
   energy, harness, caching, orchestration loop — none of it is specified by the network. The
   network rewards accepted outcomes at the best price, latency, and quality.
4. **The client prepares the task inside its own perimeter.** The client's own tooling freezes
   the task packet (brief, acceptance criteria, verify command, allowed and immutable paths,
   base commit). No model is called through Ormas for that step.
5. **Phase 1 selection is accept-on-arrival (amended 2026-10-02).** The first offer
   within the client's undisclosed spending limit wins; an offer above it is recorded and
   skipped and the job stays queued. [History-based ranking](protocol.md#offer-ranking)
   is planned. Miners can decline a queued job by omitting it from their offers.
6. **Acceptance is the validator's role, and it feeds reputation.** A validator re-runs the
   client's verify command against the delivered branch in a digest-pinned container it
   controls, checks the diff against the allowed and immutable paths, and posts a signed
   accept or reject. Settlement follows that decision. The miner's own verification report is
   advisory. Accept/reject history accrues to the miner's identity and informs selection and
   chain weights.
7. **Miners and validators sit on the same untrusted perimeter.** Both read the client's
   repository under a per-job, single-repository, short-lived credential held outside the
   agent process, in an ephemeral sandbox with default-deny egress and no data retention. The
   client opts a project in to third-party miners and validators. Ormas never receives source,
   diffs, prompts, or credentials.
8. **Chain weights.** Accepted delivery is a gate; weight is linear in settled value under a
   per-hotkey cap; a miner with no chain identity mapping earns nothing.

## Implementation status (2026-10-02)

Bounded-packet (public-profile) jobs send a committed-file artifact to the gateway
for publication. Public-repository jobs clone anonymously; private-repository jobs
use a per-job read credential for both miners and validators, with the Standard/Protected
service level stated separately. Same-tenant legacy jobs settle on the miner's own
report plus scope and commit checks. The network does not enforce the miner's sandbox,
egress or retention. These are implementation gaps against §§6–7. Chain weights
(§8) are planned and not yet implemented.

## Not yet decided

Commercial terms for external miners; assigned-validator count, decision timeouts, and
spot-check fraction (to be set from measurements on the development subnet); validator
collateral; the client dispute window; the validator fee share. None of these values appear in
this repository until the owner sets them.

## Vocabulary

*miner* (not runner, supplier, or worker); *firm bid* or *ask*; *accepted delivery*; *task
preparation* for the client-side step. The wire protocol is still named `/api/runner/v1` /
`ormas-runner-v1`; those names predate this vocabulary and change with a future protocol version.

## 2026-10-02 — task envelopes and limit settlement

Miners see a privacy-safe task envelope before offering. A limit offer carries both
`estimate_usd`, the expected charge (the expected cost of the usual recovery chain
plus margin), and `limit_usd`, the hard ceiling (the worst-case chain), with
`0 < estimate_usd ≤ limit_usd`; the gateway refuses a limit offer without an
estimate. The gateway will keep dollar-weighted settled-to-estimate and
settled-to-limit ratios per miner and task shape as a planned ranking input. Settle at actual metered
chain cost plus margin, capped at the accepted limit; the miner bears any excess.
The client pays exactly the firm price or settled limit price. Limit deliveries
require `settled_price_usd`; firm and legacy terminals omit it. Limit leases and receipts expose
`offer_kind`, `estimate_usd`, `limit_usd` (and, on receipts, `settled_usd`). The skeleton's optional `offer_fn` and
`settle_fn` support this flow; an unset settlement callback uses the limit.
Legacy asks keep working. See [protocol.md](protocol.md) for fields and refusals.

Vocabulary from this date: *firm* and *limit* name offer kinds; *ask* names the legacy
firm-price path. *Standard/Protected* names the Ormas service level; *public/private
repository* names GitHub visibility.
