"""A trivial ``solve`` implementation: run a shell command, commit the result.

This is NOT mining logic — it exists so ``MinerSkeleton`` runs end to end without
a real agent. A real miner replaces this with its own worker/harness/routing; see
``docs/DECISIONS.md`` §3 ("every
optimization dimension lives inside the miner").
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .skeleton import GitError, SolveResult, _run_git

__all__ = ["shell_solver", "make_shell_solver"]


def make_shell_solver(command: str, *, commit_message: str = "ormas-subnet: reference solve"):
    """Build a ``solve`` callable that runs ``command`` in the workdir, then commits.

    ``command`` runs through the shell inside the checked-out workdir. If it
    exits non-zero, the result reports ``failure_class="solve_command_failed"``
    but the (possibly partial) work is still committed and published — whether
    it actually verifies is the skeleton's job (it runs ``draft.verify_command``
    itself), not this solver's to declare.

    The solver stages only the draft's allowed_paths so miner-side artifacts
    never reach the customer repo.

    This solver made no model call, so it is the one honest case where
    reporting zero usage and a fixed identity is not a fabrication: it
    genuinely spent nothing and used no provider. Every one of those values
    comes from here, not from the skeleton — see ``docs/protocol.md``.
    """

    def solve(draft, workdir: Path) -> SolveResult:  # noqa: ANN001 - draft is TaskDraft, kept loose to avoid a cycle
        env = os.environ.copy()
        env.pop("ORMAS_MINER_TOKEN", None)
        proc = subprocess.run(
            command, shell=True, cwd=str(workdir), env=env, capture_output=True, text=True,
        )
        ok = proc.returncode == 0
        changed = _changed_paths(workdir)
        allowed = tuple(getattr(draft, "allowed_paths", None) or ())
        if allowed:
            try:
                _run_git(["add", "--ignore-errors", "--", *allowed], cwd=workdir)
            except GitError:
                for path in allowed:
                    try:
                        _run_git(["add", "--ignore-errors", "--", path], cwd=workdir)
                    except GitError:
                        continue
        else:
            _run_git(["add", "-A"], cwd=workdir)
        # `git commit` exits 1 with nothing to commit; treat that as "no-op solve".
        try:
            _run_git(
                ["commit", "-m", commit_message, "--allow-empty"], cwd=workdir,
            )
        except GitError:
            ok = False
        result_commit = _run_git(["rev-parse", "HEAD"], cwd=workdir)
        return SolveResult(
            result_commit=result_commit,
            changed_paths=tuple(changed),
            notes=proc.stdout[-2000:],
            failure_class=None if ok else "solve_command_failed",
            provider="reference",
            model="reference-shell-solver",
            prompt_tokens=0,
            completion_tokens=0,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            reasoning_tokens=0,
            upstream_cost_usd=0.0,
        )

    return solve


def _changed_paths(workdir: Path) -> list[str]:
    # `_run_git` removes only trailing line terminators, preserving the leading
    # status-code space used by porcelain output while pinning the executable
    # and stripping ambient service authority.
    try:
        output = _run_git(["status", "--porcelain"], cwd=workdir)
    except GitError:
        return []
    paths: list[str] = []
    for line in output.splitlines():
        if len(line) > 3:
            paths.append(line[3:].strip())
    return paths


# A ready-to-use default: runs `true` (no-op) so a bare skeleton still commits an
# empty result and completes the loop. Miners normally pass their own command.
shell_solver = make_shell_solver("true")
