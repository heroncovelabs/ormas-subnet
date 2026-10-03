"""A restarted public miner replays completion without solving again."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import hashlib
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from ormas_subnet.client import OrmasGatewayError
from ormas_subnet.protocol import TaskLease
from ormas_subnet.skeleton import MinerConfig, MinerSkeleton, SolveResult


class _RecoveryClient:
    """Re-serve one lease until its frozen completion is acknowledged."""

    def __init__(self, lease, draft, completion_results, publication_results=()) -> None:
        self.base_url = "https://gateway.example.invalid"
        self.lease = lease
        self.draft = draft
        self.completion_results = list(completion_results)
        self.publication_results = list(publication_results)
        self.completed = False
        self.claims = 0
        self.publications = 0
        self.completions: list[dict] = []
        self.events: list[str] = []

    def claim_task(self, runner_id, *, ask_usd=None):
        del runner_id, ask_usd
        self.claims += 1
        self.events.append("claim")
        if self.completed:
            return None
        return self.lease, self.draft

    def heartbeat_task(self, *args, **kwargs):
        del args, kwargs
        return {"ok": True}

    def publish_result(self, task_id, runner_id, lease_token, *, source, length):
        del task_id, runner_id, lease_token
        assert len(source.read()) == length
        self.publications += 1
        self.events.append("publish")
        result = self.publication_results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def complete_task(
        self,
        task_id,
        runner_id,
        lease_token,
        *,
        receipt,
        terminal,
        capture,
        **kwargs,
    ):
        del kwargs
        request = {
            "task_id": task_id,
            "runner_id": runner_id,
            "lease_token": lease_token,
            "receipt": receipt.to_wire(),
            "terminal": terminal.to_wire(),
            "capture": deepcopy(dict(capture)),
        }
        self.completions.append(request)
        self.events.append("complete")
        result = self.completion_results.pop(0)
        if isinstance(result, BaseException):
            raise result
        if result.get("status") != "settling":
            self.completed = True
        return result


def _lease_and_draft():
    lease = TaskLease(
        lease_id="lease-public", task_id="task-public", now="2026-09-23T12:00:00Z",
        expires_at="2026-09-23T12:05:00Z", selected_cell="task:lang/python",
        provider_pin="unset", fallback_policy="unset", hold_ref="unset", outcome_price_usd=1.25,
    )
    draft = SimpleNamespace(
        task_id="task-public",
        runner_id="miner-public",
        base_commit="a" * 40,
        verify_command="python -m pytest -q tests/test_value.py",
        allowed_paths=("value.py",),
        work_packet={"schema_version": "ormas.public-execution-packet.v2"},
        work_packet_sha256="b" * 64,
        repo_url="https://github.com/acme/demo.git",
        repo_credential=None,
    )
    return lease, draft


def _config(root: Path) -> MinerConfig:
    return MinerConfig(
        runner_id="miner-public",
        runner_version="test",
        platform="linux",
        capacity=1,
        cells=("task:lang/python",),
        workdir_root=root,
        repo_id="repo-public",
        repo_url="https://github.com/acme/demo.git",
        push_remote=None,
    )


def _patch_public_loop(monkeypatch, client, *, patch_publish=True):
    verify_calls = 0

    def clone(_self, current_draft):
        workdir = _self.config.workdir_root / current_draft.task_id
        workdir.mkdir(parents=True, exist_ok=True)
        return workdir

    def verify(*_args, **_kwargs):
        nonlocal verify_calls
        verify_calls += 1
        return 86 if verify_calls % 2 else 0

    def publish(_self, _lease, current_draft, _workdir, result, *, capture=None):
        del capture
        client.publications += 1
        return (
            f"refs/heads/ormas/job/{current_draft.task_id}",
            SolveResult(**{**result.__dict__, "result_commit": "d" * 40}),
        )

    monkeypatch.setattr("ormas_subnet.skeleton._public_packet", lambda _draft: {})
    monkeypatch.setattr("ormas_subnet.skeleton._run_verify_command", verify)
    monkeypatch.setattr(MinerSkeleton, "_clone_and_checkout", clone)
    if patch_publish:
        monkeypatch.setattr(MinerSkeleton, "_publish_public", publish)
    monkeypatch.setattr(MinerSkeleton, "_material_diff_sha256", lambda *_args: "e" * 64)
    monkeypatch.setattr(
        MinerSkeleton,
        "_compute_scope",
        lambda *_args: (True, ("value.py",)),
    )


def _successful_result() -> SolveResult:
    return SolveResult(
        result_commit="c" * 40,
        changed_paths=("value.py",),
        provider="synthetic",
        model="synthetic-shell",
        prompt_tokens=0,
        completion_tokens=0,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
        reasoning_tokens=0,
        upstream_cost_usd=0.0,
    )


_DONE = {
    "status": "done",
    "receipt": {"receipt_id": "receipt-public", "settlement": "pending_acceptance"},
}
_OUTCOME_UNKNOWN = {"status": "outcome_unknown"}


@pytest.mark.parametrize("limit_offer", [False, True])
def test_restart_replays_ambiguous_public_completion_without_another_solve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limit_offer: bool,
) -> None:
    """The completion body must survive a process loss before its response.

    The gateway can re-serve a still-running lease after the request is lost.
    Recovery must replay the exact completion request before another claim; it
    must not delete the candidate checkout and invoke the solver/provider again.
    """
    lease, draft = _lease_and_draft()
    client = _RecoveryClient(
        lease,
        draft,
        [
            KeyboardInterrupt("synthetic process loss after the request left the miner"),
            _DONE,
        ],
    )
    solve_calls: list[str] = []

    def solve(current_draft, _workdir):
        solve_calls.append(current_draft.task_id)
        return _successful_result()

    _patch_public_loop(monkeypatch, client)

    settlement_calls = []

    def settle(current_lease, result):
        settlement_calls.append((current_lease, result))
        return 0.75

    if limit_offer:
        lease = replace(lease, offer_kind="limit", estimate_usd=0.9, limit_usd=1.25, bid_id="bid-public")
        client.lease = lease
    config = _config(tmp_path / "work")
    config.settle_fn = settle
    first = MinerSkeleton(client, config, solve)
    with pytest.raises(KeyboardInterrupt, match="synthetic process loss"):
        first.run_once()

    restarted = MinerSkeleton(client, config, solve)
    restarted.run_once()

    if limit_offer:
        assert client.completions[0]["terminal"]["settled_price_usd"] == 0.75
        assert len(settlement_calls) == 1, "replay must not reprice a frozen completion"
    else:
        assert "settled_price_usd" not in client.completions[0]["terminal"]
        assert settlement_calls == []

    assert solve_calls == ["task-public"], "restart must not invoke the solver/provider again"
    assert client.publications == 1, "restart must not create another public artifact"
    assert len(client.completions) == 2
    assert client.completions[1] == client.completions[0], (
        "restart must replay the exact durable completion identity"
    )
    assert client.completed is True


def test_public_base_exit_zero_fails_non_red_without_solve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A base that already passes is a typed no-delivery, not a provider run."""
    lease, draft = _lease_and_draft()
    client = _RecoveryClient(lease, draft, [{
        "status": "failed",
        "receipt": {"receipt_id": "receipt-non-red", "settlement": "no_delivery"},
    }])
    solve_calls: list[str] = []

    def solve(current_draft, _workdir):
        solve_calls.append(current_draft.task_id)
        raise AssertionError("solve_fn must not run when the base already passes")

    def clone(_self, current_draft):
        workdir = _self.config.workdir_root / current_draft.task_id
        workdir.mkdir(parents=True, exist_ok=True)
        return workdir

    monkeypatch.setattr("ormas_subnet.skeleton._public_packet", lambda _draft: {})
    monkeypatch.setattr("ormas_subnet.skeleton._run_verify_command", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(MinerSkeleton, "_clone_and_checkout", clone)

    skeleton = MinerSkeleton(client, _config(tmp_path / "work"), solve)
    assert skeleton.run_once() is True

    assert solve_calls == []
    assert client.publications == 0
    assert len(client.completions) == 1
    body = client.completions[0]
    assert body["terminal"]["verification_state"] == "failed"
    assert body["terminal"]["result_ref"] is None
    assert body["terminal"]["result_commit"] == ""
    assert body["capture"]["failure_class"] == "base_preflight_non_red"
    assert body["receipt"]["upstream_cost_usd"] == 0.0
    assert body["receipt"]["metering_complete"] is True
    assert body["receipt"]["prompt_tokens"] == 0


def test_restart_reports_interrupted_pre_artifact_work_without_solving_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease, draft = _lease_and_draft()
    client = _RecoveryClient(lease, draft, [_OUTCOME_UNKNOWN])
    solve_calls: list[str] = []

    def interrupted_solve(current_draft, _workdir):
        solve_calls.append(current_draft.task_id)
        raise KeyboardInterrupt("synthetic process loss during solve")

    _patch_public_loop(monkeypatch, client)
    first = MinerSkeleton(client, _config(tmp_path / "work"), interrupted_solve)
    with pytest.raises(KeyboardInterrupt, match="during solve"):
        first.run_once()

    before_restart = len(client.events)
    restarted = MinerSkeleton(client, _config(tmp_path / "work"), interrupted_solve)
    restarted.run_once()

    assert client.events[before_restart] == "complete", "recovery must run before another claim"
    assert solve_calls == ["task-public"]
    assert client.publications == 0
    assert len(client.completions) == 1
    terminal = client.completions[0]["terminal"]
    assert terminal["verification_state"] == "aborted"
    assert terminal["result_ref"] is None
    assert terminal["result_commit"] == ""
    assert client.completions[0]["receipt"]["metering_complete"] is False


def test_settling_completion_stays_pending_and_replays_exactly_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease, draft = _lease_and_draft()
    client = _RecoveryClient(
        lease,
        draft,
        [
            {"status": "settling", "receipt": {"settlement": "pending_acceptance"}},
            _DONE,
        ],
    )
    solve_calls: list[str] = []

    def solve(current_draft, _workdir):
        solve_calls.append(current_draft.task_id)
        return _successful_result()

    _patch_public_loop(monkeypatch, client)
    first = MinerSkeleton(client, _config(tmp_path / "work"), solve)
    first.run_once()

    before_restart = len(client.events)
    restarted = MinerSkeleton(client, _config(tmp_path / "work"), solve)
    restarted.run_once()

    assert client.events[before_restart] == "complete", "pending completion must replay first"
    assert solve_calls == ["task-public"]
    assert client.publications == 1
    assert len(client.completions) == 2
    assert client.completions[1] == client.completions[0]


def test_corrupt_recovery_record_refuses_new_work_without_another_solve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease, draft = _lease_and_draft()
    client = _RecoveryClient(lease, draft, [_OUTCOME_UNKNOWN])
    solve_calls: list[str] = []

    def interrupted_solve(current_draft, _workdir):
        solve_calls.append(current_draft.task_id)
        raise KeyboardInterrupt("synthetic process loss during solve")

    _patch_public_loop(monkeypatch, client)
    root = tmp_path / "work"
    first = MinerSkeleton(client, _config(root), interrupted_solve)
    with pytest.raises(KeyboardInterrupt, match="during solve"):
        first.run_once()

    records = list((root / ".ormas-recovery").glob("*/pending.json"))
    assert len(records) == 1
    records[0].write_text("{not valid json", encoding="utf-8")

    restarted = MinerSkeleton(client, _config(root), interrupted_solve)
    with pytest.raises(RuntimeError, match="recover|corrupt"):
        restarted.run_once()

    assert solve_calls == ["task-public"]
    assert client.claims == 1
    assert client.publications == 0
    assert client.completions == []


def test_lease_lost_while_republishing_archives_recovery_without_another_solve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease, draft = _lease_and_draft()
    lease_lost = OrmasGatewayError(
        status_code=409,
        error_type="lease_lost",
        message="lease expired while the miner was restarting",
    )
    client = _RecoveryClient(
        lease,
        draft,
        [],
        publication_results=[
            KeyboardInterrupt("synthetic process loss during publication"),
            lease_lost,
        ],
    )
    solve_calls: list[str] = []

    def solve(current_draft, _workdir):
        solve_calls.append(current_draft.task_id)
        return _successful_result()

    def build_artifact(_draft, _workdir, _result, target):
        artifact = b"frozen-public-artifact"
        target.write(artifact)
        target.seek(0)
        return len(artifact), hashlib.sha256(artifact).hexdigest(), "f" * 40

    _patch_public_loop(monkeypatch, client, patch_publish=False)
    monkeypatch.setattr("ormas_subnet.skeleton._build_public_artifact", build_artifact)
    root = tmp_path / "work"
    first = MinerSkeleton(client, _config(root), solve)
    with pytest.raises(KeyboardInterrupt, match="during publication"):
        first.run_once()

    restarted = MinerSkeleton(client, _config(root), solve)
    try:
        restarted.run_once()
    except OrmasGatewayError as exc:
        pytest.fail(f"a dead publication lease must be tombstoned, not retried forever: {exc}")

    assert solve_calls == ["task-public"]
    assert client.publications == 2
    assert client.completions == []
    assert list((root / ".ormas-recovery").glob("*/pending.json")) == []
    assert len(list((root / ".ormas-recovery").glob("*/*.json"))) == 1


@pytest.mark.parametrize(
    "response",
    [
        {"status": "done"},
        {"status": "done", "receipt": {}},
    ],
)
def test_malformed_success_response_never_discards_frozen_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, response,
) -> None:
    lease, draft = _lease_and_draft()
    client = _RecoveryClient(lease, draft, [response])
    solve_calls: list[str] = []

    def solve(current_draft, _workdir):
        solve_calls.append(current_draft.task_id)
        return _successful_result()

    _patch_public_loop(monkeypatch, client)
    root = tmp_path / "work"
    skeleton = MinerSkeleton(client, _config(root), solve)
    with pytest.raises(RuntimeError, match="recover|acknowledge|receipt"):
        skeleton.run_once()

    assert solve_calls == ["task-public"]
    assert client.publications == 1
    assert len(client.completions) == 1
    records = list((root / ".ormas-recovery").glob("*/pending.json"))
    assert len(records) == 1, "an unproved acknowledgement must retain exact replay state"


def test_concurrent_process_owner_cannot_claim_or_solve_same_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease, draft = _lease_and_draft()
    client = _RecoveryClient(lease, draft, [_DONE])
    solve_calls: list[str] = []
    entered = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []

    def solve(current_draft, _workdir):
        solve_calls.append(current_draft.task_id)
        entered.set()
        assert release.wait(5), "test did not release the active miner"
        return _successful_result()

    _patch_public_loop(monkeypatch, client)
    root = tmp_path / "work"
    active = MinerSkeleton(client, _config(root), solve)

    def run_active():
        try:
            active.run_once()
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    thread = threading.Thread(target=run_active)
    thread.start()
    assert entered.wait(5), "active miner did not reach the solver"
    competing = MinerSkeleton(client, _config(root), solve)
    try:
        with pytest.raises(RuntimeError, match="another miner owns"):
            competing.run_once()
        assert client.claims == 1
        assert solve_calls == ["task-public"]
    finally:
        release.set()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert errors == []


@pytest.mark.parametrize("use_settle_hook", [False, True])
def test_artifact_recovery_keeps_limit_terms_for_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, use_settle_hook: bool,
) -> None:
    from ormas_subnet._recovery import PublicRecovery

    lease, draft = _lease_and_draft()
    lease = replace(lease, offer_kind="limit", estimate_usd=0.9, limit_usd=1.25, bid_id="bid-public")
    client = _RecoveryClient(lease, draft, [_DONE])
    root = tmp_path / "work"
    (root / draft.task_id).mkdir(parents=True)
    store = PublicRecovery(root, client.base_url, "miner-public")
    store.begin(lease, draft)

    def build(target):
        target.write(b"artifact")
        return 8, hashlib.sha256(b"artifact").hexdigest(), "f" * 40

    store.checkpoint_artifact(build, result=_successful_result().__dict__, capture={
        "status": "verified", "scope_ok": True, "changed_paths": [{"path": "value.py"}],
    })
    _patch_public_loop(monkeypatch, client)

    def solve(_draft, _workdir):
        pytest.fail("artifact recovery must not invoke solve_fn")

    def settle(current_lease, result):
        assert current_lease == lease, "recovery must preserve the complete lease passed to settle_fn"
        assert result.result_commit == "d" * 40
        return 0.75

    config = _config(root)
    config.settle_fn = settle if use_settle_hook else None
    assert MinerSkeleton(client, config, solve).run_once() is True
    assert client.claims == 0
    assert client.completions[0]["terminal"]["settled_price_usd"] == (
        0.75 if use_settle_hook else 1.25
    )


def test_raising_settle_hook_sends_no_public_completion_until_it_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raising hook never becomes a repriced or failed completion of published work."""
    from ormas_subnet._recovery import PublicRecovery

    lease, draft = _lease_and_draft()
    lease = replace(lease, offer_kind="limit", estimate_usd=0.9, limit_usd=1.25, bid_id="bid-public")
    client = _RecoveryClient(lease, draft, [_DONE])
    root = tmp_path / "work"
    (root / draft.task_id).mkdir(parents=True)
    store = PublicRecovery(root, client.base_url, "miner-public")
    store.begin(lease, draft)

    def build(target):
        target.write(b"artifact")
        return 8, hashlib.sha256(b"artifact").hexdigest(), "f" * 40

    store.checkpoint_artifact(build, result=_successful_result().__dict__, capture={
        "status": "verified", "scope_ok": True, "changed_paths": [{"path": "value.py"}],
    })
    _patch_public_loop(monkeypatch, client)

    def solve(_draft, _workdir):
        pytest.fail("artifact recovery must not invoke solve_fn")

    def broken(_lease, _result):
        raise RuntimeError("synthetic settle hook failure")

    config = _config(root)
    config.settle_fn = broken
    for _ in range(2):
        with pytest.raises(RuntimeError, match="synthetic settle hook failure"):
            MinerSkeleton(client, config, solve).run_once()
        assert client.completions == []
        assert client.claims == 0
        assert store.pending()["stage"] == "artifact"

    config.settle_fn = lambda _lease, _result: 0.75
    assert MinerSkeleton(client, config, solve).run_once() is True
    assert [c["terminal"]["settled_price_usd"] for c in client.completions] == [0.75]
    assert store.pending() is None
