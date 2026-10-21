"""Publication must consume a frozen, passing path-scope decision."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from ormas_subnet import outcomes_support as support
from ormas_subnet._recovery import PublicRecovery, RecoveryRequired
from ormas_subnet.protocol import TaskLease
from ormas_subnet.skeleton import (
    GitError,
    MinerConfig,
    MinerSkeleton,
    SolveResult,
    _public_packet,
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    _git(repo, "config", "user.name", "Publication Gate Test")
    _git(repo, "config", "user.email", "publication-gate@example.invalid")
    (repo / "allowed.txt").write_text("base\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "frozen.txt").write_text("frozen\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "base")
    return repo, _git(repo, "rev-parse", "HEAD")


def _commit_case(repo: Path, case: str) -> tuple[str, tuple[str, ...]]:
    if case == "valid":
        (repo / "allowed.txt").write_text("candidate\n", encoding="utf-8")
        reported = ("allowed.txt",)
    elif case == "outside":
        (repo / "outside.txt").write_text("prohibited\n", encoding="utf-8")
        reported = ("outside.txt",)
    elif case == "immutable":
        (repo / "tests" / "frozen.txt").write_text("changed\n", encoding="utf-8")
        reported = ("tests/frozen.txt",)
    elif case == "immutable-rename":
        (repo / "tests" / "frozen.txt").rename(repo / "tests" / "moved.txt")
        reported = ("tests/frozen.txt", "tests/moved.txt")
    else:  # pragma: no cover - fixture misuse
        raise AssertionError(case)
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", case)
    return _git(repo, "rev-parse", "HEAD"), reported


def _packet(base: str, *, allowed: list[str], immutable: list[str]) -> dict:
    return {
        "execution_policy": {
            "repo_base_sha": base,
            "allowed_paths": allowed,
            "immutable_paths": immutable,
        },
        "execution_requirements": {
            "repository_visibility": "public",
            "publication": {"max_changed_files": 10},
            "limits": {"max_bytes": 1024 * 1024},
        },
        "verification_command": "synthetic-public-verifier",
    }


def _packet_digest(packet: dict) -> str:
    encoded = json.dumps(
        packet, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def test_public_packet_hash_binds_nested_scope_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    packet = _packet("a" * 40, allowed=["allowed.txt"], immutable=["tests/"])
    draft = SimpleNamespace(
        base_commit="a" * 40,
        verify_command="synthetic-public-verifier",
        allowed_paths=("allowed.txt",),
        work_packet=packet,
        work_packet_sha256=_packet_digest(packet),
    )
    monkeypatch.setattr(support, "validate_public_execution_packet", lambda _packet: None)
    frozen = _public_packet(draft)
    packet["execution_policy"]["immutable_paths"].clear()

    assert frozen["execution_policy"]["immutable_paths"] == ["tests/"]
    with pytest.raises(GitError, match="packet hash mismatch"):
        _public_packet(draft)


@pytest.mark.parametrize("case", ["outside", "immutable", "immutable-rename"])
def test_artifact_builder_rejects_frozen_scope_before_reading_blobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str,
) -> None:
    repo, base = _repository(tmp_path)
    result, _reported = _commit_case(repo, case)
    packet = _packet(base, allowed=["allowed.txt", "tests/"], immutable=["tests/frozen.txt"])
    if case == "outside":
        packet["execution_policy"]["allowed_paths"] = ["allowed.txt"]

    monkeypatch.setattr(support, "validate_public_execution_packet", lambda _packet: None)
    original = support.public_git
    calls: list[tuple[str, ...]] = []

    def observed(args, *, cwd, index_file=None):
        calls.append(tuple(args))
        return original(args, cwd=cwd, index_file=index_file)

    monkeypatch.setattr(support, "public_git", observed)
    target = io.BytesIO()

    with pytest.raises(ValueError, match="scope"):
        support.build_public_artifact(packet, repo, result, target)

    assert target.getvalue() == b""
    assert not any(args[:2] == ("cat-file", "blob") for args in calls)


def test_artifact_builder_keeps_valid_scoped_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, base = _repository(tmp_path)
    result, _reported = _commit_case(repo, "valid")
    packet = _packet(base, allowed=["allowed.txt"], immutable=["tests/"])
    monkeypatch.setattr(support, "validate_public_execution_packet", lambda _packet: None)
    target = io.BytesIO()

    length, digest, tree = support.build_public_artifact(packet, repo, result, target)

    assert length == len(target.getvalue()) > 0
    assert len(digest) == 64
    assert tree == _git(repo, "rev-parse", result + "^{tree}")


def test_public_artifact_strips_restored_intermediate_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The artifact publishes a final-tree delta, never the solver's raw history."""
    repo, base = _repository(tmp_path)
    (repo / "tests" / "frozen.txt").write_text("temporary edit\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "intermediate immutable edit")
    (repo / "tests" / "frozen.txt").write_text("frozen\n", encoding="utf-8")
    (repo / "allowed.txt").write_text("candidate\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "restore immutable path")
    result = _git(repo, "rev-parse", "HEAD")
    packet = _packet(base, allowed=["allowed.txt"], immutable=["tests/"])
    monkeypatch.setattr(support, "validate_public_execution_packet", lambda _packet: None)
    target = io.BytesIO()

    support.build_public_artifact(packet, repo, result, target)

    framed = target.getvalue()
    header_length = int.from_bytes(framed[:8], "big")
    header = json.loads(framed[8:8 + header_length])
    assert [entry["path"] for entry in header["files"]] == ["allowed.txt"]


class _PublicClient:
    base_url = "https://gateway.example.invalid"

    def __init__(self, lease, draft) -> None:
        self.lease = lease
        self.draft = draft
        self.claimed = False
        self.publications = 0
        self.completions: list[dict] = []

    def claim_task(self, _runner_id, *, ask_usd=None):
        del ask_usd
        if self.claimed:
            return None
        self.claimed = True
        return self.lease, self.draft

    @staticmethod
    def heartbeat_task(*_args, **_kwargs):
        return {"ok": True}

    def complete_task(self, task_id, runner_id, lease_id, *, receipt, terminal, capture):
        self.completions.append({
            "task_id": task_id,
            "runner_id": runner_id,
            "lease_id": lease_id,
            "receipt": receipt.to_wire(),
            "terminal": terminal.to_wire(),
            "capture": dict(capture),
        })
        return {"status": "failed", "receipt": {"receipt_id": "receipt-scope"}}


def _public_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    case: str,
) -> tuple[_PublicClient, list[str], list[str]]:
    source, base = _repository(tmp_path)
    allowed = ["allowed.txt"] if case == "outside" else ["allowed.txt", "tests/"]
    packet = _packet(base, allowed=allowed, immutable=["tests/frozen.txt"])
    lease = TaskLease(
        lease_id="lease-scope", task_id="task-scope", expires_at="2026-10-14T13:00:00Z",
        selected_cell="task:lang/python", provider_pin="unset", fallback_policy="unset",
        hold_ref="unset", now="2026-10-14T12:55:00Z", outcome_price_usd=1.0,
        bid_id="bid-scope", offer_kind="limit", estimate_usd=0.5, limit_usd=1.0,
    )
    draft = SimpleNamespace(
        task_id="task-scope", runner_id="miner-scope", base_commit=base,
        verify_command="synthetic-public-verifier", allowed_paths=tuple(allowed),
        work_packet=packet, work_packet_sha256="a" * 64,
        repo_url=str(source), repo_credential=None,
    )
    client = _PublicClient(lease, draft)
    config = MinerConfig(
        runner_id="miner-scope", runner_version="test", platform="test", capacity=1,
        cells=("task:lang/python",), workdir_root=tmp_path / "work", repo_id="repo",
        repo_url=str(source), push_remote=None,
    )
    published: list[str] = []
    settlements: list[str] = []

    def settle(current_lease, _result):
        settlements.append(current_lease.lease_id)
        return 0.5

    config.settle_fn = settle

    def clone(_self, current_draft):
        workdir = config.workdir_root / current_draft.task_id
        _git(source.parent, "clone", "--quiet", str(source), str(workdir))
        _git(workdir, "checkout", "--quiet", "--detach", current_draft.base_commit)
        return workdir

    def solve(_draft, workdir):
        result_commit, reported = _commit_case(workdir, case)
        return SolveResult(result_commit=result_commit, changed_paths=reported,
                           provider="synthetic", prompt_tokens=0, completion_tokens=0,
                           cache_read_input_tokens=0, cache_creation_input_tokens=0,
                           reasoning_tokens=0, upstream_cost_usd=0.0)

    verifies = iter((86, 0))
    monkeypatch.setattr("ormas_subnet.skeleton._public_packet", lambda current: current.work_packet)
    monkeypatch.setattr(support, "validate_public_execution_packet", lambda _packet: None)
    monkeypatch.setattr("ormas_subnet.skeleton._run_verify_command", lambda *_a, **_k: next(verifies))
    monkeypatch.setattr(MinerSkeleton, "_clone_and_checkout", clone)

    def publish(_self, _lease, current_draft, _workdir, result, *, capture=None):
        published.append(current_draft.task_id)
        return f"refs/heads/ormas/job/{current_draft.task_id}", result

    monkeypatch.setattr(MinerSkeleton, "_publish_public", publish)
    assert MinerSkeleton(client, config, solve).run_once() is True
    return client, published, settlements


@pytest.mark.parametrize("case", ["outside", "immutable", "immutable-rename"])
def test_public_scope_failure_completes_without_any_publication_callback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str,
) -> None:
    client, published, settlements = _public_run(tmp_path, monkeypatch, case=case)

    assert published == []
    assert settlements == []
    assert len(client.completions) == 1
    completed = client.completions[0]
    assert completed["capture"]["scope_ok"] is False
    assert completed["capture"]["failure_class"] == "scope_violation"
    assert completed["terminal"]["verification_state"] == "failed"
    assert completed["terminal"]["result_ref"] is None
    assert completed["terminal"]["result_commit"] == ""
    assert "settled_price_usd" not in completed["terminal"]


def test_valid_public_scope_still_invokes_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, published, settlements = _public_run(tmp_path, monkeypatch, case="valid")

    assert published == ["task-scope"]
    assert settlements == ["lease-scope"]
    assert client.completions[0]["capture"]["scope_ok"] is True
    assert client.completions[0]["terminal"]["result_ref"] == (
        "refs/heads/ormas/job/task-scope"
    )
    assert client.completions[0]["terminal"]["settled_price_usd"] == 0.5


def test_public_publication_sink_requires_a_passing_scope_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = MinerConfig(
        runner_id="miner-public", runner_version="test", platform="test", capacity=1,
        cells=("task:lang/python",), workdir_root=tmp_path / "work", repo_id="repo",
        repo_url="https://github.com/acme/demo.git", push_remote=None,
    )
    client = SimpleNamespace()
    miner = MinerSkeleton(client, config, lambda *_args: None)
    built: list[bool] = []

    def build(*_args, **_kwargs):
        built.append(True)
        raise AssertionError("an unauthorized artifact build was invoked")

    monkeypatch.setattr("ormas_subnet.skeleton._build_public_artifact", build)
    with pytest.raises(GitError, match="scope"):
        miner._publish_public(
            SimpleNamespace(lease_id="lease"),
            SimpleNamespace(task_id="task", work_packet={}),
            tmp_path,
            SolveResult("b" * 40),
            capture={"scope_ok": False},
        )

    assert built == []


def test_pre_fix_artifact_recovery_cannot_bypass_the_new_scope_gate(tmp_path: Path) -> None:
    root = tmp_path / "work"
    workdir = root / "task-public"
    workdir.mkdir(parents=True)
    lease = SimpleNamespace(lease_id="lease-public")
    draft = SimpleNamespace(
        task_id="task-public", work_packet_sha256="a" * 64, base_commit="b" * 40,
        repo_url="https://github.com/acme/demo.git", work_packet={},
    )
    store = PublicRecovery(root, "https://gateway.example.invalid", "miner-public")
    store.begin(lease, draft)

    def artifact(target):
        target.write(b"pre-fix-artifact")
        return len(b"pre-fix-artifact"), "c" * 64, "d" * 40

    pending = store.checkpoint_artifact(
        artifact,
        result=SolveResult("e" * 40).__dict__,
        capture={"status": "verified", "scope_ok": True, "changed_paths": []},
    )
    pending["version"] = 1
    store.save(pending)

    class Client:
        base_url = "https://gateway.example.invalid"
        publications = 0

        def publish_result(self, *_args, **_kwargs):
            self.publications += 1
            raise AssertionError("legacy recovery artifact was uploaded")

    client = Client()
    config = MinerConfig(
        runner_id="miner-public", runner_version="test", platform="test", capacity=1,
        cells=("task:lang/python",), workdir_root=root, repo_id="repo",
        repo_url="https://github.com/acme/demo.git", push_remote=None,
    )

    with pytest.raises(RecoveryRequired, match="lacks frozen publication scope"):
        MinerSkeleton(client, config, lambda *_args: None).run_once()

    assert client.publications == 0


def test_recovery_checkpoint_refuses_failed_scope_before_artifact_build(tmp_path: Path) -> None:
    root = tmp_path / "work"
    lease = SimpleNamespace(lease_id="lease-public")
    draft = SimpleNamespace(
        task_id="task-public", work_packet_sha256="a" * 64, base_commit="b" * 40,
        repo_url="https://github.com/acme/demo.git", work_packet={},
    )
    store = PublicRecovery(root, "https://gateway.example.invalid", "miner-public")
    store.begin(lease, draft)
    builds: list[bool] = []

    def build(_target):
        builds.append(True)
        raise AssertionError("failed scope reached artifact builder")

    with pytest.raises(RecoveryRequired, match="passing frozen scope"):
        store.checkpoint_artifact(
            build,
            result=SolveResult("e" * 40).__dict__,
            capture={"status": "failed", "scope_ok": False, "changed_paths": []},
        )

    assert builds == []
    assert store.pending()["stage"] == "claimed"


def test_legacy_publication_sink_rechecks_scope_before_creating_a_ref(tmp_path: Path) -> None:
    repo, base = _repository(tmp_path)
    result_commit, reported = _commit_case(repo, "outside")
    config = MinerConfig(
        runner_id="miner-legacy", runner_version="test", platform="test", capacity=1,
        cells=("code-edit-small",), workdir_root=tmp_path / "work", repo_id="repo",
        repo_url=str(repo), push_remote=None,
    )
    miner = MinerSkeleton(SimpleNamespace(), config, lambda *_args: None)
    draft = SimpleNamespace(
        task_id="task-legacy", base_commit=base, allowed_paths=("allowed.txt",),
        work_packet={}, repo_credential=None, repo_url=str(repo),
    )

    with pytest.raises(GitError, match="scope"):
        miner._publish(
            draft.task_id, repo,
            SolveResult(result_commit=result_commit, changed_paths=reported), draft=draft,
        )

    assert _git(repo, "branch", "--list", "ormas/job/task-legacy") == ""
