"""HS-R2: opted-in jobs need no configured repository; binding still does."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from neurons import miner
from ormas_subnet import skeleton as sk
from tests.test_skeleton_repo_credential import CRED, _draft


RUN_ARGS = ["--gateway", "https://api.ormas.ai", "--token-env", "ORMAS_MINER_TOKEN",
            "--cell", "task:code", "--solve-command", "true"]


def test_run_repository_flags_are_optional():
    args = miner._parse(RUN_ARGS)
    assert not args.repo_id and not args.repo_url


@pytest.mark.parametrize("provided,missing", [
    ([], ["--repo-id", "--repo-url"]),
    (["--repo-id", "repo"], ["--repo-url"]),
    (["--repo-url", "https://github.com/acme/demo.git"], ["--repo-id"]),
])
def test_bind_requires_repository_flags(provided, missing, capsys):
    with pytest.raises(SystemExit) as result:
        miner._parse([*RUN_ARGS, "--bind-project", "proj_test", *provided])
    assert result.value.code == 2
    error = capsys.readouterr().err.rsplit("error: ", 1)[1]
    assert "--bind-project" in error
    assert all(flag in error for flag in missing)


def test_bind_accepts_both_repository_flags():
    args = miner._parse([*RUN_ARGS, "--bind-project", "proj_test",
                         "--bind-base-commit", "a" * 40, "--repo-id", "repo",
                         "--repo-url", "https://github.com/acme/demo.git"])
    assert args.repo_id == "repo"
    assert args.repo_url == "https://github.com/acme/demo.git"


def _skeleton(tmp_path, repo_url=""):
    skeleton = object.__new__(sk.MinerSkeleton)
    skeleton.config = SimpleNamespace(workdir_root=tmp_path / "work", repo_url=repo_url)
    return skeleton


def test_legacy_job_without_repository_fails_before_git(tmp_path, monkeypatch):
    monkeypatch.setattr(sk, "_run_git", lambda *a, **kw: pytest.fail("git invoked"))
    with pytest.raises(sk.GitError, match="--repo-url"):
        _skeleton(tmp_path)._clone_and_checkout(_draft(repo_url=""))


@pytest.mark.parametrize("configured,credential,expected", [
    ("https://github.com/acme/bound.git", None, "https://github.com/acme/bound.git"),
    ("", CRED, "https://github.com/acme/job.git"),
])
def test_legacy_repository_selection_is_preserved(tmp_path, monkeypatch,
                                                  configured, credential, expected):
    calls = []
    monkeypatch.setattr(sk, "_run_git", lambda args, **kw: calls.append(args))
    _skeleton(tmp_path, configured)._clone_and_checkout(
        _draft(repo_url="https://github.com/acme/job.git", repo_credential=credential))
    assert calls[0][:2] == ["clone", expected]


def test_packet_job_uses_its_repository_without_configured_fallback(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(sk, "_public_packet", lambda draft: {
        "execution_requirements": {"repository_visibility": "public"}})
    monkeypatch.setattr(sk, "_public_git", lambda args, **kw: calls.append(args))
    _skeleton(tmp_path)._clone_and_checkout(_draft(repo_url="https://github.com/acme/job.git"))
    assert calls[0][:3] == ["clone", "--no-checkout", "https://github.com/acme/job.git"]
