"""Reference miner loop — register, claim, solve, publish, complete.

This is the pluggable skeleton `docs/DECISIONS.md`
calls for: "a reference miner skeleton with pluggable solve, plus a trivial reference
solver so the skeleton runs end to end." Everything a real miner competes on — model
routing, harness, cost, speed — lives inside the ``solve`` callable you supply. This
module owns only protocol mechanics: claim the lease, give ``solve`` a clean checkout,
publish its result branch, honestly report what happened, and complete.

No model calls happen anywhere in this package.
"""
from __future__ import annotations

import hashlib
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .client import OrmasMinerClient
from .protocol import (
    RepoRegistration,
    RunnerRegistration,
    TaskDraft,
    TaskEvent,
    TaskLease,
    TaskReceipt,
    TaskTerminal,
)

__all__ = [
    "SolveResult",
    "SolveFn",
    "MinerConfig",
    "MinerSkeleton",
    "GitError",
    "VerifyCommandError",
]

# Wire-format result_ref prefixes the gateway accepts (runner_api._valid_result_ref):
# published branches use refs/heads/…; a no-push dry run records a local: pseudo-ref.
_RESULT_REF_HEADS = "refs/heads/ormas/job/"
_RESULT_REF_LOCAL = "local:ormas/job/"

# Registration/lease defaults returned authoritatively by the gateway at
# /api/runner/v1/registrations (runner_api.py: POLL_INTERVAL_S, LEASE_TTL_S,
# HEARTBEAT_S). Used only until the first registration response arrives.
DEFAULT_POLL_INTERVAL_S = 15
DEFAULT_LEASE_TTL_S = 300
DEFAULT_HEARTBEAT_S = 90

# Bound on the exception text carried in a failure capture's ``error`` field —
# enough to diagnose, short enough to stay a sane evidence payload.
_FAILURE_MESSAGE_MAX = 500

# `TaskReceipt.actual_provider` / `.model` are plain ``str`` fields on the wire —
# there is no null. The server additionally requires `model` to match
# `^[a-z0-9./:-]{1,80}$` (runner_api._MODEL_RE), so an empty string is rejected;
# this literal is the DTO-legal way to say "the solver didn't tell us" without
# inventing a fake identity. Always paired with `metering_complete=False`.
_UNKNOWN_PROVIDER = "unknown"
_UNKNOWN_MODEL = "unknown"

# POSIX-style NAME=value at the start of a verify command. Mirrors the private
# runner's exact rule (grokbuild_client._ENV_ASSIGNMENT_RE / _split_verify_command):
# leading assignment tokens are stripped into the environment; the remaining
# argv is executed directly, never through a shell.
_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


class GitError(RuntimeError):
    """A git subprocess used by the skeleton failed."""


class VerifyCommandError(RuntimeError):
    """``verify_command`` could not be parsed into a runnable argv."""


@dataclass(frozen=True)
class SolveResult:
    """What a ``solve`` implementation hands back to the skeleton.

    ``result_commit`` must already exist in the workdir (solve commits its own
    edits); the skeleton only creates the ``ormas/job/<task_id>`` branch and
    publishes it, it does not commit on the miner's behalf.

    The usage/cost fields below are all optional and default to ``None``
    ("unknown") rather than a fabricated zero — "unknown is never zero". The
    skeleton builds the wire ``TaskReceipt`` from exactly what you report here;
    it never invents a provider, model, token count, or cost on your behalf.
    Only the reference solver (`reference_solver.py`) legitimately reports all
    of these as zero/"reference", because it truly made no model call.
    """

    result_commit: str
    changed_paths: tuple[str, ...] = ()
    notes: str = ""
    failure_class: str | None = None
    provider: str | None = None
    model: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    reasoning_tokens: int | None = None
    upstream_cost_usd: float | None = None
    generation_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "generation_ids", tuple(self.generation_ids))


# solve(draft, workdir) -> SolveResult. ``draft`` is the claimed TaskDraft (brief,
# verify_command, allowed_paths, work_packet, ...); ``workdir`` is a fresh checkout
# of ``draft.base_commit``. This is the ONE pluggable step — your mining logic.
SolveFn = Callable[[TaskDraft, Path], SolveResult]


def _run_git(args: Sequence[str], *, cwd: Path) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def _build_receipt(lease_id: str, result: SolveResult) -> TaskReceipt:
    """Build an honest ``TaskReceipt`` from what ``solve`` actually reported.

    Mirrors the private runner's rule for turning partial usage into a receipt
    (``grokbuild_client.task_receipt_from_grok_json``): an unknown token count
    zero-fills on the wire — the DTO field is a plain ``int``, no null allowed
    (see ``TaskReceipt`` in ``protocol.py``) — but that flips
    ``metering_complete=False`` so nobody reads the zero as a real measurement.
    ``upstream_cost_usd`` is ``float | None`` on the wire, so an unknown cost is
    sent as ``None`` — never coerced to ``0.0``. Provider/model default to the
    literal ``"unknown"`` (see module constants) rather than an empty string,
    which the server's `model` regex would reject outright.
    """
    tokens = {
        "prompt_tokens": result.prompt_tokens,
        "completion_tokens": result.completion_tokens,
        "cache_read_input_tokens": result.cache_read_input_tokens,
        "cache_creation_input_tokens": result.cache_creation_input_tokens,
        "reasoning_tokens": result.reasoning_tokens,
    }
    metering_complete = (
        all(value is not None for value in tokens.values())
        and result.upstream_cost_usd is not None
        and bool(result.provider)
        and bool(result.model)
    )
    return TaskReceipt(
        lease_id=lease_id,
        generation_ids=result.generation_ids,
        actual_provider=result.provider or _UNKNOWN_PROVIDER,
        model=result.model or _UNKNOWN_MODEL,
        prompt_tokens=tokens["prompt_tokens"] or 0,
        completion_tokens=tokens["completion_tokens"] or 0,
        cache_read_input_tokens=tokens["cache_read_input_tokens"] or 0,
        cache_creation_input_tokens=tokens["cache_creation_input_tokens"] or 0,
        reasoning_tokens=tokens["reasoning_tokens"] or 0,
        upstream_cost_usd=result.upstream_cost_usd,
        finish_reason="stop" if metering_complete else None,
        metering_complete=metering_complete,
    )


def _path_covered_by_allowed(changed_rel: str, allowed_paths: Sequence[str]) -> bool:
    """True when ``changed_rel`` is "under" one of ``allowed_paths``.

    Mirrors the private policy's exact semantics
    (``grokbuild_client._policy_path_covers``): an entry ending in ``/`` is a
    directory prefix (covers any path starting with it); any other entry
    matches only by exact equality — ``src/foo`` never accidentally covers
    ``src/foobar``.
    """
    for entry in allowed_paths:
        if not entry:
            continue
        if entry.endswith("/"):
            if changed_rel.startswith(entry):
                return True
        elif changed_rel == entry:
            return True
    return False


def _split_verify_command(verify_command: str) -> tuple[list[str], dict[str, str]]:
    """Parse a verify command: strip leading ``NAME=value`` assignments.

    Mirrors the private runner's exact rule (``grokbuild_client._split_verify_command``):
    only *leading* assignment tokens are consumed into the environment; the
    remaining tokens are the executable argv, run directly — never through a
    shell. Raises :class:`VerifyCommandError` if no executable argv remains.
    """
    try:
        tokens = shlex.split(str(verify_command))
    except ValueError as exc:
        raise VerifyCommandError("verify_command is not a valid shell command") from exc
    env_overrides: dict[str, str] = {}
    while tokens and _ENV_ASSIGNMENT_RE.match(tokens[0]):
        token = tokens.pop(0)
        name, value = token.split("=", 1)
        env_overrides[name] = value
    if not tokens:
        raise VerifyCommandError(
            "verify_command contains only environment assignments; no executable remains"
        )
    return tokens, env_overrides


def _run_verify_command(verify_command: str, *, cwd: Path) -> int:
    """Run ``verify_command`` in ``cwd`` under a bounded, credential-free env.

    No shell, no ambient secrets: only ``PATH``, a scratch ``HOME`` (so nothing
    reads or writes the real one), and ``LANG`` cross into the child — no
    provider keys, no tokens, nothing else from this process's environment.
    Returns the exit code; a command that can't even be parsed or launched
    fails closed (1 / 127) rather than raising past the caller.
    """
    try:
        argv, env_overrides = _split_verify_command(verify_command)
    except VerifyCommandError:
        return 1
    with tempfile.TemporaryDirectory(prefix="ormas-verify-home-") as scratch_home:
        env: dict[str, str] = {}
        path = os.environ.get("PATH")
        if path:
            env["PATH"] = path
        env["HOME"] = scratch_home
        lang = os.environ.get("LANG")
        if lang:
            env["LANG"] = lang
        env.update(env_overrides)
        try:
            proc = subprocess.run(
                argv, cwd=str(cwd), env=env, capture_output=True, text=True,
            )
        except OSError:
            return 127
    return proc.returncode


@dataclass
class MinerConfig:
    """Static identity + defaults for one miner process.

    ``repo_url`` is what the skeleton clones from at bind time; ``push_remote``
    controls whether the result branch is actually pushed (``None`` records a
    ``local:`` ref instead — useful for a dry run or a test double).
    ``ask_usd`` is the miner's firm ask sent with every claim; the default
    ``None`` sends no ask — the byte-identical two-field claim body — and the
    server derives one. A bad value raises ``ValueError`` at the claim, locally.
    """

    runner_id: str
    runner_version: str
    platform: str
    capacity: int
    cells: tuple[str, ...]
    workdir_root: Path
    repo_id: str
    repo_url: str
    push_remote: str | None = "origin"
    device_nonce: str | None = None
    heartbeat_interval_s: float = float(DEFAULT_HEARTBEAT_S)
    ask_usd: float | None = None


class MinerSkeleton:
    """The claim → clone → solve → verify → publish → complete loop.

    Instantiate one per running miner process. ``solve_fn`` is the only thing a
    real miner needs to replace; everything else here is protocol plumbing.
    """

    def __init__(
        self,
        client: OrmasMinerClient,
        config: MinerConfig,
        solve_fn: SolveFn,
    ) -> None:
        self.client = client
        self.config = config
        self.solve_fn = solve_fn
        # Poll/lease cadences adopted from the gateway's registration response
        # (see register()); the module defaults stand until then.
        self.poll_interval_s = float(DEFAULT_POLL_INTERVAL_S)
        self.lease_ttl_s = float(DEFAULT_LEASE_TTL_S)

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def register(self, *, health_extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """POST a registration and adopt the gateway's response cadences.

        The registration response authoritatively carries ``poll_interval_s``,
        ``heartbeat_s`` and ``lease_ttl_s``: each field present replaces the
        local default — stored on ``self.poll_interval_s`` / ``self.lease_ttl_s``
        and on ``config.heartbeat_interval_s`` — so a gateway cadence change
        does not silently desynchronise every miner. Fields the response omits
        keep the module defaults. Returns the gateway's raw response.
        """
        health: dict[str, Any] = {"cells": list(self.config.cells)}
        if self.config.device_nonce:
            health["device_nonce"] = self.config.device_nonce
        if health_extra:
            health.update(health_extra)
        registration = RunnerRegistration(
            runner_id=self.config.runner_id,
            runner_version=self.config.runner_version,
            platform=self.config.platform,
            capacity=self.config.capacity,
            health=health,
        )
        response = self.client.register_runner(registration)
        if isinstance(response, Mapping):
            poll = response.get("poll_interval_s")
            if poll is not None:
                self.poll_interval_s = float(poll)
            heartbeat_s = response.get("heartbeat_s")
            if heartbeat_s is not None:
                self.config.heartbeat_interval_s = float(heartbeat_s)
            lease_ttl = response.get("lease_ttl_s")
            if lease_ttl is not None:
                self.lease_ttl_s = float(lease_ttl)
        return response

    def bind(self, *, project_id: str, base_commit: str) -> dict[str, Any]:
        """Bind this miner's repo_id to a client project at a base commit."""
        repository = RepoRegistration(
            repo_id=self.config.repo_id,
            display_alias=self.config.repo_id,
            base_commit=base_commit,
            preflight_state="ready",
        )
        return self.client.register_repository(
            self.config.runner_id, repository, project_id=project_id,
        )

    # ------------------------------------------------------------------
    # Git mechanics
    # ------------------------------------------------------------------

    def _fresh_workdir(self, task_id: str) -> Path:
        """Return a clean per-task workdir path, replacing any crashed-job leftover.

        A run killed mid-task leaves ``workdir_root/<task_id>`` behind, and the
        same lease is re-served on restart — the leftover must be removed,
        not treated as fatal, or every restart wedges on it.
        """
        workdir = self.config.workdir_root / task_id
        if workdir.exists():
            shutil.rmtree(workdir)
        workdir.parent.mkdir(parents=True, exist_ok=True)
        return workdir

    def _clone_and_checkout(self, draft: TaskDraft) -> Path:
        workdir = self._fresh_workdir(draft.task_id)
        _run_git(["clone", self.config.repo_url, str(workdir)], cwd=workdir.parent)
        if draft.base_commit:
            _run_git(["checkout", draft.base_commit], cwd=workdir)
        return workdir

    def _publish(self, task_id: str, workdir: Path, result: SolveResult) -> str:
        branch = f"ormas/job/{task_id}"
        _run_git(["branch", "-f", branch, result.result_commit], cwd=workdir)
        if self.config.push_remote:
            _run_git(
                ["push", self.config.push_remote, f"{branch}:refs/heads/{branch}"],
                cwd=workdir,
            )
            return f"{_RESULT_REF_HEADS}{task_id}"
        return f"{_RESULT_REF_LOCAL}{task_id}"

    def _material_diff_sha256(self, workdir: Path, base_commit: str, result_commit: str) -> str | None:
        if not base_commit or not result_commit:
            return None
        try:
            diff = _run_git(["diff", base_commit, result_commit], cwd=workdir)
        except GitError:
            return None
        return hashlib.sha256(diff.encode("utf-8")).hexdigest()

    def _compute_scope(
        self, workdir: Path, draft: TaskDraft, result: SolveResult,
    ) -> tuple[bool, tuple[str, ...]]:
        """Compute ``scope_ok``/``changed_paths`` for real, from git — not the solver's say-so.

        ``changed_paths`` = ``git diff --name-only <base_commit> <result_commit>``
        in the workdir: the ground truth of what actually landed. The solver's
        own ``SolveResult.changed_paths`` is used only as a cross-check; a
        mismatch is warned (never silently trusted over git — a solver could be
        wrong or lying about its own diff). ``scope_ok`` is vacuously ``True``
        when ``draft.allowed_paths`` is empty (no restriction declared),
        otherwise every changed path must be covered by an entry
        (`_path_covered_by_allowed`, mirroring the private policy's directory-
        prefix-vs-exact-match rule).
        """
        try:
            diff_out = _run_git(
                ["diff", "--name-only", draft.base_commit, result.result_commit], cwd=workdir,
            )
        except GitError:
            diff_out = ""
        changed_paths = tuple(p for p in diff_out.splitlines() if p)
        reported = set(result.changed_paths)
        actual = set(changed_paths)
        if reported != actual:
            warnings.warn(
                "solve()-reported changed_paths "
                f"{sorted(reported)} does not match git diff {sorted(actual)}; "
                "using git diff as ground truth for scope_ok",
                stacklevel=2,
            )
        allowed = tuple(draft.allowed_paths)
        if not allowed:
            scope_ok = True
        else:
            scope_ok = all(_path_covered_by_allowed(p, allowed) for p in changed_paths)
        return scope_ok, changed_paths

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    def _heartbeat_loop(
        self, task_id: str, lease: TaskLease, stop: threading.Event,
    ) -> None:
        interval = max(1.0, self.config.heartbeat_interval_s)
        while not stop.wait(interval):
            try:
                self.client.heartbeat_task(
                    task_id, self.config.runner_id, lease.lease_id, renew=True,
                )
            except Exception:  # noqa: BLE001 - a missed heartbeat is not fatal; lease may expire
                return

    # ------------------------------------------------------------------
    # The loop
    # ------------------------------------------------------------------

    def run_once(self) -> bool:
        """Claim one task if available, solve+verify it, and complete.

        ``False`` when idle. Once a lease is held, nothing that goes wrong
        afterwards — a raising ``solve_fn``, a git failure in clone/publish, a
        verify/publish crash — escapes: the failure is reported to the gateway
        as a failed terminal so the lease completes instead of silently
        expiring server-side, and the call still returns ``True`` (the job was
        claimed and handled). Only a claim failure, before any lease is held,
        propagates. ``KeyboardInterrupt`` is never swallowed.
        """
        claimed = self.client.claim_task(self.config.runner_id, ask_usd=self.config.ask_usd)
        if claimed is None:
            return False
        lease, draft = claimed
        start = time.monotonic()
        try:
            self._run_leased_task(lease, draft, start)
        except Exception as exc:  # noqa: BLE001 - contained + reported; KeyboardInterrupt propagates
            self._report_lease_failure(lease, draft, exc, start)
        return True

    def _run_leased_task(self, lease: TaskLease, draft: TaskDraft, start: float) -> None:
        """The claim → complete path for one held lease. Errors propagate to
        :meth:`run_once`, which reports them as a failed terminal."""
        workdir = self._clone_and_checkout(draft)
        stop = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat_loop, args=(draft.task_id, lease, stop), daemon=True,
        )
        heartbeat.start()
        try:
            result = self.solve_fn(draft, workdir)
        finally:
            stop.set()
            heartbeat.join(timeout=5.0)

        self.client.heartbeat_task(
            draft.task_id,
            self.config.runner_id,
            lease.lease_id,
            renew=False,
            event=TaskEvent(
                lease_id=lease.lease_id,
                state="verifying",
                occurred_at=lease.now,
                error_category=None,
            ),
        )

        verify_exit_code = _run_verify_command(draft.verify_command, cwd=workdir)
        wall_s = time.monotonic() - start

        result_ref = self._publish(draft.task_id, workdir, result)
        material_sha = self._material_diff_sha256(workdir, draft.base_commit, result.result_commit)
        scope_ok, changed_paths = self._compute_scope(workdir, draft, result)

        verification_state = "verified" if (verify_exit_code == 0 and scope_ok) else "failed"
        terminal = TaskTerminal(
            lease_id=lease.lease_id,
            verification_state=verification_state,
            result_ref=result_ref,
            settlement_state="unset",
            rating=None,
            result_commit=result.result_commit,
        )
        receipt = _build_receipt(lease.lease_id, result)
        capture: dict[str, Any] = {
            "status": verification_state,
            "scope_ok": scope_ok,
            "file_count": len(changed_paths),
            "changed_paths": [{"path": p} for p in changed_paths],
            "wall_s": wall_s,
        }
        # runner_api._project_attempts requires 0 <= verify_exit_code <= 255; a
        # signal-killed process (negative returncode) is omitted rather than
        # sent malformed.
        if 0 <= verify_exit_code <= 255:
            capture["attempts"] = [{"verify_exit_code": verify_exit_code}]
        if material_sha is not None:
            capture["material_diff_sha256"] = material_sha
        if result.failure_class is not None:
            capture["failure_class"] = result.failure_class

        self.client.complete_task(
            draft.task_id,
            self.config.runner_id,
            lease.lease_id,
            receipt=receipt,
            terminal=terminal,
            capture=capture,
        )

    def _report_lease_failure(
        self, lease: TaskLease, draft: TaskDraft, exc: Exception, start: float,
    ) -> None:
        """Report a crashed job on a held lease as a failed terminal.

        The heartbeat is already stopped by the time this runs (solve's
        ``finally``, or before it ever started when the clone failed). The
        terminal honestly says ``verification_state='failed'`` /
        ``settlement_state='unset'`` with ``result_commit`` pointing at the
        base commit — no delivery exists to point at — and the capture carries
        a ``failure_class`` plus the (truncated) exception message. The
        receipt reports unknown usage rather than fabricated zeros. Reporting
        itself is best-effort: if even the completion call cannot reach the
        gateway there is nothing more this process can do for the lease, so
        that secondary failure only warns.
        """
        failure_class = "git_error" if isinstance(exc, GitError) else "solve_error"
        terminal = TaskTerminal(
            lease_id=lease.lease_id,
            verification_state="failed",
            result_ref=None,
            settlement_state="unset",
            rating=None,
            result_commit=draft.base_commit,
        )
        receipt = _build_receipt(
            lease.lease_id,
            SolveResult(result_commit=draft.base_commit, failure_class=failure_class),
        )
        capture: dict[str, Any] = {
            "status": "failed",
            "scope_ok": False,
            "file_count": 0,
            "changed_paths": [],
            "wall_s": time.monotonic() - start,
            "failure_class": failure_class,
            "error": str(exc)[:_FAILURE_MESSAGE_MAX],
        }
        try:
            self.client.complete_task(
                draft.task_id,
                self.config.runner_id,
                lease.lease_id,
                receipt=receipt,
                terminal=terminal,
                capture=capture,
            )
        except Exception as report_exc:  # noqa: BLE001 - nothing more to do for this lease
            warnings.warn(
                f"could not report crashed task {draft.task_id!r} to the gateway "
                f"({report_exc}); the lease will expire server-side",
                stacklevel=2,
            )

    def run_forever(
        self,
        *,
        poll_interval_s: float | None = None,
        max_iterations: int | None = None,
        idle_sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Poll until stopped. ``max_iterations`` bounds it for tests/dry runs.

        The idle sleep between polls is the gateway-adopted
        ``self.poll_interval_s`` unless ``poll_interval_s`` is given as an
        explicit override.
        """
        interval = self.poll_interval_s if poll_interval_s is None else poll_interval_s
        iterations = 0
        while max_iterations is None or iterations < max_iterations:
            iterations += 1
            did_work = self.run_once()
            if not did_work:
                idle_sleep(interval)
