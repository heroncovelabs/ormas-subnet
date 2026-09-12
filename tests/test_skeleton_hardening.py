"""Card 5f129fc0: the reference skeleton survives what an outside miner will hit on day two.

Found by the 2026-09-12 fresh-clone rehearsal:
1. ``register()`` returns the gateway's cadences but the skeleton polls/heartbeats on module
   constants — a gateway change silently desynchronises every miner.
2. A crashed job leaves its workdir; the same lease is re-served on restart and
   ``_fresh_workdir`` raises ``workdir already exists`` forever.
3. A raising ``solve`` (or a git failure) escapes ``run_once``; nothing is reported and the
   lease expires server-side.
4. An HTTP error surfaces as a bare status code; the gateway's structured error body
   (``{"error": {"type": ..., "message": ...}}``, docs/protocol.md) is never shown.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from ormas_subnet import MinerConfig, MinerSkeleton, OrmasMinerClient
from ormas_subnet.localnet import LocalGateway, LocalResponse
from ormas_subnet.skeleton import SolveResult

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from neurons.localnet_demo import (  # noqa: E402
    ALLOWED_PATHS,
    BRIEF,
    TASK_ID,
    VERIFY_COMMAND,
    _make_client_bare_repo,
    _solve_pass,
)


def _wire(tmp_path: Path, solve_fn, **gateway_kwargs):
    bare_repo, base_commit = _make_client_bare_repo(tmp_path)
    gateway = LocalGateway(
        task_id=TASK_ID, base_commit=base_commit, verify_command=VERIFY_COMMAND,
        repo_url=str(bare_repo), brief=BRIEF, allowed_paths=ALLOWED_PATHS, **gateway_kwargs,
    )
    client = OrmasMinerClient(base_url="local://test", token="no-token", http_client=gateway)
    config = MinerConfig(
        runner_id="hardening-miner", runner_version="0.0.1", platform="local", capacity=1,
        cells=("code-edit-small",), workdir_root=tmp_path / "work", repo_id="repo1",
        repo_url=str(bare_repo), push_remote="origin",
    )
    skeleton = MinerSkeleton(client, config, solve_fn)
    skeleton.register()
    skeleton.bind(project_id="p", base_commit=base_commit)
    return gateway, skeleton


class _CadenceGateway(LocalGateway):
    """A gateway that advertises non-default cadences at registration."""

    def _handle_registration(self, body):
        resp = super()._handle_registration(body)
        payload = resp.json()
        payload.update({"poll_interval_s": 3, "heartbeat_s": 7, "lease_ttl_s": 40})
        return LocalResponse(200, payload)


def test_register_adopts_the_gateway_cadences(tmp_path: Path) -> None:
    bare_repo, base_commit = _make_client_bare_repo(tmp_path)
    gateway = _CadenceGateway(
        task_id=TASK_ID, base_commit=base_commit, verify_command=VERIFY_COMMAND,
        repo_url=str(bare_repo), brief=BRIEF, allowed_paths=ALLOWED_PATHS,
    )
    client = OrmasMinerClient(base_url="local://test", token="no-token", http_client=gateway)
    config = MinerConfig(
        runner_id="m", runner_version="0.0.1", platform="local", capacity=1,
        cells=("code-edit-small",), workdir_root=tmp_path / "work", repo_id="repo1",
        repo_url=str(bare_repo),
    )
    skeleton = MinerSkeleton(client, config, _solve_pass)
    skeleton.register()
    assert skeleton.poll_interval_s == 3
    assert skeleton.config.heartbeat_interval_s == 7


def test_leftover_workdir_from_a_crashed_run_does_not_wedge_the_miner(tmp_path: Path) -> None:
    gateway, skeleton = _wire(tmp_path, _solve_pass)
    stale = skeleton.config.workdir_root / TASK_ID
    stale.mkdir(parents=True)
    (stale / "half-written").write_text("crash residue\n")

    assert skeleton.run_once() is True
    assert gateway.receipts[-1]["settlement"] == "paid"
    assert not (stale / "half-written").exists()


def test_a_raising_solve_posts_a_failed_terminal_and_returns(tmp_path: Path) -> None:
    def exploding_solve(draft, workdir):  # noqa: ANN001
        raise RuntimeError("model endpoint down")

    gateway, skeleton = _wire(tmp_path, exploding_solve)
    assert skeleton.run_once() is True  # the job was claimed and handled, not lost
    assert gateway.completed, "a terminal must be reported so the lease does not silently expire"
    receipt = gateway.receipts[-1]
    assert receipt["settlement"] == "no_delivery"
    assert receipt["verification_state"] != "verified"


class _HttpxLikeResponse:
    """Mimics httpx: ``raise_for_status`` names only the status, never the body."""

    status_code = 401

    def raise_for_status(self) -> None:
        raise RuntimeError("Client error '401 Unauthorized' for url 'local://test/api/runner/v1/registrations'")

    def json(self):
        return {"error": {"type": "invalid_token", "message": "token revoked or unknown"}}

    @property
    def text(self) -> str:
        return '{"error": {"type": "invalid_token", "message": "token revoked or unknown"}}'


class _UnauthorizedGateway(LocalGateway):
    def post(self, path, json=None, headers=None):  # noqa: ANN001
        return _HttpxLikeResponse()


def test_http_errors_surface_the_gateway_error_body(tmp_path: Path) -> None:
    bare_repo, base_commit = _make_client_bare_repo(tmp_path)
    gateway = _UnauthorizedGateway(
        task_id=TASK_ID, base_commit=base_commit, verify_command=VERIFY_COMMAND,
        repo_url=str(bare_repo), brief=BRIEF, allowed_paths=ALLOWED_PATHS,
    )
    client = OrmasMinerClient(base_url="local://test", token="bad", http_client=gateway)
    config = MinerConfig(
        runner_id="m", runner_version="0.0.1", platform="local", capacity=1,
        cells=("code-edit-small",), workdir_root=tmp_path / "work", repo_id="repo1",
        repo_url=str(bare_repo),
    )
    skeleton = MinerSkeleton(client, config, _solve_pass)
    with pytest.raises(Exception) as excinfo:
        skeleton.register()
    text = str(excinfo.value)
    assert "invalid_token" in text and "token revoked or unknown" in text


def test_solve_result_type_is_unchanged() -> None:
    """Guard: the hardening must not change the miner-facing SolveResult contract."""
    fields = set(SolveResult.__dataclass_fields__)
    assert {"result_commit"} <= fields
