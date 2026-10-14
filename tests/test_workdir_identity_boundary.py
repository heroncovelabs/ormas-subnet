"""Untrusted task and assignment identities stay inside their local/URL boundaries."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ormas_subnet._recovery import PublicRecovery, RecoveryRequired
from ormas_subnet.client import OrmasMinerClient
from ormas_subnet.protocol import RUNNER_PROTOCOL_V1, TaskDraft, TaskLease
from ormas_subnet.skeleton import MinerConfig, MinerSkeleton
from ormas_subnet.validator import (
    VALIDATOR_DECISION_PROTOCOL_V1,
    OrmasValidatorClient,
    ValidatorConfig,
    ValidatorDaemon,
    _list_only_lines,
    canonical_evidence_fields,
    evidence_digest_hex,
)


UNSAFE_COMPONENTS = [
    "",
    ".",
    "..",
    "../outside",
    "/tmp/outside",
    "..\\outside",
    "C:\\outside",
    "\\\\server\\share",
    "nul\x00suffix",
    "x" * 129,
]


def _draft(task_id: str = "task-safe") -> TaskDraft:
    return TaskDraft(
        task_id=task_id,
        runner_id="runr_test",
        repo_id="repo_test",
        base_commit="a" * 40,
        brief="synthetic",
        verify_command="true",
        allowed_paths=("out.txt",),
        budget_usd=1.0,
        work_packet={},
        work_packet_sha256="b" * 64,
        attempt=1,
        parent_job_id="",
        repair_findings=(),
    )


def _lease(task_id: str = "task-safe") -> TaskLease:
    return TaskLease(
        lease_id="lease-safe",
        task_id=task_id,
        expires_at="2026-10-14T12:05:00Z",
        selected_cell="task:lang/python",
        provider_pin="unset",
        fallback_policy="unset",
        hold_ref="hold-safe",
        now="2026-10-14T12:00:00Z",
        outcome_price_usd=1.0,
    )


@pytest.mark.parametrize(
    "task_id",
    UNSAFE_COMPONENTS,
)
def test_task_dtos_reject_non_component_identities(task_id: str) -> None:
    with pytest.raises(ValueError, match="invalid task_id"):
        _draft(task_id)
    with pytest.raises(ValueError, match="invalid task_id"):
        _lease(task_id)


@pytest.mark.parametrize(
    "task_id",
    ["a", "task-safe", "Task_1.alpha", "a" + "." * 126 + "z"],
)
def test_task_dtos_preserve_valid_component_identities(task_id: str) -> None:
    assert _draft(task_id).task_id == task_id
    assert _lease(task_id).task_id == task_id


class _Response:
    status_code = 200

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


class _ClaimTransport:
    def __init__(self, lease: dict[str, Any], draft: dict[str, Any]) -> None:
        self.lease = lease
        self.draft = draft

    def post(self, path: str, *, json: dict[str, Any]) -> _Response:
        assert path == "/api/runner/v1/leases"
        assert json["schema_version"] == RUNNER_PROTOCOL_V1
        return _Response({"lease": self.lease, "draft": self.draft})


def test_claim_decode_rejects_lease_draft_identity_mismatch() -> None:
    client = OrmasMinerClient(
        "https://gateway.example.invalid",
        "ormr_test",
        http_client=_ClaimTransport(_lease("task-one").to_wire(), _draft("task-two").to_wire()),
    )
    with pytest.raises(ValueError, match="identity mismatch"):
        client.claim_task("runr_test")


def _miner(root: Path) -> MinerSkeleton:
    config = MinerConfig(
        runner_id="runr_test",
        runner_version="test",
        platform="test",
        capacity=1,
        cells=("task:lang/python",),
        workdir_root=root,
        repo_id="repo_test",
        repo_url="https://example.invalid/repo.git",
    )
    return MinerSkeleton(object(), config, lambda _draft, _workdir: None)  # type: ignore[arg-type]


def test_miner_direct_workdir_caller_cannot_delete_parent_sibling(tmp_path: Path) -> None:
    root = tmp_path / "work"
    root.mkdir()
    victim = tmp_path / "victim"
    victim.mkdir()
    marker = victim / "keep"
    marker.write_text("present")

    rejected = False
    try:
        _miner(root)._fresh_workdir("../victim")
    except ValueError as exc:
        rejected = "task_id" in str(exc)

    assert marker.read_text() == "present"
    assert rejected


def test_miner_refuses_symlink_workdir_leaf_without_touching_target(tmp_path: Path) -> None:
    root = tmp_path / "work"
    root.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    marker = target / "keep"
    marker.write_text("present")
    (root / "task-safe").symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        _miner(root)._fresh_workdir("task-safe")

    assert marker.read_text() == "present"


def _recovery_body(task_id: str, *, lease: TaskLease | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "version": 2,
        "gateway": "gateway.example.invalid",
        "runner_id": "runr_test",
        "task_id": task_id,
        "lease_id": "lease-safe",
        "packet_hash": "a" * 64,
        "base_commit": "b" * 40,
        "repo_url": "https://example.invalid/repo.git",
        "stage": "claimed",
    }
    if lease is not None:
        body["lease"] = lease.to_wire()
    return body


def test_recovery_rejects_invalid_task_identity_as_recovery_required(tmp_path: Path) -> None:
    store = PublicRecovery(tmp_path / "work", "gateway.example.invalid", "runr_test")
    store.save(_recovery_body("../victim"))
    with pytest.raises(RecoveryRequired, match="identity"):
        store.pending()


def test_recovery_rejects_persisted_lease_task_mismatch(tmp_path: Path) -> None:
    store = PublicRecovery(tmp_path / "work", "gateway.example.invalid", "runr_test")
    store.save(_recovery_body("task-one", lease=_lease("task-two")))
    with pytest.raises(RecoveryRequired, match="identity"):
        store.pending()


def test_recovery_begin_rejects_live_lease_task_mismatch(tmp_path: Path) -> None:
    store = PublicRecovery(tmp_path / "work", "gateway.example.invalid", "runr_test")
    with pytest.raises(RecoveryRequired, match="identity"):
        store.begin(_lease("task-one"), _draft("task-two"))
    assert not store.path.exists()


def test_direct_recovery_rejects_invalid_task_identity(tmp_path: Path) -> None:
    skeleton = _miner(tmp_path / "work")
    with pytest.raises(RecoveryRequired, match="identity"):
        skeleton._recover_public(_recovery_body("../victim"))


def _assignment(assignment_id: str = "asgn_valid") -> dict[str, Any]:
    fields = canonical_evidence_fields(
        job_id="job_test",
        miner_id="miner:synthetic",
        base_commit="a" * 40,
        result_commit="b" * 40,
        repo_url="https://example.invalid/repo.git",
        verify_command="true",
        allowed_paths=["out.txt"],
        immutable_paths=[],
    )
    return {
        "assignment_id": assignment_id,
        "decision_protocol_version": VALIDATOR_DECISION_PROTOCOL_V1,
        **fields,
        "evidence_digest_sha256": evidence_digest_hex(fields),
    }


def test_direct_validator_clone_rejects_traversal_without_deleting_sibling(
    tmp_path: Path,
) -> None:
    root = tmp_path / "validator-work"
    victim = tmp_path / "victim"
    victim.mkdir()
    marker = victim / "keep"
    marker.write_text("present")

    daemon = ValidatorDaemon(
        object(), ValidatorConfig(workdir_root=root), lambda _digest: "signature"  # type: ignore[arg-type]
    )

    with pytest.raises(ValueError, match="assignment_id"):
        daemon._clone(_assignment("../victim"))

    assert marker.read_text() == "present"


def test_validator_clone_refuses_symlink_leaf_without_touching_target(tmp_path: Path) -> None:
    root = tmp_path / "validator-work"
    root.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    marker = target / "keep"
    marker.write_text("present")
    (root / "asgn_valid").symlink_to(target, target_is_directory=True)
    daemon = ValidatorDaemon(
        object(), ValidatorConfig(workdir_root=root), lambda _digest: "signature"  # type: ignore[arg-type]
    )

    with pytest.raises(ValueError, match="symlink"):
        daemon._clone(_assignment())

    assert marker.read_text() == "present"


class _ValidatorClient:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.decisions: list[tuple[str, str, str]] = []

    def list_assignments(self) -> list[dict[str, Any]]:
        return list(self.rows)

    def post_decision(self, assignment_id: str, *, decision: str, signature_hex: str) -> dict:
        self.decisions.append((assignment_id, decision, signature_hex))
        return {}


class _RecordingProtected:
    def __init__(self) -> None:
        self.assignment_ids: list[str] = []

    def fetch_read_token(self, assignment_id: str) -> SimpleNamespace:
        self.assignment_ids.append(assignment_id)
        return SimpleNamespace(token="synthetic", expires_at="2999-01-01T00:00:00Z")


def test_invalid_protected_stub_is_skipped_before_attestation_and_valid_row_runs(
    tmp_path: Path,
) -> None:
    invalid = "../secret\nforged-control-line"
    valid = {**_assignment(), "service_level": "protected"}
    client = _ValidatorClient([
        {
            "assignment_id": invalid,
            "service_level": "protected",
            "attestation_required": True,
        },
        valid,
    ])
    protected = _RecordingProtected()
    daemon = ValidatorDaemon(
        client,  # type: ignore[arg-type]
        ValidatorConfig(workdir_root=tmp_path / "validator-work"),
        lambda _digest: "signature",
        protected=protected,
    )
    daemon._clone_with_retry = lambda _assignment: tmp_path  # type: ignore[method-assign]
    daemon._decide = lambda _assignment, _workdir: "reject"  # type: ignore[method-assign]

    assert daemon.run_once() is True
    assert protected.assignment_ids == []
    assert client.decisions == [("asgn_valid", "reject", "signature")]


def test_list_only_does_not_emit_invalid_assignment_control_data() -> None:
    invalid = "../secret\nforged-control-line"
    lines = _list_only_lines([
        {"assignment_id": invalid, "job_id": "job_test"},
        {"assignment_id": "asgn_valid", "job_id": "job_test"},
    ])
    rendered = "\n".join(lines)
    assert invalid not in rendered
    assert "forged-control-line" not in rendered
    assert lines[0].startswith("- ")
    assert lines[1].startswith("asgn_valid ")


@pytest.mark.parametrize("assignment_id", UNSAFE_COMPONENTS)
def test_validator_client_refuses_unsafe_assignment_url_before_transport(
    assignment_id: str,
) -> None:
    class NoTransport:
        def post(self, *_args, **_kwargs):
            pytest.fail("transport reached")

    client = OrmasValidatorClient(
        "https://gateway.example.invalid", "ormv_test", http_client=NoTransport()
    )
    with pytest.raises(ValueError, match="assignment_id"):
        client.post_decision(assignment_id, decision="error", signature_hex="00")
