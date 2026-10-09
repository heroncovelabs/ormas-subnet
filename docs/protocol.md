# Ormas miner protocol — `ormas-runner-v1`

Wire protocol for the Ormas gateway's `/api/runner/v1` control plane, as
implemented today by `tensorbox_spec/customer_api/runner_api.py` (server, private
repo) and mirrored by `ormas_subnet/protocol.py` + `ormas_subnet/client.py` in this
package (public). If you are implementing a miner in another language, this page
plus the DTO field lists below should be enough — no other document is required.

Read [`docs/DECISIONS.md`](DECISIONS.md)
first for the *why*: a miner offers a firm price or a limit for a whole task and
is paid only on accepted delivery; every optimization dimension (model, routing, harness, cost)
lives inside the miner; this protocol is the shape of the conversation, not the
mining logic.

## Transport

- The queue, own-registration and own-offer reads are `GET`. Offer withdrawal
  uses `DELETE`; the other routes below are `POST`.
- Base path: `/api/runner/v1`.
- Auth: `Authorization: Bearer <token>` where `<token>` is a runner token issued
  out of band (starts with `ormr_`). A missing/invalid/rate-limited token
  returns `401` with `{"error": {"type": "authentication_error", "message": ...}}`.
- Optional device binding: once a runner has registered with a `device_nonce`,
  every subsequent request must carry `X-Ormas-Runner-Device: <nonce>` matching
  what the gateway stored, or the request is rejected as unauthorized. First
  registration may omit it (the gateway can fall back to the header value).
- Runner JSON request bodies include `"schema_version": "ormas-runner-v1"`,
  except `POST /offers`, whose exact allowlist excludes `schema_version`.
  A missing/wrong version on versioned routes is rejected as
  `400 unknown_field:schema_version`. GET and DELETE routes have no body;
  binary publication uses its own schema below.
- **Strict allowlists.** Every route rejects any field not on its allowlist
  (`400 unknown_field:<field>`), and separately rejects a small set of
  universally forbidden fields even before the allowlist check
  (`400 forbidden_field:<field>`): `provider_key`, `repo_path`, `raw_source`,
  `raw_prompt`, `raw_output`, `raw_diff`, `tenant_id`, `client_0`, `rao`. These
  are excluded from control-plane JSON. Public artifact publication separately
  carries committed source file bytes, as specified below.

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
register runner  ──▶  queue + offers (or legacy claim) ──(204 idle)──▶ poll ...
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

Public-profile jobs settle under their frozen acceptance contract. V1 requires
separate miner and validator operators; the explicit v2 alpha permits an
operator-run checker. Both require validator acceptance, including for the
operator's miner. See the public acceptance fields below.

On the legacy route, a same-tenant delivery uses the miner's reported
`terminal.verification_state`, `capture.scope_ok` and a valid result commit.
Cross-tenant delivery requires unanimous assigned-validator acceptance and
refuses a claim until a validator count is configured. The reference validator
ships in `ormas_subnet/validator.py`.

A delivery awaiting validators returns `200` with
`receipt.settlement = "pending_acceptance"`. Accepted firm delivery pays exactly
the accepted price; accepted limit delivery pays exactly the pinned settled
price within the limit. Rejected delivery is unpaid. This route set provides
no miner-facing verdict callback.

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
| `miner_id` | str, optional | Chosen public miner identity. Lowercase, 3–40 chars, `[a-z0-9][a-z0-9-]*`. Globally unique (409 if another runner holds it); the same runner may re-register with its own id. When set, claims, receipts (`worker_id`) and the hotkey identity become `miner:<miner_id>` instead of `miner:<tenant>`. Omit for a byte-identical-to-today body |

**Response**: `{"runner_id": str, "poll_interval_s": int, "lease_ttl_s": int, "heartbeat_s": int, "protocol": "ormas-runner-v1", "miner_id"?: str, "qualification"?: object}`. `miner_id` is echoed only when the runner registered one.

`qualification` appears only when this token's daily claim cap is 0 and the operator has
approved a miner slot for `miner:<miner_id>`. The gateway then reserves one qualification
proof job for this miner (an `award_policy` only it can claim) and reports one of:
`{"status": "enqueued", "job_id": str}` (new proof job; keep the miner running and it will
appear on your queue read), `{"status": "pending", "job_id": str}` (a proof job already
exists or was paid), `{"status": "cells_missing", "missing": [str, ...]}` (register again
with those cells added), or `{"status": "error", "error": str}`. Re-registering is the
retry path. A paid proof job raises the token's daily claim cap, which opens the general
queue. The key is absent for every other registration.
Today's server values: `poll_interval_s=15`, `lease_ttl_s=300`, `heartbeat_s=90`
(`runner_api.py` module constants `POLL_INTERVAL_S` / `LEASE_TTL_S` /
`HEARTBEAT_S`). **Treat these as authoritative and read them from the response**
— do not hardcode them; this package's defaults exist only for use before the
first registration response arrives.

### `GET /api/runner/v1/runners/me`

Read this token's latest active miner registration. Optional query parameter
`runner_id` selects a specific active registration belonging to the same token.
The read leaves registration health and liveness unchanged. A missing registration,
a retired registration, or an id belonging to another token returns `404`.
Device-bound registrations require `X-Ormas-Runner-Device` as above.

**Response**: `{"runner_id": str, "cells": [str, ...], "hotkey": {"bound": bool, "verified": bool}, "claim_cap": int}`.
`cells` contains the registered task cells. `hotkey` reports binding and verification
state for the miner's chosen identity, or its tenant identity when none is chosen.
`claim_cap` is the token's configured daily claim cap. The response contains no
hotkey address, proof material, device nonce or credential.

`ormas-miner doctor` uses this read to check Standard Python cells, hotkey binding
and the claim cap. `--runner-id` selects the registration; `--device-nonce` supplies
a bound device's nonce. Missing required cells produce a failing exit status.
An absent hotkey produces a warning pointing to `ormas-miner register-hotkey`.
Optional future `qualification` metadata may carry the qualification job status;
its absence leaves the cap diagnostic available.

### `POST /api/runner/v1/repositories`

Bind a repository this miner can serve to a client's project.

**Request**: `RepoRegistration` fields (`repo_id`, `display_alias`,
`base_commit`, `preflight_state`) flattened into the body, plus `runner_id` and
`project_id` (both required strings). The server also accepts the DTO nested
under a `"repository"` key with `runner_id`/`project_id` alongside it — either
shape works; this client always sends the flattened form.

**Response**: `{"repo_id": str, "project_id": str}`.

### `GET /api/runner/v1/queue?runner_id=<assigned-id>`

List eligible task shapes before offering. `OrmasMinerClient.list_queue(runner_id)`
URL-encodes the assigned id and returns an object with `schema_version` and `jobs`.
Listing creates no lease. Qualification, capacity, task cells, execution profile,
service level, publication and admission checks still apply. At capacity, `jobs`
is empty. The skeleton falls back to the legacy `ask_usd` claim only on a queue
HTTP 404 with no `error.type`, indicating an older gateway without this route,
and remembers that fallback for the process. A 404 with
`error.type == "not_found_error"` is raised to the operator: it can mean an unknown
runner, or no priced binding and no eligible project. Other queue errors also
surface to the caller.

Ordinary queue entries have `job_id`, `created_at` and `envelope`, with no
window marker. A job awarded to this runner adds `awarded_to_you: true`,
`bid_id` and `award_id`. The envelope admits only these keys; absent or invalid
values are omitted:

| Key | Shape |
|---|---|
| `size_class` | `small`, `medium` or `large` |
| `attempt_limit` | Nonnegative integer |
| `turn_budget` | Nonnegative integer |
| `archetype` | Task archetype label |
| `execution_profile_id` | Known execution profile id |
| `languages` | List of canonical language labels |
| `task_text_chars` | Nonnegative integer |
| `acceptance_criteria_count` | Nonnegative integer |
| `immutable_paths_count` | Nonnegative integer |
| `verify_command_category` | `pytest`, `node`, `cargo`, `go`, `shell` or `other` |
| `allowed_paths_count` | Nonnegative integer |
| `source_path_bucket` | `f(1|2-3|4-10|11+)/d(1|2|3|4+)` |
| `service_level` | `standard` or `protected` |
| `repository_visibility` | `public` or `private` |

Counts exclude booleans. The envelope contains task shape, without task text,
source, actual paths or the exact acceptance policy. Standard/Protected are
Ormas service levels; public/private are GitHub repository visibility. A private
repository's service level is stated separately.

Example request: `GET /api/runner/v1/queue?runner_id=runr_0123456789ab`.
A response with a subset of valid envelope keys:

```json
{
  "schema_version": "ormas.runner-queue.v1",
  "jobs": [{
    "job_id": "job-1",
    "created_at": "2026-10-02T00:00:00Z",
    "envelope": {
      "service_level": "standard",
      "repository_visibility": "public",
      "turn_budget": 12,
      "allowed_paths_count": 1,
      "source_path_bucket": "f1/d1"
    }
  }, {
    "job_id": "job-2",
    "created_at": "2026-10-02T00:01:00Z",
    "envelope": {
      "service_level": "standard",
      "repository_visibility": "public",
      "turn_budget": 24,
      "allowed_paths_count": 1,
      "source_path_bucket": "f1/d1"
    }
  }]
}
```

### Offer windows: explicit opt-in

Rollout requires a gateway with offer-window support. The reference miner ships
with `MinerConfig.offer_window=False`; `--offer-window` enables it explicitly.
Window mode uses `offer_fn(entry)` for per-job firm or limit terms. With no hook,
a configured positive finite `ask_usd` supplies a static firm offer. Missing
pricing is refused. The legacy defaults and bare queue-404 fallback keep their
existing claim bodies. A miner with saved window bids refuses a route-missing
fallback so it can preserve its own award binding.

The gateway owns two clocks: `ack_by_s` defaults to 60 seconds and ranges from
15 to 60; manual `window_s` defaults to 600 seconds and has a maximum of 600.
Automatic selection may award after eligible miners answer, or at the
acknowledgement deadline. Manual selection uses its window deadline and the
configured fallback. Use returned `window_closes_at` and own gateway statuses;
miners introduce no local bidding deadlines or reserve estimates.

### `POST /api/runner/v1/offers`

`submit_offer(runner_id, offer)` sends exactly one of these bodies, with no
`schema_version`:

```json
{"runner_id": "runr_0123456789ab", "job_id": "job-1", "kind": "firm", "price_usd": 0.20}
```

```json
{"runner_id": "runr_0123456789ab", "job_id": "job-1", "kind": "limit", "estimate_usd": 0.20, "limit_usd": 0.30}
```

Firm prices are positive finite numbers. Limit amounts satisfy
`0 < estimate_usd <= limit_usd` and are finite. Booleans are refused locally.
The response contains `bid_id` and `window_closes_at`; a gateway may also include
`job_id` and `ack_by_at`. An over-reserve POST receives the same acknowledgement
shape, with no reserve feedback. Replacing terms uses a new POST and returns a
new `bid_id`; the old open bid becomes `replaced`.

### `GET /api/runner/v1/offers/{bid_id}`

`get_offer(bid_id)` reads an own bid only. Encode the id as one URL segment.
The response is `{"bid_id": str, "status": str, "award_id"?: str}`. Statuses
include `open`, `awarded`, `not_awarded`, `withdrawn`, `replaced`,
`no_capacity` and `award_lapsed`. An awarded offer that is never claimed within
three gateway poll intervals becomes terminal `award_lapsed`. The gateway records
one failed delivery commitment for that miner and comparable task shape. It creates
no receipt, customer debit, settled price, calibration or delivery-time sample.
That commitment survives any later delivery of the job. The job can receive its
next award or return to the queue. A miner may submit a fresh bid at identical terms
after observing its own `award_lapsed` status. Saved awards recover through this
explicit status; other award identity mismatches require recovery. Pending offer
POSTs and execution start/completion records retain their existing fences.
Losing offers become `not_awarded`. If the earlier winner
fails to claim, the gateway may reopen a `not_awarded` bid and award it at its
frozen terms. The gateway owns these status transitions. This route exposes
no competitor book, reserve, model identity or provider cost.

### `DELETE /api/runner/v1/offers/{bid_id}`

`withdraw_offer(bid_id)` withdraws an open own bid and returns
`{"bid_id": str, "status": "withdrawn"}`. Withdrawal is available before
award. Award commits the miner to its frozen terms; it must then claim and
execute without repricing or withdrawal. A nonopen withdrawal returns HTTP 409
with `error.type: "conflict_error"` and `error.message: "not_open"`.
A submission racing a closed window returns the same error type with
`error.message: "window_closed"`. Reconcile own status after either race.
Authentication and transport errors propagate.

Tested submit/read/withdraw example (`tests/test_offer_window.py` exercises these
calls through a standalone transport, including the device header):

```python
offer = {"job_id": "job-1", "kind": "limit", "estimate_usd": 0.20, "limit_usd": 0.30}
posted = client.submit_offer(runner_id, offer)
bid = client.get_offer(posted["bid_id"])
if bid["status"] == "open":
    client.withdraw_offer(posted["bid_id"])
```

The reference miner saves immutable terms and the returned bid identity under
`workdir_root/.ormas-recovery`, namespaced by gateway URL and runner, before
claiming. Keep this directory across restarts. Unchanged polls retain the same
bid; changed terms replace an open bid; a hook returning `None` withdraws it.
Saved public execution recovery always precedes offer reconciliation and new
claims. Polls refresh current nonlegacy bids for listed queue jobs and verify
`awarded_to_you` metadata; historical bids outside the queue remain saved.
After posting and status reads, window claims use `offers: []`. A returned
own lease is re-read and bound to its saved bid even when its award is absent
from the queue page. Saved awards can recover without fresh static pricing.
Before solver execution, the lease's `bid_id`, task/job identity and frozen
price/estimate/limit must match a saved own awarded bid. Unknown or mismatched
awards are refused. `award_id` is status/queue metadata; the lease carries
`bid_id`.

An ordinary queue entry does not identify whether its job uses a window. HTTP
409 `conflict_error` with message `not_offering` identifies a legacy job; that
job can use the existing per-job `/leases` offers API after an empty-offer claim
has reconciled awards. The default legacy route accepts on arrival within the
client's undisclosed spending limit. Saved window identities remain required
for every window award returned by either claim path.

Recovery limits: the gateway provides neither an own-offer listing nor a POST
idempotency key. A lost POST acknowledgement leaves a durable unresolved intent;
the miner refuses further claims until an operator recovers its own bid identity.
The queue also omits a window identity for reopened jobs. The reference miner
retains unchanged bid records and avoids automatic same-price resubmission
for such a job. It reconciles gateway-owned reopening and awards at the saved
terms. Explicitly changed terms can submit a new bid through the gateway;
the miner executes only an own award bound to its saved terms.

Offer windows preserve reserve privacy, task-shape-only queue disclosure,
Standard/Protected admission, public/private repository boundaries and miner
control over model routing. They change neither buyer pricing nor acceptance
and settlement requirements.

### `POST /api/runner/v1/leases`

Offer and claim through
`claim_task(runner_id, *, ask_usd=None, offers=None, claim_request_id=None)`.
Legacy jobs use accept-on-arrival pricing. Window jobs use the saved awarded
bid described above.

An offers request uses this body:

```json
{
  "schema_version": "ormas-runner-v1",
  "runner_id": "runr_0123456789ab",
  "offers": [
    {"job_id": "job-1", "kind": "firm", "price_usd": 0.20},
    {"job_id": "job-2", "kind": "limit", "estimate_usd": 0.20, "limit_usd": 0.30}
  ]
}
```

A firm entry has exactly `job_id`, `kind` and `price_usd`. A limit entry has
exactly `job_id`, `kind`, `estimate_usd` and `limit_usd`, both required, with
`0 < estimate_usd <= limit_usd`. Amounts must be positive finite numbers,
excluding booleans. Omitted jobs are declined.
`offers: []` declines new legacy jobs and may re-serve an eligible running
lease. It can also claim a window job already awarded to this runner.
`offers` is mutually exclusive with legacy `ask_usd` and `asks`; invalid fields,
amounts, kind/amount pairs, a missing estimate, an estimate above the limit or
duplicate jobs return `400 unknown_field:offers`.
`OrmasMinerClient.claim_task` validates `offers` locally and raises `ValueError`
before sending a malformed offer.

Legacy `ask_usd` and per-job `asks` requests keep their firm-price behaviour.
`ask_usd` is optional, finite and nonnegative, excluding booleans. The public
client exposes `ask_usd`; the gateway also accepts `asks`. Same-tenant claims
with no pricing field use the gateway default. A third-party (cross-tenant)
claim must send `ask_usd`, `asks` or `offers`, or gets `400 ask_required`.

| HTTP | Message | When |
|---|---|---|
| 400 | `ask_required` | A third-party claim omits all three pricing fields |

`claim_request_id` is optional. When present it must be 1..64 characters from
`[A-Za-z0-9_-]`; anything else returns `400 invalid_claim_request_id`. Null or
omitted adds no lease field. A valid id is echoed on a fresh lease. A re-served
lease echoes the originally stored id (null if none), without replacing it.

**Response**:
- `204 No Content` — nothing queued for this miner right now.
- `200` — `{"lease": <TaskLease wire>, "draft": <TaskDraft wire>}`.

Below its registered capacity, a runner receives a new lease. At capacity, the
claim returns an already-running lease (same lease token, refreshed draft)
instead of another one. An empty offers list may also re-serve a running lease
below capacity. `claim_request_id` is on the payload only when this request sent
a valid id, and then it is the original stored value.

Offer-specific fields from a successful limit claim (`lease` projection):

```json
{
  "outcome_price_usd": 0.30,
  "bid_id": "bid_0123456789ab",
  "offer_kind": "limit",
  "estimate_usd": 0.20,
  "limit_usd": 0.30
}
```

A firm claim has `offer_kind: "firm"`, its price as `outcome_price_usd`, and
`estimate_usd: null` and `limit_usd: null`. Legacy responses omit these offer fields.

**`TaskLease`**: `lease_id` (also the *lease token* used in every subsequent
call for this task), `task_id`, `expires_at`, `selected_cell`, `provider_pin`,
`fallback_policy`, `hold_ref`, `now`, `outcome_price_usd` (the accepted firm
price or limit), `claim_request_id` (present only when this request sent a valid
id; on a re-served lease the value is the original stored id, or null), `bid_id`
(nullable accepted-offer id), `offer_kind` (`firm` or `limit`, defaults to `firm`),
`estimate_usd` (nullable; the accepted expected charge for a limit offer, null
for firm), `limit_usd` (nullable; the accepted ceiling for a limit offer, null for
firm). Legacy leases without offer fields decode with `bid_id=null`,
`offer_kind="firm"`, `estimate_usd=null` and `limit_usd=null`.

**`TaskDraft`**: `task_id`, `runner_id`, `repo_id`, `base_commit`, `brief` (the
task description), `verify_command`, `allowed_paths`, `budget_usd`,
`work_packet` (the full frozen packet — task, acceptance criteria, etc.),
`work_packet_sha256`, `attempt`, `parent_job_id` (non-empty on a repair),
`repair_findings`, `repair_evidence` (present only on a repair attempt).

### Public execution and acceptance additions (September 23 development candidate)

These additions describe the candidate source contract, not a production rollout.
The older credential/toolchain route below remains separate. See
[Public tasks](PUBLIC_TASKS.md) for supported profiles and operator setup.

A public `ormas.work-packet.v2` preparation includes:

- `execution_environment`: `outcomes.execution-environment.v1`, with
  `catalog_digest`, the exact catalog `profile`, path-to-SHA256 `lock_files` and
  `acceptance_files`, and `service` only for the HTTP profile. The service has exactly
  `argv` and `port`; the allowed shape is checked before base execution.
- `execution_requirements`: the catalog/profile and environment/verifier digests,
  protocol versions, public visibility, languages, OS/architecture, runtimes,
  browser, packages, services, network policy, workspace mode and resource limits.
  Version 2 also binds the `github-artifact-v1` publication protocol. Every required
  task cell must match; language requirements are not an any-one-match hint.
- `verifier_profile` and `verify_base`: the compiled verifier identity and an
  `outcomes.base-preflight.v2` assertion-failing base bound to the same base commit,
  environment and verifier. Missing/setup/timeout failures cannot stand in for it.

The gateway and SDK recompile and compare the executable/configuration against the
frozen catalog. They do not trust a client-supplied wrapper merely because its shape
looks valid. Earlier admitted catalog versions remain loadable for in-flight jobs.

`TaskDraft.acceptance_contract` is optional for legacy drafts and required on this
public publication path. It has the exact keys `schema_version`, `policy`, `miner`,
`validators`, `claim_nonce`, with schema `ormas.public-acceptance-contract.v1`:

- `policy` uses `ormas.public-acceptance-policy.v1` and has exactly
  `required_validators`, `catalog_digest`, `profile_id`, `protocol`, `liveness_s`,
  `timeout_s`, `max_concurrent_assignments`, plus `schema_version`. Counts and times
  are positive integers; concurrency is exactly one.
- `miner` has `subject_id`, `operator_id`, `qualification_id`, `credential_id`,
  `miner_id`. A validator record has the same first four fields; its credential is
  a 64-lowercase-hex Ed25519 public key.
- The validator list length equals `required_validators`. Subjects, credentials
  and operators are distinct, and every validator operator differs from the miner
  operator. The nonblank `claim_nonce` binds the selected capacity reservation.

Registration and heartbeat do not grant qualification. Claims require current
credential-bound qualification for the exact profile/catalog, matching task cells
and available checker capacity under the frozen policy. This includes operator-miner claims.

The explicit operator-run alpha uses `ormas.public-acceptance-contract.v2` with
the same outer keys and `ormas.public-acceptance-policy.v2`. Its policy adds exactly
`verification_mode: "operator-run"` and a nonblank `validator_operator_id` to the
v1 policy fields, sets `protocol` to the v2 contract schema, and requires exactly
one validator. The validator must belong to that frozen operator; the miner may
belong to the same or another qualified operator. V1 contracts cannot contain this
exception or these extra keys. The complete v2 contract is signed with the rest
of the assignment evidence.

V2 requires explicit `task:acceptance/operator-run-v2` miner opt-in and separately
approved v2 qualifications. V3 (`ormas.public-acceptance-policy.v3` /
`ormas.public-acceptance-contract.v3`) keeps the same operator-run terms, moves checker
capacity to an operator-wide slot total on the qualification, and uses its own cells,
`task:acceptance/operator-run-v3` and `task:acceptance/independent-v3`, so a miner that
cannot read a v3 contract is never matched to one. `api.ormas.ai` runs
`OUTCOMES_PUBLIC_ACCEPTANCE_POLICY=operator-run-v3`. The gateway checks the capability before any bid,
at atomic claim and when resuming a lease. Alpha configuration is explicit:
`OUTCOMES_PUBLIC_ACCEPTANCE_POLICY=operator-run-v2` and
`OUTCOMES_PUBLIC_VALIDATOR_OPERATOR_ID=<qualified operator id>`. The default is
`independent-v1`; blank/unknown modes or an incomplete owner/quorum configuration
refuse admission. Existing queued and running jobs retain their original policy.
The client job response exposes that frozen policy. This is policy-class consent
through standing asks or per-job offers. The queue envelope omits the exact
policy; that policy arrives in the claimed draft. Exact pre-offer policy
negotiation remains planned (see [public task limits](PUBLIC_TASKS.md)).

Validator assignments carry an `execution_contract` with exactly `schema_version`,
`work_packet_sha256`, `execution_environment`, `execution_requirements`,
`verifier_profile`, `verify_base`. The schema is `outcomes.validation-contract.v2`;
the packet hash is 64 lowercase hex characters. Assignments also carry the frozen
`acceptance_contract`. Both objects join the canonical signed evidence alongside
the existing repository/commit/verifier/scope fields. Validators reject malformed
or mismatched projections before repository access; they do not need the task's
private text, prices or miner model choices.

Public validators require base exit 86, then classify the result's completed
assertions as accept/reject. Runtime setup and timeout are neutral. A public
completion cannot reduce its frozen checker count or substitute the miner's own
verification for the validator's decisions. Pending or missing decisions retain
the agreed count and deadline.

### Public artifact publication

`POST /api/runner/v1/leases/{task_id}/publication` takes the existing authenticated
miner token and headers `X-Ormas-Runner-Id`, `X-Ormas-Lease-Token`, `Content-Length`,
with `Content-Type: application/vnd.ormas.public-artifact.v1`.

The body is an eight-byte unsigned big-endian header length, canonical JSON header,
then the concatenated file bytes. The header is at most 1 MiB and has exactly
`schema_version: "ormas.public-artifact.v1"`, `base_commit`, `tree_sha`, `files`.
JSON uses sorted keys, compact separators and ASCII escapes. File entries are sorted
by path and have exactly `path`, `op`, `mode`, `byte_len`, `sha256`. An `upsert` has
mode `100644` or `100755`, its byte count and lowercase SHA256; a `delete` has null
mode/hash and zero bytes. Upsert bodies follow that same order. The artifact digest
is SHA256 over the entire framed body. A successful JSON response includes the
canonical `result_ref`, `result_commit`, `tree_sha` and `artifact_sha256`.
The SDK's `outcomes_support.build_public_artifact` is the executable serializer;
only committed regular files within allowed scope and the frozen publication bounds
are admitted. Raw Git packs, symlinks and submodules are not this protocol.

The gateway holds the saved repository write token, creates the canonical
`refs/heads/ormas/job/<task_id>` result and verifies its exact tree and sole base
parent. For both miners and validators, public-repository jobs clone anonymously;
private-repository jobs use a per-job read credential, with the Standard/Protected
service level stated separately. A write credential is absent from their draft
or assignment. The SDK verifies publication by fetching
the canonical branch before completion. Replays bind the same artifact and result;
an ambiguous publication cannot silently become a different commit. Completion
still uses the existing authenticated endpoint and gateway receipt authority.

The SDK journals the full lease, including its offer id, kind and ceiling, the
bounded artifact and exact completion before external effects. The saved terminal
includes the settled price; replay uses it without calling pricing callbacks again.
After a process restart it resolves the journal before claiming another task;
it does not call the solver again for that retained lease. Settling retains
the completion; terminal acknowledgement requires a receipt, and a lost lease
retains a tombstone. An interruption before the artifact was saved reports aborted
with an unknown outcome. Corrupt/unsafe journal state refuses further work rather
than guessing. No journal creates a second queue or settlement receipt.

### Legacy toolchain and repository credentials

`work_packet` may carry an optional **`toolchain`** block the client declared at
prepare time (protocol addition 2026-09-14):

```json
"toolchain": {"kind": "python", "python": "3.12",
              "pip_install": ["-e", ".", "pytest>=8,<9"], "lock_paths": ["pyproject.toml"]}
```

It names the environment the verify command needs. `kind` is `python` (the only
kind today); `python` is `3.X`; `pip_install` is a list of PEP 508 requirement
specifiers and/or `-e <relative in-repo path>` pairs — no other pip options, no
URLs, no `-r` files, no paths outside the repo (the gateway refuses anything
else at enqueue); `lock_paths` is informational. A miner that honours it
creates `<checkout>/.venv` with `python<X.Y>`, runs
`python -m pip --no-input install <pip_install>` in the checkout, and runs the
verify with `.venv/bin` first on `PATH`. The same block is served to validators
on their assignment and is part of the signed evidence digest, so miner and
validator provision the identical declared environment. A packet without a
toolchain is verified exactly as before.

`TaskDraft` may also carry **`repo_credential`** (protocol addition 2026-09-14) when the
claiming miner has no local bind for the project's repository:

```json
"repo_credential": {"kind": "ssh_deploy_key", "private_key": "-----BEGIN OPENSSH PRIVATE KEY-----…",
                    "fingerprint": "SHA256:…"}
```

It is the project's deploy key, served once, in the claim response, over the authenticated
runner channel; it is absent when a bound `repo_id` already covers the repository. The reference
skeleton clones `draft.repo_url` and pushes the result branch to it with that key, materialised
as a `0600` file only for the duration of each git call and removed afterwards. Never persist,
copy or log it; the gateway never re-serves it on list or status routes. A per-job, single-repo,
read-scoped credential (GitHub App token) is the planned successor.

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
`capture`, `failure_evidence`, `pr_url`, `pr_error`, `evidence`, `effort`.

**`effort`** is voluntary disclosure of counts only, with no model names or
model/provider/vendor identity. When present, it must be an object containing
exactly these five required keys:

```json
{"attempts": 2, "model_turns": 8, "models_used": 2, "output_tokens": 40, "total_tokens": 100}
```

Every value must be a nonnegative integer; booleans are rejected. Unknown keys
are rejected, including the existing forbidden/capture field vocabulary and
`model`, `model_id`, `provider`, `vendor`, `models`. Omit the block to leave
`effort: null` on the receipt and job status: **not disclosed**. Explicit `null`
is invalid. The SDK's `complete_task(..., effort=...)` validates the block locally
(`ValueError` before any request) and sends it only when its argument is supplied.
The first accepted completion fixes the stored effort; a later replay of the same
lease (`410`) never changes it, and a replay carrying an invalid block answers `400`.

Invalid effort returns HTTP 400 before any state change, including completion
auditing. Correct the block and resend. The response is
`{"error": {"type": "invalid_request_error", "message": "effort:<reason>"}}`:

| Message | When |
|---|---|
| `effort:object_required` | The block is not an object |
| `effort:forbidden_field:<key>` | A key belongs to the forbidden/capture vocabulary or names model/provider/vendor identity |
| `effort:unknown_field:<key>` | Any other extra key is supplied |
| `effort:missing_field:<key>` | A required count is absent |
| `effort:non_negative_integer_required:<key>` | A count is boolean, non-integer or negative |

**`TaskReceipt`**: `lease_id`, `generation_ids` (≤16 entries, each
`^[A-Za-z0-9_-]{1,64}$`), `actual_provider`, `model` (must match
`^[a-z0-9./:-]{1,80}$`), `prompt_tokens`, `completion_tokens`,
`cache_read_input_tokens`, `cache_creation_input_tokens`, `reasoning_tokens`,
`upstream_cost_usd` (nullable), `finish_reason` (nullable),
`metering_complete` (bool). `child_model_ids` is an optional list (defaults to
empty) for operator diagnostics. Miners do not need to disclose child models or
their per-job routing to qualify for network acceptance.

**`TaskTerminal`**: `lease_id`, `verification_state` (one of
`verified/failed/scope_violation/setup_failure/repair_refused/publish_failed/budget_exceeded/aborted`),
`result_ref` (must be exactly `refs/heads/ormas/job/<task_id>` or
`local:ormas/job/<task_id>` — anything else is rejected), `settlement_state`
(free-form, not validated server-side today), `rating` (nullable, `"1"`–`"5"`
as a string), `result_commit` (40-hex sha, required non-empty when
`verification_state == "verified"`). `settled_price_usd` is optional and omitted
when null. A verified limit delivery supplies a finite value from zero through
`limit_usd`; the client is charged exactly that price on accepted delivery.
Firm/legacy terminals and non-deliveries omit `settled_price_usd`.

For a verified limit delivery with a $0.30 ceiling, a terminal example is:

```json
{
  "lease_id": "lease-1",
  "verification_state": "verified",
  "result_ref": "refs/heads/ormas/job/job-2",
  "settlement_state": "unset",
  "rating": null,
  "result_commit": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "settled_price_usd": 0.05
}
```

The first valid settlement is atomically pinned while the lease is running.
Retries send that same value. Completed replay returns the stored receipt with
`410` before validating a new settlement value.

All five settlement refusals are HTTP 400 `invalid_request_error`:

| Message | When |
|---|---|
| `settled_price_not_allowed:settled_price_usd` | A firm/legacy terminal supplies the field |
| `settled_price_required:settled_price_usd` | A verified limit delivery omits it |
| `settled_price_invalid:settled_price_usd` | The value is nonnumeric, boolean, nonfinite or negative |
| `settled_over_limit:settled_price_usd` | The value exceeds the limit, or the stored ceiling is invalid |
| `settled_price_mismatch:settled_price_usd` | A running lease supplies a different value after pinning |

Example refusal:

```json
{"error": {"type": "invalid_request_error", "message": "settled_over_limit:settled_price_usd"}}
```

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
  projected receipt excludes `pricing` and forbidden fields. It includes
  `settlement`, `customer_billed_usd`, `debit_status`, `upstream_cost_usd`,
  and `effort` (the disclosed counts, or `null` for not disclosed).
  Only limit-job receipts also show `offer_kind`, `estimate_usd`, `limit_usd`
  and `settled_usd`. A paid limit receipt's fields for the terminal above include
  `{"offer_kind": "limit", "estimate_usd": 0.20, "limit_usd": 0.30, "settled_usd": 0.05}`.
- `202 {"status": "settling", "retry_after_s": N}` — settlement is still in
  flight (e.g. a debit hasn't confirmed); re-send the identical `complete`
  call after `retry_after_s`. This is safe to retry — the server recognizes an
  already-recorded receipt and replays it rather than double-settling.
- `410` — the task already has a terminal outcome (a previous `complete` call
  landed); body is the same shape as `200`, safe to treat as authoritative.

Settlement derivation (informational — you do not construct this, the server
does): a verified in-scope delivery with a valid commit can proceed to acceptance.
Validator-gated jobs await their frozen policy; the legacy same-tenant path
can settle directly. `verified` but
`scope_ok=false` → `"no_delivery"`/`scope_violation`; `verified` but an invalid
commit → `"no_delivery"`/`publish_failed`; any other `verification_state` →
`"no_delivery"` with a failure class derived from the state (or your own
`capture.failure_class`, if it's a recognized value for that state).

### Pricing callbacks in the reference skeleton

`MinerConfig.offer_fn` receives each `{job_id, created_at, envelope}` queue entry.
Return a wire offer (`{job_id, kind: "firm", price_usd}` or
`{job_id, kind: "limit", estimate_usd, limit_usd}`),
or `None` to decline. The skeleton lists the queue, collects those offers and
claims with them. Only a queue HTTP 404 without `error.type` enables the legacy
`ask_usd` fallback, remembered for the process. A 404 carrying
`error.type == "not_found_error"` is raised to the operator, as described under
[Queue](#get-apirunnerv1queuerunner_idassigned-id); other errors also surface.
Leaving both callbacks unset preserves the legacy `ask_usd` flow.

`MinerConfig.settle_fn(lease, result)` supplies the price for a verified limit
delivery. It must be deterministic, must not raise, and must return a finite
nonnegative number, excluding booleans. The skeleton caps it at `lease.limit_usd`;
when unset, it settles at the limit. An invalid return or a raising hook is
never silently repriced. On the public (bounded-packet) path the published work
is held for recovery, no completion is sent, and every later poll re-raises until
the hook returns a valid price; only a task without a public packet completes as
`failed`. It sends no settled price for firm leases
or non-deliveries. Recovery replays the saved completion without solving or repricing.

Set your estimate from the expected cost of your usual recovery chain plus
margin, and your limit at the worst-case chain. Settle from actual metered cost
of every attempt plus margin, within that ceiling. You bear
any loss above it. See the made-up example in [Economics](economics.md).

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
  the reference solver (`reference_solver.py`) legitimately reports all-zero
  usage and `metering_complete=True` because it makes no model call. That
  evidence comes from its own `SolveResult`.
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
  stripped into the environment. Both routes run the remaining argv with no shell
  in a credential-free environment: `PATH`, scratch `HOME`, `LANG`, plus the
  packet's explicit `NAME=value` assignments. Ambient provider keys and secrets
  are omitted. The public profile uses a digest-pinned OCI image with
  `--network none`, `--cap-drop ALL`, `--read-only` and resource limits. When the draft's
  `work_packet.toolchain` is present, the reference skeleton does not yet
  provision it (the private miner does); a reference miner serving such packets
  should provision `.venv` as described under `TaskDraft` before verification.
  `verification_state` is `"verified"` only when the exit code is 0 **and**
  `scope_ok`; otherwise `"failed"`. The exit code is recorded in
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

## Source and release status

- **`ask_usd` is live since `gateway-2026.09.11`; the queue route, offers and
  limit settlement are live since `gateway-2026.10.03`.** Against an older gateway
  the queue returns 404 without `error.type`, and the skeleton falls back to `ask_usd`.
- **History-based ranking is planned.** See [Offer ranking](#offer-ranking) for
  Phase 1 selection and the next phase.
- **A `paid` receipt on `api.ormas.ai` today reflects one operator-run validator.**
  The reference checking component ships here. Independent validator admission
  and the combined public validator service remain release work; see
  [Public tasks](PUBLIC_TASKS.md).
