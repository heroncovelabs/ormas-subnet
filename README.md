# ormas-subnet

> **Ormas pays for accepted outcomes, not inference turns.**
> A client describes a change and the test that proves it. A miner offers a firm price or a limit for the whole task. Accepted delivery pays the firm price or the miner’s settled price within the limit; a miss pays nothing.
> The client's acceptance tests are the purchase order; validators re-run them before any third-party miner is paid.

Bittensor subnet 76 · operated by Heron Cove LLC · protocol, thin client, reference miner and reference validator: MIT.

**September 23 development candidate:** start with [Public tasks and supported
environments](docs/PUBLIC_TASKS.md). It documents automatic isolated preparation,
public repository publication, profile qualification, independent acceptance and
restart recovery. A `paid` receipt on `api.ormas.ai` today reflects one
operator-run validator. Independent validator admission and the combined
validator service remain release work.

This package is normative under
[`docs/DECISIONS.md`](docs/DECISIONS.md)
(owner-locked 2026-09-10, pricing amended 2026-10-02): **the runner is the miner.**
A miner posts a `firm` or `limit` offer for a whole task and is paid only when its
delivery is **accepted** —
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
  `draft.verify_command`** with no shell in a credential-free environment
  (`PATH`, scratch `HOME`, `LANG`, plus the packet's explicit `NAME=value`
  assignments) on both routes → publish the result → complete. The public
  profile uses a digest-pinned OCI image with `--network none`, `--cap-drop ALL`,
  `--read-only` and resource limits. Plug in `solve`, and optionally `offer_fn` and
  `settle_fn` for per-task pricing. The receipt is built only from usage
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
- Need a runner key? Sign in at [ormas.ai](https://ormas.ai) → **Miner → Runner keys** (register-only until the operator approves your qualification; [`docs/INSTALL.md`](docs/INSTALL.md)). Want to try it first without one? [`docs/QUICKSTART_LOCAL.md`](docs/QUICKSTART_LOCAL.md) runs the whole loop offline against a local stand-in gateway (`ormas_subnet/localnet.py`) — no token, no network host.

## What this is NOT

- **Not our mining logic.** Our own miner — the agentic worker, model routing,
  cost capture, orchestration loop — stays private. It competes on this same
  protocol, on the same terms as every other miner. Nothing about *how* to solve
  a task well lives here.
- **Not an inference endpoint.** A miner never serves completions and gets paid
  per token; it delivers a whole outcome and gets paid on acceptance.
- **Validator acceptance is separate from a miner finishing.** The reference
  validator clones the repo, reproduces the base failure, checks scope and tests
  the exact result, then signs accept/reject. It ships as the installed
  `ormas-validator` command. The bounded-packet (public-profile) path waits for the frozen validator
  policy, including operator-miner work. V1 requires separate owners; the explicit
  v2 alpha permits our validator to check our miner and discloses operator control.
  See [supported public tasks](docs/PUBLIC_TASKS.md) for protocol, qualification
  and rollout limits. Installing this SDK does not establish live qualification.

## Connect a miner

Use the user-only installer, then log in and check your host:

```bash
curl -fsSL https://ormas.ai/install.sh | bash
ormas-miner login --gateway https://api.ormas.ai
ormas-miner doctor
ormas-miner register --cell task:code --miner-id <your-miner-name>
ormas-miner run --runner-id <assigned-id> --miner-id <your-miner-name> \
  --repo-id <id> --repo-url <url> --cell task:code --solve-command '<cmd>'
```

Login saves the key locally with mode `0600`. Run uses the saved gateway and key.
Save the assigned id from registration. Keep the same `--miner-id` on every run: it
is your public identity, and once your miner slot is approved under that name the
gateway reserves your qualification proof job on registration (the `qualification`
line on stderr tells you whether it was enqueued or which cells are missing). Register your chain identity with
`ormas-miner register-hotkey --runner-id <assigned-id> --hotkey-ss58 <ss58>
--sign-command '<signer>'`.

The equivalent source-checkout command is `python neurons/miner.py --gateway
https://api.ormas.ai --token-env ORMAS_MINER_TOKEN --runner-id <assigned-id>
--repo-id <id> --repo-url <url> --cell task:code --solve-command '<cmd>'`.
See [Install and connect](docs/INSTALL.md) for credentials and qualification.
For the Protected service level, use [Run a Protected miner on your own confidential VM](docs/PROTECTED.md).

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
        prompt_tokens=123, completion_tokens=45,
        cache_read_input_tokens=0, cache_creation_input_tokens=0,
        reasoning_tokens=0, upstream_cost_usd=0.0021,
    )

client = OrmasMinerClient(base_url="https://api.ormas.ai", token=my_token)
config = MinerConfig(
    runner_id="", runner_version="0.1.0", platform="linux",
    capacity=1, cells=("task:code",), workdir_root=Path.home() / ".ormas" / "work",
    repo_id="my-repo", repo_url="https://github.com/acme/target.git",
)
skeleton = MinerSkeleton(client, config, solve)
skeleton.register()  # Save skeleton.config.runner_id for later starts.
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
- `draft.verify_command` runs with no shell in a credential-free environment
  on both routes: `PATH`, scratch `HOME`, `LANG`, plus the packet's explicit
  `NAME=value` assignments. The public profile uses a digest-pinned OCI image
  with `--network none`, `--cap-drop ALL`, `--read-only` and resource limits.
  `verification_state` reflects its real exit code plus `scope_ok`.
  The skeleton computes it; `SolveResult` has no `verified` field.

See `docs/protocol.md` § "What this package's reference skeleton actually
does at completion" for the exact rules.

## Offers and settlement (2026-10-02)

The gateway queue exposes a privacy-safe task envelope before you offer. A
`firm` offer fixes the price. A `limit` offer states two numbers:
`estimate_usd`, your expected charge, and `limit_usd`, the maximum charge, with
`0 < estimate_usd <= limit_usd`. On a verified limit delivery, send
`settled_price_usd` at or below the limit. The client is charged exactly that
settled price. Only limit-job receipts carry `offer_kind`, `estimate_usd`,
`limit_usd` and `settled_usd`.

Estimate honestly: the expected cost of your usual recovery chain plus margin.
Set the limit at your worst-case chain. Settle at actual metered chain cost plus
margin, never above the limit; when cost runs above your estimate, consider
dropping the margin to zero. You bear any cost above the limit. See the worked
example and why both numbers matter in [Economics](docs/economics.md).

`MinerConfig.offer_fn` receives each queue entry and returns a wire offer or
`None` to decline. `settle_fn(lease, result)` supplies a limit settlement; the
skeleton caps it at the limit and defaults to the limit when unset. The hook
must be deterministic and must not raise. A raising hook never reprices the
delivery. On the public (bounded-packet) path the published work is held for
recovery, no completion is sent, and every later poll re-raises until the hook
returns a valid price; only a task without a public packet completes as
`failed`. Leaving both callbacks unset preserves the legacy firm
`ask_usd` flow.

Phase 1 accepts on arrival: the first offer within the client's undisclosed
spending limit wins; an offer above it is recorded and skipped and the job stays
queued. [History-based ranking](docs/protocol.md#offer-ranking) is planned.

`ask_usd` is live since `gateway-2026.09.11`; the queue route, offers and limit
settlement are live since `gateway-2026.10.03`. Against an older gateway the queue
returns 404 without `error.type`, and the skeleton falls back to `ask_usd`. See
[Protocol](docs/protocol.md) for the exact queue, claim and settlement fields.

## Release gaps
- **Unified validator installation is still release work.** This package ships
  task checking. The combined checker/chain service kit and live public-profile
  qualification must be proved separately. Task decisions use Ed25519; the chain
  wallet is a separate identity and is never passed into the task checker. The
  existing weight service relays a gateway-computed vector; packaging it with
  checking does not create independent reward calculation.

## Other gateways

Anyone may operate their own gateway against SN76 miners using this protocol. Ormas neither blocks nor supports that: there is no compatibility promise beyond the published protocol version, no support channel, and no shared settlement or reputation. A miner that connects to a third-party gateway is bound by that gateway's terms, not [`docs/CONTRACT.md`](docs/CONTRACT.md). Ormas's own acceptance and settlement are what the reference validator and the subnet's weights are built around.

## No secrets in the client

`OrmasMinerClient` never reads a hardcoded token path. Callers resolve their own
token — via `ormas_subnet.client.load_token(token_env=...)` or
`load_token(token_path=...)` — and pass the plain string in.
