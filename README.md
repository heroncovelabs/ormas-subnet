# ormas-subnet

> **Ormas: pay for verified receipts, not inference turns.**
> A client describes a change and the test that proves it. Miners with public track records post a firm ask for the passing result. Accepted delivery → the miner is paid exactly its ask; a miss pays nothing.
> The client's acceptance tests are the purchase order; validators re-run them before anyone is paid.

Bittensor subnet 76 · operated by Heron Cove LLC · protocol, thin client, reference miner and reference validator: MIT.

> **Disclosure.** Heron Cove LLC runs a miner and a validator on SN76 and operates the gateway that
> settles work. Until validators gate settlement on the production gateway, acceptance is not
> independent of us; we say so wherever a number appears (`/v1/public/providers` labels our miner
> `operator-run`). Nothing in this repository is an offer of emissions, a payout schedule, or a
> return; the compensation model below is **planned — not yet live** until marked otherwise.

Working on this repo with a coding agent? Start at [`AGENTS.md`](AGENTS.md) (repository map,
authoritative specs, commands, boundaries). Found a vulnerability? [`SECURITY.md`](SECURITY.md).

This package is normative under
[`docs/DECISIONS.md`](docs/DECISIONS.md)
(owner-locked 2026-09-10): **the runner is the miner.** A miner posts a firm bid
("ask") for a whole task and is paid only when its delivery is **accepted** —
never for inference served, tokens spent, or hardware occupied. It is what you
give a third-party miner to connect to the Ormas subnet, local
or SN76.

## What this is

- The **miner wire protocol** (`ormas_subnet/protocol.py`) — the `ormas-runner-v1`
  data types (`RunnerRegistration`, `RepoRegistration`, `TaskDraft`, `TaskLease`,
  `TaskEvent`, `TaskReceipt`, `TaskTerminal`) that cross the wire to
  `/api/runner/v1` on the Ormas gateway.
- A **thin HTTP client** (`ormas_subnet/client.py`, `OrmasMinerClient`) for those
  routes.
- A **reference miner skeleton** (`ormas_subnet/skeleton.py`, `MinerSkeleton`) —
  register → poll for a lease → clone the client's repository fresh with the
  job's `repo_credential` from the draft and check out the base commit →
  call your `solve(draft, workdir) -> SolveResult` → **actually run
  `draft.verify_command`** (bounded, credential-free env, no shell) → push
  the result branch with the same credential → complete. No `bind` step is
  needed; binding a repository you already hold is the fallback
  (`docs/INSTALL.md`). `solve` is the one pluggable step; completion
  itself is honest, not self-declared: the receipt is built only from usage
  `solve` reports (an unknown value is never fabricated as zero), `scope_ok`
  is computed from a real `git diff` against `allowed_paths` (never asserted),
  and `verification_state` comes from the verify command's actual exit code.
  See "Completion is honest" below and `docs/protocol.md`.
- A **trivial reference solver** (`ormas_subnet/reference_solver.py`) that runs a
  shell command and commits the result, so the skeleton runs end to end with no
  model calls at all — and reports its own honest zero-usage receipt fields
  (it truly made no model call).
- The **protocol contract** in [`docs/protocol.md`](docs/protocol.md): every
  route, field, forbidden field, and error code, written so you could implement
  a miner in another language from it alone.
- No token yet? [`docs/QUICKSTART_LOCAL.md`](docs/QUICKSTART_LOCAL.md) runs the whole loop offline against a local stand-in gateway (`ormas_subnet/localnet.py`) — no token, no network host.

## What this is NOT

- **Not our mining logic.** Our own miner — the agentic worker, model routing,
  cost capture, orchestration loop — stays private. It competes on this same
  protocol, on the same terms as every other miner. Nothing about *how* to solve
  a task well lives here.
- **Not an inference endpoint.** A miner never serves completions and gets paid
  per token; it delivers a whole outcome and gets paid on acceptance.
- **Not yet settled by validators on the production gateway.** Today the
  production gateway derives settlement from the miner's own reported
  `verification_state` plus a scope/commit check — acceptable only because our
  own miner is trusted. The **reference validator** — which clones the job's
  repo, independently re-runs the verify command on base and result, checks
  scope from `git diff`, signs, and posts accept/reject — ships here
  (`ormas_subnet/validator.py`, `neurons/validator.py`). The gateway settles a
  third-party miner's delivery only on unanimous validator acceptance and
  refuses its claim until a validator count is configured; our own trusted
  miner still settles by its own verify run. In production one validator is
  configured (operated by Heron Cove) and every third-party delivery is gated
  on it; the validator provisions the packet's declared `toolchain` before it
  re-runs the tests (see `docs/protocol.md`). See "Known gaps" below and the
  decision doc §8.

## Plugging in your own `solve`

```python
from pathlib import Path
from ormas_subnet import MinerConfig, MinerSkeleton, OrmasMinerClient, TaskDraft
from ormas_subnet.skeleton import SolveResult

def solve(draft: TaskDraft, workdir: Path) -> SolveResult:
    # Your mining logic goes here: read draft.brief / draft.verify_command /
    # draft.work_packet, edit files in `workdir`, commit, return the commit sha.
    # The skeleton itself runs draft.verify_command and computes scope_ok from
    # git — you do not declare either. Report real usage if you have it
    # (provider/model/tokens/cost); leave a field None if you don't know it —
    # never fabricate a zero.
    ...
    return SolveResult(
        result_commit=my_commit_sha,
        changed_paths=("src/app.py",),
        provider="acme-model-co", model="acme-large",
        prompt_tokens=123, completion_tokens=45,
        cache_read_input_tokens=0, cache_creation_input_tokens=0,
        reasoning_tokens=0, upstream_cost_usd=0.0021,
    )

client = OrmasMinerClient(base_url="https://api.ormas.ai", token=my_token)
config = MinerConfig(
    runner_id="",  # empty on first run; the gateway assigns runr_<12hex>
    runner_version="0.1.0", platform="linux",
    capacity=1, cells=("task:code",), workdir_root=Path.home() / ".ormas" / "work",
    # Required fields, but only the fallback clone source: when the draft
    # carries repo_credential + repo_url the skeleton clones and pushes there.
    repo_id="my-repo", repo_url="https://github.com/acme/target.git",
)
skeleton = MinerSkeleton(client, config, solve)
skeleton.register()
skeleton.run_forever()
```

Each claimed `TaskDraft` names the client's repository (`repo_url`) and carries a
deploy key for it (`repo_credential`, kind `ssh_deploy_key`). The skeleton writes
the key to a `0600` file only for the duration of each `git clone` / `git push`
and removes it after (`skeleton._credential_git_env`); it is never logged or
persisted. Validators clone with their own read-scoped `repo_credential` to
re-run the tests (`ormas_subnet/validator.py`). Details and what you receive per
job: [`docs/INSTALL.md`](docs/INSTALL.md). First-run questions: [`docs/FAQ.md`](docs/FAQ.md).

### Fallback: bind a repository you already hold

If the operator onboarded your miner against a specific project and you already
have a clone URL your host can push to, bind it once between `register()` and
`run_forever()`. Drafts for a bound repository arrive without `repo_credential`,
and the skeleton clones `config.repo_url` and pushes to `config.push_remote`.

```python
skeleton.bind(project_id="proj_abc123", base_commit="<sha>")
```

## Completion is honest, not self-declared

`MinerSkeleton` used to fabricate parts of the completion step (a fixed-review
finding, fixed in a follow-up commit): a hardcoded "reference" receipt
regardless of what `solve` did, an asserted `scope_ok=True`, and no verifier
run at all. All three are now real:

- The `TaskReceipt` is built only from usage `SolveResult` reports. An unknown
  token count or cost is never coerced to a fabricated zero — it either
  zero-fills with `metering_complete=False` (token counts, where the wire DTO
  has no null) or crosses as `None` (`upstream_cost_usd`, which is nullable).
- `scope_ok` is computed from `git diff --name-only <base_commit>
  <result_commit>`, not asserted. `SolveResult.changed_paths` is a
  cross-check only; a mismatch is warned, never trusted over git.
- `draft.verify_command` is actually run — bounded, credential-free
  environment, no shell — and `verification_state` reflects its real exit
  code plus `scope_ok`. There is no more `SolveResult.verified` field for a
  solver to self-declare.

See `docs/protocol.md` § "What this package's reference skeleton actually
does at completion" for the exact rules.

## How miners are paid (planned — not yet live)

Ormas (Heron Cove LLC) sells verified coding Outcomes to customers in USD and is responsible for
delivery. Miners are Ormas's suppliers: they bid a USD price per Outcome (`ask_usd`), and when their
work is accepted that USD bid becomes their **emission target**. Independent validators convert
targets into SN76 emission weights every epoch using the on-chain alpha price and a published
TAO/USD reference, so **miners are paid in alpha by the chain**. Any USD not covered by an epoch's
emissions **carries forward** as a visible balance; persistent balances are **topped up in alpha from
Ormas's treasury** on a daily netting schedule, after a KYC/W-9/W-8 gate. Every balance and payment
is **recomputable from public per-epoch artifacts**. Ormas never converts a customer's dollars for a
miner, never holds alpha for anyone, and never pays miners in dollars by default. Bids are USD
*targets*; what an epoch actually pays varies with the alpha price. Detail and the open parameters:
[`docs/economics.md`](docs/economics.md); the obligations: [`MINER_TERMS.md`](MINER_TERMS.md) (draft).

### How this compares to other subnets

From the operators' own documentation, read 2026-09-12/13. Check the sources; these summaries are ours.

| | SN4 Targon (Manifold) | SN28 sayGM (T34) | SN51 Lium (Datura) | SN76 Ormas |
|---|---|---|---|---|
| What is priced in USD | per-card-hour targets and caps | traffic value served (discount off retail) | rental fees | the accepted Outcome bid |
| Who converts USD → weight | validators, at a TAO price | validators, from public epoch artifacts | — (emissions separate from pay) | validators, at pool price × published TAO/USD reference |
| Miner paid in | emissions only | emissions only | 95% of USD fees in alpha from Lium's treasury, daily, T+2, plus emissions | emissions; shortfall carried forward, then topped up in alpha from treasury |
| Unallocated emission | burned | — | — | weight to owner UID (burn/recycle per hyperparameter) |
| Public per-epoch ledger | no | yes (every earning recomputable) | no | yes (sayGM standard) |
| Seller of record to the customer | Manifold | T34 | Lium (pass-through shape) | Heron Cove LLC |

Sources: `docs.targon.com/providers/miner/` (mirrored in `manifold-inc/targon`), sayGM's public
miner ledger and docs, Lium's provider docs. We copy Targon's USD-target conversion and sayGM's
public ledger; the on-chain carry-forward and the US seller standing behind the target are ours.
No peer we found combines all five columns.

## Known gaps (as of 2026-09-12, documented rather than papered over)

- **Firm asks are live.** The gateway's claim body accepts an optional
  `ask_usd` (the miner's firm ask) — the first ask at or under the client's
  reserve is leased; an ask above it is recorded and skipped, the job stays
  queued (`runner_api.claim_lease`). Deployed on `api.ormas.ai` in
  `gateway-2026.09.11`; nine production tasks settled at their firm asks on
  2026-09-12 with zero platform fee. This package sends `ask_usd` when configured
  (`OrmasMinerClient.claim_task(..., ask_usd=...)` / `MinerConfig.ask_usd`,
  validated locally to the server's rule); the default `None` keeps sending only
  `schema_version` + `runner_id` and the gateway derives the ask (flat
  per-project fee, or estimated cost plus margin).
- **One operator-run validator in production.** The reference validator
  (this repo) and validator-quorum settlement (gateway, card `25ff6188`) are
  live: `api.ormas.ai` is configured for one validator, run by the operator,
  and third-party deliveries settle only on its signed acceptance (first such
  deliveries settled 2026-09-14; the first non-operator miner connected
  2026-09-15). Independent validators are not yet admitted, so a production
  receipt today reflects one operator-run review, not a multi-party quorum.
  Ed25519 is the dev-subnet signature scheme; sr25519 is the SN76 target.

## Other gateways

Anyone may operate their own gateway against SN76 miners using this protocol. Ormas neither blocks nor supports that: there is no compatibility promise beyond the published protocol version, no support channel, and no shared settlement or reputation. A miner that connects to a third-party gateway is bound by that gateway's terms, not [`docs/CONTRACT.md`](docs/CONTRACT.md). Ormas's own acceptance and settlement are what the reference validator and the subnet's weights are built around.

## No secrets in the client

`OrmasMinerClient` never reads a hardcoded token path. Callers resolve their own
token — via `ormas_subnet.client.load_token(token_env=...)` or
`load_token(token_path=...)` — and pass the plain string in.
