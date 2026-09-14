"""The reference validator provisions the assignment's declared toolchain before it
re-runs the verify command (Ormas card 2dde8b88, unit 4 — public twin).

Why: on 2026-09-13 the validator false-rejected 2/2 correct deliveries because its fresh
clone had no project dependencies. The gateway now serves an optional ``toolchain`` block on
the assignment and signs over it in the evidence digest:

    {"kind": "python", "python": "3.12", "pip_install": ["-e", ".", "pytest"], "lock_paths": [...]}

Contract:
* ``canonical_evidence_fields(..., toolchain=...)`` includes a trailing ``toolchain`` key only
  when given (byte-identical digest otherwise) — must match the gateway's copy.
* ``_decide`` provisions ``<workdir>/.venv`` with ``python<X.Y>`` from PATH, runs
  ``pip --no-input install <argv>`` in the checkout, and runs BOTH verify legs with that
  ``.venv/bin`` first on PATH (still credential-free: only PATH/HOME/LANG cross).
* A declared interpreter missing from the host → decision ``error`` (the validator could not
  review; quorum excludes it) — never ``reject``.
* No toolchain → today's behaviour exactly.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

from ormas_subnet.validator import (
    OrmasValidatorClient,
    ValidatorConfig,
    ValidatorDaemon,
    canonical_evidence_fields,
    evidence_digest_hex,
    make_ed25519_signer,
)

from tests.test_skeleton import _FakeValidatorGateway, _init_repo, requires_git

HOST_PY = f"3.{sys.version_info.minor}"
TOOLCHAIN = {"kind": "python", "python": HOST_PY, "pip_install": ["--help"], "lock_paths": []}
BASE = dict(job_id="job_1", miner_id="miner:x", base_commit="a" * 40, result_commit="b" * 40,
            repo_url="git@github.com:x/y.git", verify_command="pytest -q",
            allowed_paths=["out.txt"], immutable_paths=[])


def test_evidence_fields_toolchain_is_trailing_and_optional() -> None:
    plain = canonical_evidence_fields(**BASE)
    assert "toolchain" not in plain
    assert canonical_evidence_fields(**BASE, toolchain=None) == plain
    with_tc = canonical_evidence_fields(**BASE, toolchain=TOOLCHAIN)
    assert list(with_tc)[-1] == "toolchain" and with_tc["toolchain"] == TOOLCHAIN
    assert evidence_digest_hex(with_tc) != evidence_digest_hex(plain)


def _assignment(repo: Path, base: str, result: str, verify: str, toolchain: dict | None) -> dict[str, Any]:
    fields = canonical_evidence_fields(
        job_id="job_1", miner_id="miner:other-tenant", base_commit=base, result_commit=result,
        repo_url=str(repo), verify_command=verify, allowed_paths=["out.txt", "check.py"],
        immutable_paths=[], toolchain=toolchain,
    )
    a = {"assignment_id": "asgn_tc", **fields, "evidence_digest_sha256": evidence_digest_hex(fields)}
    if toolchain is None:
        a["toolchain"] = None
    return a


def _run_once(tmp_path: Path, assignment: dict[str, Any]) -> _FakeValidatorGateway:
    gateway = _FakeValidatorGateway(assignment=assignment)
    client = OrmasValidatorClient(base_url="https://fake.invalid", token="ormv_test", http_client=gateway)
    sign_fn, pubkey_hex = make_ed25519_signer("11" * 32)
    daemon = ValidatorDaemon(client, ValidatorConfig(workdir_root=tmp_path / "validator-work"), sign_fn)
    daemon.register(pubkey_hex=pubkey_hex)
    assert daemon.run_once() is True
    return gateway


def _commit_result(repo: Path, check_source: str) -> str:
    (repo / "check.py").write_text(check_source)
    (repo / "out.txt").write_text("base\nmined\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "result"], cwd=repo, check=True, capture_output=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


@requires_git
def test_verify_runs_inside_the_provisioned_venv(tmp_path: Path) -> None:
    """The verify passes only if `python` resolves to a venv living inside the validator's
    own checkout (sys.prefix under the workdir) — i.e. the declared toolchain was provisioned
    and put first on PATH, not the host interpreter."""
    repo, base = _init_repo(tmp_path)
    result = _commit_result(
        repo,
        "import os, sys\n"
        "here = os.path.realpath(os.getcwd())\n"
        "prefix = os.path.realpath(sys.prefix)\n"
        "raise SystemExit(0 if prefix.startswith(here) and os.path.exists('out.txt') else 1)\n",
    )
    gateway = _run_once(tmp_path, _assignment(repo, base, result, "python check.py", TOOLCHAIN))
    assert gateway.decisions[0]["decision"] == "accept", gateway.decisions


@requires_git
def test_missing_declared_python_is_error_not_reject(tmp_path: Path) -> None:
    repo, base = _init_repo(tmp_path)
    result = _commit_result(repo, "raise SystemExit(0)\n")
    bad = {**TOOLCHAIN, "python": "3.99"}
    gateway = _run_once(tmp_path, _assignment(repo, base, result, "python check.py", bad))
    assert gateway.decisions[0]["decision"] == "error"
    assert len(gateway.decisions[0]["signature_hex"]) == 128


@requires_git
def test_no_toolchain_keeps_todays_behaviour(tmp_path: Path) -> None:
    repo, base = _init_repo(tmp_path)
    (repo / "out.txt").write_text("base\nmined\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "result"], cwd=repo, check=True, capture_output=True)
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
    gateway = _run_once(tmp_path, _assignment(repo, base, result, "grep -q mined out.txt", None))
    assert gateway.decisions[0]["decision"] == "accept"
    workdir = tmp_path / "validator-work" / "asgn_tc"
    assert not (workdir / ".venv").exists()
