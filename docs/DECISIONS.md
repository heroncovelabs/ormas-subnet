# Design decisions this repository implements

Recorded by the subnet owner (Heron Cove LLC) on 2026-09-10; reward policy updated
2026-09-24. Everything in this repository
follows these; where any document here disagrees with this page, this page wins.

1. **A miner delivers whole outcomes, never inference.** A miner clones the client's repository
   at a base commit, produces the change with its own agent, harness, and models, publishes a
   result branch, and reports evidence. It is never an inference endpoint, model supplier, or
   token vendor. Nobody is paid for tokens, completions, or uptime.
2. **Firm bids; paid only on accepted delivery.** A miner posts a firm ask for the whole task.
   Accepted, fully funded delivery can earn a 1× bid target in gross miner alpha
   under rule 8. The USD-denominated bid is not a USD payout guarantee.
3. **Everything that makes a miner good is the miner's business.** Model routing, hardware,
   energy, harness, caching, orchestration loop — none of it is specified by the network. The
   network rewards accepted outcomes at the best price, latency, and quality.
4. **The client prepares the task inside its own perimeter.** The client's own tooling freezes
   the task packet (brief, acceptance criteria, verify command, allowed and immutable paths,
   base commit). No model is called through Ormas for that step.
5. **Bid selection is accept-on-arrival.** The first ask at or under the client's reserve is
   leased; an ask above the reserve is recorded and skipped, and the job stays queued. Every
   ask is recorded with its arrival time. A timed auction is deferred, not rejected.
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
8. **Chain weights — approved policy, not activated.** Exactly funded accepted bids
   create a 1× gross miner-alpha target at a frozen reference price. Finalized miner
   emissions reduce the outstanding target; unused miner allocation is explicitly
   burned. No incentive bonus, reserve fund or treasury top-up. Known same-controller
   work is excluded: company jobs served by `miner:ormas-operator-1` or `miner:nc-arm64` earn
   zero; genuine outside-client work served by either remains eligible, as does
   company work served by independent miners. Missing verified chain mapping earns
   nothing. See [economics.md](economics.md) for accounting and rollout limits.

## Not yet decided

Commercial terms for external miners; assigned-validator count, decision timeouts, and
spot-check fraction (to be set from measurements on the development subnet); validator
collateral; the client dispute window; the validator fee share. None of these values appear in
this repository until the owner sets them.

## Vocabulary

*miner* (not runner, supplier, or worker); *firm bid* or *ask*; *accepted delivery*; *task
preparation* for the client-side step. The wire protocol is still named `/api/runner/v1` /
`ormas-runner-v1`; those names predate this vocabulary and change with a future protocol version.
