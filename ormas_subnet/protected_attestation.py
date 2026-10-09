"""Protected-level checker attestation inside the published validator CVM (MIT).

A Protected job's repository credential leaves the gateway only sealed to a key held by an
attested confidential VM. This module is the checker side of that exchange: ask the
gateway for a challenge on one assignment, obtain a TDX quote from the dstack guest agent
over ``nonce || binding digest``, post it, and open the sealed read token.

This package never imports ``tensorbox_spec`` (``tests/test_import_graph.py``), so the
wire-level pieces are restated here: the binding digest, the HPKE open, the dstack quote
call and the evidence wire. The monorepo's ``tests/privacy/test_validator_attestation_parity.py``
pins each one byte for byte against the gateway's own code. Design:
``coordination/attested_validator_8c10850e_2026_10_08.md``.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDFExpand

__all__ = [
    "PROTECTED_SERVICE_CELL",
    "VALIDATOR_ROLE",
    "SEALED_CREDENTIAL_SCHEME",
    "ProtectedAttestationError",
    "ProtectedReadToken",
    "ProtectedAssignmentAttestor",
    "DstackQuoteClient",
    "binding_digest",
    "evidence_fn_from_env",
    "generate_keypair",
    "open_sealed",
    "report_data_for_assignment",
    "seal",
]

_LOG = logging.getLogger("ormas_subnet.protected_attestation")

PROTECTED_SERVICE_CELL = "task:service/protected"
VALIDATOR_ROLE = "protected-validator"
SEALED_CREDENTIAL_SCHEME = "hpke-x25519-hkdf-sha256-aes256gcm-v1"
SEALED_CREDENTIAL_KIND = "github_app_read_token_sealed"
EVIDENCE_SOURCE_ENV = "ORMAS_PROTECTED_EVIDENCE_SOURCE"
DSTACK_SOCKET_PATH = "/var/run/dstack.sock"
NONCE_LEN = 32
REPORT_DATA_LEN = 64
_KEY_LEN = 32


class ProtectedAttestationError(RuntimeError):
    """A stable refusal reason with no credential, key or evidence material."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


# ── binding digest (same encoding as the gateway's compute_binding_digest) ──


def binding_digest(
    *, role: str, job_id: str, attempt: int, lease_or_assignment_id: str,
    plugin_digest: str, egress_list_digest: str, enclave_pubkey: bytes,
    signer_pubkey: bytes = b"",
) -> bytes:
    """SHA-256 over a domain tag and length-prefixed fields, in this fixed order.

    ``signer_pubkey`` (the checker's registered Ed25519 decision key) is appended
    only when present; the miner role binds none.
    """
    h = hashlib.sha256()
    h.update(b"tee-binding-digest-v1\0")
    blobs = [
        role.encode(), job_id.encode(), str(int(attempt)).encode(),
        lease_or_assignment_id.encode(), plugin_digest.encode(),
        egress_list_digest.encode(), bytes(enclave_pubkey),
    ]
    if signer_pubkey:
        blobs.append(bytes(signer_pubkey))
    for blob in blobs:
        h.update(len(blob).to_bytes(8, "big"))
        h.update(blob)
    return h.digest()


def report_data_for_assignment(
    nonce: bytes, *, job_id: str, attempt: int, assignment_id: str, enclave_pubkey: bytes,
    signer_pubkey: bytes,
) -> bytes:
    """``nonce || binding digest`` for a checker.

    A checker binds no plugin or egress digests; it binds the Ed25519 decision key it
    registered (``signer_pubkey``), which the gateway recomputes from its registration,
    so the quote proves that key lives in the attested CVM.
    """
    if not isinstance(nonce, bytes) or len(nonce) != NONCE_LEN:
        raise ValueError("nonce must be 32 bytes")
    if not isinstance(enclave_pubkey, bytes) or len(enclave_pubkey) != _KEY_LEN:
        raise ValueError("enclave public key must be 32 bytes")
    if not isinstance(signer_pubkey, bytes) or len(signer_pubkey) != _KEY_LEN:
        raise ValueError("signer public key must be 32 bytes")
    return nonce + binding_digest(
        role=VALIDATOR_ROLE, job_id=job_id, attempt=attempt,
        lease_or_assignment_id=assignment_id, plugin_digest="", egress_list_digest="",
        enclave_pubkey=enclave_pubkey, signer_pubkey=signer_pubkey,
    )


# ── HPKE base mode: DHKEM(X25519, HKDF-SHA256) / HKDF-SHA256 / AES-256-GCM ──

_VERSION = 1
_INFO = b"ormas-protected-credential-v1"
_KEM_ID, _KDF_ID, _AEAD_ID = 0x0020, 0x0001, 0x0002
_NH = 32
_RAW = (serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def _i2osp(value: int, length: int) -> bytes:
    return value.to_bytes(length, "big")


def _labeled_extract(suite: bytes, salt: bytes, label: bytes, ikm: bytes) -> bytes:
    return hmac.new(salt or bytes(_NH), b"HPKE-v1" + suite + label + ikm, hashlib.sha256).digest()


def _labeled_expand(suite: bytes, prk: bytes, label: bytes, info: bytes, length: int) -> bytes:
    labeled = _i2osp(length, 2) + b"HPKE-v1" + suite + label + info
    return HKDFExpand(algorithm=SHA256(), length=length, info=labeled).derive(prk)


def _shared_secret(dh: bytes, enc: bytes, pk_r: bytes) -> bytes:
    suite = b"KEM" + _i2osp(_KEM_ID, 2)
    prk = _labeled_extract(suite, b"", b"eae_prk", dh)
    return _labeled_expand(suite, prk, b"shared_secret", enc + pk_r, _NH)


def _key_schedule(shared: bytes) -> tuple[bytes, bytes]:
    suite = b"HPKE" + _i2osp(_KEM_ID, 2) + _i2osp(_KDF_ID, 2) + _i2osp(_AEAD_ID, 2)
    psk_id_hash = _labeled_extract(suite, b"", b"psk_id_hash", b"")
    info_hash = _labeled_extract(suite, b"", b"info_hash", _INFO)
    context = b"\x00" + psk_id_hash + info_hash
    secret = _labeled_extract(suite, shared, b"secret", b"")
    return (_labeled_expand(suite, secret, b"key", context, 32),
            _labeled_expand(suite, secret, b"base_nonce", context, 12))


def generate_keypair() -> tuple[bytes, bytes]:
    """``(private_key, public_key)`` as raw 32-byte X25519 values."""
    priv = X25519PrivateKey.generate()
    return (priv.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                               serialization.NoEncryption()),
            priv.public_key().public_bytes(*_RAW))


def seal(plaintext: bytes, recipient_pubkey: bytes) -> bytes:
    """The gateway's sealing, restated for tests and parity checks."""
    if not isinstance(recipient_pubkey, bytes) or len(recipient_pubkey) != _KEY_LEN:
        raise ValueError("recipient public key must be 32 bytes")
    eph = X25519PrivateKey.generate()
    enc = eph.public_key().public_bytes(*_RAW)
    shared = _shared_secret(eph.exchange(X25519PublicKey.from_public_bytes(recipient_pubkey)),
                            enc, recipient_pubkey)
    key, nonce = _key_schedule(shared)
    return bytes([_VERSION]) + enc + AESGCM(key).encrypt(nonce, plaintext, b"")


def open_sealed(sealed: bytes, recipient_private_key: bytes) -> bytes:
    """Decrypt a sealed credential; every failure raises ``ValueError``."""
    if not isinstance(sealed, bytes) or len(sealed) < 1 + _KEY_LEN + 16 or sealed[0] != _VERSION:
        raise ValueError("sealed: bad format")
    enc, ct = sealed[1:1 + _KEY_LEN], sealed[1 + _KEY_LEN:]
    try:
        priv = X25519PrivateKey.from_private_bytes(bytes(recipient_private_key))
        pk_r = priv.public_key().public_bytes(*_RAW)
        shared = _shared_secret(priv.exchange(X25519PublicKey.from_public_bytes(enc)), enc, pk_r)
        key, nonce = _key_schedule(shared)
        return AESGCM(key).decrypt(nonce, ct, b"")
    except (InvalidTag, ValueError, TypeError) as exc:
        raise ValueError("sealed: cannot open") from exc


# ── evidence ────────────────────────────────────────────────────────────────


class DstackQuoteClient:
    """TDX evidence from the dstack guest agent, already in the gateway's wire shape.

    ``GetQuote`` binds raw REPORT_DATA (the legacy ``TdxQuote`` hashes it first). Only the
    checker service mounts the socket; verifier containers never see it.
    """

    def __init__(self, socket_path: str = DSTACK_SOCKET_PATH, *, timeout_s: float = 30.0,
                 transport: Any | None = None) -> None:
        self.socket_path = socket_path
        self.timeout_s = timeout_s
        self._transport = transport

    def __call__(self, report_data: bytes) -> dict[str, str]:
        if not isinstance(report_data, bytes) or len(report_data) != REPORT_DATA_LEN:
            raise ProtectedAttestationError("report_data_invalid")
        import httpx

        transport = self._transport or httpx.HTTPTransport(uds=self.socket_path)
        try:
            with httpx.Client(transport=transport, timeout=self.timeout_s,
                              follow_redirects=False) as client:
                response = client.post("http://dstack/GetQuote",
                                       json={"report_data": report_data.hex()})
        except (httpx.HTTPError, OSError):
            raise ProtectedAttestationError("quote_request_failed") from None
        if response.status_code != 200:
            raise ProtectedAttestationError("quote_request_failed")
        try:
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError()
            quote = bytes.fromhex(result["quote"]) if isinstance(result.get("quote"), str) else b""
            event_log = result.get("event_log")
            if not quote or not isinstance(event_log, str):
                raise ValueError()
        except (ValueError, RecursionError):
            raise ProtectedAttestationError("quote_malformed") from None
        return {"kind": "tdx", "quote_b64": base64.b64encode(quote).decode("ascii"),
                "event_log_json": event_log}


def evidence_fn_from_env(environ: Mapping[str, str]) -> Callable[[bytes], dict] | None:
    """``dstack`` selects the guest agent; unset means Standard-only; anything else refuses."""
    source = environ.get(EVIDENCE_SOURCE_ENV)
    if not source:
        return None
    if source == "dstack":
        return DstackQuoteClient()
    raise ValueError(f"{EVIDENCE_SOURCE_ENV} must be 'dstack'")


# ── the exchange ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProtectedReadToken:
    token: str = field(repr=False)
    expires_at: str


class ProtectedAssignmentAttestor:
    """One challenge and one sealed release per call; the token is never logged.

    ``client`` needs ``request_attestation_challenge(assignment_id)`` and
    ``release_sealed_credential(assignment_id, challenge_nonce=, evidence=, enclave_pubkey=)``
    (``OrmasValidatorClient`` provides both). ``signer_pubkey`` is the raw 32-byte
    Ed25519 verify key this checker registered and signs decisions with.
    """

    def __init__(self, client: Any, evidence_fn: Callable[[bytes], Mapping[str, Any]], *,
                 signer_pubkey: bytes,
                 keypair_fn: Callable[[], tuple[bytes, bytes]] = generate_keypair) -> None:
        if not isinstance(signer_pubkey, bytes) or len(signer_pubkey) != _KEY_LEN:
            raise ValueError("signer public key must be 32 bytes")
        self.client = client
        self.evidence_fn = evidence_fn
        self.signer_pubkey = bytes(signer_pubkey)
        self.keypair_fn = keypair_fn

    def fetch_read_token(self, assignment_id: str) -> ProtectedReadToken:
        private_key = bytearray()
        try:
            try:
                raw_private, enclave_pubkey = self.keypair_fn()
                private_key = bytearray(raw_private)
                if len(private_key) != _KEY_LEN or not isinstance(enclave_pubkey, bytes) \
                        or len(enclave_pubkey) != _KEY_LEN:
                    raise ValueError()
            except Exception:  # noqa: BLE001 - key errors may carry key material
                raise ProtectedAttestationError("keypair_failed") from None
            try:
                challenge = self.client.request_attestation_challenge(assignment_id)
            except Exception:  # noqa: BLE001 - transport errors share one refusal
                raise ProtectedAttestationError("challenge_refused") from None
            try:
                nonce = bytes.fromhex(challenge["nonce"])
                job_id, attempt = challenge["job_id"], challenge["attempt"]
                if (not isinstance(job_id, str) or not job_id or isinstance(attempt, bool)
                        or not isinstance(attempt, int) or attempt < 0):
                    raise ValueError()
                report_data = report_data_for_assignment(
                    nonce, job_id=job_id, attempt=attempt, assignment_id=assignment_id,
                    enclave_pubkey=enclave_pubkey, signer_pubkey=self.signer_pubkey)
            except (KeyError, TypeError, ValueError):
                raise ProtectedAttestationError("challenge_malformed") from None
            try:
                evidence = dict(self.evidence_fn(report_data))
            except Exception:  # noqa: BLE001 - guest agent errors may carry request data
                raise ProtectedAttestationError("evidence_unavailable") from None
            try:
                response = self.client.release_sealed_credential(
                    assignment_id, challenge_nonce=nonce.hex(), evidence=evidence,
                    enclave_pubkey=base64.b64encode(enclave_pubkey).decode("ascii"))
            except Exception:  # noqa: BLE001 - a refused release carries no detail here
                raise ProtectedAttestationError("release_refused") from None
            try:
                credential = response["repo_credential"]
                if (credential.get("kind") != SEALED_CREDENTIAL_KIND
                        or credential.get("scheme") != SEALED_CREDENTIAL_SCHEME
                        or not isinstance(credential.get("expires_at"), str)):
                    raise ValueError()
                sealed = base64.b64decode(credential["sealed"], validate=True)
            except (KeyError, TypeError, ValueError, AttributeError):
                raise ProtectedAttestationError("release_malformed") from None
            try:
                token = open_sealed(sealed, bytes(private_key)).decode("ascii")
                if not re.fullmatch(r"[\x21-\x7e]+", token):
                    raise ValueError()
            except (ValueError, UnicodeDecodeError):
                raise ProtectedAttestationError("open_failed") from None
            return ProtectedReadToken(token=token, expires_at=credential["expires_at"])
        finally:
            private_key[:] = b"\0" * len(private_key)
