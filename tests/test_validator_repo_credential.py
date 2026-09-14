"""The reference validator clones with the READ credential served on its assignment
(Ormas launch item 2, validator half; public twin).

Live 2026-09-14: newcomer rehearsal ``job_3feef4be1e83`` — the miner delivered via its
credential; the validator posted ``error`` because its host key could not read the client's
repository. The gateway now serves ``repo_credential {kind: ssh_deploy_key, private_key,
fingerprint, scope: "read"}`` on each undecided assignment whose project has a read key.

Contract: ``_clone`` runs ``git clone`` under ``_credential_git_env(assignment.get("repo_credential"))``
(the same helper the skeleton uses), so the key exists as a 0600 file only for the clone and is
gone afterwards; ``fetch``/``checkout`` of already-cloned commits need no credential. No
credential → today's behaviour. The credential is never part of the evidence digest.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from ormas_subnet import skeleton as sk
from ormas_subnet.validator import (
    OrmasValidatorClient,
    ValidatorConfig,
    ValidatorDaemon,
    canonical_evidence_fields,
    evidence_digest_hex,
    make_ed25519_signer,
)

from tests.test_skeleton import _FakeValidatorGateway, _init_repo, requires_git

FAKE_KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nZmFrZQ==\n-----END OPENSSH PRIVATE KEY-----\n"
CRED = {"kind": "ssh_deploy_key", "private_key": FAKE_KEY, "fingerprint": "SHA256:ro", "scope": "read"}


def _key_path(ssh_cmd: str) -> str | None:
    parts = ssh_cmd.split()
    for i, p in enumerate(parts):
        if p == "-i" and i + 1 < len(parts):
            return parts[i + 1]
    return None


def _assignment(repo: Path, base: str, result: str, credential: dict | None) -> dict[str, Any]:
    fields = canonical_evidence_fields(
        job_id="job_1", miner_id="miner:other", base_commit=base, result_commit=result,
        repo_url=str(repo), verify_command="grep -q mined out.txt", allowed_paths=["out.txt"], immutable_paths=[],
    )
    a = {"assignment_id": "asgn_cred", **fields, "evidence_digest_sha256": evidence_digest_hex(fields), "repo_credential": credential}
    return a


def _result_commit(repo: Path) -> str:
    (repo / "out.txt").write_text("base\nmined\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "result"], cwd=repo, check=True, capture_output=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


@requires_git
def test_clone_runs_under_the_served_read_credential_and_key_is_gone_after(tmp_path: Path, monkeypatch) -> None:
    repo, base = _init_repo(tmp_path)
    result = _result_commit(repo)
    seen: list[dict] = []
    real_run = subprocess.run

    def spy(argv, **kwargs):
        if argv[:2] == ["git", "clone"]:
            env = kwargs.get("env") or {}
            kp = _key_path(env.get("GIT_SSH_COMMAND", ""))
            seen.append({"ssh": env.get("GIT_SSH_COMMAND", ""), "key_exists": bool(kp) and Path(kp).exists(), "kp": kp})
        return real_run(argv, **kwargs)

    monkeypatch.setattr(sk.subprocess, "run", spy)
    gateway = _FakeValidatorGateway(assignment=_assignment(repo, base, result, CRED))
    client = OrmasValidatorClient(base_url="https://fake.invalid", token="ormv_test", http_client=gateway)
    sign_fn, pub = make_ed25519_signer("11" * 32)
    daemon = ValidatorDaemon(client, ValidatorConfig(workdir_root=tmp_path / "vw"), sign_fn)
    daemon.register(pubkey_hex=pub)
    assert daemon.run_once() is True
    assert seen and "IdentitiesOnly=yes" in seen[0]["ssh"] and seen[0]["key_exists"]
    assert not Path(seen[0]["kp"]).exists()
    assert gateway.decisions[0]["decision"] == "accept"
    # The credential is not part of what was signed.
    assert "repo_credential" not in canonical_evidence_fields(
        job_id="j", miner_id="m", base_commit="a" * 40, result_commit="b" * 40, repo_url="u",
        verify_command="v", allowed_paths=[], immutable_paths=[],
    )


@requires_git
def test_no_credential_is_todays_behaviour(tmp_path: Path) -> None:
    repo, base = _init_repo(tmp_path)
    result = _result_commit(repo)
    gateway = _FakeValidatorGateway(assignment=_assignment(repo, base, result, None))
    client = OrmasValidatorClient(base_url="https://fake.invalid", token="ormv_test", http_client=gateway)
    sign_fn, pub = make_ed25519_signer("11" * 32)
    daemon = ValidatorDaemon(client, ValidatorConfig(workdir_root=tmp_path / "vw"), sign_fn)
    daemon.register(pubkey_hex=pub)
    assert daemon.run_once() is True
    assert gateway.decisions[0]["decision"] == "accept"
