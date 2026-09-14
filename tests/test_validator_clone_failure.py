"""A clone the validator cannot perform is an ``error`` decision, never a crash.

Found live 2026-09-13 (Ormas battery, card dae5396c): a private ``repo_url`` the
validator host had no key for raised ``GitError`` out of ``run_once``; the daemon
died, systemd restarted it 26 times, and the assignment sat undecided for ~9 min.
Quorum already excludes ``error`` (``outcomes_validators.resolve_quorum``), so the
right move is to sign and post ``error`` over the assignment's evidence digest and
keep polling.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ormas_subnet.validator import (
    OrmasValidatorClient,
    ValidatorConfig,
    ValidatorDaemon,
    canonical_evidence_fields,
    evidence_digest_hex,
    make_ed25519_signer,
)

from tests.test_skeleton import _FakeValidatorGateway, requires_git


def _unclonable_assignment(tmp_path: Path) -> dict[str, Any]:
    missing = tmp_path / "does-not-exist.git"
    fields = canonical_evidence_fields(
        job_id="job_1",
        miner_id="miner:other-tenant",
        base_commit="a" * 40,
        result_commit="b" * 40,
        repo_url=str(missing),
        verify_command="true",
        allowed_paths=["out.txt"],
        immutable_paths=[],
    )
    return {"assignment_id": "asgn_unclonable", **fields, "evidence_digest_sha256": evidence_digest_hex(fields)}


@requires_git
def test_clone_failure_posts_signed_error_decision_and_keeps_polling(tmp_path: Path) -> None:
    gateway = _FakeValidatorGateway(assignment=_unclonable_assignment(tmp_path))
    client = OrmasValidatorClient(base_url="https://fake.invalid", token="ormv_test", http_client=gateway)
    sign_fn, pubkey_hex = make_ed25519_signer("11" * 32)
    daemon = ValidatorDaemon(client, ValidatorConfig(workdir_root=tmp_path / "validator-work"), sign_fn)
    daemon.register(pubkey_hex=pubkey_hex)

    assert daemon.run_once() is True  # reviewed (as an error), did not raise
    assert daemon.run_once() is False  # still alive; queue drained

    assert len(gateway.decisions) == 1
    decision = gateway.decisions[0]
    assert decision["decision"] == "error"
    assert len(decision["signature_hex"]) == 128


@requires_git
def test_clone_failure_leaves_no_half_workdir_that_blocks_a_retry(tmp_path: Path) -> None:
    """A second assignment with the same id must not hit ``workdir already exists``."""
    assignment = _unclonable_assignment(tmp_path)
    workdir_root = tmp_path / "validator-work"
    for _ in range(2):
        gateway = _FakeValidatorGateway(assignment=assignment)
        client = OrmasValidatorClient(base_url="https://fake.invalid", token="ormv_test", http_client=gateway)
        sign_fn, pubkey_hex = make_ed25519_signer("11" * 32)
        daemon = ValidatorDaemon(client, ValidatorConfig(workdir_root=workdir_root), sign_fn)
        daemon.register(pubkey_hex=pubkey_hex)
        assert daemon.run_once() is True
        assert gateway.decisions[0]["decision"] == "error"
