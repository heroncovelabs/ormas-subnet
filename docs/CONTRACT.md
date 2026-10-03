# The miner contract

One page, in order of authority:

- [`docs/DECISIONS.md`](DECISIONS.md) — the owner-locked decision that defines a miner. Normative; it wins any disagreement.
- the validator acceptance design (summarized in [`docs/DECISIONS.md`](DECISIONS.md) §6) — how acceptance will work. Design signed off by the owner on 2026-09-10; implementation is in progress, so anything drawn from it below is marked **planned — not yet live**.
- [`README.md`](../README.md) and [`protocol.md`](protocol.md) — the wire protocol as implemented today. They describe the implemented source; deployment follows its release.
- [`ormas_subnet/skeleton.py`](../ormas_subnet/skeleton.py) and [`ormas_subnet/reference_solver.py`](../ormas_subnet/reference_solver.py) — the code you actually run.

A name note: the wire protocol is `/api/runner/v1` / `ormas-runner-v1`. Those names ship today and predate the miner vocabulary; they change when the miner-identity work rewrites the routes. Everywhere else the software is the miner.

## What a miner is

A miner offers a **firm price** or a **limit** for a whole task and is paid only when its delivery is **accepted**. Firm delivery pays exactly the accepted price. Limit delivery pays exactly the miner's settled price, at or below the accepted limit. It clones the client's repository, solves with its own agent, harness, and models, verifies, and publishes a result branch. The client's own harness does **task preparation** only: it freezes the task packet and calls no model through Ormas.

A miner is never an inference endpoint, model supplier, or token vendor. Nobody pays for tokens, completions, or uptime.

## The loop

1. **Register** — with capacity and the cells (task archetypes) you serve; the gateway assigns your stable id (`runr_<12hex>`) on the first call — keep it, and pass it on every later call (an id the gateway never issued to your token is refused 404). The response carries the poll/lease/heartbeat cadence — it is authoritative.
2. **Bind — optional, legacy.** Bounded-packet (public-profile) jobs use gateway publication. Public-repository jobs clone anonymously; private-repository jobs use a per-job read credential. The legacy unbound route carries a repository credential; `bind` serves a pre-arranged repository. See `INSTALL.md`.
3. **Queue, offer, then claim.** The queue exposes a privacy-safe task envelope. Offer `{job_id, kind: "firm", price_usd}` or `{job_id, kind: "limit", estimate_usd, limit_usd}` (`0 < estimate_usd ≤ limit_usd`), or decline by omitting the job. Phase 1 accepts on arrival: the first offer within the client's undisclosed spending limit wins; an offer above it is recorded and skipped and the job stays queued. Legacy `ask_usd`/`asks` claims keep firm-price behaviour. [History-based ranking](protocol.md#offer-ranking) is planned.
4. **Clone** — the skeleton clones the repo and checks out the base commit in a fresh workdir.
5. **Solve** — your `solve(draft, workdir) -> SolveResult`. Heartbeat throughout — not only here; see "Keeping your lease alive" below.
6. **Verify** — both routes run without a shell in a credential-free environment: `PATH`, scratch `HOME`, `LANG`, plus the packet's explicit `NAME=value` assignments. The public profile runs in a digest-pinned OCI image with `--network none`, `--cap-drop ALL`, `--read-only` and resource limits. It computes `scope_ok` from a real `git diff`.
7. **Publish** — the bounded-packet (public-profile) path uploads a bounded committed-file artifact for the gateway to publish; the legacy path pushes the branch. Both use `refs/heads/ormas/job/<task_id>`.
8. **Complete** — send the receipt, terminal, and capture: commit sha, changed paths, diff hash, verify exit code, usage. A verified limit delivery also supplies `settled_price_usd`; firm/legacy terminals omit it.

## Keeping your lease alive

The registration response is authoritative for your cadence: `heartbeat_s` (how
often to renew), `lease_ttl_s` (how long a lease survives without a renewal),
and `poll_interval_s` (how often to poll for work). Honor those over any local
default — a gateway cadence change should never require a new miner release.

**Renew through the whole lease, not just through solve.** The lease must keep
being renewed across solve, publish, verify, *and* complete — the skeleton
does this for you, but if you build your own runner, do not stop heartbeating
the moment `solve` returns. On 2026-09-16 a miner's job produced a correct,
already-pushed result, then hit a transient failure on the completion POST —
and had renewed its lease **zero times**, because its heartbeat had already
been shut down for the publish/verify/complete phase. The lease expired before
the retry could land. Good work, unpaid, for a reason that had nothing to do
with the work.

**A transient completion refusal is not a rejection.** An HTTP 5xx, or a
connection/transport failure, means try again with backoff, for as long as
the lease is still live — never past `lease_ttl_s` since the claim. There is
no fixed attempt count to hit; the lease's remaining life is the only honest
bound. An HTTP 4xx (other than 409) is different: the gateway has decided,
and retrying burns lease time for nothing.

**A 409 `lease_lost` means the work is no longer yours.** Someone or something
else holds the lease now. Stop. Do not retry the heartbeat, do not attempt a
completion — there is nothing left to complete against. If your solve is
long-running, poll the skeleton's cancellation signal (`MinerSkeleton.cancelled`)
cooperatively and stop your own work early; the skeleton can ask, but it
cannot reach into your solver and interrupt it for you.

**Completion errors carry the gateway's real status and body** —
`OrmasGatewayError.status_code` / `.error_type` / `.message` — not a bare
exception. A wrapper that swallows those into something like
`completion_request_failed` throws away exactly the information you need to
tell a transient blip from a terminal refusal, which is what turned a fixable
retry into a lost job on 2026-09-16.

## What you see and send

Before offering, you see the privacy-safe queue envelope listed in `protocol.md`.
After claim, you see the repository at the base commit, task brief, acceptance
criteria, verify command, allowed and immutable paths, and frozen acceptance
policy. Stay inside the allowed paths; an out-of-scope delivery pays nothing.

Completion carries hashes, path lists, exit codes and usage counts. It rejects
raw source, diffs, prompts, output and credentials by field name. Public-profile artifact
publication separately sends committed source file bytes for the gateway to
publish. Legacy claims may carry a repository credential; keep it private.

## When you get paid

Only on **accepted delivery**. A rejected, failed, or out-of-scope delivery pays nothing.

- **Bounded-packet (public-profile) jobs:** settlement waits for unanimous assigned-validator acceptance under the frozen policy. V1 separates miner and validator operators; the explicit v2 alpha uses an operator-run checker. Both apply to the operator's miner too. Miners and validators clone public-repository jobs anonymously; private-repository jobs use a per-job read credential, with the Standard/Protected service level stated separately. Validators run the qualified, frozen execution environment.
- **Legacy jobs:** cross-tenant delivery requires assigned-validator acceptance; same-tenant delivery uses the miner's report plus scope and commit checks. A legacy Python `toolchain` declares the validator's venv; repository access may use a separate read key.
- **Release work:** independent validator admission and the combined public validator service. See [Public tasks](PUBLIC_TASKS.md).

Estimate a limit offer from the expected cost of your usual recovery chain plus
margin, and set the limit at your worst-case chain. Settle from actual metered
chain cost plus margin, within the limit. You bear any loss above it. `settle_fn` supplies that price; the skeleton caps it at the limit and defaults
to the limit when unset. The hook must be deterministic and must not raise. A raising hook never reprices the delivery. On the public (bounded-packet) path the published work is held for recovery, no completion is sent, and every later poll re-raises until the hook returns a valid price; only a task without a public packet completes as `failed`. See [Economics](economics.md) for a made-up example.

## How you are scored

- **History.** Validator decisions record accept/reject history, including overclaims. Register a verified hotkey mapping after onboarding (see `INSTALL.md`). [History-based ranking](protocol.md#offer-ranking) is planned; phase 1 follows the spending-limit rule above.
- **Chain weights — planned.** Accepted delivery is the gate — no accepted deliveries, no weight. Weight is linear in settled value, under a per-hotkey cap. A miner with no chain identity mapping earns nothing however good its work.

Two honesty rules protect your score: report `None` for usage you do not know — never a fabricated zero — and never self-declare `verified`; the skeleton has no field for it.

## What is yours

Model routing, hardware, energy, harness, caching, the orchestration loop — all of it lives inside your miner, and none of it is specified by Ormas. The network rewards accepted outcomes at the best price, latency, and quality; how you produce them is your edge. This repo ships protocol mechanics only. Your `solve` is the mining.

**Execution environments.** Public-profile jobs specify a frozen catalog environment
and require current qualification for it. You provision your host to run that
profile. Legacy jobs may declare a Python toolchain or depend on tools you
provision. Serve only the verifier classes you can actually run — the gateway keeps
your history per verifier class, so your Python record says nothing about your
Node record. A task you claim but cannot verify fails as a `setup_failure` that
counts against you. Today Python and Node carry real volume; Rust and Go are
recognised but have no history yet. Public-profile validator runtime setup and
timeout failures are neutral, as described in [protocol.md](protocol.md#public-execution-and-acceptance-additions-september-23-development-candidate).
With per-job offers, return `None` from `offer_fn` or omit the job to decline before
claiming.

**Terminology.** Firm/limit are offer kinds. Standard/Protected are Ormas service
levels. Public/private are GitHub visibility; a private repository's service
level is specified separately.

## Other gateways

Anyone may operate their own gateway against SN76 miners using this protocol. Ormas neither blocks nor supports that: there is no compatibility promise beyond the published protocol version, no support channel, and no shared settlement or reputation. A miner that connects to a third-party gateway is bound by that gateway's terms, not this contract. Ormas's own acceptance and settlement are what the reference validator and the subnet's weights are built around.

## Not decided yet

Owner calls, not values this page may invent: commercial terms for external miners, the assigned validator count and decision timeouts, the validator fee share, and validator collateral. The validator count, timeouts and spot-check fraction will be set from measurements on the development subnet, not chosen up front.
