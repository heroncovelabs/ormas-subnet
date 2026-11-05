"""Standalone split sealed-env codecs. No monorepo imports or secret diagnostics."""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import socket
import tempfile
from pathlib import Path

from ._private_file import read_private_text

# Ordered duplicate of protected/miner/compose_hash.py, checked by PW-5 parity tests.
# The CLI must also work from a wheel without the deployment recipe attached.
SPLIT_ALLOWED_ENVS = (
    "ORMAS_API_URL", "ORMAS_RUNNER_TOKEN", "ORMAS_MINER_ID", "ORMAS_RUNTIME",
    "ORMAS_AUTHORIZATION_JSON", "ORMAS_CELL_BOUNDS", "ORMAS_APPROVED_BY",
    "ORMAS_TASK_CELLS", "ORMAS_BIND_PROJECT_ID",
    "MINER_WORKER_IMAGE", "MINER_WORKER_ENV_JSON", "MINER_WORKER_REGISTRY_AUTH",
    "ORMAS_PROTECTED_RELAY_POLICY_JSON", "SAYGM_API_KEY", "ENGY_API_KEY",
)
# Shell-only relay inputs (empty = CONNECT mode). Duplicates of
# tensorbox_spec/privacy/inference_relay.RelayPolicy, checked by parity tests.
RELAY_POLICY_SCHEMA = "ormas.protected-inference-relay-policy.v1"
_RELAY_DIGEST_DOMAIN = b"ormas.protected-inference-relay-policy.v1\x00"
RELAY_PRESETS = {"saygm-frontier": "saygm-frontier", "engy-zdr": "engy"}
RELAY_KINDS = {
    "saygm-frontier": ("frontier_gateway", "SAYGM_API_KEY"),
    "engy": ("zero_retention", "ENGY_API_KEY"),
}
# EngyAdapter's approved canonical ids, not a default worker model ladder. In
# this image the engy-zdr route sends data_collection: deny and does not request
# zero-data-retention routing; that arrives with the next measured image.
_ENGY_MODELS = frozenset({
    "deepseek-v4.1-flash", "deepseek-v4-flash-0731", "qwen3.8-27b",
    "glm-5.3-flash", "glm-5.2", "glm-5.3", "kimi-k3",
})
_FRONTIER_MODEL = re.compile(r"[a-z0-9][a-z0-9.\-]*")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_CREDENTIAL = re.compile(r"[\x21-\x7e]+")
_RELAY_BLOCKED_PARTS = frozenset((
    "KEY", "KEYS", "TOKEN", "TOKENS", "SECRET", "SECRETS", "PASSWORD", "CREDENTIAL",
    "CREDENTIALS", "AUTH", "AUTHORIZATION", "BEARER", "COOKIE", "SESSION",
    "BASE", "URL", "URI", "ENDPOINT", "HOST", "PROXY", "ORG", "ORGANIZATION", "PROJECT",
))
_ENV_KEY = re.compile(r"[A-Z][A-Z0-9_]*")
_PROXY_KEYS = frozenset(("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY"))
WORKER_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
# Registration wire contract: runner_api._CHOSEN_MINER_ID_RE.
MINER_ID = re.compile(r"[a-z0-9][a-z0-9-]{2,39}")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def json_object(text):
    try:
        result = json.loads(text, object_pairs_hook=_unique_object)
    except (ValueError, TypeError, RecursionError):
        raise ValueError("Invalid JSON object") from None
    if not isinstance(result, dict):
        raise ValueError("Expected a JSON object")
    return result


def parse_worker_env(text, *, relay_mode: bool = False) -> dict[str, str]:
    """Same admission rules as worker_contract.parse_worker_env; values never echoed."""
    environment = json_object(text)
    for key, value in environment.items():
        if not _ENV_KEY.fullmatch(key):
            raise ValueError("Invalid worker environment key")
        if key in _PROXY_KEYS or key.startswith(("ORMAS_", "MINER_WORKER_")):
            raise ValueError("Reserved worker environment key")
        if relay_mode and _RELAY_BLOCKED_PARTS.intersection(key.split("_")):
            raise ValueError("Provider credential or routing key in relay mode")
        if not isinstance(value, str):
            raise ValueError("Worker environment values must be strings")
        if "\x00" in value:
            raise ValueError("Worker environment value contains NUL")
        try:
            value.encode("utf-8")
        except UnicodeError:
            raise ValueError("Worker environment value is not UTF-8") from None
    return environment


def relay_policy(provider: str, models, salt: str | None = None) -> tuple[str, str]:
    """(canonical policy JSON, policy digest) for a slot's private relay policy.

    The JSON carries the private model ids and binding salt and goes to the
    shell only; the digest is what the operator approves on the slot.
    """
    if provider not in RELAY_KINDS:
        raise ValueError("relay: unsupported relay policy kind")
    models = list(models)
    if (not models or len(set(models)) != len(models)
            or any(not isinstance(m, str) or not _FRONTIER_MODEL.fullmatch(m) for m in models)):
        raise ValueError("relay: model ids must be distinct plain ids")
    if provider == "engy" and any(model not in _ENGY_MODELS for model in models):
        raise ValueError("relay: model ids must be approved Engy canonical ids")
    salt = secrets.token_hex(32) if salt is None else salt
    if not isinstance(salt, str) or not _HEX64.fullmatch(salt):
        raise ValueError("relay: invalid binding salt")
    canonical = json.dumps({
        "schema": RELAY_POLICY_SCHEMA, "provider": provider,
        "privacy_class": RELAY_KINDS[provider][0], "models": sorted(models), "binding_salt": salt,
    }, sort_keys=True, separators=(",", ":"))
    return canonical, hashlib.sha256(_RELAY_DIGEST_DOMAIN + canonical.encode("utf-8")).hexdigest()


def read_relay_policy(text: str) -> tuple[str, str]:
    """Re-canonicalize a saved policy so its digest (and slot approval) stays stable."""
    data = json_object(text)
    if set(data) != {"schema", "provider", "privacy_class", "models", "binding_salt"} or (
            data["schema"] != RELAY_POLICY_SCHEMA or data["provider"] not in RELAY_KINDS
            or data["privacy_class"] != RELAY_KINDS[data["provider"]][0]
            or not isinstance(data["models"], list)):
        raise ValueError("relay: invalid relay policy file")
    return relay_policy(data["provider"], data["models"], data["binding_salt"])


def relay_credential(key: str, value: str) -> str:
    sealed_text(key, value)
    if not _CREDENTIAL.fullmatch(value):
        raise ValueError(f"{key}: invalid provider credential")
    return value


def sealed_text(key: str, value: str) -> str:
    """Phala free-text rule; JSON syntax quotes are handled by sealed_json."""
    if any(char in value for char in "$#'\"\r\n\x00"):
        raise ValueError(f"{key}: contains a forbidden character")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ValueError(f"{key}: must be UTF-8") from None
    return value


def sealed_json(key: str, value: dict) -> str:
    def check(node):
        if isinstance(node, str):
            sealed_text(key, node)
        elif isinstance(node, dict):
            for name, child in node.items():
                sealed_text(key, name)
                check(child)
        elif isinstance(node, list):
            for child in node:
                check(child)
    try:
        check(value)
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        raise ValueError(f"{key}: invalid sealed JSON string contents") from None


def read_input(path: str, key: str) -> str:
    return read_private_text(path, label=f"{key}: input file", max_bytes=1024 * 1024)


def provider_host(value: str) -> str:
    """Same DNS-name domain as the shell allowlist, not URLs or address literals."""
    sealed_text("declared-providers", value)
    try:
        host = value.lower().removesuffix(".").encode("idna").decode("ascii")
        if not host or len(host) > 253 or any(
                char not in "abcdefghijklmnopqrstuvwxyz0123456789.-" for char in host):
            raise ValueError
        for label in host.split("."):
            if not label or label.startswith("-") or label.endswith("-"):
                raise ValueError
            if label.startswith("xn--") and (
                    label.encode("ascii").decode("idna").encode("idna").decode("ascii") != label):
                raise ValueError
        try:
            socket.inet_aton(host)
        except OSError:
            return host
    except (UnicodeError, ValueError):
        pass
    raise ValueError("declared-providers: expected a provider DNS host")


def write_private(path: Path, text: str, *, force: bool) -> None:
    """Atomic private replacement; no-force publishes with an exclusive link."""
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=".sealed-", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        if force:
            os.replace(temporary, path)  # replace a symlink, never follow it
        else:
            os.link(temporary, path)  # EEXIST is a refusal, even after a race
    except FileExistsError:
        raise ValueError("output exists; use --force to overwrite") from None
    except OSError:
        raise ValueError("cannot write private output file") from None
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
