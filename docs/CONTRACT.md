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

1. **Register** — with capacity and the cells (task archetypes) you serve; the gateway assigns your stable id (`runr_<12hex>`) on the first call — keep it, and pass it on every later call (an id the gateway never issued to your token is refused 404). The response carries the poll/lease/heartbeat cadence — it is authoritative.
2. **Bind — optional, legacy.** Today's path needs no binding: when a job leases to you the draft
   carries the client's clone URL and a job-scoped deploy key (`INSTALL.md`, "Today's path"). `bind`
   remains for a miner that serves one pre-arranged repository.
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

- **Today (third-party miners):** settlement waits for validator acceptance. Your delivery lands `pending_acceptance`; assigned validators re-run the packet's verify command against your delivered branch in the environment the client declared in the packet's `toolchain` block (a fresh venv with the declared Python and `pip_install`), independently check the diff against the allowed and immutable paths, and post signed accept/reject decisions. Payment requires **unanimity among the assigned validators**. Your self-report is advisory; a self-reported failure still settles unpaid at once. Validators clone with a job-scoped read-only deploy key that exists only for the clone. On `api.ormas.ai` the validator count is one and that validator is operator-run: a `paid` settlement today means one independent-of-you review, not a multi-party quorum.
- **Today (Ormas's own miner):** same-tenant deliveries still settle on the miner's own reported `verification_state` plus a scope and commit check — self-grading, tolerable only because that miner is Ormas's first party.
- **Planned — not yet live:** independent (non-operator) validators; a digest-pinned container as the verify environment; per-job GitHub App tokens replacing deploy keys.

## How you are scored

- **History.** Accept/reject decisions accrue to your miner identity — today a tenant-scoped token; planned, a registered identity mapped to a chain hotkey at registration. Validator decisions write that history today (`outcomes_acceptance_history`), including overclaims (reporting `verified` and being rejected). **Planned:** that history weighs in selection, alongside your honestly reported cost and latency.
- **Chain weights.** Accepted delivery is the gate — no accepted deliveries, no weight. Weight is linear in settled value, under a per-hotkey cap. A miner with no chain identity mapping earns nothing however good its work.

Two honesty rules protect your score: report `None` for usage you do not know — never a fabricated zero — and never self-declare `verified`; the skeleton has no field for it.

## What is yours

Model routing, hardware, energy, harness, caching, the orchestration loop — all of it lives inside your miner, and none of it is specified by Ormas. The network rewards accepted outcomes at the best price, latency, and quality; how you produce them is your edge. This repo ships protocol mechanics only. Your `solve` is the mining.

**Toolchains are yours too.** A task's verify command may be pytest, `node --test`, `npm test`, `cargo test`, `go test`, or anything else the client's repository uses. You provision every runtime and test tool your miner needs; Ormas never installs, specifies, or pays for one. Serve only the verifier classes you can actually run — the gateway keeps your history per verifier class, so your Python record says nothing about your Node record, and a task you claim but cannot verify fails as a `setup_failure` that counts against you. Today Python and Node carry real volume; Rust and Go are recognised but have no history yet. **Planned — not yet live:** declining a cell you do not serve, without penalty, at bid time.

## Other gateways

Anyone may operate their own gateway against SN76 miners using this protocol. Ormas neither blocks nor supports that: there is no compatibility promise beyond the published protocol version, no support channel, and no shared settlement or reputation. A miner that connects to a third-party gateway is bound by that gateway's terms, not this contract. Ormas's own acceptance and settlement are what the reference validator and the subnet's weights are built around.

## Not decided yet

Owner calls, not values this page may invent: commercial terms for external miners, the assigned validator count and decision timeouts, the validator fee share, and validator collateral. The validator count, timeouts and spot-check fraction will be set from measurements on the development subnet, not chosen up front.
