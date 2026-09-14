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

import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .skeleton import GitError, _path_covered_by_allowed, _run_git, _run_verify_command

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
) -> dict[str, Any]:
    """The §4.2 evidence fields a validator signs over. Must byte-match the

    gateway's copy of this function (see module docstring) — never add a field
    here without updating both.
    """
    return {
        "job_id": job_id,
        "miner_id": miner_id,
        "base_commit": base_commit,
        "result_commit": result_commit,
        "repo_url": repo_url or "",
        "verify_command": verify_command,
        "allowed_paths": list(allowed_paths),
        "immutable_paths": list(immutable_paths),
    }


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

    def register(self, *, pubkey_hex: str) -> dict[str, Any]:
        resp = self._client.post(
            "/api/validator/v1/registrations",
            json={"schema_version": VALIDATOR_PROTOCOL_V1, "pubkey_hex": pubkey_hex},
        )
        resp.raise_for_status()
        return resp.json()

    def list_assignments(self) -> list[dict[str, Any]]:
        resp = self._client.get("/api/validator/v1/assignments")
        resp.raise_for_status()
        payload = resp.json()
        assignments = payload.get("assignments") if isinstance(payload, Mapping) else None
        return list(assignments) if isinstance(assignments, list) else []

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


class ValidatorDaemon:
    """Poll -> clone -> re-verify base(non-zero)+result(zero) -> scope check -> sign -> post.

    The verifier is credential-free and reuses ``skeleton._run_verify_command``
    (bounded env, no shell, no ambient secrets) — the same sandboxing a miner's
    own verify run gets, per decision 8 (validator on the same perimeter).
    """

    def __init__(self, client: OrmasValidatorClient, config: ValidatorConfig, sign_fn: SignFn) -> None:
        self.client = client
        self.config = config
        self.sign_fn = sign_fn

    def register(self, *, pubkey_hex: str) -> dict[str, Any]:
        return self.client.register(pubkey_hex=pubkey_hex)

    def _clone(self, assignment: Mapping[str, Any]) -> Path:
        workdir = self.config.workdir_root / assignment["assignment_id"]
        if workdir.exists():
            # Leftover half-created workdir from a failed attempt — replace it.
            shutil.rmtree(workdir)
        workdir.parent.mkdir(parents=True, exist_ok=True)
        try:
            _run_git(["clone", str(assignment["repo_url"]), str(workdir)], cwd=workdir.parent)
        except GitError:
            shutil.rmtree(workdir, ignore_errors=True)  # drop git's partial clone
            raise
        return workdir

    def _decide(self, assignment: Mapping[str, Any], workdir: Path) -> str:
        """Independent accept/reject/error — never the miner's self-report.

        ``error`` means the VALIDATOR could not complete its own review (a git
        failure); it is excluded from quorum (``outcomes_validators.resolve_quorum``)
        rather than counted against the miner.
        """
        base_commit = str(assignment["base_commit"])
        result_commit = str(assignment["result_commit"])
        verify_command = str(assignment["verify_command"])
        allowed = list(assignment.get("allowed_paths") or [])
        immutable = list(assignment.get("immutable_paths") or [])

        try:
            _run_git(["checkout", base_commit], cwd=workdir)
        except GitError:
            return "error"
        base_exit = _run_verify_command(verify_command, cwd=workdir)

        try:
            _run_git(["checkout", result_commit], cwd=workdir)
        except GitError:
            return "error"
        result_exit = _run_verify_command(verify_command, cwd=workdir)

        try:
            diff_out = _run_git(["diff", "--name-only", base_commit, result_commit], cwd=workdir)
        except GitError:
            return "error"
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

    def run_once(self) -> bool:
        """Review one pending assignment if any. False when idle."""
        assignments = self.client.list_assignments()
        if not assignments:
            return False
        assignment = assignments[0]
        try:
            workdir = self._clone(assignment)
        except GitError:
            # The validator could not obtain the repo — its own review failed,
            # so this is an "error" decision (see _decide), never a crash.
            decision = "error"
        else:
            decision = self._decide(assignment, workdir)

        fields = canonical_evidence_fields(
            job_id=str(assignment["job_id"]),
            miner_id=str(assignment["miner_id"]),
            base_commit=str(assignment["base_commit"]),
            result_commit=str(assignment["result_commit"]),
            repo_url=assignment.get("repo_url"),
            verify_command=str(assignment["verify_command"]),
            allowed_paths=list(assignment.get("allowed_paths") or []),
            immutable_paths=list(assignment.get("immutable_paths") or []),
        )
        digest = evidence_digest_hex(fields)
        if digest != assignment.get("evidence_digest_sha256"):
            # The assignment we reviewed doesn't match what the gateway signed
            # over — never sign a decision on evidence we can't reproduce.
            decision = "error"
        signature_hex = self.sign_fn(digest)
        self.client.post_decision(assignment["assignment_id"], decision=decision, signature_hex=signature_hex)
        return True
