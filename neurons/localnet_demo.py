"""Run the whole miner loop offline, with no token and no gateway at all.

    python neurons/localnet_demo.py [--workdir DIR] [--scenario pass|noop|out-of-scope]

Builds a temporary local bare git "client repository" whose ``verify_command``
fails on the base commit, seeds one job for it on
:class:`ormas_subnet.localnet.LocalGateway` (an in-process, offline stand-in
for the gateway — see that module's docstring for exactly what it does and
does not do), runs :class:`ormas_subnet.skeleton.MinerSkeleton` for exactly one
tick against a solver appropriate to ``--scenario``, and prints the recorded
receipt.

Three scenarios (settlement vocabulary is the real wire vocabulary — see
``ormas_subnet/localnet.py`` and ``docs/protocol.md`` § "Settlement
derivation"):

- ``pass`` (default) — the solver makes the required change inside
  ``allowed_paths``. Verifies, stays in scope, settles as **``paid``**.
- ``noop`` — the solver does nothing. Verify command still fails on base
  content, so the delivery honestly settles as **``no_delivery``**.
- ``out-of-scope`` — the solver edits a file outside ``allowed_paths``. The
  verify command actually passes, but ``scope_ok`` (computed from a real
  ``git diff``, never a solver's say-so) is False, so the delivery honestly
  settles as **``no_delivery``** even though verification alone would look
  fine.

Exit code is 0 only when each scenario settles the way it is supposed to
(``paid`` for ``pass``, ``no_delivery`` for the other two); a scenario that
settles the wrong way exits 1.

No network host is contacted anywhere in this script.
"""
from __future__ import annotations

import argparse
import os
import secrets
import shutil
import stat
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ormas_subnet.localnet import LocalGateway  # noqa: E402
from ormas_subnet.client import OrmasMinerClient  # noqa: E402
from ormas_subnet._private_file import read_private_text  # noqa: E402
from ormas_subnet.skeleton import MinerConfig, MinerSkeleton, SolveResult  # noqa: E402

# Never /tmp: it is world-readable, periodically purged, and shared with every
# other user on the box — the wrong place for even a throwaway "client repo".
# See docs/QUICKSTART_LOCAL.md.
DEFAULT_WORKDIR = Path.home() / ".ormas" / "localnet-demo"
_OWNER_MARKER = ".ormas-localnet-demo"


def _owner_marker_text(target: Path) -> str:
    """Bind the reset authority marker to one canonical directory."""
    return f"ormas-localnet-demo:v2:{target}\n"


def _write_owner_marker(target_fd: int, target: Path) -> None:
    """Create the marker once with private mode; never follow an injected link."""
    if not hasattr(os, "O_NOFOLLOW"):
        raise ValueError("ownership marker requires no-follow file support")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    fd = os.open(_OWNER_MARKER, flags, 0o600, dir_fd=target_fd)
    try:
        os.fchmod(fd, 0o600)
        remaining = _owner_marker_text(target).encode("utf-8")
        while remaining:
            written = os.write(fd, remaining)
            if written <= 0:
                raise OSError("short ownership marker write")
            remaining = remaining[written:]
    finally:
        os.close(fd)


def _clear_open_directory(directory_fd: int) -> None:
    """Remove one directory tree through already-open, no-follow descriptors."""
    with os.scandir(directory_fd) as entries:
        names = [entry.name for entry in entries]
    for name in names:
        before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(before.st_mode):
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            flags |= getattr(os, "O_CLOEXEC", 0)
            child_fd = os.open(name, flags, dir_fd=directory_fd)
            try:
                opened = os.fstat(child_fd)
                if not os.path.samestat(before, opened):
                    raise ValueError("localnet demo directory changed during reset")
                _clear_open_directory(child_fd)
                current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if not os.path.samestat(opened, current):
                    raise ValueError("localnet demo directory changed during reset")
                os.rmdir(name, dir_fd=directory_fd)
            finally:
                os.close(child_fd)
        else:
            os.unlink(name, dir_fd=directory_fd)


def _remove_owned_directory(
    parent_fd: int,
    target_name: str,
    target_fd: int,
    target_info: os.stat_result,
) -> None:
    """Quarantine then remove only the directory represented by ``target_fd``."""
    quarantine = f".ormas-localnet-demo-reset-{secrets.token_hex(16)}"
    os.rename(
        target_name, quarantine,
        src_dir_fd=parent_fd, dst_dir_fd=parent_fd,
    )
    moved = os.stat(quarantine, dir_fd=parent_fd, follow_symlinks=False)
    if not os.path.samestat(target_info, moved):
        raise ValueError("localnet demo directory changed during reset")
    _clear_open_directory(target_fd)
    current = os.stat(quarantine, dir_fd=parent_fd, follow_symlinks=False)
    if not os.path.samestat(target_info, current):
        raise ValueError("localnet demo directory changed during reset")
    os.rmdir(quarantine, dir_fd=parent_fd)

TASK_ID = "local-demo-task"
VERIFY_COMMAND = "grep -q mined out.txt"
ALLOWED_PATHS = ["out.txt"]
BRIEF = "append the line 'mined' to out.txt"


def _run_git(args: list[str], *, cwd: Path) -> str:
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def _make_client_bare_repo(root: Path) -> tuple[Path, str]:
    """Create a bare "client repository" plus a seed checkout with one commit.

    Returns ``(bare_repo_path, base_commit)``. ``verify_command`` fails against
    this base content — the whole point of the task.
    """
    bare = root / "client-repo.git"
    seed = root / "client-repo-seed"
    bare.mkdir(parents=True)
    _run_git(["init", "--bare", str(bare)], cwd=root)
    seed.mkdir()
    _run_git(["init"], cwd=seed)
    _run_git(["config", "user.email", "demo@example.invalid"], cwd=seed)
    _run_git(["config", "user.name", "Ormas Localnet Demo"], cwd=seed)
    (seed / "out.txt").write_text("base\n")
    _run_git(["add", "-A"], cwd=seed)
    _run_git(["commit", "-m", "base"], cwd=seed)
    _run_git(["remote", "add", "origin", str(bare)], cwd=seed)
    _run_git(["push", "origin", "HEAD:refs/heads/main"], cwd=seed)
    base_commit = _run_git(["rev-parse", "HEAD"], cwd=seed)
    shutil.rmtree(seed)
    return bare, base_commit


def _solve_pass(draft, workdir: Path) -> SolveResult:  # noqa: ANN001 - draft is TaskDraft
    (workdir / "out.txt").write_text("base\nmined\n")
    _run_git(["add", "-A"], cwd=workdir)
    _run_git(["commit", "-m", "solve: append mined line"], cwd=workdir)
    result_commit = _run_git(["rev-parse", "HEAD"], cwd=workdir)
    return SolveResult(
        result_commit=result_commit,
        changed_paths=("out.txt",),
        provider="reference",
        model="localnet-demo-solver",
        prompt_tokens=0,
        completion_tokens=0,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
        reasoning_tokens=0,
        upstream_cost_usd=0.0,
    )


def _solve_noop(draft, workdir: Path) -> SolveResult:  # noqa: ANN001 - draft is TaskDraft
    _run_git(["commit", "--allow-empty", "-m", "solve: no-op"], cwd=workdir)
    result_commit = _run_git(["rev-parse", "HEAD"], cwd=workdir)
    return SolveResult(
        result_commit=result_commit,
        changed_paths=(),
        provider="reference",
        model="localnet-demo-solver",
        prompt_tokens=0,
        completion_tokens=0,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
        reasoning_tokens=0,
        upstream_cost_usd=0.0,
    )


def _solve_out_of_scope(draft, workdir: Path) -> SolveResult:  # noqa: ANN001 - draft is TaskDraft
    # Makes verify_command pass (edits out.txt too) *and* touches a file
    # outside allowed_paths — scope_ok must catch this even though the
    # verifier alone would be satisfied.
    (workdir / "out.txt").write_text("base\nmined\n")
    (workdir / "other.txt").write_text("out of scope\n")
    _run_git(["add", "-A"], cwd=workdir)
    _run_git(["commit", "-m", "solve: out of scope edit"], cwd=workdir)
    result_commit = _run_git(["rev-parse", "HEAD"], cwd=workdir)
    return SolveResult(
        result_commit=result_commit,
        changed_paths=("out.txt", "other.txt"),
        provider="reference",
        model="localnet-demo-solver",
        prompt_tokens=0,
        completion_tokens=0,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
        reasoning_tokens=0,
        upstream_cost_usd=0.0,
    )


_SCENARIOS = {
    "pass": _solve_pass,
    "noop": _solve_noop,
    "out-of-scope": _solve_out_of_scope,
}

# Real wire settlement vocabulary (docs/protocol.md § "Settlement
# derivation"): "paid" or "no_delivery", never "unpaid". What a
# correctly-behaving demo run for each scenario should settle as.
_EXPECT_SETTLEMENT = {"pass": "paid", "noop": "no_delivery", "out-of-scope": "no_delivery"}


def _parse(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workdir", default=str(DEFAULT_WORKDIR), help=f"default: {DEFAULT_WORKDIR}")
    ap.add_argument("--scenario", choices=sorted(_SCENARIOS), default="pass")
    return ap.parse_args(argv)


def _prepare_workdir(workdir: Path) -> Path:
    """Create or safely reset a directory owned by this demo."""
    if workdir.is_symlink():
        raise ValueError("refusing to reset a symlinked localnet demo directory")
    target = workdir.resolve()
    home = Path.home().resolve()
    repository = Path(__file__).resolve().parents[1]
    current = Path.cwd().resolve()
    filesystem_root = Path(target.anchor)
    protected = (home, repository, current)
    if target == filesystem_root or any(target == item or target in item.parents for item in protected):
        raise ValueError("refusing an unsafe localnet demo directory")

    target.parent.mkdir(parents=True, exist_ok=True)
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise ValueError("localnet demo reset requires no-follow directory support")
    parent_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    parent_flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        parent_fd = os.open(target.parent, parent_flags)
    except OSError:
        raise ValueError("refusing an unsafe localnet demo parent directory") from None
    try:
        parent_info = os.fstat(parent_fd)
        if (
            parent_info.st_uid != os.geteuid()
            or stat.S_IMODE(parent_info.st_mode) & 0o022
        ):
            raise ValueError("refusing an unsafe localnet demo parent directory")

        try:
            target_fd = os.open(target.name, parent_flags, dir_fd=parent_fd)
        except FileNotFoundError:
            target_fd = -1
        except OSError:
            raise ValueError("refusing to reset an unowned localnet demo directory") from None

        if target_fd >= 0:
            try:
                info = os.fstat(target_fd)
                named_info = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
                if (
                    not os.path.samestat(info, named_info)
                    or not stat.S_ISDIR(info.st_mode)
                    or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o700
                ):
                    raise ValueError("refusing to reset an unowned localnet demo directory")
                try:
                    marker_text = read_private_text(
                        _OWNER_MARKER,
                        label="localnet demo ownership marker",
                        max_bytes=4096,
                        dir_fd=target_fd,
                    )
                except ValueError:
                    raise ValueError("refusing to reset an unowned localnet demo directory") from None
                if marker_text != _owner_marker_text(target):
                    raise ValueError("refusing to reset an unowned localnet demo directory")
                _remove_owned_directory(parent_fd, target.name, target_fd, info)
            finally:
                os.close(target_fd)

        try:
            os.mkdir(target.name, 0o700, dir_fd=parent_fd)
            target_fd = os.open(target.name, parent_flags, dir_fd=parent_fd)
            try:
                os.fchmod(target_fd, 0o700)
                _write_owner_marker(target_fd, target)
            finally:
                os.close(target_fd)
        except FileExistsError:
            raise ValueError("localnet demo directory changed during reset") from None
    finally:
        os.close(parent_fd)
    return target


def run_demo(workdir: Path, scenario: str) -> dict:
    """Run one scenario end to end; returns the recorded receipt dict."""
    workdir = _prepare_workdir(workdir)

    bare_repo, base_commit = _make_client_bare_repo(workdir)

    gateway = LocalGateway(
        task_id=TASK_ID,
        base_commit=base_commit,
        verify_command=VERIFY_COMMAND,
        repo_url=str(bare_repo),
        brief=BRIEF,
        allowed_paths=ALLOWED_PATHS,
    )
    client = OrmasMinerClient(base_url="local://localnet-demo", token="no-token-needed", http_client=gateway)
    config = MinerConfig(
        runner_id="localnet-demo-miner",
        runner_version="0.0.1",
        platform="local",
        capacity=1,
        cells=("code-edit-small",),
        workdir_root=workdir / "work",
        repo_id="repo1",
        repo_url=str(bare_repo),
        push_remote="origin",
    )
    skeleton = MinerSkeleton(client, config, _SCENARIOS[scenario])
    skeleton.register()
    skeleton.bind(project_id="local-demo-project", base_commit=base_commit)
    did_work = skeleton.run_once()
    assert did_work, "LocalGateway had no work queued — this should not happen"

    return gateway.receipts[-1]


def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)
    receipt = run_demo(Path(args.workdir).expanduser(), args.scenario)

    print(f"scenario:            {args.scenario}")
    print(f"verification_state:  {receipt['verification_state']}")
    print(f"scope_ok:            {receipt['scope_ok']}")
    print(f"result_commit:       {receipt['result_commit']}")
    print(f"changed_paths:       {receipt['changed_paths']}")
    print(f"settlement:          {receipt['settlement']}")
    print(f"failure_class:       {receipt['failure_class']}")
    print(f"customer_billed_usd: {receipt['customer_billed_usd']}")

    expected_settlement = _EXPECT_SETTLEMENT[args.scenario]
    if receipt["settlement"] != expected_settlement:
        print(
            f"UNEXPECTED: scenario {args.scenario!r} settled {receipt['settlement']!r}, "
            f"expected {expected_settlement!r}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
