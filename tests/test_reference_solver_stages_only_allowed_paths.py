"""The reference solver commits only what the packet allows — never the miner's environment.

Jake's first live run (2026-09-15, job_87075aeb39d1): the result commit's ONLY change
was ``.rustup/settings.toml`` — a toolchain file some hook on the miner's machine wrote
into the workdir, swept up by ``git add -A``. A miner's local artifacts must never land
in a customer's repository, and nothing outside ``allowed_paths`` may be staged.

Rule: when the draft carries ``allowed_paths``, stage exactly those paths (``git add --
<paths>``; a listed path that does not exist is fine). With no ``allowed_paths`` on the
draft, keep today's ``add -A`` (legacy drafts).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ormas_subnet.reference_solver import make_shell_solver
from tests.test_skeleton import _init_repo, _run_one_task, requires_git


def _changed(repo: Path, base: str, head: str) -> list[str]:
    out = subprocess.run(
        ["git", "diff", "--name-only", base, head], cwd=repo, check=True, capture_output=True, text=True,
    ).stdout
    return sorted(line for line in out.splitlines() if line)


@requires_git
def test_environment_artifacts_outside_allowed_paths_are_not_committed(tmp_path: Path) -> None:
    repo, base_commit = _init_repo(tmp_path)
    # The solve edits the allowed file AND a toolchain hook writes a dotfile into the workdir.
    solver = make_shell_solver(
        "echo mined >> out.txt && mkdir -p .rustup && echo 'v=1' > .rustup/settings.toml && echo junk > scratch.log"
    )
    gateway, skeleton = _run_one_task(
        tmp_path, repo=repo, base_commit=base_commit, verify_command="test -f out.txt",
        solve_fn=solver, allowed_paths=["out.txt"],
    )
    workdir = skeleton.config.workdir_root / "task_1"
    result_commit = gateway.completed["terminal"]["result_commit"]
    assert _changed(workdir, base_commit, result_commit) == ["out.txt"]
    capture = gateway.completed["capture"]
    assert capture["scope_ok"] is True
    assert sorted(c["path"] for c in (capture.get("changed_paths") or [])) == ["out.txt"]
    # The stray files still exist in the workdir — they were simply never staged.
    assert (workdir / ".rustup" / "settings.toml").exists()
    assert (workdir / "scratch.log").exists()


@requires_git
def test_listed_but_untouched_allowed_path_is_fine(tmp_path: Path) -> None:
    repo, base_commit = _init_repo(tmp_path)
    solver = make_shell_solver("echo mined >> out.txt")
    gateway, skeleton = _run_one_task(
        tmp_path, repo=repo, base_commit=base_commit, verify_command="test -f out.txt",
        solve_fn=solver, allowed_paths=["out.txt", "never_created.py"],
    )
    workdir = skeleton.config.workdir_root / "task_1"
    result_commit = gateway.completed["terminal"]["result_commit"]
    assert _changed(workdir, base_commit, result_commit) == ["out.txt"]
    assert gateway.completed["terminal"]["verification_state"] == "verified"


@requires_git
def test_no_allowed_paths_keeps_add_all(tmp_path: Path) -> None:
    """Keep-green: a legacy draft without allowed_paths stages everything as today.
    (The local gateway substitutes ["out.txt"] for None, so an EMPTY list is the legacy shape.)"""
    repo, base_commit = _init_repo(tmp_path)
    solver = make_shell_solver("echo mined >> out.txt && echo extra > extra.txt")
    gateway, skeleton = _run_one_task(
        tmp_path, repo=repo, base_commit=base_commit, verify_command="test -f out.txt",
        solve_fn=solver, allowed_paths=[],
    )
    workdir = skeleton.config.workdir_root / "task_1"
    result_commit = gateway.completed["terminal"]["result_commit"]
    assert _changed(workdir, base_commit, result_commit) == ["extra.txt", "out.txt"]
