"""Clone recovery is bounded by a polling window and preserves Git diagnostics."""
from __future__ import annotations

import logging
import subprocess
import time

import pytest

from ormas_subnet import outcomes_support as support
from ormas_subnet import validator as module
from ormas_subnet.skeleton import DEFAULT_POLL_INTERVAL_S, GitError


@pytest.fixture
def clock(monkeypatch):
    now = [0.0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    monkeypatch.setattr(time, "sleep", sleep)
    return now, sleeps


@pytest.fixture
def assignment():
    fields = module.canonical_evidence_fields(
        job_id="job-1", miner_id="miner-1", base_commit="a" * 40,
        result_commit="b" * 40, repo_url="https://github.com/example/public.git",
        verify_command="true", allowed_paths=["out.txt"], immutable_paths=[],
    )
    return {"assignment_id": "assignment-1", **fields,
            "evidence_digest_sha256": module.evidence_digest_hex(fields)}


def _review(tmp_path, assignment, entry):
    if entry == "child":
        return module.review_assignment(str(tmp_path), assignment)

    class Client:
        decisions = []

        def list_assignments(self):
            return [assignment]

        def post_decision(self, assignment_id, **kwargs):
            self.decisions.append((assignment_id, kwargs))

    client = Client()
    daemon = module.ValidatorDaemon(client, module.ValidatorConfig(tmp_path), lambda digest: digest)
    assert daemon.run_once()
    assert len(client.decisions) == 1
    return client.decisions[0][1]["decision"]


@pytest.mark.parametrize("entry", ["child", "inline"])
def test_clone_fails_once_then_review_proceeds(tmp_path, monkeypatch, caplog, clock, assignment, entry):
    attempts = []
    reviewed = []

    def clone(self, row):
        attempts.append(row)
        if len(attempts) == 1:
            raise GitError("fatal: remote disconnected")
        return tmp_path

    def decide(self, row, workdir):
        reviewed.append((row, workdir))
        return "accept"

    monkeypatch.setattr(module.ValidatorDaemon, "_clone", clone)
    monkeypatch.setattr(module.ValidatorDaemon, "_decide", decide)
    with caplog.at_level(logging.WARNING):
        assert _review(tmp_path, assignment, entry) == "accept"
    assert len(attempts) == 2
    assert reviewed == [(assignment, tmp_path)]
    assert clock[1] == [1.0]
    assert len(caplog.records) == 1
    assert "GitError" in caplog.text and "fatal: remote disconnected" in caplog.text


@pytest.mark.parametrize("entry", ["child", "inline"])
def test_persistent_clone_failure_stops_at_bound(tmp_path, monkeypatch, caplog, clock, assignment, entry):
    attempts = []

    def clone(self, row):
        attempts.append(row)
        raise GitError("fatal: remote disconnected")

    monkeypatch.setattr(module.ValidatorDaemon, "_clone", clone)
    monkeypatch.setattr(module.ValidatorDaemon, "_decide", lambda *_: pytest.fail("failed clone reviewed"))
    with caplog.at_level(logging.WARNING):
        assert _review(tmp_path, assignment, entry) == "error"
    assert len(attempts) == DEFAULT_POLL_INTERVAL_S
    assert clock[1] == [1.0] * (DEFAULT_POLL_INTERVAL_S - 1)
    assert len(caplog.records) == len(attempts)
    assert all(record.levelno == logging.WARNING for record in caplog.records)
    assert all("fatal: remote disconnected" in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize("timeout_s", [3, 1800])
def test_assignment_timeout_bounds_retry_window(
    tmp_path, monkeypatch, caplog, clock, assignment, timeout_s,
):
    assignment["acceptance_contract"] = {"policy": {"timeout_s": timeout_s}}
    attempts = []

    def clone(self, row):
        attempts.append(row)
        raise GitError("offline")

    monkeypatch.setattr(module.ValidatorDaemon, "_clone", clone)
    with caplog.at_level(logging.WARNING):
        assert _review(tmp_path, assignment, "child") == "error"
    bound = min(timeout_s, DEFAULT_POLL_INTERVAL_S)
    assert len(attempts) == bound
    assert clock[1] == [1.0] * (bound - 1)
    assert len(caplog.records) == bound


def test_slow_failed_clone_consumes_retry_budget(tmp_path, monkeypatch, caplog, clock, assignment):
    attempts = []

    def clone(self, row):
        attempts.append(row)
        clock[0][0] += DEFAULT_POLL_INTERVAL_S
        raise GitError("clone timed out")

    monkeypatch.setattr(module.ValidatorDaemon, "_clone", clone)
    with caplog.at_level(logging.WARNING):
        assert _review(tmp_path, assignment, "child") == "error"
    assert len(attempts) == 1
    assert clock[1] == []
    assert len(caplog.records) == 1


@pytest.mark.parametrize("public", [False, True])
def test_clone_warning_preserves_underlying_stderr(tmp_path, monkeypatch, caplog, clock, assignment, public):
    stderr = "fatal: remote disconnected during clone"
    monkeypatch.setattr(module, "validate_assignment_execution", lambda _: public)
    if public:
        assignment["execution_contract"] = {
            "execution_requirements": {"repository_visibility": "public"}}

        def git(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 128, stdout=b"", stderr=stderr.encode())

        monkeypatch.setattr(support.subprocess, "run", git)
    else:
        def git(*args, **kwargs):
            raise GitError(stderr)

        monkeypatch.setattr(module, "_run_git", git)
    with caplog.at_level(logging.WARNING):
        assert _review(tmp_path, assignment, "child") == "error"
    assert len(caplog.records) == DEFAULT_POLL_INTERVAL_S
    assert all(stderr in record.getMessage() for record in caplog.records)
    assert ("CalledProcessError" if public else "GitError") in caplog.text


@pytest.mark.parametrize("step", ["checkout", "diff", "verify", "toolchain"])
def test_decision_error_logs_exception_diagnostics(tmp_path, monkeypatch, caplog, assignment, step):
    stderr = "fatal: review operation failed"

    def git(argv, **kwargs):
        if argv[0] == step:
            raise GitError(stderr)
        return "out.txt" if argv[0] == "diff" else ""

    def verify(*args, **kwargs):
        if step == "verify":
            raise OSError(stderr)
        return 1

    def provision(*args, **kwargs):
        if step == "toolchain":
            raise module.ToolchainUnavailable(stderr)

    monkeypatch.setattr(module, "_run_git", git)
    monkeypatch.setattr(module, "_run_verify_command", verify)
    monkeypatch.setattr(module, "provision_toolchain", provision)
    daemon = module.ValidatorDaemon(None, module.ValidatorConfig(tmp_path), lambda digest: digest)
    with caplog.at_level(logging.WARNING):
        assert daemon._decide(assignment, tmp_path) == "error"
    assert len(caplog.records) == 1
    assert stderr in caplog.text
    expected = {"verify": "OSError", "toolchain": "ToolchainUnavailable"}.get(step, "GitError")
    assert expected in caplog.text


def test_clone_warning_redacts_repository_credential(tmp_path, monkeypatch, caplog, clock, assignment):
    assignment["repo_credential"] = {"token": "test-secret-token"}

    def clone(*args):
        raise GitError("fatal: test-secret-token refused")

    monkeypatch.setattr(module.ValidatorDaemon, "_clone", clone)
    with caplog.at_level(logging.WARNING):
        assert _review(tmp_path, assignment, "child") == "error"
    assert "test-secret-token" not in caplog.text
    assert "fatal:" in caplog.text
