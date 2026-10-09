"""Private v2 source reads use the assigned SSH key; publication stays at the gateway."""

from __future__ import annotations

import os
import shlex
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from ormas_subnet import skeleton as sk
from ormas_subnet.protocol import TaskDraft
from ormas_subnet.validator import (
    OrmasValidatorClient,
    ValidatorConfig,
    ValidatorDaemon,
    canonical_evidence_fields,
    evidence_digest_hex,
    make_ed25519_signer,
)
from tests.test_skeleton import _FakeValidatorGateway


REPO = "https://github.com/acme/demo.git"
SSH_REPO = "git@github.com:acme/demo.git"
BASE = "a" * 40
RESULT = "b" * 40
TREE = "c" * 40
KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nZmFrZQ==\n-----END OPENSSH PRIVATE KEY-----\n"
GITHUB_HOST_KEY = (
    "github.com ssh-ed25519 "
    "AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"
)


def _packet(visibility: str) -> dict:
    # The packet's unrelated verifier/limits fields are tested elsewhere. Keep
    # this transport test on the existing skeleton entry points, not on prep.
    return {
        "execution_requirements": {"repository_visibility": visibility},
        "execution_policy": {"repo_base_sha": BASE},
        "verification_command": "python -m pytest -q tests/test_demo.py",
    }


def _draft(visibility: str, credential: dict | None) -> TaskDraft:
    return TaskDraft(
        task_id="job_private", runner_id="runr_private", repo_id="",
        repo_url=REPO, repo_credential=credential, base_commit=BASE,
        brief="fix demo", verify_command="python -m pytest -q tests/test_demo.py",
        allowed_paths=["demo.py"], budget_usd=None, work_packet=_packet(visibility),
        work_packet_sha256="d" * 64, attempt=0, parent_job_id="",
        repair_findings=[],
    )


def _credential(*, scope: str = "read", repository_url: str = REPO) -> dict:
    return {
        "kind": "ssh_deploy_key", "private_key": KEY,
        "fingerprint": "SHA256:read-key", "scope": scope,
        "repository_url": repository_url,
    }


def _miner(tmp_path: Path, client=None) -> sk.MinerSkeleton:
    miner = object.__new__(sk.MinerSkeleton)
    miner.config = SimpleNamespace(workdir_root=tmp_path / "work", runner_id="runr_private",
                                   repo_url="https://github.com/wrong/wrong.git")
    miner.client = client
    miner._public_recovery = None
    return miner


def _ssh_key_path(command: str) -> Path:
    words = shlex.split(command)
    return Path(words[words.index("-i") + 1])


def _assert_scoped_remote(argv: list[str], env: dict[str, str], observed: list[Path]) -> None:
    assert SSH_REPO in argv
    assert REPO not in argv
    assert env.get("HOME") == "/nonexistent"
    assert env.get("GIT_CONFIG_NOSYSTEM") == "1"
    assert env.get("GIT_CONFIG_GLOBAL") == os.devnull
    assert env.get("GIT_TERMINAL_PROMPT") == "0"
    assert "GIT_SSH_AUTH_SOCK" not in env
    assert "ORMAS_TEST_AMBIENT_SECRET" not in env
    assert "credential.helper=" in argv
    assert any(arg.startswith("core.hooksPath=") for arg in argv)
    ssh = env["GIT_SSH_COMMAND"]
    words = shlex.split(ssh)
    assert words[words.index("-F") + 1] == os.devnull
    assert "IdentityAgent=none" in words
    assert "IdentitiesOnly=yes" in words
    assert "StrictHostKeyChecking=yes" in words
    assert "GlobalKnownHostsFile=" + os.devnull in words
    assert "HostKeyAlgorithms=ssh-ed25519" in words
    assert "accept-new" not in ssh
    host_option = next(word for word in words if word.startswith("UserKnownHostsFile="))
    host_path = Path(host_option.split("=", 1)[1])
    assert host_path.exists()
    assert host_path.read_text().strip() == GITHUB_HOST_KEY
    key_path = _ssh_key_path(ssh)
    assert key_path.exists()
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    assert key_path.read_text() == KEY
    observed.append(key_path)


def test_private_v2_clone_uses_only_read_key_and_removes_it_before_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sk, "_public_packet", lambda draft: draft.work_packet)
    monkeypatch.setenv("ORMAS_TEST_AMBIENT_SECRET", "must-not-reach-git")
    monkeypatch.setenv("GIT_SSH_AUTH_SOCK", "/tmp/ambient-agent")
    observed: list[Path] = []
    remote_calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        args = list(argv)
        if "clone" in args:
            remote_calls.append(args)
            _assert_scoped_remote(args, kwargs["env"], observed)
            Path(args[-1]).mkdir(parents=True)
        return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    workdir = _miner(tmp_path)._clone_and_checkout(_draft("private", _credential()))
    assert workdir == tmp_path / "work" / "job_private"
    assert len(remote_calls) == 1
    assert observed and all(not path.exists() for path in observed)


def test_private_v2_postpublication_fetch_uses_read_key_and_checks_topology(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ORMAS_TEST_AMBIENT_SECRET", "must-not-reach-git")
    monkeypatch.setenv("GIT_SSH_AUTH_SOCK", "/tmp/ambient-agent")
    observed: list[Path] = []
    remote_calls: list[list[str]] = []
    topology_calls: list[str] = []

    def fake_run(argv, **kwargs):
        args = list(argv)
        if "fetch" in args:
            remote_calls.append(args)
            _assert_scoped_remote(args, kwargs["env"], observed)
            output = b""
        elif "rev-parse" in args:
            topology_calls.append("rev-parse")
            output = (TREE + "\n").encode()
        elif "rev-list" in args:
            topology_calls.append("rev-list")
            output = (RESULT + " " + BASE + "\n").encode()
        else:
            raise AssertionError(f"unexpected git call: {args}")
        return subprocess.CompletedProcess(args, 0, stdout=output, stderr=b"")

    def artifact(_draft, _workdir, _result, target):
        target.write(b"artifact")
        target.seek(0)
        return 8, "d" * 64, TREE

    class Client:
        def publish_result(self, task_id, runner_id, lease_id, *, source, length):
            assert (task_id, runner_id, lease_id) == ("job_private", "runr_private", "lease")
            assert length == 8 and source.read() == b"artifact"
            return {"result_ref": "refs/heads/ormas/job/job_private",
                    "result_commit": RESULT, "artifact_sha256": "d" * 64,
                    "tree_sha": TREE}

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(sk, "_build_public_artifact", artifact)
    ref, result = _miner(tmp_path, Client())._publish_public(
        SimpleNamespace(lease_id="lease"), _draft("private", _credential()),
        tmp_path, sk.SolveResult(RESULT),
    )
    assert ref == "refs/heads/ormas/job/job_private"
    assert result.result_commit == RESULT
    assert len(remote_calls) == 1
    assert topology_calls == ["rev-parse", "rev-list"]
    assert observed and all(not path.exists() for path in observed)


def test_public_v2_still_refuses_a_deploy_key_before_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sk, "_public_packet", lambda draft: draft.work_packet)
    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: pytest.fail("git invoked"))
    with pytest.raises(sk.GitError):
        _miner(tmp_path)._clone_and_checkout(_draft("public", _credential()))


@pytest.mark.parametrize("credential", [
    None,
    _credential(scope="write"),
    _credential(repository_url="https://github.com/other/repo.git"),
])
def test_private_v2_refuses_unscoped_or_other_repository_key_before_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, credential: dict | None,
) -> None:
    monkeypatch.setattr(sk, "_public_packet", lambda draft: draft.work_packet)
    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: pytest.fail("git invoked"))
    with pytest.raises(sk.GitError):
        _miner(tmp_path)._clone_and_checkout(_draft("private", credential))


def test_private_checker_git_failure_posts_error_and_keeps_polling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """c301aa8d: exhausted scoped-clone retries post only one signed error."""
    contract = {
        "schema_version": "outcomes.validation-contract.v2",
        "work_packet_sha256": "d" * 64,
        "execution_environment": {"schema_version": "fixture"},
        "execution_requirements": {"repository_visibility": "private"},
        "verifier_profile": {"schema_version": "fixture"},
        "verify_base": {"schema_version": "fixture"},
    }
    fields = canonical_evidence_fields(
        job_id="job_private", miner_id="miner:other", base_commit=BASE,
        result_commit=RESULT, repo_url=REPO,
        verify_command="python -m pytest -q tests/test_demo.py",
        allowed_paths=["demo.py"], immutable_paths=["tests/test_demo.py"],
        execution_contract=contract,
    )
    assignment = {"assignment_id": "asgn_private", **fields,
                  "evidence_digest_sha256": evidence_digest_hex(fields),
                  "repo_credential": _credential()}
    gateway = _FakeValidatorGateway(assignment=assignment)
    client = OrmasValidatorClient(base_url="https://fake.invalid", token="ormv_test", http_client=gateway)
    sign_fn, pubkey = make_ed25519_signer("11" * 32)
    daemon = ValidatorDaemon(client, ValidatorConfig(workdir_root=tmp_path / "checker"), sign_fn)
    daemon.register(pubkey_hex=pubkey)
    monkeypatch.setattr("ormas_subnet.validator.validate_assignment_execution", lambda _assignment: True)

    attempted: list[bool] = []

    def unavailable(*_args, **_kwargs):
        attempted.append(True)
        raise ValueError("private Git read unavailable")

    monkeypatch.setattr("ormas_subnet.outcomes_support.repository_git", unavailable)
    monkeypatch.setattr("ormas_subnet.validator.time.sleep", lambda _: None)
    assert daemon.run_once() is True
    assert daemon.run_once() is False
    assert attempted == [True] * sk.DEFAULT_POLL_INTERVAL_S
    assert len(gateway.decisions) == 1
    assert gateway.decisions[0]["decision"] == "error"


def test_private_checker_clone_uses_scoped_read_key_without_ambient_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("ormas_subnet.validator.validate_assignment_execution", lambda _assignment: True)
    monkeypatch.setenv("ORMAS_TEST_AMBIENT_SECRET", "must-not-reach-git")
    monkeypatch.setenv("GIT_SSH_AUTH_SOCK", "/tmp/ambient-agent")
    observed: list[Path] = []

    def fake_run(argv, **kwargs):
        args = list(argv)
        assert "clone" in args
        _assert_scoped_remote(args, kwargs["env"], observed)
        Path(args[-1]).mkdir(parents=True)
        return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    daemon = object.__new__(ValidatorDaemon)
    daemon.config = ValidatorConfig(workdir_root=tmp_path / "checker")
    assignment = {
        "assignment_id": "asgn_private", "repo_url": REPO,
        "repo_credential": _credential(),
        "execution_contract": {"execution_requirements": {"repository_visibility": "private"}},
    }
    workdir = daemon._clone(assignment)
    assert workdir == tmp_path / "checker" / "asgn_private"
    assert len(observed) == 1 and not observed[0].exists()
