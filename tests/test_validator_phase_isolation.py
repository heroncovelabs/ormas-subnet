"""Legacy host verification must not let base artifacts decide the result.

The reference daemon shares one validator-owned checkout across the base and
result legs. A verifier that edits tracked files, or writes untracked or
ignored files, must not block the result checkout or change that later run.
Provisioned ``.venv`` trees stay. The caller's repository is never cleaned.
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

from tests.test_skeleton import _FakeValidatorGateway, requires_git

HOST_PY = f"3.{sys.version_info.minor}"
TOOLCHAIN = {
    "kind": "python",
    "python": HOST_PY,
    "pip_install": ["pip"],
    "lock_paths": [],
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _init_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "isolation@example.invalid")
    _git(repo, "config", "user.name", "Isolation")
    (repo / "state.txt").write_text("base\n")
    (repo / "tracked.txt").write_text("original\n")
    (repo / ".gitignore").write_text("ignored-artifact\n.venv/\n")
    (repo / "check.py").write_text(
        "from pathlib import Path\n"
        "import os, sys\n"
        "base = Path('state.txt').read_text() == 'base\\n'\n"
        "if base:\n"
        "    Path('artifact').write_text('base-only')\n"
        "    Path('ignored-artifact').write_text('base-only')\n"
        "    Path('tracked.txt').write_text('base-test-dirt')\n"
        "    raise SystemExit(1)\n"
        "if Path('artifact').exists() or Path('ignored-artifact').exists():\n"
        "    raise SystemExit(1)\n"
        "if Path('tracked.txt').read_text() != 'result\\n':\n"
        "    raise SystemExit(1)\n"
        "prefix = os.path.realpath(sys.prefix)\n"
        "here = os.path.realpath(os.getcwd())\n"
        "raise SystemExit(0 if prefix.startswith(here) else 1)\n"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "state.txt").write_text("result\n")
    (repo / "tracked.txt").write_text("result\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "result")
    return repo, base


def _assignment(repo: Path, base: str, result: str) -> dict[str, Any]:
    fields = canonical_evidence_fields(
        job_id="job_isolation",
        miner_id="miner:isolation",
        base_commit=base,
        result_commit=result,
        repo_url=str(repo),
        verify_command="python check.py",
        allowed_paths=["state.txt", "tracked.txt"],
        immutable_paths=["check.py"],
        toolchain=TOOLCHAIN,
    )
    return {
        "assignment_id": "asgn_isolation",
        **fields,
        "evidence_digest_sha256": evidence_digest_hex(fields),
    }


@requires_git
def test_base_artifacts_do_not_change_result_verification(tmp_path: Path) -> None:
    repo, base = _init_repo(tmp_path)
    result = _git(repo, "rev-parse", "HEAD")
    assignment = _assignment(repo, base, result)
    gateway = _FakeValidatorGateway(assignment=assignment)
    client = OrmasValidatorClient(
        base_url="https://fake.invalid", token="ormv_test", http_client=gateway,
    )
    sign_fn, _pubkey = make_ed25519_signer("11" * 32)
    daemon = ValidatorDaemon(
        client, ValidatorConfig(workdir_root=tmp_path / "validator-work"), sign_fn,
    )
    assert daemon.run_once() is True
    assert gateway.decisions[0]["decision"] == "accept"
    assert len(gateway.decisions) == 1
    workdir = tmp_path / "validator-work" / "asgn_isolation"
    assert (workdir / ".venv").is_dir()
    assert not (workdir / "artifact").exists()
    assert not (workdir / "ignored-artifact").exists()
    assert (workdir / "tracked.txt").read_text() == "result\n"
    assert _git(repo, "status", "--porcelain") == ""
    assert _git(repo, "rev-parse", "HEAD") == result


@requires_git
def test_artifact_only_pass_is_rejected(tmp_path: Path) -> None:
    """A result that passes only because a base artifact remains is a reject."""
    repo = tmp_path / "source-false"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "isolation@example.invalid")
    _git(repo, "config", "user.name", "Isolation")
    (repo / "state.txt").write_text("base\n")
    (repo / "tracked.txt").write_text("original\n")
    (repo / ".gitignore").write_text("ignored-artifact\n.venv/\n")
    (repo / "check.py").write_text(
        "from pathlib import Path\n"
        "artifact = Path('artifact')\n"
        "if Path('state.txt').read_text() == 'base\\n':\n"
        "    artifact.write_text('base-only')\n"
        "    raise SystemExit(1)\n"
        "raise SystemExit(0 if artifact.exists() else 1)\n"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "state.txt").write_text("result\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "result depends on leaked artifact")
    result = _git(repo, "rev-parse", "HEAD")
    assignment = _assignment(repo, base, result)
    gateway = _FakeValidatorGateway(assignment=assignment)
    client = OrmasValidatorClient(
        base_url="https://fake.invalid", token="ormv_test", http_client=gateway,
    )
    sign_fn, _pubkey = make_ed25519_signer("22" * 32)
    daemon = ValidatorDaemon(
        client, ValidatorConfig(workdir_root=tmp_path / "validator-work"), sign_fn,
    )
    assert daemon.run_once() is True
    assert gateway.decisions[0]["decision"] == "reject"
    assert len(gateway.decisions) == 1
    assert _git(repo, "status", "--porcelain") == ""