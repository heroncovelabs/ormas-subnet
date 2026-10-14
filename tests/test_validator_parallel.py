"""The reference checker keeps its established inline review sequence."""
from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from typing import Any, Callable

import pytest

from ormas_subnet.validator import (
    VALIDATOR_DECISION_PROTOCOL_V2,
    OrmasValidatorClient,
    ValidatorConfig,
    ValidatorDaemon,
    canonical_evidence_fields,
    evidence_digest_hex,
)
from tests.test_skeleton import _FakeResponse


class _MultiGateway:
    """Validator routes for several assignments, same shape as ``_FakeValidatorGateway``."""

    def __init__(self, assignments: list[dict[str, Any]]) -> None:
        self._pending = [dict(item) for item in assignments]
        self.decisions: list[dict[str, Any]] = []

    def post(self, path: str, json: dict[str, Any] | None = None, headers: Any = None) -> _FakeResponse:
        del headers
        body = json or {}
        if path == "/api/validator/v1/registrations":
            return _FakeResponse(200, {"validator_id": "val_test"})
        if path.endswith("/decisions"):
            assignment_id = path.rstrip("/").split("/")[-2]
            self.decisions.append({"assignment_id": assignment_id, **body})
            self._pending = [item for item in self._pending if item.get("assignment_id") != assignment_id]
            return _FakeResponse(200, {"decision": body.get("decision")})
        raise AssertionError(f"unhandled path: {path}")

    def get(self, path: str) -> _FakeResponse:
        if path == "/api/validator/v1/assignments":
            return _FakeResponse(200, {"assignments": [dict(item) for item in self._pending]})
        raise AssertionError(f"unexpected GET: {path}")


def _assignment(assignment_id: str, *, repo: Path, decision: str) -> dict[str, Any]:
    fields = canonical_evidence_fields(
        job_id=f"job_{assignment_id}",
        miner_id="miner:other-tenant",
        base_commit="a" * 40,
        result_commit="b" * 40,
        repo_url=str(repo),
        verify_command="true",
        allowed_paths=["out.txt"],
        immutable_paths=[],
    )
    return {
        "assignment_id": assignment_id,
        "validator_id": "val_test",
        "decision_protocol_version": VALIDATOR_DECISION_PROTOCOL_V2,
        "decision_nonce": hashlib.sha256(assignment_id.encode()).hexdigest(),
        "validator_key_generation": 1,
        "validator_pubkey_hex": "ab" * 32,
        "decision_expires_at": "2099-01-01T00:00:00Z",
        **fields,
        "evidence_digest_sha256": evidence_digest_hex(fields),
        "fake_decision": decision,
    }


def _mismatch(assignment_id: str, *, repo: Path) -> dict[str, Any]:
    assignment = _assignment(assignment_id, repo=repo, decision="accept")
    assignment["evidence_digest_sha256"] = "0" * 64
    return assignment


def _drive(daemon: ValidatorDaemon, *, slots: int, poll_interval_s: float, until: Callable[[], bool]) -> None:
    serve = getattr(daemon, "serve", None)
    if serve is not None:
        serve(slots, poll_interval_s, until=until)
        return
    while True:
        did = daemon.run_once()
        if until():
            return
        if not did:
            return


def _daemon(tmp_path: Path, gateway: _MultiGateway, sign_log: list[dict[str, Any]]) -> ValidatorDaemon:
    client = OrmasValidatorClient(base_url="https://fake.invalid", token="ormv_test", http_client=gateway)

    def sign_fn(digest: str) -> str:
        sign_log.append({"pid": os.getpid(), "digest": digest})
        return "ab" * 64

    return ValidatorDaemon(client, ValidatorConfig(workdir_root=tmp_path / "validator-work"), sign_fn)


def _until_decisions(gateway: _MultiGateway, want: dict[str, str]):
    started = time.time()

    def until() -> bool:
        by_id = {row["assignment_id"]: row["decision"] for row in gateway.decisions}
        if all(by_id.get(key) == value for key, value in want.items()):
            return True
        # Wrong terminal decisions are a failure, not a reason to wait out the fuse.
        if set(want) <= set(by_id) and time.time() - started > 0.5:
            return True
        return time.time() - started > 20

    return until


def _assert_signed_only_in_parent(sign_log: list[dict[str, Any]], decisions: list[dict[str, Any]]) -> None:
    assert sign_log, "signer was never called"
    assert [row["pid"] for row in sign_log] == [os.getpid()] * len(decisions)
    assert len(sign_log) == len(decisions)


def _sequence(tmp_path: Path, *, slots: int | None) -> tuple[list[str], list[dict[str, Any]], _MultiGateway]:
    gateway = _MultiGateway([_mismatch("asgn_seq", repo=tmp_path / "missing.git")])
    events: list[str] = []
    sign_log: list[dict[str, Any]] = []
    daemon = _daemon(tmp_path, gateway, sign_log)
    client = daemon.client
    orig_list = client.list_assignments
    orig_post = client.post_decision

    def list_assignments() -> list[dict[str, Any]]:
        events.append("list_assignments")
        return orig_list()

    def post_decision(assignment_id: str, *, decision: str, signature_hex: str) -> dict[str, Any]:
        events.append("post_decision")
        return orig_post(assignment_id, decision=decision, signature_hex=signature_hex)

    client.list_assignments = list_assignments  # type: ignore[method-assign]
    client.post_decision = post_decision  # type: ignore[method-assign]
    real_sign = daemon.sign_fn

    def sign_fn(digest: str) -> str:
        events.append("sign")
        return real_sign(digest)

    daemon.sign_fn = sign_fn
    if slots is None:
        assert daemon.run_once() is True
    else:
        _drive(daemon, slots=slots, poll_interval_s=0.05, until=lambda: len(gateway.decisions) >= 1)
    return events, sign_log, gateway


def test_slots_one_keeps_todays_call_sequence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import multiprocessing

    contexts: list[Any] = []
    real_context = multiprocessing.get_context

    def spy_context(*args: Any, **kwargs: Any):
        contexts.append(args[0] if args else kwargs.get("method"))
        return real_context(*args, **kwargs)

    monkeypatch.setattr(multiprocessing, "get_context", spy_context)
    via_slots, sign_log, gateway = _sequence(tmp_path / "slots", slots=1)
    direct, direct_log, direct_gateway = _sequence(tmp_path / "direct", slots=None)
    assert via_slots == ["list_assignments", "sign", "post_decision"]
    assert direct == via_slots
    assert [row["decision"] for row in gateway.decisions] == [row["decision"] for row in direct_gateway.decisions]
    assert contexts == []
    _assert_signed_only_in_parent(sign_log, gateway.decisions)
    _assert_signed_only_in_parent(direct_log, direct_gateway.decisions)
