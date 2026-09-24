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
import os
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .public_acceptance import validate_acceptance_contract
from .skeleton import (
    GitError,
    _credential_git_env,
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
            # The read credential is served by the gateway per assignment and exists on disk only for this clone; later checkout/diff need no credential.
            with _credential_git_env(assignment.get("repo_credential")) as env:
                _run_git(["clone", str(assignment["repo_url"]), str(workdir)], cwd=workdir.parent, env=env)
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
        try:
            public_execution = validate_assignment_execution(assignment)
            prefix = None if public_execution else provision_toolchain(assignment.get("toolchain"), workdir)
        except (ToolchainUnavailable, ValueError, TypeError, KeyError, AttributeError):
            return "error"
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
            except GitError:
                return "error"

        try:
            _run_git(["checkout", base_commit], cwd=workdir)
        except GitError:
            return "error"
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
        except (ValueError, OSError):
            return "error"
        if public_execution and base_exit != 86:
            # The frozen preflight could not be reproduced. An already-green
            # or broken base is no evidence of a miner's implementation quality.
            return "error"
        if evidence_root(verify_command) is not None and base_exit in (74, 127):
            return "error"  # A capture/setup refusal is not an intended red base.

        try:
            _run_git(["checkout", result_commit], cwd=workdir)
        except GitError:
            return "error"
        try:
            result_exit = verify("result")
        except (ValueError, OSError):
            return "error"
        if public_execution:
            return {0: "accept", 86: "reject"}.get(result_exit, "error")
        if evidence_root(verify_command) is not None and result_exit in (74, 127):
            return "error"

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
        decision = "error"
        expected_digest = assignment.get("evidence_digest_sha256")
        if not isinstance(expected_digest, str) or _SHA256_HEX_RE.fullmatch(expected_digest) is None:
            # There is no valid assignment identity to acknowledge. Never sign
            # an invented empty digest or access a repository for this row.
            return True
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
            pass
        else:
            try:
                workdir = self._clone(assignment)
            except GitError:
                pass
            else:
                decision = self._decide(assignment, workdir)
        # An error acknowledges the original assignment identity, including a
        # malformed contract. It cannot enter the acceptance quorum, and avoids
        # retrying a deterministic invalid assignment through an empty signature.
        signature_hex = self.sign_fn(expected_digest)
        self.client.post_decision(assignment["assignment_id"], decision=decision, signature_hex=signature_hex)
        return True


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


def main(argv: list[str] | None = None) -> int:
    """Run the installed task-checker component of the SN76 validator."""
    import argparse
    import time

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
    parser.add_argument("--poll-interval-s", type=float, default=15.0)
    args = parser.parse_args(argv)
    if not 0 < args.poll_interval_s < float("inf"):
        parser.error("--poll-interval-s must be finite and positive")
    token = _load_cli_secret(token_env=args.token_env, token_path=args.token_file)
    private_key = _load_cli_secret(token_env=args.private_key_env, token_path=args.private_key_file)
    sign_fn, pubkey = make_ed25519_signer(private_key)
    client = OrmasValidatorClient(base_url=args.gateway, token=token)
    try:
        daemon = ValidatorDaemon(client, ValidatorConfig(workdir_root=Path(args.workdir_root)), sign_fn)
        daemon.register(pubkey_hex=pubkey)
        if args.once:
            return 0 if daemon.run_once() else 3
        while True:
            if not daemon.run_once():
                time.sleep(args.poll_interval_s)
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
