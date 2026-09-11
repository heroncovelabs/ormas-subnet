# Install and connect a miner

Get the package installed, get a token, run the skeleton with the reference solver, then plug in your own `solve`. What "miner" means here is defined in [`docs/DECISIONS.md`](DECISIONS.md); the commercial and acceptance terms are in [`docs/CONTRACT.md`](CONTRACT.md) (validator acceptance is a design draft, the validator acceptance design (summarized in [`docs/DECISIONS.md`](DECISIONS.md) §6)); the wire contract is [`docs/protocol.md`](protocol.md); the code below comes from [`ormas_subnet/skeleton.py`](../ormas_subnet/skeleton.py) and [`ormas_subnet/reference_solver.py`](../ormas_subnet/reference_solver.py).

## Prerequisites

- Python ≥ 3.10 (per [`pyproject.toml`](../pyproject.toml)).
- git, and credentials that can clone and push branches on the client repository you bind to — the skeleton clones it fresh per job into your workdir root.
- A gateway token (starts `ormr_`), a project id, repo id, and base commit. You get these from the operator running the gateway. The development gateway at `https://api.ormas.ai` is **by invitation**; there is no public endpoint for miners yet.

## Install

From the repository root (package name `ormas-subnet`, only dependency `httpx`):

```bash
pip install -e ./public_subnet
python -c "import ormas_subnet; print(ormas_subnet.__version__)"
```

## Token and URL — never on argv

The library hardcodes neither. Resolve the token yourself from an env-var name or a file path — never as a command-line argument, which process lists expose (`ormas_subnet/client.py`, `load_token`):

```python
token = load_token(token_env="ORMAS_MINER_TOKEN")      # or token_path="~/.ormas/miner-token" (0600)
```

The example below reads the gateway URL from `ORMAS_API_URL` — the same pattern: your names, your environment.

## Run the skeleton with the reference solver

Save as `miner.py`, export the env vars (`ORMAS_API_URL`, `ORMAS_MINER_TOKEN`, `ORMAS_REPO_URL`, `ORMAS_PROJECT_ID`, `ORMAS_BASE_COMMIT`), then `python miner.py`:

```python
import os
from pathlib import Path

from ormas_subnet import MinerConfig, MinerSkeleton, OrmasMinerClient, load_token
from ormas_subnet.reference_solver import shell_solver

client = OrmasMinerClient(
    base_url=os.environ.get("ORMAS_API_URL", "https://api.ormas.ai"),
    token=load_token(token_env="ORMAS_MINER_TOKEN"),
)
config = MinerConfig(
    runner_id="my-miner-01",
    runner_version="0.1.0",
    platform="linux",
    capacity=1,
    cells=("code-edit-small",),          # archetypes your miner serves
    workdir_root=Path("/tmp/ormas-miner"),
    repo_id="my-repo",
    repo_url=os.environ["ORMAS_REPO_URL"],
)
skeleton = MinerSkeleton(client, config, shell_solver)
skeleton.register()
skeleton.bind(
    project_id=os.environ["ORMAS_PROJECT_ID"],
    base_commit=os.environ["ORMAS_BASE_COMMIT"],
)
skeleton.run_forever()
```

The reference solver runs `true` — a no-op that commits an empty result. It exists so the loop runs end to end with no model call, and against a task whose verify expects a change it completes with `failed`, which is the honest outcome of doing nothing.

The poll, lease, and heartbeat cadences come back in the registration response. Read them from there; do not hardcode them.

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
python -m pytest public_subnet/tests -q
```

If that passes and `python miner.py` stays in the poll loop without an auth error, you are connected; the gateway returns `204` (idle) until work lands on a cell you serve.
