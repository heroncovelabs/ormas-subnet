"""U3a: a Protected checker reviews several assignments at once and proves its slot count.

Design: docs/design/miner_identity_concurrency_2026_10_14.md §4 (monorepo). The parent
attests each Protected stub and hands that assignment's sealed read token to its review
child; the signer stays in the parent. At boot a checker with more than one slot runs a
concurrent residency probe and registers a slot proof signed by its decision key.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from ormas_subnet import protected_attestation as pa
from ormas_subnet import validator
from ormas_subnet.validator import (
    SLOT_PROOF_DOMAIN,
    SLOT_PROOF_SCHEMA,
    OrmasValidatorClient,
    ValidatorConfig,
    ValidatorDaemon,
    build_slot_proof,
    canonical_evidence_fields,
    evidence_digest_hex,
    make_ed25519_signer,
    measure_concurrent_slots,
    slot_proof_message,
)
from tests.test_skeleton import _FakeResponse

TOKEN_PREFIX = "ghs_protected-token-"
EXPIRES = "2999-01-01T00:00:00+00:00"
EVIDENCE = {"kind": "tdx", "quote_b64": "cXVvdGU=", "event_log_json": "[]"}


def protected_fake_review(workdir_root: str, assignment: dict[str, Any]) -> str:
    """Review child stand-in: records the token it was handed and waits for its peers."""
    del workdir_root
    root = Path(os.environ["U3A_REVIEW_ROOT"])
    aid = str(assignment["assignment_id"])
    token = validator._take_protected_read_token(assignment)
    (root / f"token-{aid}").write_text(json.dumps(
        None if token is None else {"token": token.token, "expires_at": token.expires_at}))
    (root / f"entered-{aid}").write_text(str(time.time()))
    deadline = time.time() + 8
    while time.time() < deadline and len(list(root.glob("entered-*"))) < 2:
        time.sleep(0.01)
    (root / f"left-{aid}").write_text(str(time.time()))
    return "accept"


def _row(aid: str) -> dict[str, Any]:
    fields = canonical_evidence_fields(
        job_id=f"job_{aid}", miner_id="miner:other", base_commit="a" * 40, result_commit="b" * 40,
        repo_url="https://github.com/acme/private.git", verify_command="true",
        allowed_paths=["out.txt"], immutable_paths=[])
    return {"assignment_id": aid, **fields, "evidence_digest_sha256": evidence_digest_hex(fields),
            "repo_credential": None, "service_level": "protected"}


class _TwoProtectedGateway:
    """Two Protected assignments: stubs until each is released, then full rows."""

    def __init__(self) -> None:
        self.full = {aid: _row(aid) for aid in ("asgn_one", "asgn_two")}
        self.released: set[str] = set()
        self.decisions: list[dict[str, Any]] = []
        self.registrations: list[dict[str, Any]] = []

    def post(self, path: str, json: dict[str, Any] | None = None, headers: Any = None):
        body = json or {}
        aid = path.rstrip("/").split("/")[-2] if "/assignments/" in path else None
        if path == "/api/validator/v1/registrations":
            self.registrations.append(body)
            return _FakeResponse(200, {"validator_id": "val_slots"})
        if path.endswith("/attestation-challenge"):
            return _FakeResponse(200, {"nonce": bytes(range(32)).hex(), "issued_at": "now",
                                       "job_id": f"job_{aid}", "attempt": 1})
        if path.endswith("/repo-credential"):
            import base64
            pub = base64.b64decode(body["enclave_pubkey"])
            self.released.add(aid)
            return _FakeResponse(200, {"repo_credential": {
                "kind": pa.SEALED_CREDENTIAL_KIND, "scheme": pa.SEALED_CREDENTIAL_SCHEME,
                "sealed": base64.b64encode(pa.seal((TOKEN_PREFIX + aid).encode(), pub)).decode(),
                "expires_at": EXPIRES}})
        if path.endswith("/decisions"):
            self.decisions.append({"assignment_id": aid, **body})
            return _FakeResponse(200, {"decision": body["decision"]})
        raise AssertionError(f"unhandled path: {path}")

    def get(self, path: str):
        assert path == "/api/validator/v1/assignments"
        decided = {row["assignment_id"] for row in self.decisions}
        rows = []
        for aid, full in self.full.items():
            if aid in decided:
                continue
            rows.append(full if aid in self.released else {
                "assignment_id": aid, "service_level": "protected", "attestation_required": True})
        return _FakeResponse(200, {"assignments": rows})


def _protected_daemon(gateway, tmp_path):
    client = OrmasValidatorClient(base_url="https://fake.invalid", token="ormv_t", http_client=gateway)
    sign_fn, pub = make_ed25519_signer("21" * 32)
    signer_pids: list[int] = []

    def parent_only_sign(message):
        signer_pids.append(os.getpid())
        return sign_fn(message)

    attestor = pa.ProtectedAssignmentAttestor(client, lambda report: EVIDENCE,
                                              signer_pubkey=bytes.fromhex(pub))
    daemon = ValidatorDaemon(client, ValidatorConfig(workdir_root=tmp_path / "vw"),
                             parent_only_sign, protected=attestor)
    return daemon, signer_pids


def test_protected_checker_reviews_two_assignments_at_once(tmp_path, monkeypatch):
    root = tmp_path / "reviews"
    root.mkdir()
    monkeypatch.setenv("U3A_REVIEW_ROOT", str(root))
    monkeypatch.setattr(validator, "review_assignment", protected_fake_review, raising=False)
    gateway = _TwoProtectedGateway()
    daemon, signer_pids = _protected_daemon(gateway, tmp_path)
    started = time.time()
    daemon.serve(2, 0.05, until=lambda: len(gateway.decisions) == 2 or time.time() - started > 20)
    assert sorted(row["assignment_id"] for row in gateway.decisions) == ["asgn_one", "asgn_two"]
    assert {row["decision"] for row in gateway.decisions} == {"accept"}
    # Both children were in review at the same time.
    entered = [float((root / f"entered-{aid}").read_text()) for aid in ("asgn_one", "asgn_two")]
    left = [float((root / f"left-{aid}").read_text()) for aid in ("asgn_one", "asgn_two")]
    assert max(entered) < min(left)
    # Each child received only its own sealed token, opened by the parent's attestation.
    for aid in ("asgn_one", "asgn_two"):
        handed = json.loads((root / f"token-{aid}").read_text())
        assert handed == {"token": TOKEN_PREFIX + aid, "expires_at": EXPIRES}
    assert signer_pids == [os.getpid(), os.getpid()]
    assert daemon._protected_tokens == {}


def test_standard_parallel_checker_never_dispatches_a_protected_stub(tmp_path, monkeypatch):
    root = tmp_path / "reviews"
    root.mkdir()
    monkeypatch.setenv("U3A_REVIEW_ROOT", str(root))
    monkeypatch.setattr(validator, "review_assignment", protected_fake_review, raising=False)
    gateway = _TwoProtectedGateway()
    client = OrmasValidatorClient(base_url="https://fake.invalid", token="ormv_t", http_client=gateway)
    sign_fn, _ = make_ed25519_signer("22" * 32)
    daemon = ValidatorDaemon(client, ValidatorConfig(workdir_root=tmp_path / "vw"), sign_fn)
    polls = []
    original = client.list_assignments

    def counting():
        polls.append(1)
        return original()

    client.list_assignments = counting  # type: ignore[method-assign]
    daemon.serve(2, 0.01, until=lambda: len(polls) >= 3)
    assert gateway.decisions == [] and gateway.released == set()
    assert list(root.iterdir()) == []


def test_cli_accepts_protected_parallel_slots(monkeypatch):
    seen = {}

    class _Stop(Exception):
        pass

    def fake_serve(self, slots, poll_interval_s, **kwargs):
        seen["slots"] = slots
        raise _Stop()

    monkeypatch.setenv("ORMAS_PROTECTED_EVIDENCE_SOURCE", "dstack")
    monkeypatch.setenv("V_TOKEN", "ormv_cli")
    monkeypatch.setenv("V_KEY", "13" * 32)
    monkeypatch.setattr(validator, "measure_concurrent_slots", lambda slots, **k: (slots, 8))
    monkeypatch.setattr(ValidatorDaemon, "serve", fake_serve)
    registrations: list[dict[str, Any]] = []
    monkeypatch.setattr(OrmasValidatorClient, "register",
                        lambda self, **kwargs: registrations.append(kwargs) or {"validator_id": "val_cli"})
    with pytest.raises(_Stop):
        validator.main(["--gateway", "https://fake.invalid", "--token-env", "V_TOKEN",
                        "--private-key-env", "V_KEY", "--slots", "3"])
    assert seen["slots"] == 3
    final = registrations[-1]
    assert final["slots"] == 3
    proof = final["slot_proof"]
    assert proof["subject_id"] == "val_cli" and proof["measured_concurrent_assignments"] == 3


# ---------------------------------------------------------------- slot probe and proof


def failing_peer_probe(index, memory_bytes, ready, release, timeout_s):
    """Probe child stand-in: only the first child becomes resident."""
    if index > 0:
        os._exit(1)
    validator._slot_probe_child(index, memory_bytes, ready, release, timeout_s)


def _cpus() -> int:
    try:
        return len(os.sched_getaffinity(0))
    except AttributeError:
        return os.cpu_count() or 1


def test_probe_measures_children_resident_at_the_same_time():
    measured, cpu_count = measure_concurrent_slots(2, per_slot_memory_mib=1, timeout_s=30)
    assert cpu_count == _cpus()
    assert measured == min(2, cpu_count)


def test_probe_counts_only_children_that_became_resident():
    measured, _ = measure_concurrent_slots(3, per_slot_memory_mib=1, timeout_s=10,
                                           target=failing_peer_probe)
    assert measured == 1


def test_slot_proof_is_signed_over_the_domain_and_canonical_body():
    sign_fn, pub = make_ed25519_signer("31" * 32)
    proof = build_slot_proof(subject_id="val_x", pubkey_hex=pub, measured=4,
                             per_slot_memory_mib=1536, cpu_count=8,
                             measured_at="2026-10-14T12:00:00Z", sign_fn=sign_fn)
    assert proof["schema_version"] == SLOT_PROOF_SCHEMA == "ormas.checker-slot-proof.v2"
    assert set(proof) == {"schema_version", "subject_id", "pubkey_hex", "measured_concurrent_assignments",
                          "per_slot_memory_mib", "cpu_count", "measured_at", "signature"}
    body = {k: v for k, v in proof.items() if k != "signature"}
    message = SLOT_PROOF_DOMAIN + json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    assert slot_proof_message(proof) == message
    Ed25519PublicKey.from_public_bytes(bytes.fromhex(pub)).verify(bytes.fromhex(proof["signature"]), message)


def test_register_sends_slots_and_proof_only_when_given():
    class _Gateway:
        def __init__(self):
            self.bodies = []

        def post(self, path, json=None, headers=None):
            self.bodies.append(json)
            return _FakeResponse(200, {"validator_id": "val_r"})

    gateway = _Gateway()
    client = OrmasValidatorClient(base_url="https://fake.invalid", token="t", http_client=gateway)
    client.register(pubkey_hex="ab" * 32)
    client.register(pubkey_hex="ab" * 32, slots=2, slot_proof={"schema_version": SLOT_PROOF_SCHEMA})
    assert "slots" not in gateway.bodies[0] and "slot_proof" not in gateway.bodies[0]
    assert gateway.bodies[1]["slots"] == 2
    assert gateway.bodies[1]["slot_proof"] == {"schema_version": SLOT_PROOF_SCHEMA}


# ---------------------------------------------------------------- Sol review (REVISE) fixes


def ready_then_exit_probe(index, memory_bytes, ready, release, timeout_s):
    """Probe child stand-in: child 1 reports ready and exits at once; child 0 is slow."""
    if index == 1:
        ready.put(index)
        ready.close()
        ready.join_thread()  # the report reaches the parent before this child exits
        os._exit(0)
    time.sleep(1.5)  # child 1 is long gone before child 0 becomes resident
    validator._slot_probe_child(index, memory_bytes, ready, release, timeout_s)


def test_probe_counts_only_children_alive_together():
    # Sol P2: a child that reported ready and died must not count as a resident slot.
    measured, _ = measure_concurrent_slots(2, per_slot_memory_mib=1, timeout_s=10,
                                           target=ready_then_exit_probe)
    assert measured == 1


class _ListingGateway(_TwoProtectedGateway):
    """A Protected listing the test edits between polls."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        super().__init__()
        self.rows = rows
        self.attested: list[str] = []

    def post(self, path: str, json: dict[str, Any] | None = None, headers: Any = None):
        if path.endswith("/attestation-challenge"):
            self.attested.append(path.rstrip("/").split("/")[-2])
        return super().post(path, json=json, headers=headers)

    def get(self, path: str):
        return _FakeResponse(200, {"assignments": [dict(row) for row in self.rows]})


def _stub(aid: str) -> dict[str, Any]:
    return {"assignment_id": aid, "service_level": "protected", "attestation_required": True}


def _fill(daemon, slots=1):
    return validator._fill_slots(daemon, None, {}, {}, set(), set(), set(), slots)


def test_a_terminal_assignments_token_releases_its_slot(tmp_path):
    # Sol P2: the assignment left the listing (decided elsewhere, timed out, reassigned).
    gateway = _ListingGateway([_stub("asgn_gone")])
    daemon, _ = _protected_daemon(gateway, tmp_path)
    _fill(daemon)
    assert gateway.attested == ["asgn_gone"]
    gateway.rows = [_stub("asgn_new")]
    _fill(daemon)
    assert gateway.attested == ["asgn_gone", "asgn_new"]
    assert set(daemon._protected_tokens) == {"asgn_new"}


def test_an_expired_token_releases_its_slot(tmp_path):
    gateway = _ListingGateway([_stub("asgn_old"), _stub("asgn_new")])
    daemon, _ = _protected_daemon(gateway, tmp_path)
    daemon._protected_tokens["asgn_old"] = pa.ProtectedReadToken(
        token="expired", expires_at="2000-01-01T00:00:00+00:00")
    _fill(daemon)
    assert gateway.attested == ["asgn_old"]  # re-attested, no longer blocked by its own stale token
    assert daemon._token_valid("asgn_old")


def test_a_failed_listing_keeps_held_tokens(tmp_path):
    gateway = _ListingGateway([_stub("asgn_kept")])
    daemon, _ = _protected_daemon(gateway, tmp_path)
    _fill(daemon)

    def broken():
        raise OSError("gateway unreachable")

    daemon.client.list_assignments = broken  # type: ignore[method-assign]
    _fill(daemon)
    assert set(daemon._protected_tokens) == {"asgn_kept"}
