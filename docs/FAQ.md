# FAQ — from the first external miner's day one (2026-09-15)

Short answers to the questions the first third-party run raised. Longer treatment in
`INSTALL.md`, `CONTRACT.md`, `protocol.md`.

**My first `--once` run "failed". Is that expected?**
With `--solve-command 'true'` yes: the reference solver changes nothing, the client's tests fail, the
skeleton reports that honestly and the job settles `no_delivery` for $0. That run proves the whole loop —
register, claim, clone with the job key, verify, push, complete — from your machine. The next run needs a
real solve.

**Where is my `runner_id`? The library example printed nothing.**
The CLI prints `runner_id=<assigned>` to stderr on every start. The library's `register()` adopts the id
silently — the INSTALL example now prints it after `register()` and reads it back from `ORMAS_RUNNER_ID`.
Pass the assigned id on every later start; a self-chosen id is refused 404.

**How do I choose my public miner identity?**
Set `MinerConfig(miner_id="your-miner-name")`. Lowercase, 3–40 characters, `[a-z0-9][a-z0-9-]*`. It must be globally unique — the gateway returns 409 if another runner already holds it; re-registering with your own id is fine. When set, receipts (`worker_id`), the providers row, and the hotkey challenge bind to `miner:<miner_id>` instead of `miner:<tenant>`. Omit it and registration is byte-identical to today.

**Do I have to bind a repository?**
No. On today's path the claim carries the client's clone URL and a job-scoped deploy key. `--repo-id` /
`--repo-url` are required fields but only the fallback clone source. `bind` is a legacy option for a miner
serving one pre-arranged repository.

**Why is my ask accepted when another miner asked more (or less)?**
"Accept on arrival": the first ask at or under the client's reserve is leased at that ask. An ask above the
reserve is recorded and skipped; the job stays queued. Price is not compared across miners; arrival order is.

**Who decides whether I'm paid?**
For third-party miners, assigned validators re-run the client's verify command on your delivered branch in
the environment the packet declares (`toolchain`: Python version + `pip_install`), check the diff against
the allowed and immutable paths, and post signed decisions; unanimity pays. Today there is one validator on
`api.ormas.ai` and it is operator-run. Your own `verified` report is advisory; a self-reported failure
settles unpaid at once.

**What may my solve change?**
Only paths in the packet's `allowed_paths`; immutable paths must not change. The reference solver stages
only `allowed_paths` (since 2026-09-15 — the first external run had committed a `.rustup/settings.toml`
written into the workdir by a local toolchain hook). Your own solver should do the same.

**What environment do the tests run in?**
The packet's `toolchain` block: a fresh venv of the declared Python (3.12 today) with the declared
`pip_install`. Provisioning that environment is yours; the validator provisions the same one independently.

**How many claims can I make?**
Your token carries a daily claim cap (10 at onboarding; raised on request). Lease TTL and heartbeat cadence
come from the register response and are authoritative.

**Is there a chain emission?**
Yes, SN76 already emits alpha. The approved 1× accepted-bid target and surplus-burn
policy is an implementation candidate awaiting activation. Customer USD stays with
Heron Cove; miners receive protocol alpha, not USD credit payouts. See
[economics.md](economics.md) for funding, self-dealing exclusions and rollout limits.

**GitHub sign-in on ormas.ai says my email is already linked.**
That account already exists via Google (same email); sign in with Google. If GitHub "returned an error",
your GitHub email is private — use Google or the email link. Neither affects mining, which uses the token.

**Something in the docs is wrong.**
Open an issue on this repository — your run is the next rehearsal. Security-sensitive:
operations@heroncove.us.
