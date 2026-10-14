"""Privileged Git and SSH never resolve through repository-controlled PATH."""
from __future__ import annotations

import os
import shlex
import stat
import subprocess
from pathlib import Path

import pytest

from ormas_subnet import _trusted_tools as tools
from ormas_subnet import outcomes_support as support
from ormas_subnet import skeleton as sk


_KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nZmFrZQ==\n-----END OPENSSH PRIVATE KEY-----\n"
_CREDENTIAL = {"kind": "ssh_deploy_key", "private_key": _KEY}
_REPOSITORY = "https://github.com/acme/demo.git"
_READ_CREDENTIAL = {
    **_CREDENTIAL,
    "fingerprint": "SHA256:test",
    "scope": "read",
    "repository_url": _REPOSITORY,
}


def _executable(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)


def test_run_git_ignores_writable_inherited_path_and_keeps_legitimate_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "fake-ran"
    fake = tmp_path / "bin" / "git"
    _executable(fake, f"touch {shlex.quote(str(marker))}\nexit 99\n")
    monkeypatch.setenv("PATH", str(fake.parent))

    output = sk._run_git(["--version"], cwd=tmp_path)

    assert output.startswith("git version ")
    assert not marker.exists()


def test_public_git_ignores_writable_inherited_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "fake-public-ran"
    fake = tmp_path / "bin" / "git"
    _executable(fake, f"touch {shlex.quote(str(marker))}\nexit 99\n")
    monkeypatch.setenv("PATH", str(fake.parent))

    output = support.public_git(["--version"], cwd=tmp_path)

    assert output.startswith(b"git version ")
    assert not marker.exists()


def test_run_git_passes_only_bounded_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ORMAS_TEST_AMBIENT_SECRET", "must-not-reach-git")
    monkeypatch.setenv("GIT_SSH_AUTH_SOCK", str(tmp_path / "agent"))
    observed = {}

    def run(argv, **kwargs):
        observed.update(argv=list(argv), env=dict(kwargs["env"]))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(sk.subprocess, "run", run)
    assert sk._run_git(["status"], cwd=tmp_path) == ""

    assert Path(observed["argv"][0]).is_absolute()
    assert observed["argv"][1:] == ["status"]
    assert observed["env"]["HOME"] == "/nonexistent"
    assert observed["env"]["GIT_NO_REPLACE_OBJECTS"] == "1"
    assert observed["env"]["GIT_CONFIG_KEY_0"] == "core.hooksPath"
    assert observed["env"]["GIT_CONFIG_VALUE_0"] == os.devnull
    assert observed["env"]["PATH"] == str(Path(observed["argv"][0]).parent)
    assert "ORMAS_TEST_AMBIENT_SECRET" not in observed["env"]
    assert "GIT_SSH_AUTH_SOCK" not in observed["env"]


def test_credential_command_uses_absolute_vetted_ssh_not_inherited_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = tmp_path / "bin" / "ssh"
    _executable(fake, "exit 99\n")
    monkeypatch.setenv("PATH", str(fake.parent))

    with sk._credential_git_env(_CREDENTIAL) as env:
        words = shlex.split(env["GIT_SSH_COMMAND"])
        key = Path(words[words.index("-i") + 1])
        assert Path(words[0]).is_absolute()
        assert Path(words[0]) != fake
        assert key.exists()
    assert not key.exists()


def test_private_repository_git_uses_absolute_vetted_git_and_ssh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "fake-ssh-ran"
    fake = tmp_path / "bin" / "ssh"
    _executable(fake, f"touch {shlex.quote(str(marker))}\nexit 99\n")
    monkeypatch.setenv("PATH", str(fake.parent))
    monkeypatch.setenv("ORMAS_TEST_AMBIENT_SECRET", "must-not-reach-git")
    observed = {}

    def run(argv, **kwargs):
        observed.update(argv=list(argv), env=dict(kwargs["env"]))
        return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(support.subprocess, "run", run)
    support.repository_git(
        ["clone", "--no-checkout", _REPOSITORY, str(tmp_path / "checkout")],
        cwd=tmp_path,
        repo_url=_REPOSITORY,
        credential=_READ_CREDENTIAL,
    )

    ssh = shlex.split(observed["env"]["GIT_SSH_COMMAND"])
    assert Path(observed["argv"][0]).is_absolute()
    assert Path(ssh[0]).is_absolute()
    assert Path(ssh[0]) != fake
    assert observed["env"]["PATH"] == str(Path(observed["argv"][0]).parent)
    assert "ORMAS_TEST_AMBIENT_SECRET" not in observed["env"]
    assert not marker.exists()


def test_run_git_rejects_unneeded_environment_authority(tmp_path: Path) -> None:
    with pytest.raises(sk.GitError, match="unsupported git environment override"):
        sk._run_git(["--version"], cwd=tmp_path, env={"SSH_AUTH_SOCK": "/tmp/agent"})


def test_missing_trusted_git_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tools, "_SYSTEM_EXECUTABLE_DIRS", (tmp_path / "missing",))
    with pytest.raises(sk.GitError, match="trusted git executable unavailable"):
        sk._run_git(["--version"], cwd=tmp_path)
    with pytest.raises(ValueError, match="trusted git executable unavailable"):
        support.public_git(["--version"], cwd=tmp_path)


def test_missing_trusted_ssh_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    git = tools.trusted_executable("git")

    def resolve(name: str) -> Path:
        if name == "ssh":
            raise tools.TrustedExecutableUnavailable("missing")
        return git

    monkeypatch.setattr(sk, "trusted_executable", resolve)
    with pytest.raises(sk.GitError, match="trusted ssh executable unavailable"):
        with sk._credential_git_env(_CREDENTIAL):
            pass


def test_service_writable_tool_or_parent_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = tmp_path / "system" / "git"
    _executable(fake, "exit 0\n")
    monkeypatch.setattr(tools, "_SYSTEM_EXECUTABLE_DIRS", (fake.parent,))

    with pytest.raises(tools.TrustedExecutableUnavailable):
        tools.trusted_executable("git")
