# FAQ — from the first external miner's day one (2026-09-15)

Short answers to the questions the first third-party run raised. Longer treatment in
`INSTALL.md`, `CONTRACT.md`, `protocol.md`.

**My first `--once` run "failed". Is that expected?**
With `--solve-command 'true'` yes: the reference solver changes nothing, the client's tests fail, the
skeleton reports that honestly and the job settles `no_delivery` for $0. That legacy run proves the
register, claim, clone with the job key, verify, push and complete loop from your machine. The next run
needs a real solve.

**Where is my `runner_id`? The library example printed nothing.**
The CLI prints `runner_id=<assigned>` to stderr on every start. The library's `register()` adopts the id
silently — the INSTALL example now prints it after `register()` and reads it back from `ORMAS_RUNNER_ID`.
Pass the assigned id on every later start; a self-chosen id is refused 404.

**How do I choose my public miner identity?**
Set `MinerConfig(miner_id="your-miner-name")`. Lowercase, 3–40 characters, `[a-z0-9][a-z0-9-]*`. It must be globally unique — the gateway returns 409 if another runner already holds it; re-registering with your own id is fine. When set, receipts (`worker_id`), the providers row, and the hotkey challenge bind to `miner:<miner_id>` instead of `miner:<tenant>`. Omit it and registration is byte-identical to today.

**Do I have to bind a repository?**
Binding is optional. Bounded-packet (public-profile) jobs use gateway publication.
Public-repository jobs clone anonymously; private-repository jobs use a per-job
read credential for both miners and validators. The legacy route supplies a clone
URL and a job-scoped deploy key. `--repo-id` / `--repo-url` remain
required fallback fields; `bind` serves a pre-arranged repository.

**How do offers and payment work?**
A `firm` offer fixes the client’s charge at `price_usd`. A `limit` offer states your expected charge as
`estimate_usd` and a ceiling as `limit_usd` (`0 < estimate_usd ≤ limit_usd`, both required); you send `settled_price_usd` on verified delivery and the client pays exactly that amount. Phase 1
accepts on arrival: the first offer within the client's undisclosed spending limit wins;
an offer above it is recorded and skipped and the job stays queued.
[History-based ranking](protocol.md#offer-ranking) is planned. Legacy `ask_usd` claims remain firm.

Estimate the expected cost of your usual recovery chain plus margin, and set the limit at your
worst-case chain. Settle at your actual metered chain cost plus margin within the limit. You bear any excess; see [Economics](economics.md).

`ask_usd` is live since `gateway-2026.09.11`; the queue route, offers and limit
settlement are live since `gateway-2026.10.03`. Against an older gateway the queue
returns 404 without `error.type`, and the skeleton falls back to `ask_usd`.

**What do I have to disclose about effort?**
The optional completion `effort` block is voluntary disclosure of five counts: `attempts`,
`model_turns`, `models_used`, `output_tokens`, and `total_tokens`. Supply all five as nonnegative
integers, with counts only and no model names or provider/vendor identity. Omission appears as
`effort: null` (“not disclosed”) on the receipt and job status. An invalid block returns HTTP 400
before any state change; correct it and resend. See [Protocol](protocol.md#post-apirunnerv1leasestask_idcomplete).

**What can I see before offering?**
`list_queue(runner_id)` returns job ids, creation times and privacy-safe task shapes. The exact
envelope keys are in [Protocol](protocol.md). `offer_fn` returns an offer for a job or `None` to
decline it. `settle_fn` prices a verified limit delivery; the skeleton caps it at the limit and
defaults to the limit when the callback is unset. The hook must be deterministic
and must not raise. A raising hook never reprices the delivery: on the public (bounded-packet)
path the published work is held for recovery, no completion is sent, and every later poll
re-raises until the hook returns a valid price; only a task without a public packet completes as
`failed`. Both unset preserves the legacy claim flow.

**Does a private repository mean Protected service?**
Repository visibility is `public` or `private`; the service level is Standard or Protected. Each
is a separate envelope field. Offer kinds are `firm` and `limit`.

**Who decides whether I'm paid?**
Bounded-packet (public-profile) jobs and cross-tenant legacy jobs require
assigned-validator acceptance of the result, scope and tests. `independent-v1`
uses separate owners; the explicit
`operator-run-v2`/`operator-run-v3` alpha uses operator-controlled acceptance under the frozen
policy (`api.ormas.ai` runs v3; advertise `task:acceptance/operator-run-v3`). Same-tenant
legacy jobs use the miner's report plus scope and commit checks. Legacy environments may be declared
in `toolchain`. A self-reported failure settles unpaid. Live validator qualification requires
separate onboarding. A `paid` receipt on `api.ormas.ai` today reflects one
operator-run validator.

**What may my solve change?**
Only paths in the packet's `allowed_paths`; immutable paths must not change. The reference solver stages
only `allowed_paths` (since 2026-09-15 — the first external run had committed a `.rustup/settings.toml`
written into the workdir by a local toolchain hook). Your own solver should do the same.

**What environment do the tests run in?**
Bounded-packet (public-profile) jobs use the qualified execution profile named
in the draft. Provision it as described in [Public tasks](PUBLIC_TASKS.md).
Legacy packets may declare Python and `pip_install` in a `toolchain` block; your
solve provisions those dependencies. Both verifier routes run with no shell and
a credential-free environment (`PATH`, scratch `HOME`, `LANG`, plus the packet's
explicit `NAME=value` assignments).

**How many claims can I make?**
Your runner key carries a daily claim cap: 0 when you mint it at ormas.ai (register-only), 1 for the supervised first job once your qualification is approved, then raised. Claims over the cap answer `429`. Lease TTL and heartbeat cadence
come from the register response and are authoritative.

**Is there a chain emission?**
Yes. The client is charged the firm price or the settled limit price in USD; for you, each accepted
delivery freezes an emission target of that settled USD times the published rate (2× since
2026-10-05T15:00Z, `earned-bid-2x-v1`), and validators convert targets into SN76 weights each epoch
with carry-forward of any shortfall. Treasury top-ups remain planned. See [Economics](economics.md)
and [`MINER_TERMS.md`](../MINER_TERMS.md).

**GitHub sign-in on ormas.ai says my email is already linked.**
That account already exists via Google (same email); sign in with Google. If GitHub "returned an error",
your GitHub email is private — use Google or the email link. Neither affects mining, which uses the token.

**Something in the docs is wrong.**
Open an issue on this repository — your run is the next rehearsal. Security-sensitive:
ops@ormas.ai (see SECURITY.md).
