"""Privileged Git and SSH never resolve through repository-controlled PATH."""
from __future__ import annotations

import os
import shlex
import stat
import subprocess
import sys
import time
import json
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from ormas_subnet import _trusted_tools as tools
from ormas_subnet import outcomes_support as support
from ormas_subnet import skeleton as sk
from ormas_subnet import verifier_runtime as runtime
from ormas_subnet.reference_solver import _changed_paths, make_shell_solver


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


def _identity_free_repo(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "empty-home"
    home.mkdir()
    git = tools.trusted_executable("git")
    env = {
        "PATH": str(git.parent),
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }
    subprocess.run([git, "init"], cwd=repo, env=env, check=True, capture_output=True)
    (repo / "out.txt").write_text("base\n")
    subprocess.run([git, "add", "-A"], cwd=repo, env=env, check=True, capture_output=True)
    subprocess.run(
        [git, "-c", "user.name=Base Author", "-c", "user.email=base@ormas.invalid",
         "-c", "commit.gpgSign=false", "commit", "-m", "base"],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
    )
    assert subprocess.run(
        [git, "config", "--local", "--get", "user.name"],
        cwd=repo,
        env=env,
        capture_output=True,
    ).returncode == 1
    return repo, git, env


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


def test_generated_public_verifier_ignores_writable_inherited_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PATH fake cannot forge the public base/result exit sequence."""
    marker = tmp_path / "fake-python-ran"
    state = tmp_path / "fake-python-state"
    fake = tmp_path / "job-bin" / "python3"
    _executable(
        fake,
        f"printf ran > {shlex.quote(str(marker))}\n"
        f"if [ -e {shlex.quote(str(state))} ]; then exit 0; fi\n"
        f": > {shlex.quote(str(state))}\n"
        "exit 86\n",
    )
    monkeypatch.setenv("PATH", str(fake.parent))
    command = support.effective_command({})

    exits = [
        sk._run_verify_command(command, cwd=tmp_path, trusted_public_python=True),
        sk._run_verify_command(command, cwd=tmp_path, trusted_public_python=True),
    ]

    # The real embedded runtime rejects the deliberately incomplete config.
    # The fake would instead produce the validator's accepting [86, 0] pair.
    assert exits == [98, 98]
    assert not marker.exists()
    assert not state.exists()


def test_generated_public_verifier_requires_trusted_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = support.effective_command({})

    def unavailable(name: str) -> Path:
        assert name == "python3"
        raise tools.TrustedExecutableUnavailable("missing")

    monkeypatch.setattr(sk, "trusted_executable", unavailable)

    assert sk._run_verify_command(
        command, cwd=tmp_path, trusted_public_python=True,
    ) == 127
    with pytest.raises(ValueError, match="trusted_verifier_python_unavailable"):
        sk._run_verify_command(
            command,
            cwd=tmp_path,
            trusted_public_python=True,
            raise_setup_errors=True,
        )


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
    command_config = {
        observed["env"][f"GIT_CONFIG_KEY_{index}"]:
            observed["env"][f"GIT_CONFIG_VALUE_{index}"]
        for index in range(int(observed["env"]["GIT_CONFIG_COUNT"]))
    }
    assert command_config == {
        "core.hooksPath": os.devnull,
        "core.fsmonitor": "false",
        "credential.helper": "",
        "protocol.ext.allow": "never",
        "user.name": "Ormas Reference Miner",
        "user.email": "reference-miner@ormas.invalid",
        "commit.gpgSign": "false",
    }
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
        hosts = Path(next(word for word in words if word.startswith(
            "UserKnownHostsFile=")).split("=", 1)[1])
        assert Path(words[0]).is_absolute()
        assert Path(words[0]) != fake
        assert words[words.index("-F") + 1] == os.devnull
        assert "IdentityAgent=none" in words
        assert "GlobalKnownHostsFile=" + os.devnull in words
        assert key.exists()
        assert hosts.exists() and hosts.read_bytes() == b""
        assert stat.S_IMODE(hosts.stat().st_mode) == 0o600
    assert not key.exists()
    assert not hosts.exists()


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


def test_reference_solver_commits_without_ambient_or_local_identity(tmp_path: Path) -> None:
    repo, git, env = _identity_free_repo(tmp_path)
    result = make_shell_solver("printf 'mined\\n' >> out.txt")(
        SimpleNamespace(allowed_paths=["out.txt"]), repo,
    )

    assert result.failure_class is None
    assert subprocess.run(
        [git, "rev-list", "--count", "HEAD"], cwd=repo, env=env,
        check=True, capture_output=True, text=True,
    ).stdout.strip() == "2"
    assert subprocess.run(
        [git, "show", "-s", "--format=%an|%ae|%G?", "HEAD"], cwd=repo, env=env,
        check=True, capture_output=True, text=True,
    ).stdout.strip() == "Ormas Reference Miner|reference-miner@ormas.invalid|N"


def test_reference_status_ignores_fake_git_in_inherited_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, _git, _env = _identity_free_repo(tmp_path)
    (repo / "out.txt").write_text("changed\n")
    marker = tmp_path / "fake-status-ran"
    fake = tmp_path / "bin" / "git"
    _executable(fake, f"touch {shlex.quote(str(marker))}\nprintf ' M forged.txt\\n'\n")
    monkeypatch.setenv("PATH", str(fake.parent))

    assert _changed_paths(repo) == ["out.txt"]
    assert not marker.exists()


def test_run_git_disables_repository_fsmonitor(
    tmp_path: Path,
) -> None:
    repo, git, env = _identity_free_repo(tmp_path)
    marker = tmp_path / "fsmonitor-ran"
    hook = tmp_path / "fsmonitor"
    _executable(hook, f"touch {shlex.quote(str(marker))}\nprintf '0\\n'\n")
    subprocess.run(
        [git, "config", "--local", "core.fsmonitor", str(hook)],
        cwd=repo,
        env=env,
        check=True,
    )

    assert sk._run_git(["status", "--porcelain"], cwd=repo) == ""
    assert not marker.exists()


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


def test_embedded_runtime_ignores_hostile_sibling_path_and_bounds_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    trusted = tmp_path / "operator" / "bin"
    hostile = tmp_path / "job" / "bin"
    for directory in (trusted, hostile):
        for name in ("git", "docker"):
            _executable(directory / name, "exit 0\n")
    monkeypatch.setenv("PATH", str(hostile))
    monkeypatch.setenv("ORMAS_TEST_AMBIENT_SECRET", "must-not-cross")
    monkeypatch.setattr(runtime, "_SYSTEM_EXECUTABLE_DIRS", (trusted,))
    monkeypatch.setattr(runtime, "_service_can_write", lambda _path: False)

    git = runtime._trusted_executable("git")
    docker = runtime._trusted_executable("docker")
    host_env = runtime._host_environment(git, docker)

    assert git == (trusted / "git").resolve()
    assert docker == (trusted / "docker").resolve()
    assert runtime.safe_git_argv(str(git), "status")[0] == str(git)
    assert host_env["PATH"] == str(trusted.resolve())
    assert host_env["HOME"] == "/nonexistent"
    assert "ORMAS_TEST_AMBIENT_SECRET" not in host_env


def test_embedded_runtime_rechecks_gitlinks_with_frozen_git_path(tmp_path: Path) -> None:
    repo, git, _env = _identity_free_repo(tmp_path)
    target = tmp_path / "snapshot"
    target.mkdir()
    config = {
        "repository_gitlinks": {}, "lock_files": {},
        "limits": {"max_files": 10, "max_bytes": 1024 * 1024,
                   "source_max_bytes": 1024 * 1024},
    }

    fingerprints = runtime._snapshot(
        repo, target, config, time.monotonic() + 10,
        runtime._host_environment(git, git), str(git),
    )

    assert fingerprints["out.txt"] == hashlib.sha256(b"base\n").hexdigest()
    assert (target / "out.txt").read_text() == "base\n"


def test_embedded_runtime_rejects_missing_and_writable_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime, "_SYSTEM_EXECUTABLE_DIRS", (tmp_path / "missing",))
    with pytest.raises(runtime.RuntimeRefusal, match="runtime_executable_unavailable"):
        runtime._trusted_executable("git")

    writable = tmp_path / "writable"
    _executable(writable / "git", "exit 0\n")
    monkeypatch.setattr(runtime, "_SYSTEM_EXECUTABLE_DIRS", (writable,))
    with pytest.raises(runtime.RuntimeRefusal, match="runtime_executable_unavailable"):
        runtime._trusted_executable("git")


def test_cleanup_guard_uses_passed_absolute_docker_with_hostile_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    trusted_marker = tmp_path / "trusted-cleanup"
    hostile_marker = tmp_path / "hostile-cleanup"
    trusted = tmp_path / "trusted" / "docker"
    hostile = tmp_path / "hostile" / "docker"
    _executable(trusted, f"printf '%s\\n' \"$*\" >> {shlex.quote(str(trusted_marker))}\n")
    _executable(hostile, f"touch {shlex.quote(str(hostile_marker))}\n")
    monkeypatch.setenv("PATH", str(hostile.parent))
    payload = json.dumps([99999999, 0, str(tmp_path / "absent"),
                          ["candidate"], ["volume"], str(trusted)])

    subprocess.run(
        [sys.executable, "-I", "-S", "-c", runtime._CLEANUP_GUARD, payload],
        env={"PATH": str(hostile.parent)},
        check=True,
    )

    assert trusted_marker.read_text().splitlines() == [
        "rm -f candidate", "volume rm volume",
    ]
    assert not hostile_marker.exists()


def test_verifier_catalog_history_loads_after_compiler_revocation() -> None:
    assets = Path(support.__file__).with_name("client_assets")
    current = json.loads(assets.joinpath("outcomes_support.v1.json").read_text())
    previous = json.loads(assets.joinpath(
        "outcomes_support_history", "2026-10-04.1-e4511421b6b2.json").read_text())
    broken = json.loads(assets.joinpath(
        "outcomes_support_history", "2026-10-14.1-aebcdc4a64d5.json").read_text())
    source = Path(runtime.__file__).read_text()

    assert current["revision"] == "2026-10-14.2"
    assert current["verifier_compiler_sha256"] == hashlib.sha256(source.encode()).hexdigest()
    assert support.digest(previous) == (
        "sha256:e4511421b6b2d459810e31cf50338c12bd91b02cee054a98d69d602cd9b0717f"
    )
    assert support.load_support_catalog(support.digest(previous)) == previous
    assert support.digest(broken) == (
        "sha256:aebcdc4a64d54b94c27a1eae933144290abe82f0062626f0187ad33d7a8195e7"
    )
    assert support.load_support_catalog(support.digest(broken)) == broken
