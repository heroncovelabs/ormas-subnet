# Install and connect a miner

No token yet? Run the [local quickstart](QUICKSTART_LOCAL.md) first — the whole loop runs offline, with no token and no gateway.

Get the package installed, get a token, run the skeleton with the reference solver, then plug in your own `solve`. What "miner" means here is defined in [`docs/DECISIONS.md`](DECISIONS.md); the commercial and acceptance terms are in [`docs/CONTRACT.md`](CONTRACT.md) (validator acceptance per [`docs/DECISIONS.md`](DECISIONS.md) §6 — the reference validator ships in this package; see [`README.md`](../README.md) "Release gaps" for release status); the wire contract is [`docs/protocol.md`](protocol.md); the code below comes from [`ormas_subnet/skeleton.py`](../ormas_subnet/skeleton.py) and [`ormas_subnet/reference_solver.py`](../ormas_subnet/reference_solver.py).

For the September 23 development candidate, read [Public tasks](PUBLIC_TASKS.md)
first. Bounded-packet (public-profile) jobs publish through the gateway, which holds
the saved GitHub write access. Public-repository jobs clone anonymously;
private-repository jobs use a per-job read credential for both miners and validators.
All required task cells and exact environment qualifications must match, and every
miner—including the operator—requires acceptance under the frozen policy. V1
separates miner and validator operators; the explicit v2 alpha uses an operator-run
checker. The credential and bind instructions below cover the legacy route.
Installing this candidate does not activate it in production.

## Prerequisites

- Python ≥ 3.10 (per [`pyproject.toml`](../pyproject.toml)). A **legacy validator** host additionally needs every Python minor a client may declare in a packet `toolchain` installed as `python3.X` on `PATH` (today: `python3.12`); a declared interpreter that is missing makes the validator post `error` for that assignment, which is excluded from quorum. Create the venv with a ≥ 3.10 interpreter explicitly — `python3.12 -m venv .venv` — because the stock macOS `python3` is 3.9 and its bundled pip fails the editable install with a misleading setuptools error.
- `git` on `PATH`; the legacy credential route also needs an `ssh` client. Bounded-packet (public-profile) jobs publish through the gateway: public-repository jobs clone anonymously; private-repository jobs use a per-job read credential for both miners and validators. A legacy unbound claim carries `repo_credential`; the bind fallback uses access you already hold.
- A runner key (starts `ormr_`). Mint it yourself: sign in at [ormas.ai](https://ormas.ai), open **Miner → Runner keys**, and create a key. It is shown once; save it to `~/.ormas/miner-token` with mode `0600`. A new key is **register-only** (daily claim cap 0): it can register your runner and hotkey and submit qualification evidence against `https://api.ormas.ai`, but job claims return 429 until the operator approves your qualification and raises the cap (see [`CONTRACT.md`](CONTRACT.md)). Keys expire after 30 days; mint a new one on the same page. A project id and base commit are needed only for the bind fallback.

## Install

From the repository root (package name `ormas-subnet`, dependencies `httpx` and `cryptography`):

```bash
pip install -e .
python -c "import ormas_subnet; print(ormas_subnet.__version__)"
```

## Token and URL — never on argv

The library hardcodes neither. Resolve the token yourself from an env-var name or a file path — never as a command-line argument, which process lists expose (`ormas_subnet/client.py`, `load_token`):

```python
token = load_token(token_env="ORMAS_MINER_TOKEN")      # or token_path="~/.ormas/miner-token" (0600)
```

The example below reads the gateway URL from `ORMAS_API_URL` — the same pattern: your names, your environment.

## Legacy route: the credential arrives with the claim

This section covers legacy jobs. Bounded-packet (public-profile) jobs follow
[Public tasks](PUBLIC_TASKS.md): public-repository jobs clone anonymously;
private-repository jobs use per-job read credentials, and both publish through
the gateway.
For a legacy unbound job, register and poll; the claimed draft names the
repository and gives the skeleton its key:

1. `MinerSkeleton.register()` posts your registration; the gateway assigns your `runner_id`.
2. `MinerSkeleton.run_once()` polls `claim_task`. Idle is `204`. A `200` returns a `TaskLease` and a `TaskDraft`.
3. An unbound draft carries `repo_url` (the clone URL) and `repo_credential` (`{"kind": "ssh_deploy_key", "private_key": …, "fingerprint": …}`; `ormas_subnet/protocol.py`, `TaskDraft`).
4. `_clone_and_checkout` clones `draft.repo_url` into `workdir_root/<task_id>` under `_credential_git_env`, which writes the key to a `0600` file that exists only for the duration of the git call and is removed after. It then checks out `draft.base_commit`.
5. Your `solve(draft, workdir)` runs; the skeleton heartbeats the lease meanwhile.
6. The skeleton runs `draft.verify_command`, then `_publish` pushes `ormas/job/<task_id>` to `draft.repo_url` with the same key, again materialised only for that push.
7. `complete_task` posts the receipt, terminal, and capture. A third-party delivery settles on validator acceptance (`docs/protocol.md` §Lifecycle).

Validators get their own credential. When the project has a read key, the assignment a validator polls carries a `repo_credential` with `"scope": "read"`; `ValidatorDaemon._clone` clones with it through the same `_credential_git_env` helper and re-runs the client's tests (`ormas_subnet/validator.py`; `tests/test_validator_repo_credential.py`). Validators do not use your key, and no credential is part of the signed evidence digest (`canonical_evidence_fields`).

This path first ran live on 2026-09-14 against a repository the miner had never configured; `tests/test_skeleton_repo_credential.py` and `tests/test_validator_repo_credential.py` pin the behaviour.

### Run the task-validator component

The installed wheel includes `ormas-validator`; a source checkout is unnecessary:

```bash
ormas-validator --gateway https://api.ormas.ai \
  --token-file /private/path/validator-token \
  --private-key-file /private/path/validator-ed25519-key \
  --workdir-root /private/path/validator-work
```

Both files must be private to the service account. The decision key is separate
from a Bittensor chain hotkey. `--once` reviews at most one assignment and returns
3 when idle. The old `python neurons/validator.py` script delegates to this command.
Public task checks need a qualified nonroot Linux/amd64 OCI environment. Keep the
checker account, container authority, work directory and credentials separate from
the wallet-bearing weight submission service. A rootful Docker socket can access
host wallets and is not that separation.

Task checking and weight submission are responsibilities of the SN76 validator;
this command supplies the checking component. The combined public service installer
and chain identity onboarding remain separate release work. Miner operators do not
need to recruit their own checker. The gateway assigns qualified validator capacity
under the job's frozen [acceptance policy](PUBLIC_TASKS.md).

### What you receive per legacy job

- **`repo_url`** — the clone URL for the client's repository, on the draft. Credential-free by itself (`TaskDraft.repo_url`).
- **`repo_credential`** — an `ssh_deploy_key` for that repository, with `private_key` and `fingerprint`. It is served once, in the claim response, over your authenticated runner channel; the gateway does not re-serve it on any list or status route (`docs/protocol.md` §`POST /api/runner/v1/leases`). It is absent when a bound `repo_id` already covers the repository.
- **Where it lives on your host** — nowhere, except a `0600` file under a `tempfile.mkdtemp(prefix="ormas-job-key-")` directory for the duration of each `git clone` / `git push`, deleted in a `finally` block (`skeleton._credential_git_env`). The skeleton never logs it and never writes it anywhere else. Do not persist, copy, or reuse it yourself.
- **What it is for** — cloning the base commit and pushing `refs/heads/ormas/job/<task_id>`. Validators receive a separate read-scoped key; you never handle theirs.

Key rotation and revocation on the client's repository are gateway-side and are not described in this package; `docs/protocol.md` names a per-job, single-repo, read-scoped credential as the planned successor.

## Run the skeleton with the reference solver

Save as `miner.py`, export the env vars (`ORMAS_API_URL`, `ORMAS_MINER_TOKEN`, `ORMAS_REPO_URL`), then `python miner.py`. There is no `bind` call: the skeleton registers, polls, and clones whatever repository the draft names.

```python
import os
import sys
from pathlib import Path

from ormas_subnet import MinerConfig, MinerSkeleton, OrmasMinerClient, load_token
from ormas_subnet.reference_solver import shell_solver

client = OrmasMinerClient(
    # Fail closed on a missing URL — a .get() default would silently send
    # your token to the production gateway when the env var is not exported.
    base_url=os.environ["ORMAS_API_URL"],
    token=load_token(token_env="ORMAS_MINER_TOKEN"),
)
config = MinerConfig(
    # Empty on the first run — the gateway assigns runr_<12hex> and register()
    # adopts it into config.runner_id. The library does NOT print it; the
    # print() below does. Save it (export ORMAS_RUNNER_ID) and it is reused
    # here on every later run; a self-chosen id is refused 404.
    runner_id=os.environ.get("ORMAS_RUNNER_ID", ""),
    miner_id="your-miner-name",  # optional public identity
    runner_version="0.1.0",
    platform="linux",
    capacity=1,
    cells=("task:code",),  # task-type cell — see notes below
    workdir_root=Path.home() / ".ormas" / "work",  # private — see notes below
    repo_id="my-repo",
    repo_url=os.environ["ORMAS_REPO_URL"],  # fallback clone source only — see notes below
)
skeleton = MinerSkeleton(client, config, shell_solver)
skeleton.register()
print(f"runner_id={skeleton.config.runner_id}", file=sys.stderr)  # keep this; pass it next time
skeleton.run_forever()
```

On the second and later starts: `export ORMAS_RUNNER_ID=runr_…` (the value printed above) before
launching, and the same example reuses it. The CLI (`python -m neurons.miner`) already prints
`runner_id=<assigned>` to stderr on every start and takes `--runner-id` on later runs.

### Enable per-job offers

Configure `offer_fn` on `MinerConfig` to receive each privacy-safe queue entry
(`job_id`, `created_at`, `envelope`). Return a wire offer or `None` to decline:

```python
def offer(entry):
    return {"job_id": entry["job_id"], "kind": "limit",
            "estimate_usd": 0.80, "limit_usd": 1.00}

config.offer_fn = offer
```

The fixed amounts above are a made-up wiring example. Both numbers are required,
with `0 < estimate_usd <= limit_usd`. The estimate is your expected charge: the
expected cost of your usual recovery chain plus margin. The limit is your hard
ceiling: the worst-case chain. If you price with one number, send it as both. A firm offer instead
returns `{"job_id": entry["job_id"], "kind": "firm", "price_usd": 1.00}`.

Set `config.settle_fn` to your callable `(lease, result) -> float`, using actual
metered cost of all attempts plus your margin. It must be deterministic and must
not raise. A raising hook never reprices the delivery. On the public (bounded-packet) path the published work is held for recovery, no completion is sent, and every later poll re-raises until the hook returns a valid price; only a task without a public packet completes as `failed`.
The skeleton caps it at the limit; unset, it settles at the limit. It calls this
only for a verified limit delivery. You bear any loss above the ceiling. Recovery
replays the frozen price.

Leaving both callbacks unset preserves legacy firm `ask_usd` claims. With offers
enabled, the skeleton lists the queue before claiming. It falls back to a legacy
`ask_usd` claim only on a queue 404 with no `error.type`, indicating an older
gateway without the route, and remembers that for the process. A 404 carrying
`error.type == "not_found_error"` is raised to the operator: it can mean an unknown
runner, or no priced binding and no eligible project. `claim_task` validates
`offers` locally and raises `ValueError` before sending a malformed offer.

Phase 1 accepts on arrival: the first offer within the client's undisclosed
spending limit wins; an offer above it is recorded and skipped and the job stays
queued. [History-based ranking](protocol.md#offer-ranking) is planned.

`ask_usd` is live since `gateway-2026.09.11`; the queue route, offers and limit
settlement are live since `gateway-2026.10.03`. Against an older gateway the queue
returns 404 without `error.type`, and the skeleton falls back to `ask_usd`. A `paid`
receipt on `api.ormas.ai` today reflects one operator-run validator. See
[Protocol](protocol.md) for fields and settlement errors.

Four fields a newcomer cannot guess:

- **`cells`** — the task types your miner serves, as **task-type cells**. Register `task:code` to
  serve every bounded coding task, or narrow it: `task:code/small`, `task:code/medium`,
  `task:code/large` (the packet's turn budget: 12 / 24 / 40 turns), and `task:lang/<language>`
  (for example `task:lang/python`, `task:lang/typescript`) for the languages in the packet. A
  legacy job leases when any one of your cells matches a job cell. Public-profile
  jobs require every required cell and exact environment qualification. The gateway
  derives cells from the work packet. The older
  `outcomes-…` strings are the operator's own model-bound cells; they are not yours to
  register and, since gateway `2026.09.12`+1, a third-party miner registering only those never
  leases work. Registration accepts any string; a cell no queued job carries simply never leases.
- **`workdir_root`** — client clones land here, one directory per `task_id`; keep it private and persistent (`mkdir -p -m 700 ~/.ormas/work`). Public-profile jobs retain their checkout and journal for recovery after a crash. The legacy route retains its fresh-checkout behavior. Do not delete a public-profile recovery directory to force a new claim.
- **`repo_url`** — `MinerConfig` requires it, but on the legacy credential path it is only the fallback: `_clone_and_checkout` clones `draft.repo_url` whenever the draft carries a `repo_credential` and a non-empty `repo_url`, and clones `config.repo_url` only when it does not. Point it at a repository you actually hold, or at the bound repository if you use the fallback below.
- **`repo_id`** — `MinerConfig` requires it; it is the id sent in a bind request (`RepoRegistration.repo_id`). On the credential path the draft's own `repo_id` describes the job, and this field is not used to choose the clone source.

The reference solver runs `true` — a no-op that commits an empty result. It exists so the loop runs end to end with no model call, and against a task whose verify expects a change it completes with `failed`, which is the honest outcome of doing nothing.

The same loop is available as a CLI: `python neurons/miner.py --gateway … --token-env ORMAS_MINER_TOKEN --repo-id <id> --repo-url <url> --cell task:code --solve-command '<cmd>'`. It reads the token from `--token-env` / `--token-file`, never from argv, and prints the assigned `runner_id=…` on stderr after the first registration.

## Fallback: bind a repository you already hold locally

Use this only when the operator has onboarded your miner against a specific project and you already have a clone URL your host can pull from and push to. `MinerSkeleton.bind(project_id=…, base_commit=…)` posts a `RepoRegistration` for `config.repo_id`; a draft for a bound repository then arrives **without** `repo_credential`, and the skeleton clones `config.repo_url` and pushes to `config.push_remote` (`"origin"` by default; `None` records a `local:` ref instead). Place the call between `register()` and `run_forever()` (export `ORMAS_PROJECT_ID` and `ORMAS_BASE_COMMIT`):

```python
skeleton.bind(
    project_id=os.environ["ORMAS_PROJECT_ID"],
    base_commit=os.environ["ORMAS_BASE_COMMIT"],
)
```

The CLI equivalent is `--bind-project <project_id> --bind-base-commit <sha>` on `neurons/miner.py`. `tests/test_skeleton_repo_credential.py::test_no_credential_keeps_configured_clone_source` pins this behaviour.

One gateway rule applies to both legacy paths: a third-party claim leases only when the gateway has a validator count configured, because a third-party delivery settles on validator acceptance, never on the miner's own report (`docs/protocol.md` §Lifecycle; [`README.md`](../README.md) "What this is NOT" for the production state).

## Restart and completion recovery

Keep the same `workdir_root` across miner restarts. The public miner keeps a private
atomic journal there, bound to the gateway, runner, task, full lease and prepared
packet. It retains the accepted offer id, kind and ceiling.
Only one process may own that journal. A restart resolves retained work before it
claims another task; it never invokes the solver again for that retained lease.

The miner saves the bounded artifact before publication and the exact completion
request, including its settled price, before posting it. If the process dies at
either boundary, the normal miner loop resumes through the gateway's idempotent
endpoints. Replaying a saved completion sends the exact settled price and calls no
pricing callback. Recovery from the artifact stage reuses the saved result and
calls `settle_fn` again, which is why that hook must be deterministic.
Transient completion failures are also retried in-process within the lease budget.
A settling response keeps the completion pending. A terminal receipt acknowledges
it; an explicit lost lease retains a tombstone and releases the miner for new work.

If the process died before the artifact was saved, its provider outcome is unknown.
The miner reports an aborted result without another solver call or an invented
settlement receipt. Corrupt or unsafe recovery evidence refuses new work for
inspection. Retain the journal and checkout; deleting them loses recovery evidence.
The service manager must restart a stopped miner process: the SDK recovers work
when launched, but does not itself supervise or reboot the host.

Poll, heartbeat and lease cadences come from the gateway registration response.
Gateway errors surface as `OrmasGatewayError` with the gateway's error type and
message. Legacy jobs retain their existing completion behavior.

## Plug in your own `solve`

Your `solve` produces the result. Optional pricing callbacks set its offer and
limit settlement:

```python
from ormas_subnet.skeleton import SolveResult

def solve(draft, workdir):
    # draft.brief, draft.verify_command, draft.allowed_paths, draft.work_packet.
    # Edit inside workdir, stay inside allowed_paths, commit your work.
    return SolveResult(
        result_commit="<sha you committed>",
        changed_paths=("src/app.py",),
        prompt_tokens=123, completion_tokens=45,
        upstream_cost_usd=0.0021,
    )
```

Three rules the skeleton enforces with you:

- `result_commit` must already exist in the workdir — you commit; the skeleton branches and pushes for you.
- Report `None` for usage you do not know. Unknown cost crosses the wire as unknown — never a fabricated zero.
- The skeleton computes verification and scope; `SolveResult` has no `verified` field. Both routes run with no shell and a credential-free environment (`PATH`, scratch `HOME`, `LANG`, plus the packet's explicit `NAME=value` assignments). Bounded-packet (public-profile) jobs use a digest-pinned OCI image with `--network none`, `--cap-drop ALL`, `--read-only` and resource limits. Legacy verification runs direct argv. Scope comes from a real `git diff`.

## Where results and evidence go

- **The result** is `refs/heads/ormas/job/<task_id>` in the client's repository. Public-profile jobs upload a bounded committed-file artifact for the gateway to publish. Legacy jobs push with `repo_credential` or use the bound remote. `MinerConfig(push_remote=None)` records a `local:` ref on the bind fallback, for local dry runs.
- **The evidence** rides the complete call: commit sha, changed-path list, diff hash, verify exit code, your usage receipt. Source, diffs, prompts, and credentials never cross it — the wire rejects those fields outright; the list is in `docs/protocol.md`.

## Register your hotkey

Record your chain hotkey↔miner mapping on the gateway once, after onboarding. The gateway mints a one-time challenge; your `--sign-command` — your own program, holding your own key — reads the challenge bytes on stdin and prints the sr25519 signature hex on stdout; the CLI posts it for verification. The key (and the hotkey) never enter this package; with `bittensor` installed a signer is a couple of lines around `wallet.hotkey.sign(challenge_bytes).hex()`.

```bash
python -m neurons.miner register-hotkey \
  --gateway https://api.ormas.ai --token-env ORMAS_MINER_TOKEN \
  --runner-id runr_0123456789ab --hotkey-ss58 <your-ss58> \
  --sign-command 'my-signer --hotkey alice'
```

Here `--runner-id` is the assigned `runr_<12hex>` id from your first registration — never a self-chosen one: the gateway refuses an id it has not issued to your token (404). The third-party skeleton registers with `runner_id=""` and adopts the assigned id automatically (see the example above).

The same call exists on the client as `client.register_hotkey(runner_id, hotkey_ss58=..., sign_fn=...)`; the wire contract (`hotkey/challenge` + `hotkey`, refusal codes) is in [`docs/protocol.md`](protocol.md). Without a verified hotkey mapping a miner earns no chain weight, however good its work (see [`docs/CONTRACT.md`](CONTRACT.md) "How you are scored").

## Smoke check

```bash
pip install -e . pytest
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider tests
```

If that passes and `python miner.py` stays in the poll loop without an auth error, you are connected; the gateway returns `204` (idle) until work lands on a cell you serve.
