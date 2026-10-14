"""Reference validator daemon — poll assignments, independently re-verify, sign, post.

Reference validator. Same untrusted-perimeter shape as ``skeleton.py``'s
miner loop (``docs/design/miner_delivers_outcomes_decision_2026_09_10.md`` decision
8: a validator inherits the miner's own repository-access model) — it clones the
same ``repo_url``, runs the same ``verify_command`` the miner ran, and reaches its
own accept/reject conclusion from git ground truth, never from the miner's
self-report. See ``docs/design/validator_acceptance_v2_2026_09_10.md`` §4.2-§4.3.

``canonical_evidence_fields``/``evidence_digest_hex`` and
``decision_signature_message`` below are a COPY of
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
from urllib.parse import quote

from ._gateway_url import require_secure_gateway_url
from ._workdir import contained_workdir, require_safe_component
from .public_acceptance import SUPPORTED_ACCEPTANCE_CONTRACTS, validate_acceptance_contract
from .skeleton import (
    DEFAULT_POLL_INTERVAL_S,
    GitError,
    _app_read_expired,
    _credential_git_env,
    _refreshed_app_read,
    _path_covered_by_allowed,
    _nul_git_paths,
    _run_git,
    _run_verify_command,
)
from .verification_context import evidence_root

__all__ = [
    "VALIDATOR_PROTOCOL_V1",
    "VALIDATOR_DECISION_PROTOCOL_V1",
    "VALIDATOR_DECISION_PROTOCOL_V2",
    "SUPPORTED_VALIDATOR_DECISION_PROTOCOLS",
    "canonical_evidence_fields",
    "decision_signature_message",
    "evidence_digest_hex",
    "make_ed25519_signer",
    "SignFn",
    "OrmasValidatorClient",
    "ValidatorConfig",
    "ValidatorDaemon",
]

VALIDATOR_PROTOCOL_V1 = "ormas-validator-v1"
VALIDATOR_DECISION_PROTOCOL_V1 = "ormas-validator-decision-v1"
VALIDATOR_DECISION_PROTOCOL_V2 = "ormas-validator-decision-v2"
SUPPORTED_VALIDATOR_DECISION_PROTOCOLS = (
    VALIDATOR_DECISION_PROTOCOL_V1,
    VALIDATOR_DECISION_PROTOCOL_V2,
)
_VALIDATOR_DECISION_V2_DOMAIN = "ormas-validator-decision-v2\x00"
_VALIDATOR_DECISIONS = frozenset({"accept", "reject", "error"})

# Decision message (UTF-8) or domain-separated re-key bytes -> signature_hex.
SignFn = Callable[[str | bytes], str]
REKEY_SIGNATURE_DOMAIN = b"ormas-validator-rekey-v1"
_SINGLE_SLOT_ERROR = "checker review requires exactly one slot"


def _require_single_checker_slot(slots: object) -> None:
    """Disable same-UID parallel review around credentials and signing authority.

    ``bool`` and numeric aliases are refused so direct callers cannot bypass the
    same exact-one invariant the CLI enforces.
    """
    if type(slots) is not int or slots != 1:
        raise ValueError(_SINGLE_SLOT_ERROR)


def require_assignment_id(assignment_id: Any) -> str:
    """Accept generated and legacy assignment ids only as safe path components."""
    return require_safe_component(assignment_id, field_name="assignment_id")


def _assignment_path_segment(assignment_id: Any) -> str:
    return quote(require_assignment_id(assignment_id), safe="")


def _assignment_id_or_none(assignment_id: Any) -> str | None:
    try:
        return require_assignment_id(assignment_id)
    except ValueError:
        return None


# ── execution contract ────────────────────────────────────────────────────────
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


def decision_signature_message(
    *, protocol_version: str, validator_id: str, assignment_id: str,
    decision: str, evidence_digest_sha256: str, decision_nonce: str = "",
    validator_key_generation: int | None = None,
    validator_pubkey_hex: str | None = None,
    decision_expires_at: str | None = None,
) -> str:
    """Return the exact message signed for one validator decision.

    V1 is retained only to drain assignments created by a pre-v2 gateway. V2
    domain-separates a closed canonical envelope and binds the authenticated
    validator identity, the assignment attempt, the exact verdict and the
    gateway-frozen evidence digest.
    """
    if not isinstance(evidence_digest_sha256, str) or _SHA256_HEX_RE.fullmatch(
        evidence_digest_sha256
    ) is None:
        raise ValueError("invalid evidence digest")
    if protocol_version == VALIDATOR_DECISION_PROTOCOL_V1:
        return evidence_digest_sha256
    if protocol_version != VALIDATOR_DECISION_PROTOCOL_V2:
        raise ValueError("unsupported validator decision protocol")
    if not isinstance(validator_id, str) or not validator_id:
        raise ValueError("invalid validator identity")
    if not isinstance(assignment_id, str) or not assignment_id:
        raise ValueError("invalid assignment identity")
    if not isinstance(decision_nonce, str) or _SHA256_HEX_RE.fullmatch(decision_nonce) is None:
        raise ValueError("invalid decision nonce")
    if type(validator_key_generation) is not int or validator_key_generation <= 0:
        raise ValueError("invalid validator key generation")
    try:
        normalized_pubkey = bytes.fromhex(validator_pubkey_hex or "").hex()
    except (TypeError, ValueError):
        raise ValueError("invalid validator public key") from None
    if len(normalized_pubkey) != 64 or normalized_pubkey != validator_pubkey_hex:
        raise ValueError("invalid validator public key")
    if not isinstance(decision_expires_at, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", decision_expires_at,
    ):
        raise ValueError("invalid decision expiry")
    if decision not in _VALIDATOR_DECISIONS:
        raise ValueError("invalid validator decision")
    envelope = {
        "assignment_id": assignment_id,
        "decision": decision,
        "decision_expires_at": decision_expires_at,
        "decision_nonce": decision_nonce,
        "evidence_digest_sha256": evidence_digest_sha256,
        "protocol_version": VALIDATOR_DECISION_PROTOCOL_V2,
        "validator_id": validator_id,
        "validator_key_generation": validator_key_generation,
        "validator_pubkey_hex": normalized_pubkey,
    }
    return _VALIDATOR_DECISION_V2_DOMAIN + json.dumps(
        envelope, sort_keys=True, separators=(",", ":"),
    )


def _assignment_decision_protocol(assignment: Mapping[str, Any]) -> str:
    """Resolve an explicit wire protocol without downgrade inference."""
    version = assignment.get("decision_protocol_version")
    if version == VALIDATOR_DECISION_PROTOCOL_V1:
        if assignment.get("decision_nonce") is not None:
            raise ValueError("invalid legacy decision nonce")
        return VALIDATOR_DECISION_PROTOCOL_V1
    if version == VALIDATOR_DECISION_PROTOCOL_V2:
        nonce = assignment.get("decision_nonce")
        if not isinstance(nonce, str) or _SHA256_HEX_RE.fullmatch(nonce) is None:
            raise ValueError("invalid decision nonce")
        generation = assignment.get("validator_key_generation")
        if type(generation) is not int or generation <= 0:
            raise ValueError("invalid validator key generation")
        pubkey = assignment.get("validator_pubkey_hex")
        try:
            normalized_pubkey = bytes.fromhex(pubkey).hex() if isinstance(pubkey, str) else ""
        except ValueError:
            normalized_pubkey = ""
        if len(normalized_pubkey) != 64 or normalized_pubkey != pubkey:
            raise ValueError("invalid validator public key")
        expires_at = assignment.get("decision_expires_at")
        if not isinstance(expires_at, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", expires_at,
        ):
            raise ValueError("invalid decision expiry")
        return VALIDATOR_DECISION_PROTOCOL_V2
    raise ValueError("unsupported validator decision protocol")


def make_ed25519_signer(private_key_hex: str) -> tuple[SignFn, str]:
    """Build a ``(sign_fn, pubkey_hex)`` pair from a raw 32-byte Ed25519 private

    key, hex-encoded. String messages are signed as UTF-8 bytes, matching the
    gateway verifier. Bytes are signed directly for the domain-separated re-key
    proof. New decisions sign :func:`decision_signature_message`; legacy drain
    assignments retain their digest-only message.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private_key_hex))
    pubkey_hex = key.public_key().public_bytes_raw().hex()

    def sign_fn(message: str | bytes) -> str:
        raw = message.encode("utf-8") if isinstance(message, str) else message
        return key.sign(raw).hex()

    return sign_fn, pubkey_hex


class OrmasValidatorClient:
    """Thin HTTP client for the gateway's ``/api/validator/v1`` routes.

    Same injectable-transport shape as ``client.OrmasMinerClient`` (any object
    with ``.post``/``.get`` returning ``.status_code``/``.json()``/
    ``.raise_for_status()``) so tests never touch a real network.
    """

    def __init__(self, base_url: str, token: str, *, http_client: Any | None = None) -> None:
        self.base_url = require_secure_gateway_url(
            base_url, allow_local_transport=http_client is not None,
        )
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
                follow_redirects=False,
                trust_env=False,
            )
            self._owns_client = True

    def register(
        self, *, pubkey_hex: str, task_cells: list[str] | None = None,
        current_sign_fn: SignFn | None = None,
    ) -> dict[str, Any]:
        """Register; on a re-key refusal, prove the retained CURRENT key once.

        ``current_sign_fn`` signs raw bytes, not the decision digest encoding.
        With no retained signer, surface the refusal rather than try the new key.
        """
        body: dict[str, Any] = {
            "schema_version": VALIDATOR_PROTOCOL_V1,
            "pubkey_hex": pubkey_hex,
            "acceptance_contracts": list(SUPPORTED_ACCEPTANCE_CONTRACTS),
            "decision_protocol_versions": list(SUPPORTED_VALIDATOR_DECISION_PROTOCOLS),
        }
        if task_cells is not None:
            # Only a checker that can attest advertises the Protected cell.
            body["task_cells"] = list(task_cells)
        resp = self._client.post("/api/validator/v1/registrations", json=body)
        if resp.status_code == 409 and current_sign_fn is not None:
            error = resp.json().get("error")
            if (isinstance(error, Mapping) and error.get("type") == "conflict_error"
                    and error.get("message") == "validator_rekey_proof_required"):
                challenge = self.request_rekey_challenge()
                nonce, new_key = bytes.fromhex(challenge["nonce_hex"]), bytes.fromhex(pubkey_hex)
                if len(nonce) != 32 or len(new_key) != 32:
                    raise ValueError("invalid validator re-key nonce or key")
                body["rekey_proof"] = {
                    "challenge_id": challenge["challenge_id"],
                    "signature": current_sign_fn(REKEY_SIGNATURE_DOMAIN + nonce + new_key),
                }
                resp = self._client.post("/api/validator/v1/registrations", json=body)
        resp.raise_for_status()
        return resp.json()

    def request_rekey_challenge(self) -> dict[str, Any]:
        resp = self._client.post(
            "/api/validator/v1/rekey-challenge", json={"schema_version": VALIDATOR_PROTOCOL_V1})
        resp.raise_for_status()
        return resp.json()

    def request_attestation_challenge(self, assignment_id: str) -> dict[str, Any]:
        """One-use nonce plus the ``job_id``/``attempt`` binding for a Protected assignment."""
        segment = _assignment_path_segment(assignment_id)
        resp = self._client.post(
            f"/api/validator/v1/assignments/{segment}/attestation-challenge",
            json={"schema_version": VALIDATOR_PROTOCOL_V1},
        )
        resp.raise_for_status()
        return resp.json()

    def release_sealed_credential(
        self, assignment_id: str, *, challenge_nonce: str, evidence: Mapping[str, Any],
        enclave_pubkey: str,
    ) -> dict[str, Any]:
        """The read token sealed to the attested enclave key."""
        segment = _assignment_path_segment(assignment_id)
        resp = self._client.post(
            f"/api/validator/v1/assignments/{segment}/repo-credential",
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
        segment = _assignment_path_segment(assignment_id)
        resp = self._client.post(
            f"/api/validator/v1/assignments/{segment}/repo-credential",
            json={"schema_version": VALIDATOR_PROTOCOL_V1},
        )
        resp.raise_for_status()
        return resp.json()

    def post_decision(self, assignment_id: str, *, decision: str, signature_hex: str) -> dict[str, Any]:
        segment = _assignment_path_segment(assignment_id)
        resp = self._client.post(
            f"/api/validator/v1/assignments/{segment}/decisions",
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


def _with_usable_read_credential(client: Any, assignment: Mapping[str, Any]) -> Mapping[str, Any]:
    """The assignment with its expired App read token replaced for this checker.

    Only an expired App token is refreshed, through the checker's own assignment.
    Without a client the expired token is kept and the read refuses it. A refused
    refresh raises ``GitError``.
    """
    if not _app_read_expired(assignment.get("repo_credential")) or client is None:
        return assignment
    fresh = _refreshed_app_read(
        lambda: client.read_repository_credential(assignment["assignment_id"]))
    return {**assignment, "repo_credential": fresh}


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

    def register(self, *, pubkey_hex: str, current_sign_fn: SignFn | None = None) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"pubkey_hex": pubkey_hex}
        if current_sign_fn is not None:
            kwargs["current_sign_fn"] = current_sign_fn
        if self.protected is not None:
            from .protected_attestation import PROTECTED_SERVICE_CELL
            kwargs["task_cells"] = [PROTECTED_SERVICE_CELL]
        return self.client.register(**kwargs)

    def _attest(self, assignment_id: str) -> Any:
        """Open one sealed read token for this assignment; raises ``GitError`` when refused."""
        from .protected_attestation import ProtectedAttestationError
        assignment_id = require_assignment_id(assignment_id)
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

        valid_assignment_id = _assignment_id_or_none(assignment_id)
        if valid_assignment_id is None:
            return False
        assignment_id = valid_assignment_id
        token = self._protected_tokens.get(assignment_id)
        try:
            return token is not None and datetime.fromisoformat(
                token.expires_at.replace("Z", "+00:00")) > datetime.now(timezone.utc)
        except (TypeError, ValueError, AttributeError):
            return False

    def _protected_clone(self, assignment: Mapping[str, Any], workdir: Path) -> None:
        from .outcomes_support import protected_repository_git
        assignment_id = require_assignment_id(assignment.get("assignment_id"))
        if self._token_valid(assignment_id):
            token = self._protected_tokens[assignment_id]
        else:
            token = self._attest(assignment_id)
        repo_url = assignment.get("repo_url")
        protected_repository_git(
            ["clone", "--no-checkout", repo_url, str(workdir)], cwd=workdir.parent,
            repo_url=repo_url, token=token.token, expires_at=token.expires_at)

    def _clone(self, assignment: Mapping[str, Any]) -> Path:
        workdir = contained_workdir(
            self.config.workdir_root,
            assignment.get("assignment_id"),
            field_name="assignment_id",
        )
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
        protected_id = _assignment_id_or_none(assignment.get("assignment_id"))
        token = self._protected_tokens.get(protected_id) if protected_id is not None else None
        if token is not None:
            detail = detail.replace(token.token, "[redacted]")
        kind = type(cause).__name__ if isinstance(cause, BaseException) else "verification"
        logging.getLogger(__name__).warning(
            "%s failed for assignment %s: %s: %s",
            action, _assignment_id_or_none(assignment.get("assignment_id")) or "-", kind,
            " ".join(detail.splitlines()),
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
        if _assignment_id_or_none(assignment.get("assignment_id")) is None:
            return "error"
        try:
            public_execution = validate_assignment_execution(assignment)
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            return self._review_error(assignment, exc)
        base_commit = str(assignment["base_commit"])
        result_commit = str(assignment["result_commit"])
        verify_command = str(assignment["verify_command"])
        allowed = list(assignment.get("allowed_paths") or [])
        immutable = list(assignment.get("immutable_paths") or [])
        validation_run_id = uuid.uuid4().hex

        try:
            diff_out = _run_git(
                ["diff", "--no-ext-diff", "--no-textconv",
                 "--name-only", "--no-renames", "-z",
                 base_commit, result_commit, "--"],
                cwd=workdir,
            )
        except GitError as exc:
            return self._review_error(assignment, exc)
        # Decide scope before toolchain provisioning or either verifier can run
        # repository-controlled code.  Keep this result rather than consulting
        # mutable Git metadata after an untrusted process has started.
        try:
            changed = list(_nul_git_paths(diff_out))
        except GitError as exc:
            return self._review_error(assignment, exc)
        touches_immutable = bool(immutable) and any(
            _path_covered_by_allowed(p, immutable) for p in changed
        )
        scope_ok = (not allowed) or all(_path_covered_by_allowed(p, allowed) for p in changed)

        if touches_immutable or not scope_ok:
            return "reject"

        try:
            prefix = None if public_execution else provision_toolchain(
                assignment.get("toolchain"), workdir
            )
        except (ToolchainUnavailable, ValueError, TypeError, KeyError, AttributeError) as exc:
            return self._review_error(assignment, exc)

        try:
            _run_git(["checkout", base_commit], cwd=workdir)
        except GitError as exc:
            return self._review_error(assignment, exc)
        def verify(phase):
            if evidence_root(verify_command) is None:
                return _run_verify_command(verify_command, cwd=workdir, path_prefix=prefix,
                                           raise_setup_errors=True,
                                           trusted_public_python=public_execution)
            return _run_verify_command(verify_command, cwd=workdir, path_prefix=prefix,
                                       raise_setup_errors=True,
                                       trusted_public_python=public_execution,
                                       evidence_identity={
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
        if _assignment_id_or_none(assignment.get("assignment_id")) is None:
            return ("skip", None)
        expected_digest = assignment.get("evidence_digest_sha256")
        if not isinstance(expected_digest, str) or _SHA256_HEX_RE.fullmatch(expected_digest) is None:
            # There is no valid assignment identity to acknowledge. Never sign
            # an invented empty digest or access a repository for this row.
            return ("skip", None)
        try:
            protocol = _assignment_decision_protocol(assignment)
            if protocol == VALIDATOR_DECISION_PROTOCOL_V2:
                validator_id = assignment.get("validator_id")
                if not isinstance(validator_id, str) or not validator_id:
                    raise ValueError("missing validator identity")
        except ValueError:
            # There is no agreed message to sign. Do not access the repository
            # and do not reinterpret an unknown version as the legacy format.
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

    @staticmethod
    def _signature_message(
        assignment: Mapping[str, Any], digest: str, decision: str,
    ) -> str:
        return decision_signature_message(
            protocol_version=_assignment_decision_protocol(assignment),
            validator_id=str(assignment.get("validator_id") or ""),
            assignment_id=str(assignment["assignment_id"]),
            decision_nonce=str(assignment.get("decision_nonce") or ""),
            validator_key_generation=assignment.get("validator_key_generation"),
            validator_pubkey_hex=assignment.get("validator_pubkey_hex"),
            decision_expires_at=assignment.get("decision_expires_at"),
            decision=decision,
            evidence_digest_sha256=digest,
        )

    def _sign_and_post(self, assignment: Mapping[str, Any], digest: str, decision: str) -> None:
        # An error acknowledges the original assignment identity, including a
        # malformed contract. It cannot enter the acceptance quorum, and avoids
        # retrying a deterministic invalid assignment through an empty signature.
        assignment_id = require_assignment_id(assignment.get("assignment_id"))
        signature_hex = self.sign_fn(self._signature_message(assignment, digest, decision))
        self.client.post_decision(
            assignment_id, decision=decision, signature_hex=signature_hex,
        )

    def run_once(self) -> bool:
        """Review one pending assignment if any. False when idle."""
        assignments = self.client.list_assignments()
        if self.protected is None:
            # Only an attesting checker can act on a Protected stub.
            assignments = [a for a in assignments if not a.get("attestation_required")]
        else:
            # A checker holding Protected release authority must never process
            # work from a less isolated service cell. Keep that key and bearer
            # confined to assignments the gateway explicitly labels Protected.
            assignments = [a for a in assignments if a.get("service_level") == "protected"]
        if not assignments:
            return False
        assignment = None
        attested = False
        for row in assignments:
            if not isinstance(row, Mapping):
                continue
            assignment_id = _assignment_id_or_none(row.get("assignment_id"))
            if assignment_id is None:
                continue
            if not row.get("attestation_required"):
                assignment = row
                break
            # Phase one: attest for this stub; a later poll lists its evidence. A stub
            # already attested with a live token waits for that listing, and a refused
            # stub never blocks the rows after it; both retry on the next poll.
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
        self._protected_tokens.pop(assignment_id, None)
        return True

    def serve(
        self,
        slots: int,
        poll_interval_s: float,
        *,
        until: Callable[[], bool] | None = None,
    ) -> None:
        """Review sequentially until stopped; parallel review is fail-closed."""
        import time

        _require_single_checker_slot(slots)
        while True:
            if until is not None and until():
                return
            worked = self.run_once()
            if until is not None and until():
                return
            if not worked:
                time.sleep(poll_interval_s)

def _load_cli_secret(*, token_env: str | None, token_path: str | None) -> str:
    """Read a service credential without following links or exposing its bytes."""
    from .client import load_token
    from ._private_file import read_private_text

    if token_path is None:
        return load_token(token_env=token_env)
    value = read_private_text(token_path, label="validator credential", strip=True)
    if not value:
        raise ValueError("validator credential must be a private regular file owned by the service user")
    return value


def _single_slot_count(value: str) -> int:
    import argparse

    if value != "1":
        raise argparse.ArgumentTypeError("--slots must be exactly 1")
    return 1


def _assignment_timeout_s(assignment: Mapping[str, Any]) -> Any:
    """The review timeout from ``acceptance_contract.policy.timeout_s``, if any."""
    contract = assignment.get("acceptance_contract")
    if not isinstance(contract, Mapping):
        return None
    policy = contract.get("policy")
    if not isinstance(policy, Mapping):
        return None
    return policy.get("timeout_s")


def _list_only_lines(assignments: Any) -> list[str]:
    """One stable stdout line per assignment; ``-`` marks a missing field."""
    rows = list(assignments) if isinstance(assignments, (list, tuple)) else []
    if not rows:
        return ["no assignments"]
    lines = []
    for assignment in rows:
        record = assignment if isinstance(assignment, Mapping) else {}
        assignment_id = _assignment_id_or_none(record.get("assignment_id"))
        job_id = record.get("job_id")
        timeout_s = _assignment_timeout_s(record)
        lines.append(
            f"{assignment_id if assignment_id else '-'} "
            f"job={job_id if job_id else '-'} "
            f"timeout_s={timeout_s if timeout_s else '-'}"
        )
    return lines


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
    current_key_args = parser.add_mutually_exclusive_group()
    current_key_args.add_argument("--current-private-key-env", help="Env var holding the old key for re-key proof")
    current_key_args.add_argument("--current-private-key-file", help="Private file holding the old key for re-key proof")
    parser.add_argument("--workdir-root", default=str(Path.cwd() / "ormas-validator-work"))
    parser.add_argument("--once", action="store_true", help="Review one assignment, exit 3 if idle")
    parser.add_argument(
        "--list-only", action="store_true",
        help="Print current assignments (id, job, review timeout) and exit without reviewing",
    )
    parser.add_argument(
        "--slots", type=_single_slot_count, default=1,
        help="exactly 1 assignment review (default: 1)",
    )
    parser.add_argument("--poll-interval-s", type=float, default=15.0)
    args = parser.parse_args(argv)
    if not 0 < args.poll_interval_s < float("inf"):
        parser.error("--poll-interval-s must be finite and positive")
    try:
        args.gateway = require_secure_gateway_url(args.gateway)
    except ValueError as exc:
        parser.error(str(exc))
    token = _load_cli_secret(token_env=args.token_env, token_path=args.token_file)
    if args.token_env:
        os.environ.pop(args.token_env, None)
    private_key = _load_cli_secret(token_env=args.private_key_env, token_path=args.private_key_file)
    if args.private_key_env:
        os.environ.pop(args.private_key_env, None)
    current_sign_fn = None
    if args.current_private_key_env or args.current_private_key_file:
        current_key = _load_cli_secret(token_env=args.current_private_key_env,
                                       token_path=args.current_private_key_file)
        if args.current_private_key_env:
            os.environ.pop(args.current_private_key_env, None)
        current_sign_fn, _ = make_ed25519_signer(current_key)
    from .protected_attestation import ProtectedAssignmentAttestor, evidence_fn_from_env
    try:
        evidence_fn = evidence_fn_from_env(os.environ)
    except ValueError as exc:
        parser.error(str(exc))
    sign_fn, pubkey = make_ed25519_signer(private_key)
    client = OrmasValidatorClient(base_url=args.gateway, token=token)
    try:
        protected = (ProtectedAssignmentAttestor(
                         client, evidence_fn, signer_pubkey=bytes.fromhex(pubkey))
                     if evidence_fn is not None else None)
        # List-only never clones, reviews, posts, reads a credential, or creates
        # the review workdir; it only reports the current assignments.
        registrar = (ValidatorDaemon(client, ValidatorConfig(workdir_root=Path(args.workdir_root)),
                                     sign_fn, protected=protected)
                     if not args.list_only else None)

        def _register() -> None:
            if registrar is not None:
                if current_sign_fn is None:
                    registrar.register(pubkey_hex=pubkey)
                else:
                    registrar.register(pubkey_hex=pubkey, current_sign_fn=current_sign_fn)
            else:
                if protected is None:
                    client.register(pubkey_hex=pubkey)
                else:
                    from .protected_attestation import PROTECTED_SERVICE_CELL
                    client.register(pubkey_hex=pubkey, task_cells=[PROTECTED_SERVICE_CELL])

        _register()
        if args.list_only:
            for line in _list_only_lines(client.list_assignments()):
                print(line)
            return 0
        daemon = registrar
        assert daemon is not None
        if args.once:
            return 0 if daemon.run_once() else 3
        daemon.serve(args.slots, args.poll_interval_s)
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
