# ormas-subnet

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
  register → bind a repo → poll for a lease → clone/checkout the base commit →
  call your `solve(draft, workdir) -> SolveResult` → **actually run
  `draft.verify_command`** (bounded, credential-free env, no shell) → publish
  the result branch → complete. `solve` is the one pluggable step; completion
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

## What this is NOT

- **Not our mining logic.** Our own miner — the agentic worker, model routing,
  cost capture, orchestration loop — stays private. It competes on this same
  protocol, on the same terms as every other miner. Nothing about *how* to solve
  a task well lives here.
- **Not an inference endpoint.** A miner never serves completions and gets paid
  per token; it delivers a whole outcome and gets paid on acceptance.
- **Not the acceptance oracle (yet).** Today the gateway derives settlement from
  the miner's own reported `verification_state` plus a scope/commit check — that
  is acceptable only because our own miner is trusted. The **validator** — which
  independently re-runs the verify command and is the real acceptance authority
  for third-party miners — ships in this same public repo, but is not yet built.
  See "Known gaps" below and the decision doc §8.

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
    runner_id="my-miner-01", runner_version="0.1.0", platform="linux",
    capacity=1, cells=("code-edit-small",), workdir_root=Path("/tmp/ormas-miner"),
    repo_id="my-repo", repo_url="https://github.com/acme/target.git",
)
skeleton = MinerSkeleton(client, config, solve)
skeleton.register()
skeleton.bind(project_id="proj_abc123", base_commit="<sha>")
skeleton.run_forever()
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

## Known gaps (as of 2026-09-10, documented rather than papered over)

- **Firm-bid field mirrored here; gateway deploy pending.** The gateway's claim
  body accepts an optional `ask_usd` (the miner's firm ask) as of 2026-09-10 —
  the first ask at or under the client's reserve is leased; an ask above it is
  recorded and skipped, the job stays queued (`runner_api.claim_lease`). That
  gateway change is on the integration branch, not yet deployed to
  `api.ormas.ai`. This package now sends `ask_usd` when configured
  (`OrmasMinerClient.claim_task(..., ask_usd=...)` / `MinerConfig.ask_usd`,
  validated locally to the server's rule); the default `None` keeps sending only
  `schema_version` + `runner_id` and the gateway derives the ask (flat
  per-project fee, or estimated cost plus margin).
- **No validator yet.** Settlement is the miner's own self-report today. Item
  1b in the decision doc's consequence list is the validator design + build; it
  belongs in this repo alongside the miner protocol, and is not part of this
  card.
- **No license file yet.** Owner decision pending before this package is
  published as its own repository.

## No secrets in the client

`OrmasMinerClient` never reads a hardcoded token path. Callers resolve their own
token — via `ormas_subnet.client.load_token(token_env=...)` or
`load_token(token_path=...)` — and pass the plain string in.
