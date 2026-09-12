# The miner contract

One page, in order of authority:

- [`docs/DECISIONS.md`](DECISIONS.md) — the owner-locked decision that defines a miner. Normative; it wins any disagreement.
- the validator acceptance design (summarized in [`docs/DECISIONS.md`](DECISIONS.md) §6) — how acceptance will work. Design signed off by the owner on 2026-09-10; implementation is in progress, so anything drawn from it below is marked **planned — not yet live**.
- [`README.md`](../README.md) and [`protocol.md`](protocol.md) — the wire protocol as implemented today. They describe what is live now.
- [`ormas_subnet/skeleton.py`](../ormas_subnet/skeleton.py) and [`ormas_subnet/reference_solver.py`](../ormas_subnet/reference_solver.py) — the code you actually run.

A name note: the wire protocol is `/api/runner/v1` / `ormas-runner-v1`. Those names ship today and predate the miner vocabulary; they change when the miner-identity work rewrites the routes. Everywhere else the software is the miner.

## What a miner is

A miner posts a **firm bid** — an ask — for a whole task and is paid only when its delivery is **accepted**. It clones the client's repository, solves with its own agent, harness, and models, verifies, and publishes a result branch. The client's own harness does **task preparation** only: it freezes the task packet and calls no model through Ormas.

A miner is never an inference endpoint, model supplier, or token vendor. Nobody pays for tokens, completions, or uptime.

## The loop

1. **Register** with a stable id, capacity, and the cells (task archetypes) you serve. The response carries the poll/lease/heartbeat cadence — it is authoritative.
2. **Bind** the repository you serve to a client project at a base commit.
3. **Poll, then claim.** Your claim may carry your firm ask (`ask_usd`); the first ask at or under the client's reserve is leased, and that ask is the price you are paid. An ask above the reserve is recorded and skipped — the job stays queued for the next miner. That is "accept on arrival". If you send no ask, the gateway derives one from the task's expected cost plus a margin. **Status:** live on `api.ormas.ai` since `gateway-2026.09.11`; this package's client and skeleton send `ask_usd` when it is configured (`MinerConfig.ask_usd`), and send no ask otherwise.
4. **Clone** — the skeleton clones the repo and checks out the base commit in a fresh workdir.
5. **Solve** — your `solve(draft, workdir) -> SolveResult`. Heartbeat during it; an unrenewed lease expires.
6. **Verify** — the skeleton runs the packet's verify command itself, in a bounded, credential-free environment with no shell, and computes `scope_ok` from a real `git diff`.
7. **Publish** — your result commit lands on `refs/heads/ormas/job/<task_id>` in the client's repository.
8. **Complete** — send the receipt, terminal, and capture: commit sha, changed paths, diff hash, verify exit code, usage.

## What you see — and what the gateway never sees

You see the client's repository at the base commit (clients opt in), the task brief, the acceptance criteria, the verify command, and the allowed and immutable paths. Stay inside the allowed paths; a delivery outside them pays nothing.

Never send the gateway source, diffs, prompts, model output, or credentials — the wire rejects those by field name (full list in `protocol.md`). Evidence is hashes, path lists, exit codes, and usage counts. The gateway never clones your work; all it knows of your model and harness is the provider/model string and usage you report.

## When you get paid

Only on **accepted delivery**. A rejected, failed, or out-of-scope delivery pays nothing.

- **Today:** acceptance derives from your own reported `verification_state == "verified"` plus a scope and commit check. That is self-grading, tolerable only because today's miner is Ormas's own trusted first party. Do not read a `paid` settlement for an external miner as proof of correctness yet.
- **Planned — not yet live:** settlement waits for validator acceptance. Assigned validators re-run the packet's verify command against your delivered branch in a digest-pinned container, independently check the diff against the allowed and immutable paths, and post signed accept/reject decisions. Payment requires **unanimity among the assigned validators**. Your self-report becomes advisory; a self-reported failure still settles unpaid at once. A validator reads the code under the same untrusted perimeter you run on — a per-job, single-repo, read-only GitHub App token, revoked when its decision posts.

## How you are scored

- **History.** Accept/reject decisions accrue to your miner identity — today a tenant-scoped token; planned, a registered identity mapped to a chain hotkey at registration. Validator decisions write that history once validators ship. **Planned:** reporting `verified` and being rejected anyway (overclaiming) counts against you in selection, alongside your honestly reported cost and latency.
- **Chain weights.** Accepted delivery is the gate — no accepted deliveries, no weight. Weight is linear in settled value, under a per-hotkey cap. A miner with no chain identity mapping earns nothing however good its work.

Two honesty rules protect your score: report `None` for usage you do not know — never a fabricated zero — and never self-declare `verified`; the skeleton has no field for it.

## What is yours

Model routing, hardware, energy, harness, caching, the orchestration loop — all of it lives inside your miner, and none of it is specified by Ormas. The network rewards accepted outcomes at the best price, latency, and quality; how you produce them is your edge. This repo ships protocol mechanics only. Your `solve` is the mining.

## Not decided yet

Owner calls, not values this page may invent: commercial terms for external miners, the assigned validator count and decision timeouts, the validator fee share, and validator collateral. The validator count, timeouts and spot-check fraction will be set from measurements on the development subnet, not chosen up front.
