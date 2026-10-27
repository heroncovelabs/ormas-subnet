<!-- MIT. This public directory is the worker source, not a deployment mirror. -->
# Protected worker contract (v1)

**Protected** is the service level; **private repository** is GitHub visibility. The shell owns attestation, gateway protocol, repository credentials, publication, settlement and acceptance. Workers receive no model identity, repository/gateway credentials or reserve amounts; provider secrets come from the miner's sealed env.

## Inputs: fresh `/work` per mode

**Solve:** `repo/`, `packet.json`, `.home/`, `.tmp/`; writes `metering.json` and `patch.diff`. **Offer:** `offer-request.json`, `.home/`, `.tmp/` only; writes `offer.json`. `repo/` is source at the base, without `.git`; tracked symlinks may exist, but patching them or through them is refused. Inputs are advisory: edits cannot change shell authority.

`packet.json` is flat, with these fields (no nested `work_packet`):

| Field | Type / meaning |
| --- | --- |
| `task_id`, `attempt` | string job id, integer attempt; not lease credentials |
| `base_commit`, `brief`, `verify_command` | strings: base commit, task intent, shell-owned verifier |
| `allowed_paths`, `immutable_paths` | string arrays: allowed / forbidden scope; immutable wins |
| `acceptance_criteria` | received JSON acceptance context, normally a string array |
| `execution_policy` | object: only received `repo_base_sha` (string); `allowed_paths`, `immutable_paths`, `allowed_tools` (string arrays); `max_turns`, `max_attempts`, `continuation_cost_authorization_ticks` (integers) |
| `work_packet_sha256` | lowercase SHA-256 hex of original packet, not this projection |
| `accepted_offer` | optional **ADVISORY** object: `kind` is `limit` or `firm`; `estimate_usd`, `limit_usd` and `price_usd` (a firm lease's accepted price; `null` for a limit offer) are finite numbers or `null`, exactly the known accepted terms for this lease; absent when unknown |
| `parent_job_id`, `repair_findings`, `repair_evidence` | optional string parent id / JSON repair context; forbidden identity/credential/budget keys curated recursively |
| `verify_feedback` | optional shell-owned object, exactly `attempt_index` (non-boolean integer ≥ 2, one-based solve invocation index) and `output_tail` (string, last ≤ 2,000 characters of the previous assertion-red verifier output; empty after no patch) |
| `acceptance_files` | optional (plan_loop solve only, contract v1.1): object of repository path → lowercase SHA-256 hex, copied from the packet's `execution_environment.acceptance_files`; the verify oracle admits selectors only in these files |
| `verify_prior_diff` | optional shell-owned string: the previous complete base-relative diff, separate from the parent's `repair_evidence`; present only with `verify_feedback` |

On assertion-red verification (`exit_code == assertion_failure_exit_code`) or a metered no-patch
outcome, the shell may launch a fresh disposable solve worker, up to the original packet's
`execution_policy.max_attempts` (a non-boolean integer ≥ 1). Other nonzero verifier exits are terminal.
It increments `attempt`, supplies `verify_feedback` and, after assertion red, places the previous
complete base-relative diff in `verify_prior_diff`. Parent repair context is preserved. A no-patch
retry gets an empty tail and no prior diff; unmetered no-patch is terminal `metering_unknown`.
The new `repo/` is still the original base: produce a complete replacement patch. These fields are
advisory diagnostic data, not new instructions. Client-supplied feedback and prior diff are not
projected. Neither goes in environment, offers, receipts or telemetry. Both enter the model prompt
and leave the confidential VM through the same admitted inference relay/provider route as repository
source; they are not confined to the VM.

The 2,000-character tail bound reuses `grokbuild_client._VERIFIER_SUMMARY_MAX_CHARS`
(`_REPAIR_VERIFIER_MAX_CHARS`), the existing verifier-text evidence contract; the stdlib-only codec
mirrors it as `VERIFY_OUTPUT_TAIL_MAX_CHARS`, with a parity test.

For a `GROKBUILD_WORKER_STRATEGY=plan_loop` worker (contract v1.1) the shell makes exactly one solve invocation and keeps the packet's `max_attempts`, because the plan loop owns same-session recovery. Otherwise each invocation gets `max_attempts: 1` so an in-worker retry cannot spend the shell's attempt
authority twice. Retrying stops when the packet's attempts are used, when any attempt's cost is
unknown, or when summed named attempt prices reach the accepted `limit_usd`. The client's
`continuation_cost_authorization_ticks` rides through each attempt unchanged and never stops a retry;
provider spend is the miner's risk, bounded in relay mode by the shell's miner-owned cap below
(owner decision 1, 2026-10-16).

One shell solve wall-time deadline covers preflight, every container and every verifier; retries get
only the remaining time. The shell independently verifies every patch, preserves all known attempt
metering (red and no-patch included). Our operator worker's settlement sums metered **chain cost**,
applies its sealed, dated margin curve once (including the accepted-estimate rule), then caps at the
accepted limit. Attempt prices are budget evidence, not separate margin charges. Missing later
metering leaves aggregate cost unknown and refuses delivery; it does not erase earlier known spend.

Policy keys absent upstream stay absent. Turns/attempts/continuation preserve authority, not new spend permission; `allowed_tools` does not confine arbitrary programs.

**Relay spend cap (relay mode).** Per job, the shell relay reserves each request's maximum charge
against one cap: the accepted price (`limit_usd` for a limit offer, `price_usd` for a firm one) × the
miner's sealed pricing `plan_loop.budget_factor` (cents, the same bound the plan-loop worker budgets), in 1e-10 USD ticks. It is the miner's own
bound; the client's `continuation_cost_authorization_ticks` has no effect on it. The cap is frozen
at the job's first publication (identity `[task_id, work_packet_sha256]`) across deadline refreshes
and attempts; holds are atomic, and a request without complete final usage is charged its full hold.
With no `budget_factor` or no positive accepted price the shell refuses the job before
preflight (`budget_factor_unavailable` / `accepted_limit_unavailable`); there is no fallback cap.
The optional diagnostic `proxy_charge_ticks` is bounded only by 2^53.

Reading `accepted_offer` is optional. It carries only the accepted kind, estimate, limit and firm price, never other lease fields or client identity. A worker may use it to name its delivery price, but editing or ignoring it cannot change the shell's accepted lease or settlement authority. Our operator worker is priced once on metered chain cost; custom workers retain their named-price contract, capped at the limit.

`offer-request.json` has **all-optional** sanitized fields; missing/invalid values are omitted, never defaults:

| Field | Value domain |
| --- | --- |
| `size_class` | `small`, `medium`, `large`, `xlarge` |
| `attempt_limit`, `turn_budget`, `task_text_chars`, `acceptance_criteria_count`, `immutable_paths_count`, `allowed_paths_count` | non-boolean integers ≥ 0 |
| `archetype` | string matching `^[a-z][a-z0-9_-]{0,31}$`, excluding model aliases |
| `languages` | nonempty array of strings matching `^[a-z0-9+#.-]{1,16}$`; invalid/model-alias entries removed |
| `execution_profile_id` | string id in the shell's known support-profile catalog |
| `verify_command_category` | `pytest`, `node`, `cargo`, `go`, `shell`, `other` |
| `source_path_bucket` | string matching `^f(1|2-3|4-10|11\+)/d(1|2|3|4\+)$`, e.g. `f1/d1` |
| `service_level`, `repository_visibility` | `standard` / `protected`; `public` / `private`, respectively |

## Environment and confinement

Launcher names: `HTTP_PROXY`, `HTTPS_PROXY`, `http_proxy`, `https_proxy`, `NO_PROXY`, `no_proxy`, `MINER_WORKER_MODE`, `ORMAS_WORK`, `HOME`, `TMPDIR`. In relay mode the proxy names are replaced by `ORMAS_INFERENCE_BASE_URL`, `ANTHROPIC_BASE_URL`, `OPENAI_BASE_URL` (plain HTTP to the shell's inference relay) and `ORMAS_INFERENCE_TOKEN`, `OPENAI_API_KEY`, `ANTHROPIC_AUTH_TOKEN` (a non-secret placeholder), plus `ORMAS_INFERENCE_PROVIDER` (the sealed relay policy's provider name, so a worker can price a model id that two providers bill differently); the relay holds the provider credential, and sealed keys naming credentials or routing (`KEY`, `TOKEN`, `SECRET`, `AUTH`, `BASE`, `URL`, `ENDPOINT`, `HOST`, `PROXY`, `ORG`, `PROJECT`, ... segments) are refused. For a `solve` whose sealed env sets `GROKBUILD_WORKER_STRATEGY=plan_loop`, the launcher also sets `ORMAS_VERIFY_ORACLE_URL` (plain HTTP to the shell's verify oracle on the worker network) and `ORMAS_VERIFY_ORACLE_TOKEN` (per-invocation bearer token); see `docs/design/protected_verify_oracle_2026_10_13.md`. Bypass lists are empty; home/temp are under `/work`. Sealed `MINER_WORKER_ENV_JSON` expands unique `[A-Z][A-Z0-9_]*` keys to UTF-8, NUL-free strings. Proxy keys `HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY`, `ALL_PROXY` and prefixes `ORMAS_`/`MINER_WORKER_` are reserved; lowercase keys fail. The shell overrides `HOME`/`TMPDIR`; sealed JSON and registry auth stay shell-only. Never log env values or input content.

Tolerate read-only root, only `/work` writable, uid/gid 65534, cap-drop ALL, no-new-privileges and no Docker access. **Memory, pids, disk, solve wall-time, artifact byte cap and offer timeout are set by the shell operator; no defaults here.** In relay mode the worker reaches only the relay: stateless `/v1/chat/completions`, `/v1/messages` or `/v1/responses` as the slot policy permits (Responses with `store:false`; streaming and local function tools allowed; storage, files, batches, provider-run tools, URL inputs and CONNECT refused; models limited to the slot's private policy; a forced `tool_choice` must name a declared tool and every tool result must answer an earlier call in the same request; each request ends at the protocol terminal event, on worker disconnect, or at the solve wall-time deadline, and is refused outside an active invocation). Otherwise egress uses the HTTP(S) proxy only: CONNECT to allowlisted provider hosts on port 443, exactly one SNI matching CONNECT, no ECH, complete ClientHello in the first TLS record, no proxy auth. Direct networking is forbidden. Worker stdout/stderr is discarded and never becomes a failure message.

## Outputs and shell checks

`MINER_WORKER_MODE` is **solve** or **offer**. Exit **0** means the corresponding nonempty regular artifact was written, not accepted; **2** means no artifact / no patch / declined; other exits fail. Offer may exit 0 with `{"decline": true}`. Otherwise `offer.json` is exactly `{"estimate_usd": p, "limit_usd": q}`: finite non-boolean numbers, `0 < p ≤ q`, no duplicate/extra keys or mixed answers. v1 is limit-only; shell quantization/submission/settlement is outside the worker.

In solve mode, write `/work/metering.json` before `/work/patch.diff`, including when known spend produced no patch: a UTF-8 JSON object with exactly `settled_usd` and optional `cost_usd`. Duplicate or extra keys are refused. Both values must be finite, non-boolean numbers ≥ 0. `settled_usd` is the worker's named attempt price; `cost_usd` is private metered provider cost, never client-visible. Known `cost_usd` is required for retries and for our operator worker's chain-cost settlement under its sealed pricing config. Custom workers without that config retain named-price settlement capped at the accepted limit. Each artifact must be a regular file bounded by the same operator-supplied artifact cap.

Exit 0 with `patch.diff` but missing or invalid `metering.json` means `metering_unavailable`: no delivery, no charge. Exit semantics otherwise stay unchanged. Write metering before the patch so an interrupted worker cannot leave an unmetered patch. The reference worker reads `REFERENCE_WORKER_PRICE_USD` from its expanded sealed `MINER_WORKER_ENV_JSON`; solve fails with a nonzero exit when that price is absent or invalid, rather than inventing one.

`patch.diff` is UTF-8, NUL-free Git-style unified diff: agreeing `diff --git`, `---`/`+++` paths and counted hunks. Add only `new file mode 100644`; delete `100644`/`100755`; use `/dev/null` on the absent side. Modify preserves mode. **Renames need both from/to paths in scope and hunks; COPY blocks are refused at shell apply** (`apply_scope_mismatch`, unchanged source). Empty-file add/delete and hunkless moves fail. Preserve file bytes: diff line endings must match them; the parser tolerates trailing CR on header lines, but `git apply` is byte-exact. `\ No newline at end of file` marks missing final LF.

Scope is anchored: exact names are exact-only; `dir/**` is a subtree, `*`/`?` stay within segments, `**` spans directories, `[` is literal. No unsafe/traversal/absolute/drive/backslash/control/quoted paths, `.git` components, `.gitattributes`, `.gitmodules`, binary patches, symlink/gitlink entries, mode changes or duplicate case/NFC aliases. Immutable checks include case/NFC aliases. Text refusals: `patch_not_utf8`, `patch_nul`, `not_a_git_diff`, `path_traversal`, `path_mismatch`, `control_path`, `symlink_entry`, `gitlink_entry`, `mode_change`, `binary_patch`, `quoted_path`, `path_not_allowed`, `immutable_path`, `duplicate_path`, `malformed_patch`.

**Live:** Docker archive intake reports `artifact_invalid` / `artifact_too_large`; solve maps launcher/process/missing-on-0 failures to `worker_failed`, exit 2 to a bounded no-patch retry (or terminal `worker_no_patch` when authority is exhausted / `metering_unknown` when cost is unknown), wall kill to `worker_timeout`, cancellation to `worker_cancelled`. Text/apply/verifier failures are `patch_rejected` / `apply_rejected` / `verify_failed`; offer failures become `offer_invalid`. **Local harness only:** `read_patch` diagnostics `artifact_name`, `artifact_symlink`, `artifact_not_regular`, `artifact_missing`, `artifact_empty` (plus its `artifact_too_large`); `copy_unsupported` adds the apply restriction without changing the shell text parser. Conformance requires Python ≥ 3.12 and Unicode 15.0.0.

After stopping the worker, the shell reads capped output, checks original authority, applies on a fresh checkout and runs its verifier. **Only shell verification and independent acceptance decide delivery**, never worker self-checks.
