"""Public publication binds bytes, tree, and one frozen-base parent."""

from __future__ import annotations

import io
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from ormas_subnet import outcomes_support as support
from ormas_subnet.client import OrmasMinerClient
from ormas_subnet.skeleton import GitError, MinerConfig, MinerSkeleton, SolveResult


def _git(repo: Path, *args: str, stdin: bytes | None = None) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        input=stdin,
        check=True,
        capture_output=True,
    ).stdout.decode().strip()


def _repo(tmp_path: Path) -> tuple[Path, str, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    (repo / "value.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "value.txt")
    _git(repo, "commit", "--quiet", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "value.txt").write_text("candidate\n", encoding="utf-8")
    _git(repo, "add", "value.txt")
    _git(repo, "commit", "--quiet", "-m", "candidate")
    candidate = _git(repo, "rev-parse", "HEAD")
    tree = _git(repo, "rev-parse", candidate + "^{tree}")
    return repo, base, candidate, tree


class _PublishClient:
    def __init__(self, *, commit: str, tree: str) -> None:
        self.commit = commit
        self.tree = tree

    def publish_result(self, task_id, runner_id, lease_token, *, source, length):
        del runner_id, lease_token
        assert source.read() == b"artifact"
        assert length == len(b"artifact")
        return {
            "result_ref": "refs/heads/ormas/job/" + task_id,
            "result_commit": self.commit,
            "artifact_sha256": "d" * 64,
            "tree_sha": self.tree,
        }


def _skeleton(tmp_path: Path, client) -> MinerSkeleton:
    config = MinerConfig(
        runner_id="miner-public",
        runner_version="test",
        platform="linux",
        capacity=1,
        cells=("task:lang/python",),
        workdir_root=tmp_path / "work",
        repo_id="repo-public",
        repo_url="https://github.com/acme/demo.git",
    )
    return MinerSkeleton(client, config, lambda *_args: pytest.fail("solver called"))


def _publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    repo: Path,
    base: str,
    candidate: str,
    tree: str,
    published: str,
) -> tuple[str, SolveResult]:
    client = _PublishClient(commit=published, tree=tree)
    miner = _skeleton(tmp_path, client)
    draft = SimpleNamespace(
        task_id="task-public",
        base_commit=base,
        repo_url="https://github.com/acme/demo.git",
    )
    lease = SimpleNamespace(lease_id="lease-public")
    result = SolveResult(candidate)

    def build_artifact(_draft, _workdir, _result, target):
        target.write(b"artifact")
        target.seek(0)
        return len(b"artifact"), "d" * 64, tree

    monkeypatch.setattr(
        "ormas_subnet.skeleton._build_public_artifact",
        build_artifact,
    )

    def local_public_git(args, *, cwd):
        if args[0] == "fetch":
            return b""
        return subprocess.check_output(["git", *args], cwd=cwd)

    monkeypatch.setattr("ormas_subnet.skeleton._public_git", local_public_git)
    return miner._publish_public(lease, draft, repo, result)


def _commit_with_parents(repo: Path, tree: str, *parents: str) -> str:
    argv = ["commit-tree", tree]
    for parent in parents:
        argv.extend(["-p", parent])
    return _git(repo, *argv, stdin=b"gateway result\n")


def test_public_result_accepts_exactly_one_frozen_base_parent(tmp_path, monkeypatch):
    repo, base, candidate, tree = _repo(tmp_path)
    published = _commit_with_parents(repo, tree, base)

    ref, result = _publish(
        tmp_path,
        monkeypatch,
        repo=repo,
        base=base,
        candidate=candidate,
        tree=tree,
        published=published,
    )

    assert ref == "refs/heads/ormas/job/task-public"
    assert result.result_commit == published


@pytest.mark.parametrize("topology", ["wrong-parent", "merge"])
def test_public_result_rejects_same_tree_with_non_frozen_topology(
    tmp_path, monkeypatch, topology,
):
    repo, base, candidate, tree = _repo(tmp_path)
    wrong_parent = _commit_with_parents(repo, _git(repo, "rev-parse", base + "^{tree}"), base)
    parents = (wrong_parent,) if topology == "wrong-parent" else (base, wrong_parent)
    published = _commit_with_parents(repo, tree, *parents)

    with pytest.raises(GitError, match="parent|identity"):
        _publish(
            tmp_path,
            monkeypatch,
            repo=repo,
            base=base,
            candidate=candidate,
            tree=tree,
            published=published,
        )


class _Response:
    status_code = 200

    @staticmethod
    def raise_for_status():
        return None

    @staticmethod
    def json():
        return {"ok": True}


class _Http:
    def post(self, path, *, content, headers, timeout):
        self.path = path
        self.body = b"".join(content)
        self.headers = headers
        self.timeout = timeout
        return _Response()


def test_public_client_streams_exact_binary_bytes_and_binding_headers():
    http = _Http()
    client = OrmasMinerClient(
        base_url="https://gateway.test",
        token="ormr_test",
        device_nonce="device-public",
        http_client=http,
    )
    raw = b"\x00\xffpublic\x00artifact"
    source = io.BytesIO(raw)
    source.seek(len(raw))

    assert client.publish_result(
        "task-public", "miner-public", "lease-public", source=source, length=len(raw)
    ) == {"ok": True}
    assert http.path == "/api/runner/v1/leases/task-public/publication"
    assert http.body == raw
    assert http.headers == {
        "X-Ormas-Runner-Device": "device-public",
        "X-Ormas-Runner-Id": "miner-public",
        "X-Ormas-Lease-Token": "lease-public",
        "Content-Type": "application/vnd.ormas.public-artifact.v1",
        "Content-Length": str(len(raw)),
    }
    assert http.timeout == 180.0


def test_public_artifact_enforces_frozen_changed_file_cap(tmp_path, monkeypatch):
    repo, base, _candidate, _tree = _repo(tmp_path)
    _git(repo, "reset", "--hard", base)
    for name in ("one.txt", "two.txt"):
        (repo / name).write_text(name, encoding="utf-8")
    _git(repo, "add", "one.txt", "two.txt")
    _git(repo, "commit", "--quiet", "-m", "two files")
    result = _git(repo, "rev-parse", "HEAD")
    packet = {
        "execution_policy": {"repo_base_sha": base},
        "execution_requirements": {
            "publication": {"max_changed_files": 1},
            "limits": {"max_bytes": 1024},
        },
    }
    monkeypatch.setattr(support, "validate_public_execution_packet", lambda _packet: None)

    with pytest.raises(ValueError, match="file limit"):
        support.build_public_artifact(packet, repo, result, io.BytesIO())
