# AGENTS.md

For a coding agent (Claude Code, Cursor, GrokBuild, Codex) pointed at this
repository by a miner operator. Read this first; it is short on purpose.

## What this repository is

The public Ormas subnet package (Bittensor SN76; Heron Cove LLC; MIT): the miner
wire protocol (`ormas_subnet/protocol.py`), a thin HTTP client
(`ormas_subnet/client.py`), a reference miner skeleton (`ormas_subnet/skeleton.py`),
a trivial reference solver (`ormas_subnet/reference_solver.py`), and a reference
validator (`ormas_subnet/validator.py`). It is NOT our mining logic — nothing about
how to solve a task well lives here; our own miner competes on this same protocol,
on the same terms as every other miner. A miner posts a firm ask for a whole task,
delivers a whole outcome (a result branch on the client's repository), and is paid
only on accepted delivery. A miner is never an inference endpoint: nobody is paid
for tokens, completions, or uptime.

## Layout

- `ormas_subnet/protocol.py` — the `ormas-runner-v1` data types
  (`RunnerRegistration`, `RepoRegistration`, `TaskDraft`, `TaskLease`, `TaskEvent`,
  `TaskReceipt`, `TaskTerminal`) that cross the wire to `/api/runner/v1` on the
  Ormas gateway.
- `ormas_subnet/client.py` — `OrmasMinerClient`, the thin HTTP client for those
  routes, and `load_token` (token from env or file, never argv).
- `ormas_subnet/skeleton.py` — `MinerSkeleton`: register → bind a repo → poll →
  claim → clone → solve → verify → publish → complete. Completion is honest, not
  self-declared: the receipt is built only from usage `solve` reports, `scope_ok`
  comes from a real `git diff`, and `verification_state` from the verify command's
  actual exit code.
- `ormas_subnet/reference_solver.py` — `shell_solver` / `make_shell_solver`: runs a
  shell command and commits the result, so the loop runs end to end with no model
  call at all.
- `ormas_subnet/validator.py` — the reference validator: clones the job's repo,
  independently re-runs the verify command, checks scope from `git diff`, signs,
  and posts accept/reject.
- `neurons/miner.py` — reference miner entry point (skeleton + shell solver).
- `neurons/validator.py` — validator entry point.
- `docs/protocol.md` — the wire contract: every route, field, forbidden field, and
  error code.
- `docs/CONTRACT.md` — commercial and acceptance terms (owner-controlled).
- `docs/INSTALL.md` — install, get a token, run the skeleton, plug in your `solve`.
- `docs/DECISIONS.md` — the owner-locked decisions this repository implements.
- `tests/` — the gates (below).

## Running the tests

From the repository root, the commands CI runs (`.github/workflows/ci.yml`, on
Python 3.10 and 3.12; `pyproject.toml` pins `testpaths = ["tests"]` and ruff
line-length 100):

```bash
pip install -e . pytest ruff
ruff check .
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider tests
```

The protocol-parity tests skip in this standalone checkout; they run in the owner's
private monorepo. Everything else must pass here.

## The one rule that matters

1. `ormas_subnet` must never import `tensorbox_spec`. A miner installs this package
   alone, with no monorepo attached. `tests/test_import_graph.py` fails the build
   on any leak, direct or transitive.
2. The protocol data types must stay wire-identical to the server's accepted
   contract. `tests/test_protocol_parity.py` compares every DTO field-for-field
   and both `to_wire()` outputs. It skips in this checkout, so a drift can land
   green here — do not change `ormas_subnet/protocol.py` casually, and pair any
   wire change with a matching entry in `docs/protocol.md`.

## Where your code goes

`solve(draft, workdir) -> SolveResult` (`ormas_subnet/skeleton.py`) is the one
pluggable step. Everything else in a running miner — model routing, harness,
caching, orchestration loop, cost capture — is the operator's own code and is
expected to stay private. Do not add mining logic to this repository, and do not
weaken the honest-completion rules in the skeleton.

## Tokens and keys

Tokens (they start `ormr_`) and keys come from an environment variable or a file
(`ormas_subnet.client.load_token(token_env=...)` / `token_path=...`, file mode
0600), never as a command-line argument (process lists expose argv), never
hardcoded, never committed. The only production hostname that belongs in code or
fixtures is `api.ormas.ai`.

## Vocabulary

Say **miner**, **firm ask**, **accepted delivery**, **validator**. Never "runner",
"supplier", "inference miner", or "marketplace". The wire names `/api/runner/v1`,
`ormas-runner-v1`, and the `runner_id` / `RunnerRegistration` identifiers predate
this vocabulary; they are historical wire names, not prose, and change only with a
future protocol version.
