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
import json
import logging
import math
import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import warnings
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterator, Mapping, Sequence

from httpx import HTTPError

from .client import OrmasGatewayError, OrmasMinerClient, _positive_usd, _validate_window_offer
from ._recovery import PublicRecovery, RecoveryRequired
from .verification_context import verification_call
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

# `TaskReceipt.model` is optional. Omit it when the solver did not name one.
# Do not send a synthetic stand-in such as "unknown": a third-party receipt
# is valid without a model. ``actual_provider`` is still a plain string, so an
# unnamed provider stays the literal "unknown", paired with
# `metering_complete=False` unless the solver reported a real provider.
_UNKNOWN_PROVIDER = "unknown"

# POSIX-style NAME=value at the start of a verify command. Mirrors the private
# runner's exact rule (grokbuild_client._ENV_ASSIGNMENT_RE / _split_verify_command):
# leading assignment tokens are stripped into the environment; the remaining
# argv is executed directly, never through a shell.
_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

_LOGGER = logging.getLogger(__name__)


class GitError(RuntimeError):
    """A git subprocess used by the skeleton failed."""


def _app_read_expired(credential: Any) -> bool:
    """True only for a GitHub App read token whose own ``expires_at`` has passed."""
    if not isinstance(credential, Mapping) or credential.get('kind') != 'github_app_read_token':
        return False
    try:
        expiry = datetime.fromisoformat(credential['expires_at'])
    except (KeyError, TypeError, ValueError):
        return False  # malformed: the read-time validator refuses it
    return expiry.tzinfo is not None and expiry <= datetime.now(timezone.utc)


def _refreshed_app_read(fetch: Callable[[], Any]) -> dict[str, Any]:
    """The App read credential from an authenticated refresh, or ``GitError``.

    The response is validated again at read time. No fallback credential.
    """
    try:
        response = fetch()
    except Exception as exc:  # noqa: BLE001 — any refusal fails this read closed
        logging.getLogger(__name__).warning(
            "repository credential refresh failed: %s", type(exc).__name__)
        raise GitError('private_repository_read_access_unavailable') from None
    credential = response.get('repo_credential') if isinstance(response, Mapping) else None
    if not isinstance(credential, Mapping) or credential.get('kind') != 'github_app_read_token':
        raise GitError('private_repository_read_access_unavailable')
    return dict(credential)


class VerifyCommandError(RuntimeError):
    """``verify_command`` could not be parsed into a runnable argv."""


@dataclass(frozen=True)
class SolveResult:
    """What a ``solve`` implementation hands back to the skeleton.

    ``result_commit`` must already exist in the workdir (solve commits its own
    edits). For public jobs, the gateway publishes that candidate tree as one
    commit on the frozen base, and the skeleton fetches and verifies it. Legacy
    jobs publish the solver's commit to ``ormas/job/<task_id>`` directly.

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


def _run_git(args: Sequence[str], *, cwd: Path, env: Mapping[str, str] | None = None) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False,
        env=dict(env) if env is not None else None,
    )
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def _public_git(args, *, cwd):
    from .outcomes_support import public_git
    return public_git(args, cwd=cwd)


def _public_packet(draft):
    from .outcomes_support import has_public_execution, validate_public_execution_packet
    raw = getattr(draft, 'work_packet', None)
    packet = dict(raw) if isinstance(raw, Mapping) else {}
    if not has_public_execution(packet):
        return None
    validate_public_execution_packet(packet)
    if draft.base_commit != packet['execution_policy']['repo_base_sha'] or draft.verify_command != packet['verification_command']:
        raise GitError('public draft contract mismatch')
    return packet


def _build_public_artifact(draft, workdir, result, target):
    from .outcomes_support import build_public_artifact
    packet = _public_packet(draft)
    if packet is None:
        raise GitError('public artifact contract required')
    return build_public_artifact(packet, workdir, result.result_commit, target)


@contextmanager
def _credential_git_env(credential: Mapping[str, Any] | None) -> Iterator[dict[str, str] | None]:
    """Yield a GIT_SSH_COMMAND env for a job-scoped ssh_deploy_key, else None.

    The private key is written to a 0600 file that exists only for the
    duration of the ``with`` block. Never logs the key.
    """
    if not isinstance(credential, Mapping):
        yield None
        return
    if credential.get("kind") != "ssh_deploy_key":
        yield None
        return
    private_key = credential.get("private_key")
    if not isinstance(private_key, str) or not private_key:
        yield None
        return
    key_dir = tempfile.mkdtemp(prefix="ormas-job-key-")
    path = os.path.join(key_dir, "id_job")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            payload = private_key if private_key.endswith("\n") else private_key + "\n"
            os.write(fd, payload.encode())
        finally:
            os.close(fd)
        os.chmod(path, 0o600)
        yield {
            **os.environ,
            "GIT_SSH_COMMAND": (
                f"ssh -i {path} -o IdentitiesOnly=yes "
                f"-o StrictHostKeyChecking=accept-new -o BatchMode=yes"
            ),
        }
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
        try:
            os.rmdir(key_dir)
        except OSError:
            pass


def _build_receipt(lease_id: str, result: SolveResult) -> TaskReceipt:
    """Build an honest ``TaskReceipt`` from what ``solve`` actually reported.

    Mirrors the private runner's rule for turning partial usage into a receipt
    (``grokbuild_client.task_receipt_from_grok_json``): an unknown token count
    zero-fills on the wire — the DTO field is a plain ``int``, no null allowed
    (see ``TaskReceipt`` in ``protocol.py``) — but that flips
    ``metering_complete=False`` so nobody reads the zero as a real measurement.
    ``upstream_cost_usd`` is ``float | None`` on the wire, so an unknown cost is
    sent as ``None`` — never coerced to ``0.0``. An unnamed provider is the
    literal ``"unknown"``. An unnamed model is omitted, not a synthetic value.
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
    )
    named_model = result.model if isinstance(result.model, str) and result.model else None
    return TaskReceipt(
        lease_id=lease_id,
        generation_ids=result.generation_ids,
        actual_provider=result.provider or _UNKNOWN_PROVIDER,
        model=named_model,
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


def _run_verify_command(
    verify_command: str, *, cwd: Path, path_prefix: str | None = None,
    evidence_identity=None, raise_setup_errors: bool = False,
) -> int:
    """Run ``verify_command`` in ``cwd`` under a bounded, credential-free env.

    No shell, no ambient secrets: only ``PATH``, a scratch ``HOME`` (so nothing
    reads or writes the real one), and ``LANG`` cross into the child — no
    provider keys or tokens. The nonsecret ORMAS_VERIFIER_SCRATCH_DIR also crosses
    so the external driver's Docker bind mounts resolve on a sibling-container host.
    Returns the exit code; a command that can't even be parsed or launched
    fails closed (1 / 127) rather than raising past the caller.
    When ``path_prefix`` is set, it is prepended to the child's ``PATH`` and
    ``VIRTUAL_ENV`` is set to that prefix's parent directory.
    Validators set ``raise_setup_errors`` to distinguish a command that never
    launched from a real verifier that returned the same numeric exit code.
    """
    try:
        argv, env_overrides = _split_verify_command(verify_command)
    except VerifyCommandError as exc:
        if raise_setup_errors:
            raise ValueError("invalid_verifier_command") from exc
        return 1
    with tempfile.TemporaryDirectory(prefix="ormas-verify-home-") as scratch_home:
        env: dict[str, str] = {}
        if path_prefix is not None:
            env["PATH"] = path_prefix + os.pathsep + os.environ.get("PATH", "")
            env["VIRTUAL_ENV"] = os.path.dirname(path_prefix)
        else:
            path = os.environ.get("PATH")
            if path:
                env["PATH"] = path
        env["HOME"] = scratch_home
        lang = os.environ.get("LANG")
        if lang:
            env["LANG"] = lang
        scratch_root = os.environ.get("ORMAS_VERIFIER_SCRATCH_DIR")
        if scratch_root:
            # The verifier runtime's scratch root is tempfile.gettempdir(), steered by TMPDIR.
            env["ORMAS_VERIFIER_SCRATCH_DIR"] = scratch_root
            env["TMPDIR"] = scratch_root
        env.update(env_overrides)
        try:
            with verification_call(verify_command, env, cwd=cwd,
                                   identity=evidence_identity) as bound_env:
                proc = subprocess.run(
                    argv, cwd=str(cwd), env=bound_env, capture_output=True, text=True,
                )
        except ValueError:
            if raise_setup_errors:
                raise
            return 74  # Missing/ambiguous retained evidence is not a verifier pass.
        except OSError:
            if raise_setup_errors:
                raise
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

    ``runner_id`` may be ``""`` on the first run — the gateway assigns it
    (``runr_<12hex>``) at registration, and ``MinerSkeleton.register`` adopts
    the assigned id into this field. Afterwards it is the assigned/known id:
    pass the one the gateway issued; a non-empty id it has not issued to this
    token is refused 404.

    ``miner_id`` is the chosen public miner identity; lowercase 3–40
    ``[a-z0-9-]``; globally unique; optional.

    ``offer_fn`` receives each queue entry ``{job_id, created_at, envelope}``
    and returns a wire offer, or ``None`` to decline: firm
    ``{job_id, kind: "firm", price_usd}``, or limit
    ``{job_id, kind: "limit", estimate_usd, limit_usd}`` with
    ``0 < estimate_usd <= limit_usd``. The estimate is the expected charge
    (expected cost of the usual chain plus margin); the limit is the hard
    ceiling (worst-case chain). An operator with a single number sends it as
    both. When set, it replaces ``ask_usd``. Only a queue HTTP 404 with
    no ``error.type`` (an older gateway without the route) falls back to the
    legacy ``ask_usd`` claim, and that is remembered for the process; a 404
    carrying ``not_found_error`` (unknown runner, or no priced binding) and
    every other queue error are raised.

    ``offer_window`` explicitly opts in to the gateway's offer-window routes.
    It defaults to False. In window mode, ``offer_fn`` supplies terms or a
    positive ``ask_usd`` supplies static firm pricing. Own bid identities and
    immutable terms persist in the existing gateway/runner recovery namespace;
    an unknown award or ambiguous POST refuses execution.

    ``settle_fn(lease, result)`` sets the price for a verified limit delivery,
    capped at the lease's limit. Without it, the price is the limit. It must be
    deterministic and must not raise, and must return a finite non-negative
    number. An invalid return or a raise is never repriced. A task without a
    public packet completes as ``failed``. A public-packet task has already
    published its artifact, so it is retained for recovery with no completion
    sent, and every later ``run_once`` re-raises from recovery (claiming
    nothing) until the hook returns a valid price.
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
    miner_id: str | None = None
    offer_fn: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None
    settle_fn: Callable[[TaskLease, SolveResult], float] | None = None
    offer_window: bool = False


class MinerSkeleton:
    """The claim → clone → solve → verify → publish → complete loop.

    Instantiate one per running miner process. ``solve_fn`` is the only thing a
    real miner needs to replace; everything else here is protocol plumbing.
    """

    # Live lease id for the initial clone only; refreshes an expired App read.
    _clone_lease_id: str | None = None

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
        # Set (from the heartbeat thread or a completion attempt) the moment the
        # gateway tells us our lease is gone (409 lease_lost). A long-running
        # ``solve_fn`` is expected to poll this cooperatively — the skeleton
        # cannot interrupt arbitrary user code, only ask it to stop. Cleared at
        # the start of every claimed run.
        self.cancelled = threading.Event()
        # Summary of the most recently claimed run (see ``_run_leased_task``).
        # ``None`` until a lease has been claimed and handled at least once, so
        # an idle ``run_once()`` never reports a stale prior run's summary.
        self.last_run: dict[str, Any] | None = None
        self._public_recovery: PublicRecovery | None = None
        # Set once the queue answers a bare 404 (an older gateway without the
        # route) so later polls claim with ``ask_usd`` without re-probing.
        self._queue_unsupported = False
        # Only the exact claim-step exception is retryable by the daemon.
        self._claim_error: BaseException | None = None

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def register(self, *, health_extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """POST a registration and adopt the gateway's response cadences.

        When ``config.runner_id`` is empty the gateway assigns one
        (``runr_<12hex>``) and returns it; whatever non-empty ``runner_id``
        the response carries is adopted into ``config.runner_id`` so every
        later claim/heartbeat/complete uses the assigned id.
        The registration response authoritatively carries ``poll_interval_s``,
        ``heartbeat_s`` and ``lease_ttl_s``: each field present replaces the
        local default — stored on ``self.poll_interval_s`` / ``self.lease_ttl_s``
        and on ``config.heartbeat_interval_s`` — so a gateway cadence change
        does not silently desynchronise every miner. Fields the response omits
        keep the module defaults. Returns the gateway's raw response.
        """
        cells = list(self.config.cells)
        if any(c.startswith('task:lang/') for c in cells):
            cells.append('task:publication/github-artifact-v1')
            cells.append('task:acceptance/independent-v1')
            cells.append('task:acceptance/independent-v3')
            # Card 70cd3006: a public-execution miner already runs the real
            # (Docker) base check for every packet it claims regardless of
            # whether the client proved it locally (v2) or deferred it
            # (v3) — no new infrastructure, so it advertises this cell too.
            cells.append('task:preflight/deferred-v1')
        health: dict[str, Any] = {"cells": list(dict.fromkeys(cells))}
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
            miner_id=self.config.miner_id,
        )
        response = self.client.register_runner(registration)
        if isinstance(response, Mapping):
            # The gateway ASSIGNS the runner id on a first registration (empty
            # ``runner_id`` in, ``runr_<12hex>`` out) and echoes a known one;
            # adopt whatever it returns so every later claim/heartbeat/complete
            # carries the assigned id, not the locally configured one.
            assigned = response.get("runner_id")
            if isinstance(assigned, str) and assigned:
                self.config.runner_id = assigned
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

    def _read_credential(self, draft: TaskDraft, lease_id: str | None) -> Any:
        """The credential for one private read under this still-live lease.

        An expired App token is replaced through the authenticated lease; the
        replacement stays local to this read, never in the draft or receipt.
        """
        credential = draft.repo_credential
        if lease_id is None or not _app_read_expired(credential):
            return credential
        return _refreshed_app_read(lambda: self.client.read_repository_credential(
            draft.task_id, self.config.runner_id, lease_id))

    def _clone_and_checkout(self, draft: TaskDraft) -> Path:
        workdir = self._fresh_workdir(draft.task_id)
        packet = _public_packet(draft)
        if packet is not None:
            from .outcomes_support import validate_repository_credential, repository_git
            visibility = packet['execution_requirements']['repository_visibility']
            try:
                credential = (self._read_credential(draft, self._clone_lease_id)
                              if visibility == 'private' else draft.repo_credential)
                validate_repository_credential(visibility, draft.repo_url, credential)
                if visibility == 'private':
                    repository_git(['clone', '--no-checkout', draft.repo_url, str(workdir)],
                        cwd=workdir.parent, repo_url=draft.repo_url, credential=credential)
                else:
                    _public_git(['clone', '--no-checkout', draft.repo_url, str(workdir)], cwd=workdir.parent)
            except ValueError as exc:
                shutil.rmtree(workdir, ignore_errors=True)  # a killed clone leaves a partial checkout
                raise GitError(str(exc)) from None
            except BaseException:
                shutil.rmtree(workdir, ignore_errors=True)
                raise
            _public_git(['checkout', '--detach', draft.base_commit], cwd=workdir)
            return workdir
        source = draft.repo_url if (getattr(draft, "repo_credential", None) and draft.repo_url) else self.config.repo_url
        with _credential_git_env(getattr(draft, "repo_credential", None)) as env:
            _run_git(["clone", source, str(workdir)], cwd=workdir.parent, env=env)
        if draft.base_commit:
            _run_git(["checkout", draft.base_commit], cwd=workdir)
        return workdir

    def _publish(self, task_id: str, workdir: Path, result: SolveResult, draft: TaskDraft | None = None) -> str:
        branch = f"ormas/job/{task_id}"
        _run_git(["branch", "-f", branch, result.result_commit], cwd=workdir)
        if draft is not None and getattr(draft, "repo_credential", None) and draft.repo_url:
            with _credential_git_env(draft.repo_credential) as env:
                _run_git(
                    ["push", draft.repo_url, f"{branch}:refs/heads/{branch}"],
                    cwd=workdir,
                    env=env,
                )
            return f"{_RESULT_REF_HEADS}{task_id}"
        if self.config.push_remote:
            _run_git(
                ["push", self.config.push_remote, f"{branch}:refs/heads/{branch}"],
                cwd=workdir,
            )
            return f"{_RESULT_REF_HEADS}{task_id}"
        return f"{_RESULT_REF_LOCAL}{task_id}"

    def _publish_public(self, lease, draft, workdir, result, *, capture=None):
        store = self._public_recovery
        with ExitStack() as stack:
            if store is not None:
                pending = store.pending()
                if pending is not None and pending['stage'] == 'claimed':
                    if capture is None:
                        raise RecoveryRequired('public artifact requires frozen capture')
                    pending = store.checkpoint_artifact(
                        lambda source: _build_public_artifact(draft, workdir, result, source),
                        result=asdict(result), capture=capture,
                    )
                if pending is None or pending['stage'] != 'artifact':
                    raise RecoveryRequired('public artifact checkpoint is missing')
                source = stack.enter_context(store.artifact_source(pending))
                length, digest, tree_sha = (pending['artifact_length'],
                                           pending['artifact_sha256'], pending['tree_sha'])
            else:
                source = stack.enter_context(tempfile.TemporaryFile())
                length, digest, tree_sha = _build_public_artifact(draft, workdir, result, source)
            published = self.client.publish_result(draft.task_id, self.config.runner_id,
                lease.lease_id, source=source, length=length)
        commit = published.get('result_commit')
        ref = f'refs/heads/ormas/job/{draft.task_id}'
        if (published.get('result_ref') != ref or published.get('artifact_sha256') != digest
                or published.get('tree_sha') != tree_sha or not isinstance(commit, str)
                or not re.fullmatch('[0-9a-f]{40}', commit)):
            raise GitError('public publication identity mismatch')
        if draft.work_packet['execution_requirements']['repository_visibility'] == 'private':
            from .outcomes_support import repository_git
            credential = self._read_credential(draft, lease.lease_id)
            try:
                repository_git(['fetch', '--no-tags', draft.repo_url, commit], cwd=workdir,
                    repo_url=draft.repo_url, credential=credential)
            except ValueError as exc:
                raise GitError(str(exc)) from None
        else:
            _public_git(['fetch', '--no-tags', draft.repo_url, commit], cwd=workdir)
        fetched_tree = _public_git(['rev-parse', commit + '^{tree}'], cwd=workdir).decode().strip()
        if fetched_tree != tree_sha:
            raise GitError('public publication tree mismatch')
        parents = _public_git(['rev-list', '--parents', '-n', '1', commit], cwd=workdir).decode().split()
        if parents != [commit, draft.base_commit]:
            raise GitError('public publication parent mismatch')
        return ref, replace(result, result_commit=commit)

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
        self, task_id: str, lease: TaskLease, stop: threading.Event, state: dict[str, int],
    ) -> None:
        """Renew the lease on a fixed cadence until told to stop or the lease is lost.

        Runs for as long as the lease is held by the caller — the caller (see
        :meth:`_run_leased_task`) only signals ``stop`` once the completion has
        been resolved, not merely once ``solve_fn`` returns; that is the fix for
        job_c978f3fe82be, whose lease had zero renewals because the heartbeat
        thread was already dead during exactly the publish/verify/complete
        window it died in.

        A transient renewal failure is logged at WARNING (with the gateway's
        HTTP status and message when it is an :class:`OrmasGatewayError`) and
        the loop CONTINUES — the previous behaviour (``except Exception:
        return``) is why that renewal count was zero even on a working
        network: the thread died silently on the very first failure. A missed
        renewal also erodes the live lease, so the FIRST failure is retried
        immediately, recovering the tick it just cost rather than waiting out a
        full new cadence on top of it. A second consecutive failure falls back
        to the gateway's cadence: a refused connection fails in milliseconds, so
        retrying every failure immediately would spin this thread against a dead
        gateway. The normal cadence resumes as soon as a renewal succeeds. Only a confirmed ``lease_lost`` (409) stops
        the loop outright: the lease is provably gone, so nothing is left to
        renew, and ``self.cancelled`` is set so a cooperative ``solve_fn`` can
        notice and stop early. ``state["renewals"]`` counts successful
        renewals for the run summary. ``KeyboardInterrupt`` is a
        ``BaseException``, not caught here, and propagates.
        """
        interval = max(1.0, self.config.heartbeat_interval_s)
        wait_s = interval
        # One immediate retry recovers the tick a failure just cost the lease.
        # A SECOND consecutive failure falls back to the gateway's cadence: with
        # no floor at all, a refused connection (which fails in milliseconds)
        # would turn this thread into a spin loop against a dead gateway.
        consecutive_failures = 0
        while not stop.wait(wait_s):
            try:
                self.client.heartbeat_task(
                    task_id, self.config.runner_id, lease.lease_id, renew=True,
                )
            except OrmasGatewayError as exc:
                if exc.status_code == 409 and exc.error_type == "lease_lost":
                    _LOGGER.warning(
                        "lease lost for task %s: gateway %s [%s]: %s",
                        task_id, exc.status_code, exc.error_type, exc.message,
                    )
                    self.cancelled.set()
                    return
                _LOGGER.warning(
                    "heartbeat renewal failed for task %s: gateway %s [%s]: %s",
                    task_id, exc.status_code, exc.error_type, exc.message,
                )
                consecutive_failures += 1
                wait_s = 0.0 if consecutive_failures == 1 else interval
                continue
            except Exception as exc:  # noqa: BLE001 - transport error; keep renewing
                _LOGGER.warning("heartbeat renewal failed for task %s: %s", task_id, exc)
                consecutive_failures += 1
                wait_s = 0.0 if consecutive_failures == 1 else interval
                continue
            state["renewals"] = state.get("renewals", 0) + 1
            consecutive_failures = 0
            wait_s = interval

    # ------------------------------------------------------------------
    # The loop
    # ------------------------------------------------------------------

    def run_once(self) -> bool:
        """Claim one task if available, solve+verify it, and complete.

        ``False`` when idle. For an ordinary task (no public packet), once a
        lease is held nothing that goes wrong afterwards — a raising
        ``solve_fn`` or ``settle_fn``, a git failure in clone/publish, a
        verify/publish crash — escapes: the failure is reported to the gateway
        as a failed terminal so the lease completes instead of silently
        expiring server-side, and the call returns ``True``.

        A public (bounded-packet) task is journalled. A failure after its
        artifact is saved — including a raising ``settle_fn`` — sends no
        completion: the artifact is retained for exact recovery and the call
        returns ``False``. Every later call resolves that retained work before
        claiming and raises (claiming nothing) until it can complete, for
        example once the hook returns a valid price. ``RecoveryRequired``,
        claim failures and ``KeyboardInterrupt`` propagate.
        """
        self._claim_error = None
        namespace = getattr(self.client, 'base_url', None)
        if namespace and self.config.runner_id:
            store = PublicRecovery(self.config.workdir_root, namespace, self.config.runner_id)
            with store.lock():
                self._public_recovery = store
                try:
                    return self._run_once_locked()
                finally:
                    self._public_recovery = None
        return self._run_once_locked()

    def _claim_task(self) -> tuple[TaskLease, TaskDraft] | None:
        if self.config.offer_window:
            return self._claim_offer_window()
        offer_fn = self.config.offer_fn
        if offer_fn is not None and not self._queue_unsupported:
            try:
                queue = self.client.list_queue(self.config.runner_id)
            except OrmasGatewayError as exc:
                if exc.status_code != 404 or exc.error_type is not None:
                    raise
                self._queue_unsupported = True
            else:
                offers = []
                for entry in queue.get("jobs", []):
                    offer = offer_fn(entry)
                    if offer is not None:
                        offers.append(offer)
                return self.client.claim_task(self.config.runner_id, offers=offers)
        return self.client.claim_task(self.config.runner_id, ask_usd=self.config.ask_usd)

    def _offer_state(self) -> dict[str, Any]:
        store = self._public_recovery
        if store is None:
            raise RecoveryRequired("offer recovery requires gateway and runner identity")
        path = store.root / "offers.json"
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return {"gateway": store.namespace, "runner_id": store.runner_id,
                    "bids": {}, "current": {}, "pending": None}
        try:
            with os.fdopen(fd, "r", encoding="utf-8") as source:
                metadata = os.fstat(source.fileno())
                if (not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077
                        or metadata.st_uid != os.getuid()):
                    raise RecoveryRequired("offer recovery record is unsafe")
                state = json.load(source)
            if (state["gateway"] != store.namespace or state["runner_id"] != store.runner_id
                    or not isinstance(state["bids"], dict) or not isinstance(state["current"], dict)):
                raise RecoveryRequired("offer recovery identity mismatch")
            for bid_id, bid in state["bids"].items():
                if not isinstance(bid_id, str) or not bid_id:
                    raise ValueError("invalid bid identity")
                _validate_window_offer(bid["terms"])
            for job_id, bid_id in state["current"].items():
                if state["bids"][bid_id]["terms"]["job_id"] != job_id:
                    raise ValueError("invalid bid job identity")
            if state["pending"] is not None:
                raise RecoveryRequired("offer POST outcome unknown; recover own bid identity before claiming")
            return state
        except (ValueError, KeyError, TypeError):
            raise RecoveryRequired("offer recovery record is corrupt") from None

    def _save_offers(self, state: dict[str, Any]) -> None:
        root = self._public_recovery.root
        raw = json.dumps(state, allow_nan=False, separators=(",", ":")).encode()
        fd, name = tempfile.mkstemp(prefix="offers-", dir=root)
        try:
            with os.fdopen(fd, "wb") as target:
                target.write(raw)
                target.flush()
                os.fsync(target.fileno())
            os.replace(name, root / "offers.json")
            fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    @staticmethod
    def _offer_conflict(exc: OrmasGatewayError, message: str) -> bool:
        return (exc.status_code == 409
                and (exc.error_type == message
                     or (exc.error_type == "conflict_error" and exc.message == message)))

    def _read_bid(self, state: dict[str, Any], bid_id: str) -> dict[str, Any]:
        bid = state["bids"].get(bid_id)
        if bid is None:
            raise RecoveryRequired("unknown own award bid")
        if bid.get("legacy"):
            return bid
        response = self.client.get_offer(bid_id)
        status, award = response.get("status"), response.get("award_id")
        if (response.get("bid_id") != bid_id or status not in
                {"open", "awarded", "not_awarded", "withdrawn", "replaced", "no_capacity",
                 "award_lapsed"}
                or (status == "awarded" and (not isinstance(award, str) or not award))
                or (bid["status"] == "awarded" and status != "award_lapsed" and (
                    status != "awarded" or bid.get("award_id") != award))):
            raise RecoveryRequired("invalid own bid status or award identity")
        bid["status"] = status
        bid["award_id"] = award
        self._save_offers(state)
        return bid

    def _check_offer_lease(self, state, claimed, legacy_offers=()):
        if claimed is None:
            return None
        lease, draft = claimed
        bid = state["bids"].get(lease.bid_id)
        legacy = False
        if bid is None:
            terms = next((offer for offer in legacy_offers if offer["job_id"] == lease.task_id), None)
            if terms is None:
                raise RecoveryRequired("lease has unknown own award bid")
            bid = {"terms": terms, "status": "awarded", "legacy": True}
            legacy = True
        else:
            bid = self._read_bid(state, lease.bid_id)
        terms = bid["terms"]
        if (bid["status"] != "awarded" or lease.task_id != terms["job_id"]
                or draft.task_id != terms["job_id"] or draft.runner_id != self.config.runner_id
                or lease.offer_kind != terms["kind"]
                or lease.outcome_price_usd != float(terms.get("price_usd", terms.get("limit_usd")))
                or lease.estimate_usd != (float(terms["estimate_usd"]) if terms["kind"] == "limit" else None)
                or lease.limit_usd != (float(terms["limit_usd"]) if terms["kind"] == "limit" else None)):
            raise RecoveryRequired("lease award bid identity or frozen terms mismatch")
        if legacy:
            if not isinstance(lease.bid_id, str) or not lease.bid_id:
                raise RecoveryRequired("legacy offer lease has no bid identity")
            state["bids"][lease.bid_id] = bid
            state["current"][lease.task_id] = lease.bid_id
            self._save_offers(state)
        return claimed

    def _claim_offer_window(self) -> tuple[TaskLease, TaskDraft] | None:
        state = self._offer_state()
        if self._queue_unsupported:
            if state["bids"]:
                raise RecoveryRequired("own offer bids require gateway queue support")
            return self.client.claim_task(self.config.runner_id, ask_usd=self.config.ask_usd)
        try:
            queue = self.client.list_queue(self.config.runner_id)
        except OrmasGatewayError as exc:
            if exc.status_code != 404 or exc.error_type is not None:
                raise
            if state["bids"]:
                raise RecoveryRequired("own offer bids require gateway queue support") from exc
            self._queue_unsupported = True
            return self.client.claim_task(self.config.runner_id, ask_usd=self.config.ask_usd)
        missing_pricing = self.config.offer_fn is None and not _positive_usd(self.config.ask_usd)
        legacy_offers = []
        for entry in queue.get("jobs", []):
            job_id = entry["job_id"]
            bid_id = state["current"].get(job_id)
            bid = state["bids"].get(bid_id)
            if entry.get("awarded_to_you"):
                awarded = self._read_bid(state, entry.get("bid_id"))
                if (awarded["terms"]["job_id"] != job_id or awarded["status"] != "awarded"
                        or awarded.get("award_id") != entry.get("award_id")):
                    raise RecoveryRequired("queue award bid identity mismatch")
                continue
            if bid is not None and not bid.get("legacy"):
                bid = self._read_bid(state, bid_id)
                if bid["status"] == "awarded":
                    continue
            if self.config.offer_fn is not None:
                offer = self.config.offer_fn(entry)
            else:
                if missing_pricing:
                    continue
                offer = {"job_id": job_id, "kind": "firm", "price_usd": self.config.ask_usd}
            if offer is None:
                if bid is not None and not bid.get("legacy") and bid["status"] == "open":
                    try:
                        response = self.client.withdraw_offer(bid_id)
                    except OrmasGatewayError as exc:
                        if not self._offer_conflict(exc, "not_open"):
                            raise
                        self._read_bid(state, bid_id)
                    else:
                        if response != {"bid_id": bid_id, "status": "withdrawn"}:
                            raise RecoveryRequired("invalid own bid withdrawal")
                        bid["status"] = "withdrawn"
                        self._save_offers(state)
                continue
            _validate_window_offer(offer)
            if offer["job_id"] != job_id:
                raise ValueError("offer job_id must match its queue entry")
            offer = dict(offer)
            if (bid is not None and not bid.get("legacy") and bid["terms"] == offer
                    and bid["status"] == "open"):
                continue
            # Save intent first: a lost POST response cannot be recovered by the
            # own-status API, which requires the returned bid id.
            state["pending"] = offer
            self._save_offers(state)
            try:
                response = self.client.submit_offer(self.config.runner_id, offer)
            except OrmasGatewayError as exc:
                if exc.status_code is None or exc.status_code >= 500:
                    raise  # The gateway may have recorded the POST.
                state["pending"] = None
                self._save_offers(state)
                if self._offer_conflict(exc, "not_offering") and (bid is None or bid.get("legacy")):
                    legacy_offers.append(offer)
                    continue
                if self._offer_conflict(exc, "window_closed"):
                    if bid is not None:
                        self._read_bid(state, bid_id)
                    continue
                raise
            new_id, closes = response.get("bid_id"), response.get("window_closes_at")
            if (not isinstance(new_id, str) or not new_id or new_id in state["bids"]
                    or not isinstance(closes, str) or not closes):
                raise RecoveryRequired("offer response has no new bid identity or window_closes_at")
            state["pending"] = None
            state["bids"][new_id] = {"terms": offer, "status": "open", "window_closes_at": closes}
            state["current"][job_id] = new_id
            self._save_offers(state)
        if missing_pricing and not state["bids"]:
            raise ValueError("offer-window pricing requires a positive ask_usd or offer_fn")
        # An own award can be absent from the queue page; recover its frozen
        # terms through an empty-offer claim before requiring fresh pricing.
        claimed = self.client.claim_task(self.config.runner_id, offers=[])
        if claimed is not None:
            return self._check_offer_lease(state, claimed)
        if missing_pricing:
            raise ValueError("offer-window pricing requires a positive ask_usd or offer_fn")
        if legacy_offers:
            claimed = self.client.claim_task(self.config.runner_id, offers=legacy_offers)
            return self._check_offer_lease(state, claimed, legacy_offers)
        return None

    def _run_once_locked(self) -> bool:
        store = self._public_recovery
        if store is not None and (pending := store.pending()) is not None:
            return self._recover_public(pending)
        try:
            claimed = self._claim_task()
        except (OrmasGatewayError, HTTPError, OSError) as exc:
            self._claim_error = exc
            raise
        if claimed is None:
            return False
        lease, draft = claimed
        start = time.monotonic()
        try:
            if _public_packet(draft) is not None:
                if store is None:
                    raise RecoveryRequired('public recovery requires gateway identity')
                store.begin(lease, draft)
                if (self.config.workdir_root / draft.task_id).exists():
                    raise RecoveryRequired('public checkout has no matching recovery record')
            self._run_leased_task(lease, draft, start)
        except RecoveryRequired:
            raise
        except Exception as exc:  # noqa: BLE001 - contained + reported; KeyboardInterrupt propagates
            if store is not None and (pending := store.pending()) is not None:
                if pending['stage'] in {'artifact', 'completion'}:
                    # A publication or completion may already have reached the
                    # gateway. Never replace it with a new failure completion.
                    _LOGGER.warning('public task %s retained for exact recovery', draft.task_id)
                    return False
            self._report_lease_failure(lease, draft, exc, start)
        return True

    def _recover_public(self, pending) -> bool:
        """Resolve retained public work before any claim or solver invocation."""
        self.cancelled.clear()
        lease = (TaskLease.from_wire(pending['lease']) if 'lease' in pending else
                 SimpleNamespace(lease_id=pending['lease_id'], task_id=pending['task_id']))
        draft = SimpleNamespace(task_id=pending['task_id'], base_commit=pending['base_commit'],
                                repo_url=pending['repo_url'])
        start = time.monotonic()
        last_run = {'task_id': draft.task_id, 'renewals': 0, 'lease_lost': False,
                    'completion_attempts': 0, 'completion_error': None,
                    'settlement': None, 'recovered': True}
        self.last_run = last_run
        if pending['stage'] == 'claimed':
            # The process died before a bounded artifact was durably captured.
            # Its provider outcome is unknown, so do not call the solver again.
            self._report_lease_failure(lease, draft, RecoveryRequired('public execution interrupted'), start)
            return self._public_recovery.pending() is None
        if pending['stage'] == 'artifact':
            workdir = self.config.workdir_root / draft.task_id
            if workdir.is_symlink() or not workdir.is_dir():
                raise RecoveryRequired('public recovery checkout is missing or unsafe')
            try:
                result_ref, result = self._publish_public(
                    lease, draft, workdir, SolveResult(**pending['result']))
            except OrmasGatewayError as exc:
                if exc.status_code != 409 or exc.error_type != 'lease_lost':
                    raise
                self.cancelled.set()
                last_run['lease_lost'] = True
                self._acknowledge_public(None)
                return True
            capture = pending['capture']
            self._freeze_public_completion(lease, result_ref, result, capture)
            pending = self._public_recovery.pending()
        kwargs = pending['completion']
        response = self._complete_with_retry(
            draft.task_id, lease, start, receipt=TaskReceipt.from_wire(kwargs['receipt']),
            terminal=TaskTerminal.from_wire(kwargs['terminal']), capture=kwargs['capture'],
            last_run=last_run,
        )
        self._acknowledge_public(response)
        return self._public_recovery.pending() is None

    def _settled_price(self, lease: TaskLease, result: SolveResult, state: str) -> float | None:
        if state != "verified" or getattr(lease, "offer_kind", None) != "limit":
            return None
        limit = getattr(lease, "limit_usd", None)
        if (not isinstance(limit, (int, float)) or isinstance(limit, bool)
                or not math.isfinite(limit) or limit < 0):
            raise ValueError("limit_usd must be a finite, non-negative number")
        price = limit if self.config.settle_fn is None else self.config.settle_fn(lease, result)
        if (not isinstance(price, (int, float)) or isinstance(price, bool)
                or not math.isfinite(price) or price < 0):
            raise ValueError("settle_fn must return a finite, non-negative number")
        return float(min(limit, price))

    def _freeze_public_completion(self, lease, result_ref, result, capture):
        terminal = TaskTerminal(lease_id=lease.lease_id,
            verification_state=capture['status'], result_ref=result_ref,
            settlement_state='unset', rating=None, result_commit=result.result_commit,
            settled_price_usd=self._settled_price(lease, result, capture['status']))
        receipt = _build_receipt(lease.lease_id, result)
        if self._public_recovery is not None and self._public_recovery.pending() is not None:
            self._public_recovery.freeze(receipt=receipt, terminal=terminal, capture=capture)
        return receipt, terminal

    def _acknowledge_public(self, response):
        if self._public_recovery is None or self._public_recovery.pending() is None:
            return
        if self.cancelled.is_set():
            # The gateway explicitly revoked this lease. Retain a tombstone so
            # it can never trigger another local solver execution.
            self._public_recovery.acknowledge()
        elif isinstance(response, Mapping) and response.get('status') in {'done', 'failed'}:
            receipt = response.get('receipt')
            if (not isinstance(receipt, Mapping) or not isinstance(receipt.get('receipt_id'), str)
                    or not receipt['receipt_id'].strip()):
                raise RecoveryRequired('public completion response has no receipt acknowledgement')
            if receipt.get('task_id', self.last_run['task_id']) != self.last_run['task_id']:
                raise RecoveryRequired('public completion response identity mismatch')
            self._public_recovery.acknowledge()
        elif (isinstance(response, Mapping) and response.get('status') == 'outcome_unknown'
                and self._public_recovery.pending().get('completion', {}).get('terminal', {}).get('verification_state') == 'aborted'):
            self._public_recovery.acknowledge()
        elif not isinstance(response, Mapping) or response.get('status') != 'settling':
            raise RecoveryRequired('public completion response was not acknowledged')

    def _run_leased_task(self, lease: TaskLease, draft: TaskDraft, start: float) -> None:
        """The claim → complete path for one held lease. Errors propagate to
        :meth:`run_once`, which reports them as a failed terminal, except for
        public work already saved at the artifact or completion stage, which is
        retained for recovery without a completion.

        The heartbeat thread keeps renewing across the ENTIRE lease hold — not
        just through ``solve_fn`` — and is only stopped once the completion is
        resolved: accepted, terminally refused, or the lease already lost. That
        is what job_c978f3fe82be needed and did not get. ``self.last_run`` is
        always set before this method returns or an exception leaves it (see
        the ``finally`` below), so a caller can read the summary of even a
        failed run.
        """
        self.cancelled.clear()
        # The clone runs before the heartbeat starts, under the just-claimed lease.
        self._clone_lease_id = lease.lease_id
        try:
            workdir = self._clone_and_checkout(draft)
        finally:
            self._clone_lease_id = None
        stop = threading.Event()
        state: dict[str, int] = {"renewals": 0}
        heartbeat = threading.Thread(
            target=self._heartbeat_loop, args=(draft.task_id, lease, stop, state), daemon=True,
        )
        heartbeat.start()
        last_run: dict[str, Any] = {
            "task_id": draft.task_id,
            "renewals": 0,
            "lease_lost": False,
            "completion_attempts": 0,
            "completion_error": None,
            "settlement": None,
        }
        try:
            packet = _public_packet(draft)
            if packet is not None:
                base_exit = _run_verify_command(draft.verify_command, cwd=workdir)
                if base_exit == 0:
                    # A passing base is not an interrupted run. Complete it
                    # before solve_fn; the gateway rejects the generic abort.
                    if not self.cancelled.is_set():
                        self._report_lease_failure(
                            lease,
                            draft,
                            VerifyCommandError('public base already passes'),
                            start,
                            failure_class='base_preflight_non_red',
                            verification_state='failed',
                            known_cost_usd=0.0,
                        )
                    return
                if base_exit != 86:
                    raise VerifyCommandError(
                        'public base did not reproduce the frozen assertion failure')
            if self.cancelled.is_set():
                return
            result = self.solve_fn(draft, workdir)

            if self.cancelled.is_set():
                # The lease is already gone (the heartbeat thread saw a 409
                # lease_lost while solve_fn ran). Publishing/verifying/
                # completing against a dead lease is pointless — and
                # completing is explicitly forbidden (item 4 of the contract:
                # "no completion is attempted on a dead lease").
                return

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

            material_sha = self._material_diff_sha256(workdir, draft.base_commit, result.result_commit)
            scope_ok, changed_paths = self._compute_scope(workdir, draft, result)

            verification_state = "verified" if (verify_exit_code == 0 and scope_ok) else "failed"
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

            if packet is not None:
                result_ref, result = self._publish_public(lease, draft, workdir, result, capture=capture)
            else:
                result_ref = self._publish(draft.task_id, workdir, result, draft=draft)
            receipt, terminal = self._freeze_public_completion(lease, result_ref, result, capture)

            response = self._complete_with_retry(
                draft.task_id,
                lease,
                start,
                receipt=receipt,
                terminal=terminal,
                capture=capture,
                last_run=last_run,
            )
            if isinstance(response, Mapping):
                receipt_wire = response.get("receipt")
                if isinstance(receipt_wire, Mapping):
                    last_run["settlement"] = receipt_wire.get("settlement")
            self.last_run = last_run
            self._acknowledge_public(response)
        finally:
            stop.set()
            heartbeat.join(timeout=5.0)
            last_run["renewals"] = state.get("renewals", 0)
            if self.cancelled.is_set():
                last_run["lease_lost"] = True
            self.last_run = last_run

    def _complete_with_retry(
        self,
        task_id: str,
        lease: TaskLease,
        start: float,
        *,
        receipt: TaskReceipt,
        terminal: TaskTerminal,
        capture: Mapping[str, Any],
        last_run: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Call ``complete_task``, retrying a TRANSIENT refusal while the lease lives.

        The retry bound is the lease itself, never an invented attempt count:
        keep retrying only while ``time.monotonic() - start < self.lease_ttl_s``
        (``lease_ttl_s`` comes from the gateway's registration response — see
        :meth:`register` — not a local constant), leaving room for one more
        attempt before the deadline. Backoff starts at the registered heartbeat
        cadence (``self.config.heartbeat_interval_s``, also gateway-supplied)
        and doubles each retry. There is no ``max_attempts``: the lease TTL is
        the real, gateway-authoritative bound, so adding one would be an
        arbitrary ceiling with no basis.

        A TERMINAL refusal (HTTP 4xx other than 409) is never retried — the
        gateway has decided, and burning the rest of the lease on a doomed
        retry only delays the crash report. It is re-raised so
        :meth:`run_once`'s existing contained-failure path (``_report_lease_failure``)
        handles it. A 409 ``lease_lost`` means the lease is gone outright: no
        retry, no further completion attempt, ``self.cancelled`` is set and
        ``None`` is returned (not raised — there is nothing left to report a
        crash against).
        """
        deadline = start + self.lease_ttl_s
        backoff = max(1.0, self.config.heartbeat_interval_s)
        attempts = 0
        while True:
            attempts += 1
            last_run["completion_attempts"] = attempts
            try:
                response = self.client.complete_task(
                    task_id, self.config.runner_id, lease.lease_id,
                    receipt=receipt, terminal=terminal, capture=capture,
                )
            except OrmasGatewayError as exc:
                last_run["completion_error"] = {
                    "status_code": exc.status_code,
                    "error_type": exc.error_type,
                    "message": exc.message,
                }
                if exc.status_code == 409 and exc.error_type == "lease_lost":
                    self.cancelled.set()
                    return None
                terminal_refusal = (
                    exc.status_code is not None
                    and 400 <= exc.status_code < 500
                    and exc.status_code != 409
                )
                if terminal_refusal:
                    raise
                if time.monotonic() + backoff >= deadline:
                    raise
                time.sleep(backoff)
                backoff *= 2
                continue
            except Exception as exc:  # noqa: BLE001 - a transport/connection error is transient too
                last_run["completion_error"] = {
                    "status_code": None,
                    "error_type": None,
                    "message": str(exc),
                }
                if time.monotonic() + backoff >= deadline:
                    raise
                time.sleep(backoff)
                backoff *= 2
                continue
            last_run["completion_error"] = None
            return response

    def _report_lease_failure(
        self, lease: TaskLease, draft: TaskDraft, exc: Exception, start: float,
        *,
        failure_class: str | None = None,
        verification_state: str | None = None,
        known_cost_usd: float | None = None,
    ) -> None:
        """Report a held lease as a failed terminal.

        The heartbeat is already stopped by the time this runs (solve's
        ``finally``, or before it ever started when the clone failed), except
        when the public base check calls it before ``solve_fn``. The terminal
        says ``verification_state='failed'`` / ``settlement_state='unset'``
        with no result ref. A public crash leaves ``result_commit`` empty; a
        private crash points it at the base commit. The capture carries a
        ``failure_class`` plus the (truncated) exception message. The default
        receipt reports unknown usage rather than fabricated zeros. A caller
        that already knows no provider ran (a public base that exits 0) passes
        ``failure_class``, ``verification_state='failed'``, and
        ``known_cost_usd=0.0`` so this same completion carries a known zero
        cost instead of the public abort class. Reporting itself is
        best-effort: if even the completion call cannot reach the gateway
        there is nothing more this process can do for the lease, so that
        secondary failure only warns.
        """
        public = self._public_recovery is not None and self._public_recovery.pending() is not None
        if failure_class is None:
            failure_class = "git_error" if isinstance(exc, GitError) else "solve_error"
            if public:
                failure_class = 'public_execution_interrupted'
        if verification_state is None:
            verification_state = "aborted" if public else "failed"
        terminal = TaskTerminal(
            lease_id=lease.lease_id,
            verification_state=verification_state,
            result_ref=None,
            settlement_state="unset",
            rating=None,
            result_commit="" if public else draft.base_commit,
        )
        solved = SolveResult(result_commit=draft.base_commit, failure_class=failure_class)
        if known_cost_usd is not None:
            # No provider ran, so the zero is a measurement, not a fill-in.
            solved = SolveResult(
                result_commit=draft.base_commit,
                failure_class=failure_class,
                provider="none",
                prompt_tokens=0,
                completion_tokens=0,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
                reasoning_tokens=0,
                upstream_cost_usd=known_cost_usd,
            )
        receipt = _build_receipt(lease.lease_id, solved)
        capture: dict[str, Any] = {
            "status": "failed",
            "scope_ok": False,
            "file_count": 0,
            "changed_paths": [],
            "wall_s": time.monotonic() - start,
            "failure_class": failure_class,
            "error": str(exc)[:_FAILURE_MESSAGE_MAX],
        }
        if public:
            self._public_recovery.freeze(receipt=receipt, terminal=terminal, capture=capture)
            self.last_run = {'task_id': draft.task_id}
        try:
            response = self.client.complete_task(
                draft.task_id,
                self.config.runner_id,
                lease.lease_id,
                receipt=receipt,
                terminal=terminal,
                capture=capture,
            )
            if public:
                self._acknowledge_public(response)
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
        explicit override. Claim refusals and transport errors log one warning
        and idle at that cadence; credential refusals (401/403), recovery
        failures and ``KeyboardInterrupt`` still propagate.
        """
        interval = self.poll_interval_s if poll_interval_s is None else poll_interval_s
        iterations = 0
        while max_iterations is None or iterations < max_iterations:
            iterations += 1
            try:
                did_work = self.run_once()
            except (OrmasGatewayError, HTTPError, OSError) as exc:
                if self._claim_error is not exc or (
                    isinstance(exc, OrmasGatewayError) and exc.status_code in (401, 403)
                ):
                    raise
                self._claim_error = None
                _LOGGER.warning("claim failed: %s: %s", type(exc).__name__, exc)
                did_work = False
            if not did_work:
                idle_sleep(interval)
