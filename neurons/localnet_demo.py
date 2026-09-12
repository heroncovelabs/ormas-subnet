"""Run the whole miner loop offline, with no token and no gateway at all.

    python neurons/localnet_demo.py [--workdir DIR] [--scenario pass|noop|out-of-scope]

Builds a temporary local bare git "client repository" whose ``verify_command``
fails on the base commit, seeds one job for it on
:class:`ormas_subnet.localnet.LocalGateway` (an in-process, offline stand-in
for the gateway — see that module's docstring for exactly what it does and
does not do), runs :class:`ormas_subnet.skeleton.MinerSkeleton` for exactly one
tick against a solver appropriate to ``--scenario``, and prints the recorded
receipt.

Three scenarios (settlement vocabulary is the real wire vocabulary — see
``ormas_subnet/localnet.py`` and ``docs/protocol.md`` § "Settlement
derivation"):

- ``pass`` (default) — the solver makes the required change inside
  ``allowed_paths``. Verifies, stays in scope, settles as **``paid``**.
- ``noop`` — the solver does nothing. Verify command still fails on base
  content, so the delivery honestly settles as **``no_delivery``**.
- ``out-of-scope`` — the solver edits a file outside ``allowed_paths``. The
  verify command actually passes, but ``scope_ok`` (computed from a real
  ``git diff``, never a solver's say-so) is False, so the delivery honestly
  settles as **``no_delivery``** even though verification alone would look
  fine.

Exit code is 0 only when each scenario settles the way it is supposed to
(``paid`` for ``pass``, ``no_delivery`` for the other two); a scenario that
settles the wrong way exits 1.

No network host is contacted anywhere in this script.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ormas_subnet.localnet import LocalGateway  # noqa: E402
from ormas_subnet.client import OrmasMinerClient  # noqa: E402
from ormas_subnet.skeleton import MinerConfig, MinerSkeleton, SolveResult  # noqa: E402

# Never /tmp: it is world-readable, periodically purged, and shared with every
# other user on the box — the wrong place for even a throwaway "client repo".
# See docs/QUICKSTART_LOCAL.md.
DEFAULT_WORKDIR = Path.home() / ".ormas" / "localnet-demo"

TASK_ID = "local-demo-task"
VERIFY_COMMAND = "grep -q mined out.txt"
ALLOWED_PATHS = ["out.txt"]
BRIEF = "append the line 'mined' to out.txt"


def _run_git(args: list[str], *, cwd: Path) -> str:
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def _make_client_bare_repo(root: Path) -> tuple[Path, str]:
    """Create a bare "client repository" plus a seed checkout with one commit.

    Returns ``(bare_repo_path, base_commit)``. ``verify_command`` fails against
    this base content — the whole point of the task.
    """
    bare = root / "client-repo.git"
    seed = root / "client-repo-seed"
    bare.mkdir(parents=True)
    _run_git(["init", "--bare", str(bare)], cwd=root)
    seed.mkdir()
    _run_git(["init"], cwd=seed)
    _run_git(["config", "user.email", "demo@example.invalid"], cwd=seed)
    _run_git(["config", "user.name", "Ormas Localnet Demo"], cwd=seed)
    (seed / "out.txt").write_text("base\n")
    _run_git(["add", "-A"], cwd=seed)
    _run_git(["commit", "-m", "base"], cwd=seed)
    _run_git(["remote", "add", "origin", str(bare)], cwd=seed)
    _run_git(["push", "origin", "HEAD:refs/heads/main"], cwd=seed)
    base_commit = _run_git(["rev-parse", "HEAD"], cwd=seed)
    shutil.rmtree(seed)
    return bare, base_commit


def _solve_pass(draft, workdir: Path) -> SolveResult:  # noqa: ANN001 - draft is TaskDraft
    (workdir / "out.txt").write_text("base\nmined\n")
    _run_git(["add", "-A"], cwd=workdir)
    _run_git(["commit", "-m", "solve: append mined line"], cwd=workdir)
    result_commit = _run_git(["rev-parse", "HEAD"], cwd=workdir)
    return SolveResult(
        result_commit=result_commit,
        changed_paths=("out.txt",),
        provider="reference",
        model="localnet-demo-solver",
        prompt_tokens=0,
        completion_tokens=0,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
        reasoning_tokens=0,
        upstream_cost_usd=0.0,
    )


def _solve_noop(draft, workdir: Path) -> SolveResult:  # noqa: ANN001 - draft is TaskDraft
    _run_git(["commit", "--allow-empty", "-m", "solve: no-op"], cwd=workdir)
    result_commit = _run_git(["rev-parse", "HEAD"], cwd=workdir)
    return SolveResult(
        result_commit=result_commit,
        changed_paths=(),
        provider="reference",
        model="localnet-demo-solver",
        prompt_tokens=0,
        completion_tokens=0,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
        reasoning_tokens=0,
        upstream_cost_usd=0.0,
    )


def _solve_out_of_scope(draft, workdir: Path) -> SolveResult:  # noqa: ANN001 - draft is TaskDraft
    # Makes verify_command pass (edits out.txt too) *and* touches a file
    # outside allowed_paths — scope_ok must catch this even though the
    # verifier alone would be satisfied.
    (workdir / "out.txt").write_text("base\nmined\n")
    (workdir / "other.txt").write_text("out of scope\n")
    _run_git(["add", "-A"], cwd=workdir)
    _run_git(["commit", "-m", "solve: out of scope edit"], cwd=workdir)
    result_commit = _run_git(["rev-parse", "HEAD"], cwd=workdir)
    return SolveResult(
        result_commit=result_commit,
        changed_paths=("out.txt", "other.txt"),
        provider="reference",
        model="localnet-demo-solver",
        prompt_tokens=0,
        completion_tokens=0,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
        reasoning_tokens=0,
        upstream_cost_usd=0.0,
    )


_SCENARIOS = {
    "pass": _solve_pass,
    "noop": _solve_noop,
    "out-of-scope": _solve_out_of_scope,
}

# Real wire settlement vocabulary (docs/protocol.md § "Settlement
# derivation"): "paid" or "no_delivery", never "unpaid". What a
# correctly-behaving demo run for each scenario should settle as.
_EXPECT_SETTLEMENT = {"pass": "paid", "noop": "no_delivery", "out-of-scope": "no_delivery"}


def _parse(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workdir", default=str(DEFAULT_WORKDIR), help=f"default: {DEFAULT_WORKDIR}")
    ap.add_argument("--scenario", choices=sorted(_SCENARIOS), default="pass")
    return ap.parse_args(argv)


def run_demo(workdir: Path, scenario: str) -> dict:
    """Run one scenario end to end; returns the recorded receipt dict."""
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True, mode=0o700)

    bare_repo, base_commit = _make_client_bare_repo(workdir)

    gateway = LocalGateway(
        task_id=TASK_ID,
        base_commit=base_commit,
        verify_command=VERIFY_COMMAND,
        repo_url=str(bare_repo),
        brief=BRIEF,
        allowed_paths=ALLOWED_PATHS,
    )
    client = OrmasMinerClient(base_url="local://localnet-demo", token="no-token-needed", http_client=gateway)
    config = MinerConfig(
        runner_id="localnet-demo-miner",
        runner_version="0.0.1",
        platform="local",
        capacity=1,
        cells=("code-edit-small",),
        workdir_root=workdir / "work",
        repo_id="repo1",
        repo_url=str(bare_repo),
        push_remote="origin",
    )
    skeleton = MinerSkeleton(client, config, _SCENARIOS[scenario])
    skeleton.register()
    skeleton.bind(project_id="local-demo-project", base_commit=base_commit)
    did_work = skeleton.run_once()
    assert did_work, "LocalGateway had no work queued — this should not happen"

    return gateway.receipts[-1]


def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)
    receipt = run_demo(Path(args.workdir).expanduser(), args.scenario)

    print(f"scenario:            {args.scenario}")
    print(f"verification_state:  {receipt['verification_state']}")
    print(f"scope_ok:            {receipt['scope_ok']}")
    print(f"result_commit:       {receipt['result_commit']}")
    print(f"changed_paths:       {receipt['changed_paths']}")
    print(f"settlement:          {receipt['settlement']}")
    print(f"failure_class:       {receipt['failure_class']}")
    print(f"customer_billed_usd: {receipt['customer_billed_usd']}")

    expected_settlement = _EXPECT_SETTLEMENT[args.scenario]
    if receipt["settlement"] != expected_settlement:
        print(
            f"UNEXPECTED: scenario {args.scenario!r} settled {receipt['settlement']!r}, "
            f"expected {expected_settlement!r}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
