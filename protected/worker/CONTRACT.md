<!-- MIT. This public directory is the worker source, not a deployment mirror. -->
# Protected worker contract (v1)

**Protected** is the service level; **private repository** is GitHub visibility. The shell owns attestation, gateway protocol, repository credentials, publication, settlement and acceptance. Workers receive no model identity, repository/gateway credentials or reserve amounts; provider secrets come from the miner's sealed env.

## Inputs: fresh `/work` per mode

**Solve:** `repo/`, `packet.json`, `.home/`, `.tmp/`; writes `patch.diff`. **Offer:** `offer-request.json`, `.home/`, `.tmp/` only; writes `offer.json`. `repo/` is source at the base, without `.git`; tracked symlinks may exist, but patching them or through them is refused. Inputs are advisory: edits cannot change shell authority.

`packet.json` is flat, with these fields (no nested `work_packet`):

| Field | Type / meaning |
| --- | --- |
| `task_id`, `attempt` | string job id, integer attempt; not lease credentials |
| `base_commit`, `brief`, `verify_command` | strings: base commit, task intent, shell-owned verifier |
| `allowed_paths`, `immutable_paths` | string arrays: allowed / forbidden scope; immutable wins |
| `acceptance_criteria` | received JSON acceptance context, normally a string array |
| `execution_policy` | object: only received `repo_base_sha` (string); `allowed_paths`, `immutable_paths`, `allowed_tools` (string arrays); `max_turns`, `max_attempts`, `continuation_cost_authorization_ticks` (integers) |
| `work_packet_sha256` | lowercase SHA-256 hex of original packet, not this projection |
| `parent_job_id`, `repair_findings`, `repair_evidence` | optional string parent id / JSON repair context; forbidden identity/credential/budget keys curated recursively |

Policy keys absent upstream stay absent. Turns/attempts/continuation preserve authority, not new spend permission; `allowed_tools` does not confine arbitrary programs.

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
| `reserve_source` | `project_pin` only: existing provenance marker, not dollars |

## Environment and confinement

Launcher names: `HTTP_PROXY`, `HTTPS_PROXY`, `http_proxy`, `https_proxy`, `NO_PROXY`, `no_proxy`, `MINER_WORKER_MODE`, `ORMAS_WORK`, `HOME`, `TMPDIR`. Bypass lists are empty; home/temp are under `/work`. Sealed `MINER_WORKER_ENV_JSON` expands unique `[A-Z][A-Z0-9_]*` keys to UTF-8, NUL-free strings. Proxy keys `HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY`, `ALL_PROXY` and prefixes `ORMAS_`/`MINER_WORKER_` are reserved; lowercase keys fail. The shell overrides `HOME`/`TMPDIR`; sealed JSON and registry auth stay shell-only. Never log env values or input content.

Tolerate read-only root, only `/work` writable, uid/gid 65534, cap-drop ALL, no-new-privileges and no Docker access. **Memory, pids, disk, solve wall-time, artifact byte cap and offer timeout are set by the shell operator; no defaults here.** Egress uses the HTTP(S) proxy only: CONNECT to allowlisted provider hosts on port 443, exactly one SNI matching CONNECT, no ECH, complete ClientHello in the first TLS record, no proxy auth. Direct networking is forbidden. Worker stdout/stderr is discarded and never becomes a failure message.

## Outputs and shell checks

`MINER_WORKER_MODE` is **solve** or **offer**. Exit **0** means the corresponding nonempty regular artifact was written, not accepted; **2** means no artifact / no patch / declined; other exits fail. Offer may exit 0 with `{"decline": true}`. Otherwise `offer.json` is exactly `{"estimate_usd": p, "limit_usd": q}`: finite non-boolean numbers, `0 < p ≤ q`, no duplicate/extra keys or mixed answers. v1 is limit-only; shell quantization/submission/settlement is outside the worker, with no metering handoff defined here.

`patch.diff` is UTF-8, NUL-free Git-style unified diff: agreeing `diff --git`, `---`/`+++` paths and counted hunks. Add only `new file mode 100644`; delete `100644`/`100755`; use `/dev/null` on the absent side. Modify preserves mode. **Renames need both from/to paths in scope and hunks; COPY blocks are refused at shell apply** (`apply_scope_mismatch`, unchanged source). Empty-file add/delete and hunkless moves fail. Preserve file bytes: diff line endings must match them; the parser tolerates trailing CR on header lines, but `git apply` is byte-exact. `\ No newline at end of file` marks missing final LF.

Scope is anchored: exact names are exact-only; `dir/**` is a subtree, `*`/`?` stay within segments, `**` spans directories, `[` is literal. No unsafe/traversal/absolute/drive/backslash/control/quoted paths, `.git` components, `.gitattributes`, `.gitmodules`, binary patches, symlink/gitlink entries, mode changes or duplicate case/NFC aliases. Immutable checks include case/NFC aliases. Text refusals: `patch_not_utf8`, `patch_nul`, `not_a_git_diff`, `path_traversal`, `path_mismatch`, `control_path`, `symlink_entry`, `gitlink_entry`, `mode_change`, `binary_patch`, `quoted_path`, `path_not_allowed`, `immutable_path`, `duplicate_path`, `malformed_patch`.

**Live:** Docker archive intake reports `artifact_invalid` / `artifact_too_large`; solve maps launcher/process/missing-on-0 failures to `worker_failed`, exit 2 to `worker_no_patch`, wall kill to `worker_timeout`, cancellation to `worker_cancelled`. Text/apply/verifier failures are `patch_rejected` / `apply_rejected` / `verify_failed`; offer failures become `offer_invalid`. **Local harness only:** `read_patch` diagnostics `artifact_name`, `artifact_symlink`, `artifact_not_regular`, `artifact_missing`, `artifact_empty` (plus its `artifact_too_large`); `copy_unsupported` adds the apply restriction without changing the shell text parser. Conformance requires Python ≥ 3.12 and Unicode 15.0.0.

After stopping the worker, the shell reads capped output, checks original authority, applies on a fresh checkout and runs its verifier. **Only shell verification and independent acceptance decide delivery**, never worker self-checks.
