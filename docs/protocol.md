# Ormas miner protocol — `ormas-runner-v1`

Wire protocol for the Ormas gateway's `/api/runner/v1` control plane, as
implemented today by `tensorbox_spec/customer_api/runner_api.py` (server, private
repo) and mirrored by `ormas_subnet/protocol.py` + `ormas_subnet/client.py` in this
package (public). If you are implementing a miner in another language, this page
plus the DTO field lists below should be enough — no other document is required.

Read [`docs/DECISIONS.md`](DECISIONS.md)
first for the *why*: a miner posts a firm bid for a whole task and is paid only on
accepted delivery; every optimization dimension (model, routing, harness, cost)
lives inside the miner; this protocol is the shape of the conversation, not the
mining logic.

## Transport

- All routes are `POST` (except the paths embedding `{task_id}`, which are also
  `POST` — there are no `GET`s in this control plane today).
- Base path: `/api/runner/v1`.
- Auth: `Authorization: Bearer <token>` where `<token>` is a runner token issued
  out of band (starts with `ormr_`). A missing/invalid/rate-limited token
  returns `401` with `{"error": {"type": "authentication_error", "message": ...}}`.
- Optional device binding: once a runner has registered with a `device_nonce`,
  every subsequent request must carry `X-Ormas-Runner-Device: <nonce>` matching
  what the gateway stored, or the request is rejected as unauthorized. First
  registration may omit it (the gateway can fall back to the header value).
- Every request body must include `"schema_version": "ormas-runner-v1"`. A
  missing/wrong value is rejected as `400 unknown_field:schema_version`.
- **Strict allowlists.** Every route rejects any field not on its allowlist
  (`400 unknown_field:<field>`), and separately rejects a small set of
  universally forbidden fields even before the allowlist check
  (`400 forbidden_field:<field>`): `provider_key`, `repo_path`, `raw_source`,
  `raw_prompt`, `raw_output`, `raw_diff`, `tenant_id`, `client_0`, `rao`. These
  never cross the wire in either direction.

## Error shape

```json
{"error": {"type": "<type>", "message": "<code>[:<field>]"}}
```

| HTTP | `type` | When |
|---|---|---|
| 400 | `invalid_request_error` | Missing/unknown/forbidden field, bad DTO value, `failure_class_mismatch` |
| 401 | `authentication_error` | Missing/invalid token, device mismatch, rate-limited by repeated auth failures |
| 404 | `not_found_error` | Unknown runner/repo/task, or the tenant's runner feature is off |
| 409 | `conflict_error` | `lease_lost` — the lease no longer belongs to you (message `"lease_lost"`) |
| 429 | `rate_limited` | Daily per-token claim cap exceeded (message `"daily_claim_cap"`) |
| 503 | `service_unavailable` | Pricing/quoting subsystem unavailable (message `"runner_pricing_unavailable"`) |
| 500 | `internal_error` | Settlement produced no receipt (should not happen; report it) |

`complete` also returns non-error status codes `202` (`{"status": "settling", "retry_after_s": N}`,
retry the same complete call) and `410` (terminal already recorded; body is the
same shape as a `200`, replay-safe).

## Lifecycle

```
register runner  ──▶  bind repo(s)  ──▶  poll claim ──(204 idle)──▶ poll claim ...
                                              │
                                        (200: lease + draft)
                                              ▼
                                  clone + checkout base_commit
                                              │
                                         heartbeat (renew)  ◀── periodic, keeps lease alive
                                              │
                                            solve
                                              │
                                    publish result branch
                                              │
                                            complete ──▶ 200 done | 202 settling (poll again) | 410 replay
```

Acceptance today: for a miner in the client's own tenant (our trusted miner) the
gateway derives `settlement` from the miner's own `terminal.verification_state`
plus a scope check on `capture.scope_ok` and a valid 40-hex `result_commit`.
This is **self-reported**, acceptable only because that miner is ours. For a
third-party (cross-tenant) miner the gateway settles only on **unanimous
validator acceptance** — the delivery goes `pending_acceptance`, assigned
validators independently re-run the packet's verify command against the
delivered branch and post signed decisions — and it refuses the claim outright
until a validator count is configured (decision doc §8, item 1b; reference
validator in `ormas_subnet/validator.py`). In production no validators are
configured yet and no third-party miner has connected.

`pending_acceptance` is what a third-party miner sees at `complete`: the route
returns `200` with `receipt.settlement = "pending_acceptance"` (never `paid` for
a cross-tenant delivery), and the miner's job ends there. Settlement then
follows the validator quorum — unanimous accept settles `paid` at exactly the
accepted ask; any reject settles `no_delivery`/`validator_reject`, unpaid. There
is no miner-facing route to learn the verdict in this protocol version; do not
build a callback around one.

## Routes

### `POST /api/runner/v1/registrations`

Register (or re-register) this miner.

**Request** (`RunnerRegistration`):

| Field | Type | Notes |
|---|---|---|
| `runner_id` | str | Empty on first registration — the gateway assigns `runr_<12hex>` and returns it; pass the assigned id on every later call. A non-empty id the gateway has not issued to this token is refused 404 |
| `runner_version` | str | Free-form version string |
| `platform` | str | Free-form platform label |
| `capacity` | int | Concurrent task capacity |
| `health` | object | Must include non-empty `cells: [str, ...]` — the task-type cells this miner serves: `task:code`, `task:code/<small|medium|large>`, `task:lang/<language>` (see INSTALL.md); may include `device_nonce` |

**Response**: `{"runner_id": str, "poll_interval_s": int, "lease_ttl_s": int, "heartbeat_s": int, "protocol": "ormas-runner-v1"}`.
Today's server values: `poll_interval_s=15`, `lease_ttl_s=300`, `heartbeat_s=90`
(`runner_api.py` module constants `POLL_INTERVAL_S` / `LEASE_TTL_S` /
`HEARTBEAT_S`). **Treat these as authoritative and read them from the response**
— do not hardcode them; this package's defaults exist only for use before the
first registration response arrives.

### `POST /api/runner/v1/repositories`

Bind a repository this miner can serve to a client's project.

**Request**: `RepoRegistration` fields (`repo_id`, `display_alias`,
`base_commit`, `preflight_state`) flattened into the body, plus `runner_id` and
`project_id` (both required strings). The server also accepts the DTO nested
under a `"repository"` key with `runner_id`/`project_id` alongside it — either
shape works; this client always sends the flattened form.

**Response**: `{"repo_id": str, "project_id": str}`.

### `POST /api/runner/v1/leases`

Poll for work. **Body**: `{"schema_version": ..., "runner_id": str, "ask_usd"?: number}`
— nothing else is accepted. `ask_usd` (optional, finite, ≥ 0) is the miner's firm
ask for any job leased on this claim. Selection is accept-on-arrival: a job is
leased to this miner when `ask_usd` is at or under the client's reserve; an ask
above the reserve is recorded and the job is skipped (it stays queued for the
next miner). When `ask_usd` is absent the server derives the ask (flat per-project
fee, or estimated cost plus margin) and applies the same reserve gate. Every ask
is recorded with its arrival offset. **Deployment status:** `ask_usd` is live on
`api.ormas.ai` since `gateway-2026.09.11`. This package's client sends it when configured
(`claim_task(..., ask_usd=...)` / `MinerConfig.ask_usd`); with no ask
configured the body is exactly `schema_version` + `runner_id`.

**Response**:
- `204 No Content` — nothing queued for this miner right now.
- `200` — `{"lease": <TaskLease wire>, "draft": <TaskDraft wire>}`.

If this miner already holds a running lease (e.g. after a restart), the same
lease + a refreshed draft is returned instead of a new claim — a miner never
holds two concurrent leases.

**`TaskLease`**: `lease_id` (also the *lease token* used in every subsequent
call for this task), `task_id`, `expires_at`, `selected_cell`, `provider_pin`,
`fallback_policy`, `hold_ref`, `now`, `outcome_price_usd` (the fee this miner
will be paid on accepted delivery — fixed, not proposed by the miner).

**`TaskDraft`**: `task_id`, `runner_id`, `repo_id`, `base_commit`, `brief` (the
task description), `verify_command`, `allowed_paths`, `budget_usd`,
`work_packet` (the full frozen packet — task, acceptance criteria, etc.),
`work_packet_sha256`, `attempt`, `parent_job_id` (non-empty on a repair),
`repair_findings`, `repair_evidence` (present only on a repair attempt).

Daily claims per token are capped (`daily_claim_cap`, default 50 unless the
token row overrides it); exceeding it returns `429`.

### `POST /api/runner/v1/leases/{task_id}/heartbeat`

Keep the lease alive, or emit a progress event, or both.

**Body**: `schema_version`, `runner_id`, `lease_token` (the lease's `lease_id`),
`renew` (bool, default true), `event` (optional `TaskEvent` wire dict).

- `renew=true` extends `expires_at` by another `lease_ttl_s` and returns
  `{"expires_at": str, "now": str}`.
- `renew=false` with an `event` records progress only (state → phase mapping:
  `queued/claimed/executing/verifying/repairing/publishing/blocked` map roughly
  1:1; `running` also maps to `executing`) and returns the current
  `expires_at`/`now` unchanged.
- A lease that no longer belongs to this miner (wrong owner, wrong token, not
  `running`) returns `409 lease_lost`.

**`TaskEvent`**: `lease_id`, `state` (one of the mapped states above),
`occurred_at`, `error_category` (optional).

Call this **during** `solve`, not just before/after — a lease not renewed within
`lease_ttl_s` expires and the task can be reassigned.

### `POST /api/runner/v1/leases/{task_id}/complete`

Report the outcome.

**Body**: `schema_version`, `runner_id`, `lease_token`, `receipt`
(`TaskReceipt` wire), `terminal` (`TaskTerminal` wire), and optionally
`capture`, `failure_evidence`, `pr_url`, `pr_error`, `evidence`.

**`TaskReceipt`**: `lease_id`, `generation_ids` (≤16 entries, each
`^[A-Za-z0-9_-]{1,64}$`), `actual_provider`, `model` (must match
`^[a-z0-9./:-]{1,80}$`), `prompt_tokens`, `completion_tokens`,
`cache_read_input_tokens`, `cache_creation_input_tokens`, `reasoning_tokens`,
`upstream_cost_usd` (nullable), `finish_reason` (nullable),
`metering_complete` (bool).

**`TaskTerminal`**: `lease_id`, `verification_state` (one of
`verified/failed/scope_violation/setup_failure/repair_refused/publish_failed/budget_exceeded/aborted`),
`result_ref` (must be exactly `refs/heads/ormas/job/<task_id>` or
`local:ormas/job/<task_id>` — anything else is rejected), `settlement_state`
(free-form, not validated server-side today), `rating` (nullable, `"1"`–`"5"`
as a string), `result_commit` (40-hex sha, required non-empty when
`verification_state == "verified"`).

**`capture`** (all optional, this route's own allowlist — separate from the
DTOs above): `status`, `session_id` (≤128 chars), `model_ids` (list[str]),
`total_cost_usd`, `wall_s`, `attempts` (list of `{"verify_exit_code": 0-255}`),
`scope_ok` (bool — did the diff stay inside `allowed_paths`?),
`material_diff_sha256` (64-hex — a hash of the diff, never the diff itself),
`file_count`, `changed_paths` (list of `{"path": str, "policy": str?}`, paths
must be relative, no `..`), `policy`, `failure_class`, `identity`,
`development_authorization_id` / `production_authorization_id` /
`provider_expense_bound_enforced` (execution-authorization evidence, only
relevant if the miner participates in that program). **Forbidden inside
`capture`, at any nesting depth**: the universal forbidden-field list above,
plus `settlement`, `settled_usd`, `actual_cost_usd`, `sla_decay`, `winner`,
`worker_id`, `ok`, `pricing`, `quote_id`, any key ending in `stderr`,
containing `stdout`, or named `diff`/`prompt`/`source`. **Never send a raw
diff, prompt, or model output over this route — only its hash and metadata.**

**Response**:
- `200 {"status": "done"|"failed", "receipt": {...projection...}}` — the
  projected receipt never includes `pricing`, hidden reserve math, or
  anything from the forbidden list; it does include `settlement`,
  `customer_billed_usd`, `debit_status`, `upstream_cost_usd`.
- `202 {"status": "settling", "retry_after_s": N}` — settlement is still in
  flight (e.g. a debit hasn't confirmed); re-send the identical `complete`
  call after `retry_after_s`. This is safe to retry — the server recognizes an
  already-recorded receipt and replays it rather than double-settling.
- `410` — the task already has a terminal outcome (a previous `complete` call
  landed); body is the same shape as `200`, safe to treat as authoritative.

Settlement derivation (informational — you do not construct this, the server
does): `verified` + `scope_ok` + a valid commit → `"paid"`; `verified` but
`scope_ok=false` → `"no_delivery"`/`scope_violation`; `verified` but an invalid
commit → `"no_delivery"`/`publish_failed`; any other `verification_state` →
`"no_delivery"` with a failure class derived from the state (or your own
`capture.failure_class`, if it's a recognized value for that state).

### What this package's reference skeleton actually does at completion

`MinerSkeleton.run_once` (`ormas_subnet/skeleton.py`) builds the `complete` call
honestly from what `solve` reported and from ground truth it checks itself —
it does not trust `solve`'s self-report for anything settlement-relevant:

- **Receipt.** `TaskReceipt` is built from `SolveResult`'s optional usage
  fields (`provider`, `model`, token counts, `upstream_cost_usd`,
  `generation_ids`). A missing token count zero-fills on the wire (the DTO
  field is a plain `int`, no null) but flips `metering_complete=False`; a
  missing `upstream_cost_usd` is sent as `None` — the DTO's nullable float —
  never coerced to `0.0`. A missing `provider`/`model` is sent as the literal
  `"unknown"` (an empty string would fail the server's `model` regex). Only
  the reference solver (`reference_solver.py`) legitimately reports
  `provider="reference"`, `model="reference-shell-solver"`, all-zero usage,
  and `metering_complete=True` — because it made no model call — and that
  comes from the solver's own `SolveResult`, not a skeleton default.
- **`scope_ok`.** Computed from `git diff --name-only <base_commit>
  <result_commit>` in the workdir — never asserted `True`. `SolveResult
  .changed_paths` (the solver's own report) is used only as a cross-check; a
  mismatch is warned, not trusted. A path is "under" `allowed_paths` using
  the same rule as the private policy engine
  (`grokbuild_client._policy_path_covers`): a trailing-`/` entry is a
  directory prefix, anything else matches only by exact equality.
  `scope_ok` is vacuously `True` when `allowed_paths` is empty.
- **Verification.** The skeleton actually runs `draft.verify_command` after
  `solve` returns — it never trusted a `SolveResult.verified` self-declaration
  (that field no longer exists). Parsing mirrors the private runner's
  `_split_verify_command`: leading POSIX `NAME=value` assignments are
  stripped into the environment, the remaining argv runs directly with no
  shell. The child environment is bounded and credential-free — only `PATH`,
  a fresh scratch `HOME`, and `LANG` cross in; no provider keys, no ambient
  secrets. `verification_state` is `"verified"` only when the exit code is 0
  **and** `scope_ok`; otherwise `"failed"`. The exit code is recorded in
  `capture.attempts` (`[{"verify_exit_code": N}]`), the same shape the
  private runner uses (`outcomes_worker.py`'s per-attempt projection).

### `POST /api/runner/v1/hotkey/challenge`

Mint a one-time challenge for chain-hotkey registration, bound to this
(runner, miner identity, hotkey). The challenge expires in 300 s and can be
consumed exactly once.

**Request**:

| Field | Type | Notes |
|---|---|---|
| `runner_id` | str | Miner's stable id |
| `hotkey_ss58` | str | SS58 address of the hotkey to register |

**Response**: `{"challenge": str, "expires_at": str, "now": str, "miner_identity": str}`.

### `POST /api/runner/v1/hotkey`

Verify the miner's signature over the challenge and record the verified
hotkey↔miner-identity mapping the weights scorer pays on. The signature is
**sr25519 over the UTF-8 bytes of the challenge string** — exactly those bytes,
nothing prepended — made with the miner's own tooling (e.g. a Bittensor wallet
`Keypair.sign`); this package never holds, derives, or loads a hotkey
(`OrmasMinerClient.register_hotkey` takes an injected `sign_fn`; the miner CLI
takes a `--sign-command` that reads the challenge on stdin and prints hex).

**Request**:

| Field | Type | Notes |
|---|---|---|
| `runner_id` | str | Miner's stable id |
| `hotkey_ss58` | str | Must match the minted challenge's hotkey |
| `challenge` | str | The exact challenge string the challenge route returned |
| `signature_hex` | str | sr25519 signature over the challenge's UTF-8 bytes, hex-encoded |

**Response**: `{"miner_identity": str, "hotkey_ss58": str, "verified": true,
"signature_scheme": "sr25519"}`. Nothing is written on any refusal:

| HTTP | Message | When |
|---|---|---|
| 400 | `hotkey_invalid` | `hotkey_ss58` is not a valid SS58 address |
| 400 | `challenge_unknown` | No challenge minted for this (runner, hotkey) |
| 400 | `challenge_mismatch` | `challenge` does not match the minted string |
| 400 | `challenge_expired` | Challenge older than 300 s |
| 400 | `hotkey_signature_invalid` | sr25519 verification failed |
| 409 | `challenge_used` | Challenge was already consumed (one-time) |
| 409 | `hotkey_claimed` | Hotkey already registered to a different miner identity |
| 503 | `hotkey_verification_unavailable` | Verification subsystem unavailable |

## Known gaps between this doc and the decision record

- **Firm asks are live.** The gateway's claim body accepts an optional `ask_usd`
  (the miner's firm ask) — deployed on `api.ormas.ai` in `gateway-2026.09.11`;
  the first ask at or under the client's reserve is leased, and an ask above it
  is recorded and skipped. This package sends it when configured
  (`client.claim_task(..., ask_usd=...)` / `MinerConfig.ask_usd`, validated
  locally to the server's rule: finite, ≥ 0, not a bool — a bad value raises
  `ValueError` before any request). The default `None` keeps the two-field
  claim body and the server-derived ask (`outcome_price_usd` on the lease).
- **Validator-quorum settlement is built but not configured in production.**
  The reference validator ships in this package (`ormas_subnet/validator.py`,
  `neurons/validator.py`), and the gateway settles a third-party miner's
  delivery only on unanimous validator acceptance, refusing its claim until a
  validator count is configured. `api.ormas.ai` has no validator count
  configured and no third-party miner has connected, so every production
  receipt to date reflects our trusted miner's own verify run, not an
  independent decision.
- **No reputation feed from this protocol version.** Nothing in this route set
  writes to a miner-identity reputation history a chain weight could read;
  that is consequence item 3 in the decision doc, not yet built.
