"""Standalone split sealed-env codecs. No monorepo imports or secret diagnostics."""
from __future__ import annotations

import json
import os
import re
import socket
import tempfile
from pathlib import Path

# Ordered duplicate of protected/miner/compose_hash.py, checked by PW-5 parity tests.
# The CLI must also work from a wheel without the deployment recipe attached.
SPLIT_ALLOWED_ENVS = (
    "ORMAS_API_URL", "ORMAS_RUNNER_TOKEN", "ORMAS_MINER_ID", "ORMAS_RUNTIME",
    "ORMAS_AUTHORIZATION_JSON", "ORMAS_CELL_BOUNDS", "ORMAS_APPROVED_BY",
    "ORMAS_TASK_CELLS", "ORMAS_BIND_PROJECT_ID",
    "MINER_WORKER_IMAGE", "MINER_WORKER_ENV_JSON", "MINER_WORKER_REGISTRY_AUTH",
)
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


def parse_worker_env(text) -> dict[str, str]:
    """Same admission rules as worker_contract.parse_worker_env; values never echoed."""
    environment = json_object(text)
    for key, value in environment.items():
        if not _ENV_KEY.fullmatch(key):
            raise ValueError("Invalid worker environment key")
        if key in _PROXY_KEYS or key.startswith(("ORMAS_", "MINER_WORKER_")):
            raise ValueError("Reserved worker environment key")
        if not isinstance(value, str):
            raise ValueError("Worker environment values must be strings")
        if "\x00" in value:
            raise ValueError("Worker environment value contains NUL")
        try:
            value.encode("utf-8")
        except UnicodeError:
            raise ValueError("Worker environment value is not UTF-8") from None
    return environment


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
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, ValueError):
        raise ValueError(f"{key}: cannot read input file") from None


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
