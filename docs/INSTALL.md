# Install and connect a miner

No token yet? Run the [local quickstart](QUICKSTART_LOCAL.md) first — the whole loop runs offline, with no token and no gateway.

Get the package installed, get a token, run the skeleton with the reference solver, then plug in your own `solve`. What "miner" means here is defined in [`docs/DECISIONS.md`](DECISIONS.md); the commercial and acceptance terms are in [`docs/CONTRACT.md`](CONTRACT.md) (validator acceptance per [`docs/DECISIONS.md`](DECISIONS.md) §6 — the reference validator ships in this package; see [`README.md`](../README.md) "Known gaps" for what is live today); the wire contract is [`docs/protocol.md`](protocol.md); the code below comes from [`ormas_subnet/skeleton.py`](../ormas_subnet/skeleton.py) and [`ormas_subnet/reference_solver.py`](../ormas_subnet/reference_solver.py).

## Prerequisites

- Python ≥ 3.10 (per [`pyproject.toml`](../pyproject.toml)). Create the venv with a ≥ 3.10 interpreter explicitly — `python3.12 -m venv .venv` — because the stock macOS `python3` is 3.9 and its bundled pip fails the editable install with a misleading setuptools error.
- git, and credentials that can clone and push branches on the client repository you bind to — the skeleton clones it fresh per job into your workdir root.
- A gateway token (starts `ormr_`), a project id, repo id, and base commit. You get these from the operator running the gateway. The development gateway at `https://api.ormas.ai` is **by invitation**; there is no public endpoint for miners yet.

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

## Run the skeleton with the reference solver

Save as `miner.py`, export the env vars (`ORMAS_API_URL`, `ORMAS_MINER_TOKEN`, `ORMAS_REPO_URL`), then `python miner.py`. As written this is the **third-party path** — register, then claim work on opted-in projects, no `bind`. **Today that path does not lease** (see the note after the code): the operator onboards you inside its tenant instead, which means adding the `bind` call shown below before `run_forever()`.

```python
import os
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
    runner_id="my-miner-01",
    runner_version="0.1.0",
    platform="linux",
    capacity=1,
    cells=("outcomes-grok-46-native",),  # fleet-cell alias — see notes below
    workdir_root=Path.home() / ".ormas" / "work",  # private — see notes below
    repo_id="my-repo",
    repo_url=os.environ["ORMAS_REPO_URL"],
)
skeleton = MinerSkeleton(client, config, shell_solver)
skeleton.register()
skeleton.run_forever()
```

Three fields a newcomer cannot guess:

- **`cells`** — the gateway's fleet-cell aliases, for example `outcomes-grok-46-native`. The operator tells you the exact strings for your miner when your token is minted; there is no public list. Registration accepts any string, but an unrecognized alias is normalized to a prefix no queued job ever carries, so a made-up cell registers fine and never leases work.
- **`workdir_root`** — fresh clones of the *client's repository* land here; keep it private (`mkdir -p -m 700 ~/.ormas/work`). `/tmp` is world-readable, periodically purged, and shared with every other user — the wrong place for client source.
- **`repo_id`** — the id of the repository binding created at bind time; the gateway assigns one (`rrep_…`) when a bind request leaves it empty, and the id rides every task draft.

**Today's path — onboarded inside the operator's tenant.** The operator gives you a project id and a base commit with your token; bind the repository to that project before polling (export `ORMAS_PROJECT_ID` and `ORMAS_BASE_COMMIT`), placing this call between `register()` and `run_forever()`:

```python
skeleton.bind(
    project_id=os.environ["ORMAS_PROJECT_ID"],
    base_commit=os.environ["ORMAS_BASE_COMMIT"],
)
```

Why: until a validator count is configured on the gateway (none is configured in production yet), a cross-tenant claim is refused outright — nothing is written, no bid, no lease. The third-party path (no `bind`, claim on any opted-in project, settlement only on validator acceptance) opens when validators are configured; your miner code does not change, only the `bind` call goes away.

The reference solver runs `true` — a no-op that commits an empty result. It exists so the loop runs end to end with no model call, and against a task whose verify expects a change it completes with `failed`, which is the honest outcome of doing nothing.

## Known gaps (skeleton)

- Poll, heartbeat and lease cadences are adopted from the registration response; a job that crashes mid-run is reported to the gateway as a failed terminal and its workdir is replaced on the next claim; gateway errors surface as `OrmasGatewayError` with the gateway's `error.type` and message. Remaining gap: the skeleton does not retry a failed `complete` call — if the gateway is unreachable at that moment the lease expires server-side (a warning is emitted).

## Plug in your own `solve`

One function carries everything that makes your miner yours:

```python
from ormas_subnet.skeleton import SolveResult

def solve(draft, workdir):
    # draft.brief, draft.verify_command, draft.allowed_paths, draft.work_packet.
    # Edit inside workdir, stay inside allowed_paths, commit your work.
    return SolveResult(
        result_commit="<sha you committed>",
        changed_paths=("src/app.py",),
        provider="acme", model="acme-large",
        prompt_tokens=123, completion_tokens=45,
        upstream_cost_usd=0.0021,
    )
```

Three rules the skeleton enforces with you:

- `result_commit` must already exist in the workdir — you commit; the skeleton branches and pushes for you.
- Report `None` for usage you do not know. Unknown cost crosses the wire as unknown — never a fabricated zero.
- Do not declare yourself verified — there is no `verified` field. The skeleton runs `draft.verify_command` itself (bounded, credential-free environment, no shell) and computes scope from a real `git diff`.

## Where results and evidence go

- **The result** is the branch `refs/heads/ormas/job/<task_id>` on the bound repository. `MinerConfig(push_remote=None)` records a `local:` ref instead — a dry run that nobody else can see.
- **The evidence** rides the complete call: commit sha, changed-path list, diff hash, verify exit code, your usage receipt. Source, diffs, prompts, and credentials never cross it — the wire rejects those fields outright; the list is in `docs/protocol.md`.

## Smoke check

```bash
pip install -e . pytest
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider tests
```

If that passes and `python miner.py` stays in the poll loop without an auth error, you are connected; the gateway returns `204` (idle) until work lands on a cell you serve.
