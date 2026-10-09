"""Reference validator daemon — poll assignments, independently re-verify, sign, post.

Card 25ff6188 (Validator 2/2). Same untrusted-perimeter shape as ``skeleton.py``'s
miner loop (``docs/design/miner_delivers_outcomes_decision_2026_09_10.md`` decision
8: a validator inherits the miner's own repository-access model) — it clones the
same ``repo_url``, runs the same ``verify_command`` the miner ran, and reaches its
own accept/reject conclusion from git ground truth, never from the miner's
self-report. See ``docs/design/validator_acceptance_v2_2026_09_10.md`` §4.2-§4.3.

``canonical_evidence_fields``/``evidence_digest_hex`` below are a COPY of
``tensorbox_spec.customer_api.outcomes_validators``'s functions of the same name —
this package must never import ``tensorbox_spec`` (``tests/test_import_graph.py``),
so the digest math is duplicated, not shared. Keep the two byte-identical (same
field set, same sorted-key/compact-separator JSON, same sha256-hex encoding) or a
correct decision will fail to verify against the gateway's recomputed digest.

Ed25519 here is the dev-subnet reference signature scheme. SN76's production
target is sr25519 (Substrate/Bittensor keys) — swap the signing primitive there,
not this loop's shape.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import os
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .public_acceptance import SUPPORTED_ACCEPTANCE_CONTRACTS, validate_acceptance_contract
from .skeleton import (
    DEFAULT_POLL_INTERVAL_S,
    GitError,
    _app_read_expired,
    _credential_git_env,
    _refreshed_app_read,
    _path_covered_by_allowed,
    _run_git,
    _run_verify_command,
)
from .verification_context import evidence_root

__all__ = [
    "VALIDATOR_PROTOCOL_V1",
    "canonical_evidence_fields",
    "evidence_digest_hex",
    "make_ed25519_signer",
    "SignFn",
    "OrmasValidatorClient",
    "ValidatorConfig",
    "ValidatorDaemon",
]

VALIDATOR_PROTOCOL_V1 = "ormas-validator-v1"

# digest_hex -> signature_hex.
SignFn = Callable[[str], str]


# ── execution contract (card 709e2a53) ───────────────────────────────────────
# COPY of ``tensorbox_spec.customer_api.outcomes_validators``'s shape check
# (see module docstring — this package never imports tensorbox_spec). Keep the
# two byte-identical: same key set, same schema_version literal, same
# 64-lowercase-hex digest rule, same "nonempty dict" requirement per field.
EXECUTION_CONTRACT_SCHEMA_VERSION = "outcomes.validation-contract.v2"
_EXECUTION_CONTRACT_PACKET_FIELDS = (
    "execution_environment", "execution_requirements", "verifier_profile", "verify_base",
)
_EXECUTION_CONTRACT_KEYS = frozenset(
    {"schema_version", "work_packet_sha256", *_EXECUTION_CONTRACT_PACKET_FIELDS},
)
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


def _validate_execution_contract_shape(contract: Any) -> None:
    """Fail closed on anything that isn't exactly the frozen contract shape —

    see the gateway twin for the rationale (never silently drop a malformed
    or partial contract from signed evidence).
    """
    if not isinstance(contract, dict):
        raise ValueError("execution_contract must be a dict")
    if set(contract.keys()) != _EXECUTION_CONTRACT_KEYS:
        raise ValueError("execution_contract has an unexpected key set")
    if contract.get("schema_version") != EXECUTION_CONTRACT_SCHEMA_VERSION:
        raise ValueError("execution_contract schema_version mismatch")
    sha = contract.get("work_packet_sha256")
    if not isinstance(sha, str) or _SHA256_HEX_RE.fullmatch(sha) is None:
        raise ValueError("execution_contract work_packet_sha256 must be 64 lowercase hex chars")
    for field in _EXECUTION_CONTRACT_PACKET_FIELDS:
        value = contract.get(field)
        if not isinstance(value, dict) or not value:
            raise ValueError(f"execution_contract.{field} must be a nonempty dict")


def canonical_evidence_fields(
    *,
    job_id: str,
    miner_id: str,
    base_commit: str,
    result_commit: str,
    repo_url: str | None,
    verify_command: str,
    allowed_paths: list[str],
    immutable_paths: list[str],
    toolchain: dict | None = None,
    execution_contract: dict | None = None,
    acceptance_contract: dict | None = None,
) -> dict[str, Any]:
    """The §4.2 evidence fields a validator signs over. Must byte-match the

    gateway's copy of this function (see module docstring) — never add a field
    here without updating both. This must byte-match
    ``tensorbox_spec/customer_api/outcomes_validators.canonical_evidence_fields``
    on the gateway, which adds the same trailing optional key.
    """
    fields: dict[str, Any] = {
        "job_id": job_id,
        "miner_id": miner_id,
        "base_commit": base_commit,
        "result_commit": result_commit,
        "repo_url": repo_url or "",
        "verify_command": verify_command,
        "allowed_paths": list(allowed_paths),
        "immutable_paths": list(immutable_paths),
    }
    if isinstance(toolchain, dict) and toolchain:
        fields["toolchain"] = toolchain
    if execution_contract is not None:
        _validate_execution_contract_shape(execution_contract)
        fields["execution_contract"] = copy.deepcopy(execution_contract)
    if acceptance_contract is not None:
        fields["acceptance_contract"] = validate_acceptance_contract(acceptance_contract)
    return fields


class ToolchainUnavailable(RuntimeError):
    """Declared interpreter missing or venv/pip provision failed."""


def validate_assignment_execution(assignment: Mapping[str, Any]) -> bool:
    """Reconstruct the public execution projection before Git or execution.

    The full private packet is intentionally absent from assignments. Its hash
    is signed alongside these exact fields; the compiler validates the execution
    projection without needing task text, prices or miner model choices.
    """
    contract = assignment.get("execution_contract")
    if contract is None:
        return False
    _validate_execution_contract_shape(contract)
    from .outcomes_support import validate_public_execution_packet

    for name in ("base_commit", "result_commit"):
        if not isinstance(assignment.get(name), str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", assignment[name]):
            raise ValueError("public_assignment_commit_required")
    for name in ("allowed_paths", "immutable_paths"):
        paths = assignment.get(name)
        if not isinstance(paths, list) or not paths or any(
            not isinstance(path, str) or path.startswith(("/", "~")) or "\\" in path
            or any(part in ("", ".", "..", ".git") for part in path.rstrip("/").split("/"))
            for path in paths
        ):
            raise ValueError("public_assignment_scope_required")
    packet = {name: contract[name] for name in _EXECUTION_CONTRACT_PACKET_FIELDS}
    packet.update(
        verification_command=assignment.get("verify_command"),
        task_features={"languages": contract["execution_requirements"].get("languages")},
        execution_policy={"repo_base_sha": assignment["base_commit"],
                          "allowed_paths": assignment["allowed_paths"],
                          "immutable_paths": assignment["immutable_paths"]},
    )
    if assignment.get("toolchain") is not None:
        packet["toolchain"] = assignment["toolchain"]
    validate_public_execution_packet(packet)
    return True


_TOOLCHAIN_REQ_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*"
    r"(\[[A-Za-z0-9._,-]+\])?"
    r"((==|!=|<=|>=|<|>|~=|===)[A-Za-z0-9.*+!-]+"
    r"(,(==|!=|<=|>=|<|>|~=|===)[A-Za-z0-9.*+!-]+)*)?$"
)


def _editable_install_path_ok(path: str) -> bool:
    """Relative in-repo path; optional trailing extras. No URL, abs, or ``..`` segment."""
    if not path or path.startswith("/") or "\\" in path or ":" in path:
        return False
    if any(ch.isspace() for ch in path):
        return False
    base = path
    if path.endswith("]") and "[" in path:
        base = path[: path.rfind("[")]
        if not base:
            return False
    return ".." not in base.split("/")


def validate_toolchain_shape(toolchain: Mapping[str, Any]) -> None:
    """These rules must match ``tensorbox_spec.outcomes_prep.validate_toolchain_v1`` on the gateway."""
    if set(toolchain.keys()) != {"kind", "python", "pip_install", "lock_paths"}:
        raise ToolchainUnavailable("malformed toolchain")
    if toolchain.get("kind") != "python":
        raise ToolchainUnavailable("malformed toolchain")
    python = toolchain.get("python")
    if not isinstance(python, str) or re.fullmatch(r"^3\.\d{1,2}$", python) is None:
        raise ToolchainUnavailable("malformed toolchain")
    pip_install = toolchain.get("pip_install")
    if not isinstance(pip_install, list) or not pip_install:
        raise ToolchainUnavailable("malformed toolchain")
    i = 0
    while i < len(pip_install):
        token = pip_install[i]
        if not isinstance(token, str) or not token:
            raise ToolchainUnavailable("malformed toolchain")
        if token == "-e":
            if i + 1 >= len(pip_install):
                raise ToolchainUnavailable("malformed toolchain")
            path = pip_install[i + 1]
            if not isinstance(path, str) or not _editable_install_path_ok(path):
                raise ToolchainUnavailable("malformed toolchain")
            i += 2
            continue
        if token.startswith("-"):
            raise ToolchainUnavailable("malformed toolchain")
        if _TOOLCHAIN_REQ_RE.fullmatch(token) is None:
            raise ToolchainUnavailable("malformed toolchain")
        i += 1
    lock_paths = toolchain.get("lock_paths")
    if not isinstance(lock_paths, list):
        raise ToolchainUnavailable("malformed toolchain")
    for path in lock_paths:
        if not isinstance(path, str) or not path:
            raise ToolchainUnavailable("malformed toolchain")
        if path.startswith("/") or "\\" in path or ".." in path.split("/"):
            raise ToolchainUnavailable("malformed toolchain")


def provision_toolchain(toolchain: Mapping[str, Any] | None, workdir: Path) -> str | None:
    """Return ``<workdir>/.venv/bin`` to prepend to PATH, or None if no python toolchain."""
    if not isinstance(toolchain, Mapping) or toolchain.get("kind") != "python":
        return None
    validate_toolchain_shape(toolchain)
    interpreter = shutil.which(f"python{toolchain['python']}")
    if interpreter is None:
        raise ToolchainUnavailable(f"python{toolchain['python']} not on PATH")
    venv = workdir / ".venv"
    if venv.exists() or venv.is_symlink():
        if venv.is_dir() and not venv.is_symlink():
            shutil.rmtree(venv)
        else:
            venv.unlink()
    bounded_env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(workdir),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
    }
    created = subprocess.run(
        [interpreter, "-m", "venv", str(venv)],
        cwd=workdir,
        env=bounded_env,
        capture_output=True,
        text=True,
        check=False,
    )
    if created.returncode != 0:
        raise ToolchainUnavailable("python -m venv failed")
    pip_install = toolchain.get("pip_install")
    if isinstance(pip_install, list) and pip_install:
        venv_bin = str(venv / "bin")
        pip_env = {
            "PATH": venv_bin + os.pathsep + bounded_env["PATH"],
            "HOME": bounded_env["HOME"],
            "LANG": bounded_env["LANG"],
        }
        installed = subprocess.run(
            [str(venv / "bin" / "python"), "-m", "pip", "--no-input",
             "--disable-pip-version-check", "install", *pip_install],
            cwd=workdir,
            env=pip_env,
            capture_output=True,
            text=True,
            check=False,
        )
        if installed.returncode != 0:
            raise ToolchainUnavailable("pip install failed")
    return str(venv / "bin")


def evidence_digest_hex(fields: Mapping[str, Any]) -> str:
    blob = json.dumps(dict(fields), sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def make_ed25519_signer(private_key_hex: str) -> tuple[SignFn, str]:
    """Build a ``(sign_fn, pubkey_hex)`` pair from a raw 32-byte Ed25519 private

    key, hex-encoded. ``sign_fn(digest_hex)`` signs the digest's UTF-8 bytes —
    the exact convention the gateway verifies against
    (``outcomes_validators.verify_ed25519_signature``).
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private_key_hex))
    pubkey_hex = key.public_key().public_bytes_raw().hex()

    def sign_fn(digest_hex: str) -> str:
        return key.sign(digest_hex.encode("utf-8")).hex()

    return sign_fn, pubkey_hex


class OrmasValidatorClient:
    """Thin HTTP client for the gateway's ``/api/validator/v1`` routes.

    Same injectable-transport shape as ``client.OrmasMinerClient`` (any object
    with ``.post``/``.get`` returning ``.status_code``/``.json()``/
    ``.raise_for_status()``) so tests never touch a real network.
    """

    def __init__(self, base_url: str, token: str, *, http_client: Any | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        if http_client is not None:
            self._client = http_client
            self._owns_client = False
        else:
            import httpx  # optional dep — only needed on the real HTTP path

            self._client = httpx.Client(
                base_url=self.base_url,
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=60.0,
            )
            self._owns_client = True

    def register(self, *, pubkey_hex: str, task_cells: list[str] | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "schema_version": VALIDATOR_PROTOCOL_V1,
            "pubkey_hex": pubkey_hex,
            "acceptance_contracts": list(SUPPORTED_ACCEPTANCE_CONTRACTS),
        }
        if task_cells is not None:
            # Only a checker that can attest advertises the Protected cell.
            body["task_cells"] = list(task_cells)
        resp = self._client.post("/api/validator/v1/registrations", json=body)
        resp.raise_for_status()
        return resp.json()

    def request_attestation_challenge(self, assignment_id: str) -> dict[str, Any]:
        """One-use nonce plus the ``job_id``/``attempt`` binding for a Protected assignment."""
        resp = self._client.post(
            f"/api/validator/v1/assignments/{assignment_id}/attestation-challenge",
            json={"schema_version": VALIDATOR_PROTOCOL_V1},
        )
        resp.raise_for_status()
        return resp.json()

    def release_sealed_credential(
        self, assignment_id: str, *, challenge_nonce: str, evidence: Mapping[str, Any],
        enclave_pubkey: str,
    ) -> dict[str, Any]:
        """The read token sealed to the attested enclave key."""
        resp = self._client.post(
            f"/api/validator/v1/assignments/{assignment_id}/repo-credential",
            json={
                "schema_version": VALIDATOR_PROTOCOL_V1,
                "challenge_nonce": challenge_nonce,
                "evidence": dict(evidence),
                "enclave_pubkey": enclave_pubkey,
            },
        )
        resp.raise_for_status()
        return resp.json()

    def list_assignments(self) -> list[dict[str, Any]]:
        resp = self._client.get("/api/validator/v1/assignments")
        resp.raise_for_status()
        payload = resp.json()
        assignments = payload.get("assignments") if isinstance(payload, Mapping) else None
        return list(assignments) if isinstance(assignments, list) else []

    def read_repository_credential(self, assignment_id: str) -> dict[str, Any]:
        """``{repo_credential}`` for this checker's current assignment."""
        resp = self._client.post(
            f"/api/validator/v1/assignments/{assignment_id}/repo-credential",
            json={"schema_version": VALIDATOR_PROTOCOL_V1},
        )
        resp.raise_for_status()
        return resp.json()

    def post_decision(self, assignment_id: str, *, decision: str, signature_hex: str) -> dict[str, Any]:
        resp = self._client.post(
            f"/api/validator/v1/assignments/{assignment_id}/decisions",
            json={
                "schema_version": VALIDATOR_PROTOCOL_V1,
                "decision": decision,
                "signature_hex": signature_hex,
            },
        )
        resp.raise_for_status()
        return resp.json()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "OrmasValidatorClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


@dataclass
class ValidatorConfig:
    """Where the daemon clones assignments to review. One workdir per assignment."""

    workdir_root: Path


@dataclass
class _ReviewFlight:
    """One in-flight child review. The signer stays on the parent daemon."""

    proc: Any
    recv: Any
    assignment: Mapping[str, Any]
    digest: str


def _with_usable_read_credential(client: Any, assignment: Mapping[str, Any]) -> Mapping[str, Any]:
    """The assignment with its expired App read token replaced for this checker.

    Only an expired App token is refreshed, through the checker's own assignment.
    Without a client (a review child) the expired token is kept and the read
    refuses it. A refused refresh raises ``GitError``.
    """
    if not _app_read_expired(assignment.get("repo_credential")) or client is None:
        return assignment
    fresh = _refreshed_app_read(
        lambda: client.read_repository_credential(assignment["assignment_id"]))
    return {**assignment, "repo_credential": fresh}


def _child_sign_refused(digest_hex: str) -> str:
    raise RuntimeError("validator signing key must not enter a review child")


def review_assignment(workdir_root: str, assignment: Mapping[str, Any]) -> str:
    """Picklable child entry: clone and decide. No client and no signing key.

    ``_clone`` reads ``config.workdir_root`` only. ``_decide`` uses the assignment
    and the workdir, not the client or the signer.
    """
    reviewer = ValidatorDaemon(
        client=None,  # type: ignore[arg-type]
        config=ValidatorConfig(workdir_root=Path(workdir_root)),
        sign_fn=_child_sign_refused,
    )
    workdir = reviewer._clone_with_retry(assignment)
    if workdir is None:
        return "error"
    return reviewer._decide(assignment, workdir)


def _child_process_main(
    send_conn: Any,
    workdir_root: str,
    assignment: dict[str, Any],
    review_fn: Callable[[str, Mapping[str, Any]], str],
) -> None:
    """Spawn target. A missing result means the parent posts nothing."""
    try:
        try:
            decision = review_fn(workdir_root, assignment)
        except GitError:
            decision = "error"
        if isinstance(decision, str):
            send_conn.send(decision)
    finally:
        send_conn.close()


def _release_process(proc: Any) -> None:
    if proc.is_alive():
        return
    close = getattr(proc, "close", None)
    if close is not None:
        close()


def _finish_flight(flight: _ReviewFlight, poll_interval_s: float) -> None:
    flight.proc.join(poll_interval_s)
    try:
        flight.recv.close()
    except OSError:
        pass
    _release_process(flight.proc)


_STILL_RUNNING = object()


@dataclass
class _PendingPost:
    """A finished review whose post has not succeeded. Signed once, in the parent."""

    assignment: Mapping[str, Any]
    digest: str
    decision: str
    signature_hex: str


def _log_gateway_failure(action: str, exc: BaseException) -> None:
    """Class and one line. No assignment body and no credential."""
    text = str(exc)
    detail = text.splitlines()[0] if text else ""
    logging.getLogger(__name__).warning("%s failed: %s: %s", action, type(exc).__name__, detail)


def _child_result(flight: _ReviewFlight, poll_interval_s: float) -> Any:
    """Decision string, None if the child died without one, or still running."""
    if flight.recv.poll():
        try:
            message = flight.recv.recv()
        except (EOFError, OSError):
            return None
        return message if isinstance(message, str) else None
    if not flight.proc.is_alive():
        flight.proc.join(poll_interval_s)
        if flight.recv.poll():
            try:
                message = flight.recv.recv()
            except (EOFError, OSError):
                return None
            return message if isinstance(message, str) else None
        return None
    return _STILL_RUNNING


def _remember_post(
    daemon: ValidatorDaemon,
    pending: dict[Any, _PendingPost],
    assignment: Mapping[str, Any],
    digest: str,
    decision: str,
) -> None:
    aid = assignment["assignment_id"]
    if aid in pending:
        return
    pending[aid] = _PendingPost(
        assignment=assignment,
        digest=digest,
        decision=decision,
        signature_hex=daemon.sign_fn(digest),
    )


def _retry_pending_posts(
    daemon: ValidatorDaemon,
    pending: dict[Any, _PendingPost],
    posted: set[Any],
) -> bool:
    """Retry stored signatures. A failure stays pending and does not raise."""
    progressed = False
    for aid, item in list(pending.items()):
        try:
            daemon.client.post_decision(
                aid, decision=item.decision, signature_hex=item.signature_hex,
            )
        except Exception as exc:
            _log_gateway_failure("post_decision", exc)
            continue
        posted.add(aid)
        del pending[aid]
        progressed = True
    return progressed


def _reap_reviews(
    daemon: ValidatorDaemon,
    in_flight: dict[Any, _ReviewFlight],
    pending: dict[Any, _PendingPost],
    crashed: set[Any],
    deferred: set[Any],
    poll_interval_s: float,
) -> bool:
    """Collect finished children. A dead child is not posted and not restarted here."""
    done: list[Any] = []
    for aid, flight in in_flight.items():
        decision = _child_result(flight, poll_interval_s)
        if decision is _STILL_RUNNING:
            continue
        if isinstance(decision, str):
            _remember_post(daemon, pending, flight.assignment, flight.digest, decision)
        else:
            crashed.add(aid)
            deferred.add(aid)
        _finish_flight(flight, poll_interval_s)
        done.append(aid)
    for aid in done:
        del in_flight[aid]
    return bool(done)


def _dispatch_reviews(
    daemon: ValidatorDaemon,
    ctx: Any,
    in_flight: dict[Any, _ReviewFlight],
    pending: dict[Any, _PendingPost],
    posted: set[Any],
    crashed: set[Any],
    deferred: set[Any],
    assignments: list[Any],
    slots: int,
) -> int:
    """Start reviews while a slot is free. Never-attempted rows go first."""
    fresh: list[Any] = []
    again: list[Any] = []
    for assignment in assignments:
        aid = assignment["assignment_id"]
        if aid in in_flight or aid in posted or aid in pending or aid in deferred:
            continue
        if aid in crashed:
            again.append(assignment)
        else:
            fresh.append(assignment)
    started = 0
    for assignment in [*fresh, *again]:
        if len(in_flight) >= slots:
            break
        aid = assignment["assignment_id"]
        kind, digest = daemon._preface(assignment)
        if kind == "skip" or digest is None:
            continue
        if kind == "error":
            _remember_post(daemon, pending, assignment, digest, "error")
            started += 1
            continue
        # The child has no client or bearer; hand it a usable read credential.
        try:
            readable = _with_usable_read_credential(daemon.client, assignment)
        except GitError:
            continue  # nothing posted; a later poll may try this assignment again
        payload = copy.deepcopy(dict(readable))
        recv_conn, send_conn = ctx.Pipe(duplex=False)
        # Look up the entry here so a test can substitute a picklable fake.
        # The signing key is not an argument.
        proc = ctx.Process(
            target=_child_process_main,
            args=(send_conn, str(daemon.config.workdir_root), payload, review_assignment),
            name=f"ormas-validator-{aid}",
            daemon=True,
        )
        try:
            proc.start()
        except Exception:
            send_conn.close()
            recv_conn.close()
            raise
        send_conn.close()
        in_flight[aid] = _ReviewFlight(
            proc=proc, recv=recv_conn, assignment=assignment, digest=digest,
        )
        started += 1
    return started


def _fill_slots(
    daemon: ValidatorDaemon,
    ctx: Any,
    in_flight: dict[Any, _ReviewFlight],
    pending: dict[Any, _PendingPost],
    posted: set[Any],
    crashed: set[Any],
    deferred: set[Any],
    slots: int,
) -> int:
    """List, then start work. A list failure leaves current children alone."""
    if len(in_flight) >= slots:
        return 0
    try:
        assignments = daemon.client.list_assignments()
    except Exception as exc:
        _log_gateway_failure("list_assignments", exc)
        assignments = []
    started = _dispatch_reviews(
        daemon, ctx, in_flight, pending, posted, crashed, deferred, assignments, slots,
    )
    # Crashes from this poll wait until this list call has returned.
    deferred.clear()
    return started


def _stop_reviews(in_flight: dict[Any, _ReviewFlight], poll_interval_s: float) -> None:
    for flight in in_flight.values():
        if flight.proc.is_alive():
            flight.proc.terminate()
        flight.proc.join(poll_interval_s)
        if flight.proc.is_alive():
            flight.proc.kill()
            flight.proc.join(poll_interval_s)
        try:
            flight.recv.close()
        except OSError:
            pass
        _release_process(flight.proc)
    in_flight.clear()


class ValidatorDaemon:
    """Poll -> clone -> re-verify base(non-zero)+result(zero) -> scope check -> sign -> post.

    The verifier is credential-free and reuses ``skeleton._run_verify_command``
    (bounded env, no shell, no ambient secrets) — the same sandboxing a miner's
    own verify run gets, per decision 8 (validator on the same perimeter).
    """

    def __init__(
        self, client: OrmasValidatorClient, config: ValidatorConfig, sign_fn: SignFn,
        *, protected: Any | None = None,
    ) -> None:
        self.client = client
        self.config = config
        self.sign_fn = sign_fn
        # A ProtectedAssignmentAttestor when this checker runs in the attested CVM.
        self.protected = protected
        self._protected_tokens: dict[str, Any] = {}

    def register(self, *, pubkey_hex: str) -> dict[str, Any]:
        if self.protected is None:
            return self.client.register(pubkey_hex=pubkey_hex)
        from .protected_attestation import PROTECTED_SERVICE_CELL
        return self.client.register(pubkey_hex=pubkey_hex, task_cells=[PROTECTED_SERVICE_CELL])

    def _attest(self, assignment_id: str) -> Any:
        """Open one sealed read token for this assignment; raises ``GitError`` when refused."""
        from .protected_attestation import ProtectedAttestationError
        if self.protected is None:
            raise GitError("protected attestation unavailable")
        try:
            token = self.protected.fetch_read_token(assignment_id)
        except ProtectedAttestationError as exc:
            logging.getLogger(__name__).warning("protected attestation refused: %s", exc.reason)
            raise GitError("protected attestation refused") from None
        self._protected_tokens[assignment_id] = token
        return token

    def _token_valid(self, assignment_id: str) -> bool:
        from datetime import datetime, timezone

        token = self._protected_tokens.get(assignment_id)
        try:
            return token is not None and datetime.fromisoformat(
                token.expires_at.replace("Z", "+00:00")) > datetime.now(timezone.utc)
        except (TypeError, ValueError, AttributeError):
            return False

    def _protected_clone(self, assignment: Mapping[str, Any], workdir: Path) -> None:
        from .outcomes_support import protected_repository_git
        assignment_id = str(assignment["assignment_id"])
        if self._token_valid(assignment_id):
            token = self._protected_tokens[assignment_id]
        else:
            token = self._attest(assignment_id)
        repo_url = assignment.get("repo_url")
        protected_repository_git(
            ["clone", "--no-checkout", repo_url, str(workdir)], cwd=workdir.parent,
            repo_url=repo_url, token=token.token, expires_at=token.expires_at)

    def _clone(self, assignment: Mapping[str, Any]) -> Path:
        workdir = self.config.workdir_root / assignment["assignment_id"]
        if workdir.exists():
            # Leftover half-created workdir from a failed attempt — replace it.
            shutil.rmtree(workdir)
        workdir.parent.mkdir(parents=True, exist_ok=True)
        try:
            if assignment.get("service_level") == "protected":
                # The token arrives only sealed to this CVM's key, never on the assignment.
                self._protected_clone(assignment, workdir)
                return workdir
            if validate_assignment_execution(assignment):
                from .outcomes_support import validate_repository_credential, repository_git, public_git
                visibility = assignment['execution_contract']['execution_requirements']['repository_visibility']
                repo_url, credential = assignment.get('repo_url'), assignment.get('repo_credential')
                if visibility == 'private' and _app_read_expired(credential):
                    credential = _with_usable_read_credential(
                        self.client, assignment)["repo_credential"]
                validate_repository_credential(visibility, repo_url, credential)
                args = ['clone', '--no-checkout', repo_url, str(workdir)]
                if visibility == 'private':
                    repository_git(args, cwd=workdir.parent, repo_url=repo_url, credential=credential)
                else:
                    public_git(args, cwd=workdir.parent)
                return workdir
            # The read credential is served by the gateway per assignment and exists on disk only for this clone; later checkout/diff need no credential.
            with _credential_git_env(assignment.get("repo_credential")) as env:
                _run_git(["clone", str(assignment["repo_url"]), str(workdir)], cwd=workdir.parent, env=env)
        except (GitError, ValueError, OSError, subprocess.SubprocessError) as exc:
            shutil.rmtree(workdir, ignore_errors=True)  # drop git's partial clone
            raise GitError('repository clone failed') from exc
        return workdir

    def _review_error(
        self, assignment: Mapping[str, Any], error: BaseException | str, *, action: str = "review",
    ) -> str:
        cause = error
        while isinstance(cause, BaseException) and cause.__cause__ is not None:
            cause = cause.__cause__
        stderr = getattr(cause, "stderr", None)
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        detail = str(cause) + (f": {stderr}" if stderr else "")
        credential = assignment.get("repo_credential")
        if isinstance(credential, Mapping):
            for field in ("token", "private_key"):
                value = credential.get(field)
                if isinstance(value, str) and value:
                    detail = detail.replace(value, "[redacted]")
        token = self._protected_tokens.get(str(assignment.get("assignment_id")))
        if token is not None:
            detail = detail.replace(token.token, "[redacted]")
        kind = type(cause).__name__ if isinstance(cause, BaseException) else "verification"
        logging.getLogger(__name__).warning(
            "%s failed for assignment %s: %s: %s",
            action, assignment.get("assignment_id"), kind, " ".join(detail.splitlines()),
        )
        return "error"

    def _clone_with_retry(self, assignment: Mapping[str, Any]) -> Path | None:
        # No remaining deadline is sent. Use one default polling window (15 s),
        # capped by the assignment's timeout_s; its whole-second unit sets the pause.
        window_s = float(DEFAULT_POLL_INTERVAL_S)
        contract = assignment.get("acceptance_contract")
        policy = contract.get("policy") if isinstance(contract, Mapping) else None
        timeout_s = policy.get("timeout_s") if isinstance(policy, Mapping) else None
        if type(timeout_s) is int and timeout_s > 0:
            window_s = min(window_s, timeout_s)
        pause_s = 1.0
        attempts = math.ceil(window_s / pause_s)
        deadline = time.monotonic() + window_s
        for attempt in range(attempts):
            try:
                return self._clone(assignment)
            except GitError as exc:
                self._review_error(assignment, exc, action="repository clone")
            if attempt + 1 == attempts or time.monotonic() + pause_s >= deadline:
                break
            time.sleep(pause_s)
            if time.monotonic() >= deadline:
                break
        return None

    def _decide(self, assignment: Mapping[str, Any], workdir: Path) -> str:
        """Independent accept/reject/error — never the miner's self-report.

        ``error`` means the VALIDATOR could not complete its own review (a git
        failure); it is excluded from quorum (``outcomes_validators.resolve_quorum``)
        rather than counted against the miner.
        """
        try:
            public_execution = validate_assignment_execution(assignment)
            prefix = None if public_execution else provision_toolchain(assignment.get("toolchain"), workdir)
        except (ToolchainUnavailable, ValueError, TypeError, KeyError, AttributeError) as exc:
            return self._review_error(assignment, exc)
        base_commit = str(assignment["base_commit"])
        result_commit = str(assignment["result_commit"])
        verify_command = str(assignment["verify_command"])
        allowed = list(assignment.get("allowed_paths") or [])
        immutable = list(assignment.get("immutable_paths") or [])
        validation_run_id = uuid.uuid4().hex

        def scope_valid():
            diff_out = _run_git(["diff", "--name-only", base_commit, result_commit], cwd=workdir)
            changed = [p for p in diff_out.splitlines() if p]
            return not any(_path_covered_by_allowed(p, immutable) for p in changed) and (
                not allowed or all(_path_covered_by_allowed(p, allowed) for p in changed))

        if public_execution:
            try:
                if not scope_valid():
                    return "reject"
            except GitError as exc:
                return self._review_error(assignment, exc)

        try:
            _run_git(["checkout", base_commit], cwd=workdir)
        except GitError as exc:
            return self._review_error(assignment, exc)
        def verify(phase):
            if evidence_root(verify_command) is None:
                return _run_verify_command(verify_command, cwd=workdir, path_prefix=prefix,
                                           raise_setup_errors=True)
            return _run_verify_command(verify_command, cwd=workdir, path_prefix=prefix,
                                       raise_setup_errors=True, evidence_identity={
                "job_id": assignment.get("job_id"), "role": "validator",
                "assignment_id": assignment.get("assignment_id"), "phase": phase,
                "validation_run_id": validation_run_id,
                "evidence_digest_sha256": assignment.get("evidence_digest_sha256"),
            })

        try:
            base_exit = verify("base")
        except (ValueError, OSError) as exc:
            return self._review_error(assignment, exc)
        if public_execution and base_exit != 86:
            # The frozen preflight could not be reproduced. An already-green
            # or broken base is no evidence of a miner's implementation quality.
            return self._review_error(assignment, f"base verifier returned {base_exit}, expected 86")
        if evidence_root(verify_command) is not None and base_exit in (74, 127):
            return self._review_error(assignment, f"base verifier setup failed: exit {base_exit}")

        try:
            _run_git(["checkout", result_commit], cwd=workdir)
        except GitError as exc:
            return self._review_error(assignment, exc)
        try:
            result_exit = verify("result")
        except (ValueError, OSError) as exc:
            return self._review_error(assignment, exc)
        if public_execution:
            if result_exit not in (0, 86):
                return self._review_error(assignment, f"result verifier failed: exit {result_exit}")
            return {0: "accept", 86: "reject"}[result_exit]
        if evidence_root(verify_command) is not None and result_exit in (74, 127):
            return self._review_error(assignment, f"result verifier setup failed: exit {result_exit}")

        try:
            diff_out = _run_git(["diff", "--name-only", base_commit, result_commit], cwd=workdir)
        except GitError as exc:
            return self._review_error(assignment, exc)
        changed = [p for p in diff_out.splitlines() if p]

        touches_immutable = bool(immutable) and any(
            _path_covered_by_allowed(p, immutable) for p in changed
        )
        scope_ok = (not allowed) or all(_path_covered_by_allowed(p, allowed) for p in changed)

        if base_exit == 0:
            # The fail-on-base contract is broken (the base already passes) —
            # the pass can't be attributed to this delivery. Fail closed.
            return "reject"
        if result_exit != 0:
            return "reject"
        if touches_immutable or not scope_ok:
            return "reject"
        return "accept"

    def _preface(self, assignment: Mapping[str, Any]) -> tuple[str, str | None]:
        """Parent-only gate: digest check before any clone.

        ``skip`` — no digest to sign (same as today's early return). ``error`` —
        sign and post ``error`` without a repository. ``review`` — clone and decide.
        """
        expected_digest = assignment.get("evidence_digest_sha256")
        if not isinstance(expected_digest, str) or _SHA256_HEX_RE.fullmatch(expected_digest) is None:
            # There is no valid assignment identity to acknowledge. Never sign
            # an invented empty digest or access a repository for this row.
            return ("skip", None)
        try:
            fields = canonical_evidence_fields(
                job_id=str(assignment["job_id"]),
                miner_id=str(assignment["miner_id"]),
                base_commit=str(assignment["base_commit"]),
                result_commit=str(assignment["result_commit"]),
                repo_url=assignment.get("repo_url"),
                verify_command=str(assignment["verify_command"]),
                allowed_paths=list(assignment.get("allowed_paths") or []),
                immutable_paths=list(assignment.get("immutable_paths") or []),
                toolchain=(
                    assignment.get("toolchain") if isinstance(assignment.get("toolchain"), dict) else None
                ),
                execution_contract=assignment.get("execution_contract"),
                acceptance_contract=assignment.get("acceptance_contract"),
            )
            digest = evidence_digest_hex(fields)
            if digest != expected_digest:
                raise ValueError("assignment_evidence_mismatch")
            validate_assignment_execution(assignment)
        except (ValueError, KeyError, TypeError, AttributeError):
            return ("error", expected_digest)
        return ("review", expected_digest)

    def _sign_and_post(self, assignment: Mapping[str, Any], digest: str, decision: str) -> None:
        # An error acknowledges the original assignment identity, including a
        # malformed contract. It cannot enter the acceptance quorum, and avoids
        # retrying a deterministic invalid assignment through an empty signature.
        signature_hex = self.sign_fn(digest)
        self.client.post_decision(
            assignment["assignment_id"], decision=decision, signature_hex=signature_hex,
        )

    def run_once(self) -> bool:
        """Review one pending assignment if any. False when idle."""
        assignments = self.client.list_assignments()
        if self.protected is None:
            # Only an attesting checker can act on a Protected stub.
            assignments = [a for a in assignments if not a.get("attestation_required")]
        if not assignments:
            return False
        assignment = None
        attested = False
        for row in assignments:
            if not row.get("attestation_required"):
                assignment = row
                break
            # Phase one: attest for this stub; a later poll lists its evidence. A stub
            # already attested with a live token waits for that listing, and a refused
            # stub never blocks the rows after it; both retry on the next poll.
            assignment_id = str(row.get("assignment_id"))
            if self._token_valid(assignment_id):
                continue
            try:
                self._attest(assignment_id)
            except GitError:
                continue
            attested = True
        if assignment is None:
            return attested
        kind, digest = self._preface(assignment)
        if kind == "skip" or digest is None:
            return True
        decision = "error"
        if kind == "review":
            workdir = self._clone_with_retry(assignment)
            if workdir is not None:
                decision = self._decide(assignment, workdir)
        self._sign_and_post(assignment, digest, decision)
        self._protected_tokens.pop(str(assignment.get("assignment_id")), None)
        return True

    def serve(
        self,
        slots: int,
        poll_interval_s: float,
        *,
        until: Callable[[], bool] | None = None,
    ) -> None:
        """Review until stopped. One slot stays inline; more slots use child processes."""
        import time

        if self.protected is not None and slots != 1:
            # Review children hold no client, so they cannot attest.
            raise ValueError("Protected attestation runs with exactly one slot")
        if slots == 1:
            while True:
                if until is not None and until():
                    return
                worked = self.run_once()
                if until is not None and until():
                    return
                if not worked:
                    time.sleep(poll_interval_s)
            return
        self._serve_parallel(slots, poll_interval_s, until=until)

    def _serve_parallel(
        self,
        slots: int,
        poll_interval_s: float,
        *,
        until: Callable[[], bool] | None,
    ) -> None:
        """Up to ``slots`` reviews at once. Each review is its own process."""
        import multiprocessing
        import time

        ctx = multiprocessing.get_context("spawn")
        in_flight: dict[Any, _ReviewFlight] = {}
        pending: dict[Any, _PendingPost] = {}
        posted: set[Any] = set()
        crashed: set[Any] = set()
        deferred: set[Any] = set()
        try:
            while True:
                finished = _reap_reviews(
                    self, in_flight, pending, crashed, deferred, poll_interval_s,
                )
                posted_now = _retry_pending_posts(self, pending, posted)
                dispatched = _fill_slots(
                    self, ctx, in_flight, pending, posted, crashed, deferred, slots,
                )
                posted_now = posted_now or _retry_pending_posts(self, pending, posted)
                if until is not None and until():
                    return
                if finished or posted_now or dispatched:
                    continue
                if in_flight:
                    multiprocessing.connection.wait(
                        [flight.recv for flight in in_flight.values()],
                        timeout=poll_interval_s,
                    )
                else:
                    time.sleep(poll_interval_s)
        finally:
            _stop_reviews(in_flight, poll_interval_s)


def _load_cli_secret(*, token_env: str | None, token_path: str | None) -> str:
    """Read a service credential without following links or exposing its bytes."""
    import os
    import stat

    from .client import load_token

    if token_path is None:
        return load_token(token_env=token_env)
    error = "validator credential must be a private regular file owned by the service user"
    if not hasattr(os, "geteuid") or not hasattr(os, "O_NOFOLLOW"):
        raise ValueError(error)
    try:
        fd = os.open(token_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) not in (0o400, 0o600) or info.st_size > 8192):
                raise ValueError(error)
            value = os.read(fd, 8193).decode("utf-8").strip()
            if not value or len(value) > 8192:
                raise ValueError(error)
            return value
        finally:
            os.close(fd)
    except (OSError, UnicodeError):
        raise ValueError(error) from None


def _positive_slot_count(value: str) -> int:
    import argparse

    if not re.fullmatch(r"[0-9]+", value):
        raise argparse.ArgumentTypeError("--slots must be a positive integer")
    slots = int(value, 10)
    if slots < 1:
        raise argparse.ArgumentTypeError("--slots must be a positive integer")
    return slots


def main(argv: list[str] | None = None) -> int:
    """Run the installed task-checker component of the SN76 validator."""
    import argparse

    parser = argparse.ArgumentParser(description="Ormas SN76 task validator")
    parser.add_argument("--gateway", required=True, help="Gateway base URL")
    token_args = parser.add_mutually_exclusive_group(required=True)
    token_args.add_argument("--token-env", help="Env var holding the validator bearer token")
    token_args.add_argument("--token-file", help="Private file holding the validator bearer token")
    key_args = parser.add_mutually_exclusive_group(required=True)
    key_args.add_argument("--private-key-env", help="Env var holding the hex Ed25519 decision key")
    key_args.add_argument("--private-key-file", help="Private file holding the hex Ed25519 decision key")
    parser.add_argument("--workdir-root", default=str(Path.cwd() / "ormas-validator-work"))
    parser.add_argument("--once", action="store_true", help="Review one assignment, exit 3 if idle")
    parser.add_argument(
        "--slots", type=_positive_slot_count, default=1,
        help="assignment reviews at once (default: 1)",
    )
    parser.add_argument("--poll-interval-s", type=float, default=15.0)
    args = parser.parse_args(argv)
    if not 0 < args.poll_interval_s < float("inf"):
        parser.error("--poll-interval-s must be finite and positive")
    token = _load_cli_secret(token_env=args.token_env, token_path=args.token_file)
    if args.token_env:
        os.environ.pop(args.token_env, None)
    private_key = _load_cli_secret(token_env=args.private_key_env, token_path=args.private_key_file)
    if args.private_key_env:
        os.environ.pop(args.private_key_env, None)
    from .protected_attestation import ProtectedAssignmentAttestor, evidence_fn_from_env
    try:
        evidence_fn = evidence_fn_from_env(os.environ)
    except ValueError as exc:
        parser.error(str(exc))
    if evidence_fn is not None and args.slots != 1:
        parser.error("--slots must be 1 with ORMAS_PROTECTED_EVIDENCE_SOURCE")
    sign_fn, pubkey = make_ed25519_signer(private_key)
    client = OrmasValidatorClient(base_url=args.gateway, token=token)
    try:
        protected = (ProtectedAssignmentAttestor(
                         client, evidence_fn, signer_pubkey=bytes.fromhex(pubkey))
                     if evidence_fn is not None else None)
        daemon = ValidatorDaemon(client, ValidatorConfig(workdir_root=Path(args.workdir_root)),
                                 sign_fn, protected=protected)
        daemon.register(pubkey_hex=pubkey)
        if args.once:
            return 0 if daemon.run_once() else 3
        daemon.serve(args.slots, args.poll_interval_s)
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
