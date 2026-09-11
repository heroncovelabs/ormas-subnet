"""Run MinerSkeleton against a fake in-process gateway with the reference solver.

Uses a fake transport (no real network, no FastAPI/private server dependency) so
this test runs standalone in the public package. Asserts the full loop —
register, bind, claim, solve, verify, publish, complete — actually happens, and
that the three honesty properties of the completion step hold: the receipt is
built from what `solve` actually reported (never fabricated), `scope_ok` is
computed from a real `git diff` (never asserted), and `verification_state` comes
from actually running `verify_command` (never a solver self-declaration).
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from ormas_subnet.client import OrmasMinerClient
from ormas_subnet.protocol import RUNNER_PROTOCOL_V1
from ormas_subnet.reference_solver import make_shell_solver
from ormas_subnet.skeleton import MinerConfig, MinerSkeleton, SolveResult


class _FakeResponse:
    def __init__(self, status_code: int, body: Any) -> None:
        self.status_code = status_code
        self._body = body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}: {self._body}")

    def json(self) -> Any:
        return self._body


class FakeGateway:
    """Enough of ``/api/runner/v1`` to drive the skeleton end to end.

    Deliberately simplified vs. the real server (no auth, no persistence) — this
    is a protocol-shape double, not a reimplementation of runner_api.py.
    """

    def __init__(
        self,
        *,
        task_id: str,
        base_commit: str,
        verify_command: str,
        allowed_paths: list[str] | None = None,
    ) -> None:
        self.task_id = task_id
        self.base_commit = base_commit
        self.verify_command = verify_command
        self.allowed_paths = allowed_paths if allowed_paths is not None else ["out.txt"]
        self.registered: dict[str, Any] | None = None
        self.bound: dict[str, Any] | None = None
        self.claims: list[dict[str, Any]] = []
        self.claimed = False
        self.heartbeats: list[dict[str, Any]] = []
        self.completed: dict[str, Any] | None = None
        self._lease_token = "lease-token-1"

    def post(self, path: str, json: dict[str, Any] | None = None, headers: Any = None) -> _FakeResponse:
        body = json or {}
        assert body.get("schema_version") == RUNNER_PROTOCOL_V1
        if path == "/api/runner/v1/registrations":
            self.registered = body
            return _FakeResponse(
                200,
                {
                    "runner_id": body["runner_id"],
                    "poll_interval_s": 15,
                    "lease_ttl_s": 300,
                    "heartbeat_s": 90,
                    "protocol": RUNNER_PROTOCOL_V1,
                },
            )
        if path == "/api/runner/v1/repositories":
            self.bound = body
            return _FakeResponse(200, {"repo_id": body["repo_id"], "project_id": body["project_id"]})
        if path == "/api/runner/v1/leases":
            self.claims.append(body)
            if self.claimed:
                return _FakeResponse(204, None)
            self.claimed = True
            lease = {
                "schema_version": RUNNER_PROTOCOL_V1,
                "lease_id": self._lease_token,
                "task_id": self.task_id,
                "expires_at": "2026-01-01T00:05:00Z",
                "selected_cell": "code-edit-small",
                "provider_pin": "unset",
                "fallback_policy": "unset",
                "hold_ref": "unset",
                "now": "2026-01-01T00:00:00Z",
                "outcome_price_usd": 0.05,
            }
            draft = {
                "schema_version": RUNNER_PROTOCOL_V1,
                "task_id": self.task_id,
                "runner_id": body["runner_id"],
                "repo_id": "repo1",
                "base_commit": self.base_commit,
                "brief": "append a line to out.txt",
                "verify_command": self.verify_command,
                "allowed_paths": self.allowed_paths,
                "budget_usd": 1.0,
                "work_packet": {"task": "append a line to out.txt"},
                "work_packet_sha256": "b" * 64,
                "attempt": 0,
                "parent_job_id": "",
                "repair_findings": [],
            }
            return _FakeResponse(200, {"lease": lease, "draft": draft})
        if path.endswith("/heartbeat"):
            self.heartbeats.append(body)
            return _FakeResponse(200, {"expires_at": "2026-01-01T00:05:00Z", "now": "2026-01-01T00:00:30Z"})
        if path.endswith("/complete"):
            assert body["receipt"]["schema_version"] == RUNNER_PROTOCOL_V1
            assert body["terminal"]["verification_state"] in {"verified", "failed"}
            self.completed = body
            return _FakeResponse(
                200,
                {
                    "status": "done" if body["terminal"]["verification_state"] == "verified" else "failed",
                    "receipt": {
                        "receipt_id": "rcpt_1",
                        "settlement": "paid",
                        "customer_billed_usd": 0.05,
                        "debit_status": "delivered",
                        "upstream_cost_usd": 0.0,
                    },
                },
            )
        raise AssertionError(f"unhandled path: {path}")

    def get(self, path: str) -> _FakeResponse:  # pragma: no cover - unused by the skeleton
        raise AssertionError(f"unexpected GET: {path}")


def _init_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "source_repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "miner@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Miner Test"], cwd=repo, check=True)
    (repo / "out.txt").write_text("base\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)
    base_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()
    return repo, base_commit


def _run_one_task(
    tmp_path: Path,
    *,
    repo: Path,
    base_commit: str,
    verify_command: str,
    solve_fn,
    allowed_paths: list[str] | None = None,
    ask_usd: float | None = None,
) -> tuple[FakeGateway, MinerSkeleton]:
    gateway = FakeGateway(
        task_id="task_1", base_commit=base_commit, verify_command=verify_command,
        allowed_paths=allowed_paths,
    )
    client = OrmasMinerClient(base_url="https://fake.invalid", token="ormr_test", http_client=gateway)
    config = MinerConfig(
        runner_id="miner-1",
        runner_version="0.0.1",
        platform="linux",
        capacity=1,
        cells=("code-edit-small",),
        workdir_root=tmp_path / "work",
        repo_id="repo1",
        repo_url=str(repo),
        push_remote=None,  # fake gateway has no real remote; record a local: ref
        ask_usd=ask_usd,
    )
    skeleton = MinerSkeleton(client, config, solve_fn)
    skeleton.register()
    skeleton.bind(project_id="proj_abc", base_commit=base_commit)
    did_work = skeleton.run_once()
    assert did_work is True
    return gateway, skeleton


_GIT_AVAILABLE = subprocess.run(["git", "--version"], capture_output=True).returncode == 0
requires_git = pytest.mark.skipif(not _GIT_AVAILABLE, reason="git binary not available")


@requires_git
def test_skeleton_registers_claims_solves_and_completes(tmp_path: Path) -> None:
    repo, base_commit = _init_repo(tmp_path)
    solver = make_shell_solver("echo mined >> out.txt")
    gateway, skeleton = _run_one_task(
        tmp_path, repo=repo, base_commit=base_commit, verify_command="test -f out.txt", solve_fn=solver,
    )

    # No more work queued.
    assert skeleton.run_once() is False

    assert gateway.completed is not None
    terminal = gateway.completed["terminal"]
    assert terminal["verification_state"] == "verified"
    assert terminal["result_ref"] == "local:ormas/job/task_1"
    assert len(terminal["result_commit"]) == 40

    workdir = skeleton.config.workdir_root / "task_1"
    assert (workdir / "out.txt").read_text().splitlines() == ["base", "mined"]


@requires_git
def test_skeleton_passes_configured_ask_to_claim(tmp_path: Path) -> None:
    """MinerConfig.ask_usd must reach the claim body — it is the miner's firm bid."""
    repo, base_commit = _init_repo(tmp_path)
    solver = make_shell_solver("echo mined >> out.txt")
    gateway, _ = _run_one_task(
        tmp_path, repo=repo, base_commit=base_commit, verify_command="test -f out.txt", solve_fn=solver,
        ask_usd=0.75,
    )

    assert gateway.claims[0]["ask_usd"] == 0.75


@requires_git
def test_skeleton_claim_omits_ask_by_default(tmp_path: Path) -> None:
    """Default ask_usd=None sends the byte-identical two-field claim body."""
    repo, base_commit = _init_repo(tmp_path)
    solver = make_shell_solver("echo mined >> out.txt")
    gateway, _ = _run_one_task(
        tmp_path, repo=repo, base_commit=base_commit, verify_command="test -f out.txt", solve_fn=solver,
    )

    assert "ask_usd" not in gateway.claims[0]


@requires_git
def test_reference_solver_reports_complete_zero_usage(tmp_path: Path) -> None:
    """The reference solver made no model call: metering_complete=True with
    honest all-zero usage is the ONE legitimate case for reporting zeros — and
    it must come from the solver's own SolveResult, not be hardcoded by the
    skeleton (that hardcoding was the fabricated-receipt defect)."""
    repo, base_commit = _init_repo(tmp_path)
    solver = make_shell_solver("echo mined >> out.txt")
    gateway, _ = _run_one_task(
        tmp_path, repo=repo, base_commit=base_commit, verify_command="test -f out.txt", solve_fn=solver,
    )

    receipt = gateway.completed["receipt"]
    assert receipt["metering_complete"] is True
    assert receipt["actual_provider"] == "reference"
    assert receipt["model"] == "reference-shell-solver"
    assert receipt["prompt_tokens"] == 0
    assert receipt["completion_tokens"] == 0
    assert receipt["cache_read_input_tokens"] == 0
    assert receipt["cache_creation_input_tokens"] == 0
    assert receipt["reasoning_tokens"] == 0
    assert receipt["upstream_cost_usd"] == 0.0


@requires_git
def test_unknown_cost_yields_incomplete_metering_and_no_fabricated_zero(tmp_path: Path) -> None:
    """A solver that doesn't know its cost must not have that unknown coerced to
    0.0 on the wire ("unknown is never zero") — the DTO's `upstream_cost_usd` is
    nullable precisely so `None` can cross honestly, and `metering_complete`
    must flip to False so nothing downstream mistakes the gap for a real $0."""
    repo, base_commit = _init_repo(tmp_path)

    def solve(draft, workdir: Path) -> SolveResult:
        (workdir / "out.txt").write_text("base\nmined\n")
        subprocess.run(["git", "add", "-A"], cwd=workdir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "solve"], cwd=workdir, check=True, capture_output=True)
        result_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=workdir, check=True, capture_output=True, text=True,
        ).stdout.strip()
        return SolveResult(
            result_commit=result_commit,
            changed_paths=("out.txt",),
            provider="acme-model-co",
            model="acme-large",
            prompt_tokens=10,
            completion_tokens=5,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            reasoning_tokens=0,
            upstream_cost_usd=None,  # genuinely unknown
        )

    gateway, _ = _run_one_task(
        tmp_path, repo=repo, base_commit=base_commit, verify_command="test -f out.txt", solve_fn=solve,
    )

    receipt = gateway.completed["receipt"]
    assert receipt["metering_complete"] is False
    assert receipt["upstream_cost_usd"] is None
    assert "upstream_cost_usd" in receipt  # sent explicitly as null, never omitted-as-zero


@requires_git
def test_solve_outside_allowed_paths_yields_scope_violation(tmp_path: Path) -> None:
    """scope_ok must be COMPUTED from the real git diff against allowed_paths,
    not asserted True — a solve that edits a file outside allowed_paths is
    caught even though the verify command itself passes."""
    repo, base_commit = _init_repo(tmp_path)

    def solve(draft, workdir: Path) -> SolveResult:
        (workdir / "other.txt").write_text("out of scope\n")
        subprocess.run(["git", "add", "-A"], cwd=workdir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "solve"], cwd=workdir, check=True, capture_output=True)
        result_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=workdir, check=True, capture_output=True, text=True,
        ).stdout.strip()
        # Solver under-reports its own diff on purpose — the skeleton must not
        # trust this and must use git as ground truth instead.
        return SolveResult(result_commit=result_commit, changed_paths=())

    with pytest.warns(UserWarning, match="does not match git diff"):
        gateway, _ = _run_one_task(
            tmp_path, repo=repo, base_commit=base_commit, verify_command="true", solve_fn=solve,
            allowed_paths=["out.txt"],
        )

    assert gateway.completed["capture"]["scope_ok"] is False
    assert gateway.completed["capture"]["changed_paths"] == [{"path": "other.txt"}]
    assert gateway.completed["terminal"]["verification_state"] == "failed"


@requires_git
def test_verify_command_failure_yields_failed(tmp_path: Path) -> None:
    """verification_state must reflect ACTUALLY running verify_command — a
    solve that stays in scope still fails completion if the verifier itself
    exits non-zero."""
    repo, base_commit = _init_repo(tmp_path)
    solver = make_shell_solver("echo mined >> out.txt")
    gateway, _ = _run_one_task(
        tmp_path, repo=repo, base_commit=base_commit, verify_command="false", solve_fn=solver,
    )

    assert gateway.completed["capture"]["scope_ok"] is True
    assert gateway.completed["capture"]["attempts"] == [{"verify_exit_code": 1}]
    assert gateway.completed["terminal"]["verification_state"] == "failed"
