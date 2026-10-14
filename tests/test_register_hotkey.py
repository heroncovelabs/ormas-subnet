"""Miner hotkey registration: challenge → sign → register.

The gateway's ``POST /api/runner/v1/hotkey/challenge`` mints a one-time challenge bound
to (runner, miner identity, hotkey); ``POST /api/runner/v1/hotkey`` verifies an sr25519
signature over the challenge bytes and records the verified mapping the weights scorer
pays on. The public client must drive that flow with the miner's OWN signer injected —
this package never holds or derives a hotkey, so signing is a callable the miner supplies
(``sign_fn(challenge_bytes) -> signature_hex``). Wire bodies below are the server's
(``runner_api.hotkey_challenge`` / ``register_hotkey``).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from ormas_subnet.client import OrmasMinerClient
from ormas_subnet.protocol import RUNNER_PROTOCOL_V1

HOTKEY = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"  # well-known dev SS58 (Alice)
NOW = datetime.now(timezone.utc).replace(microsecond=0)
NOW_TEXT = NOW.isoformat().replace("+00:00", "Z")
EXPIRES_TEXT = (NOW + timedelta(seconds=300)).isoformat().replace("+00:00", "Z")
NONCE = "de" * 16
CHALLENGE = f"ormas-hotkey:v1:miner:t1:{HOTKEY}:{NONCE}:{EXPIRES_TEXT}"


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ormas_subnet.client._utc_now", lambda: NOW)


class _FakeResponse:
    def __init__(self, status_code: int, body: Any = None) -> None:
        self.status_code = status_code
        self._body = body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}: {self._body}")

    def json(self) -> Any:
        return self._body


class _HotkeyTransport:
    """Answers the two hotkey routes the way the gateway does; records every body."""

    def __init__(
        self, *, register_status: int = 200, register_body: Any = None,
        challenge_body: Any = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._register_status = register_status
        self._register_body = register_body
        self._challenge_body = challenge_body

    def post(
        self, path: str, json: dict[str, Any] | None = None, headers: Any = None,
    ) -> _FakeResponse:
        self.calls.append((path, json or {}))
        if path == "/api/runner/v1/hotkey/challenge":
            body = self._challenge_body or {
                "challenge": CHALLENGE,
                "expires_at": EXPIRES_TEXT,
                "now": NOW_TEXT,
                "miner_identity": "miner:t1",
            }
            return _FakeResponse(200, body)
        if path == "/api/runner/v1/hotkey":
            body = self._register_body
            if body is None:
                body = {
                    "miner_identity": "miner:t1",
                    "hotkey_ss58": HOTKEY,
                    "verified": True,
                    "signature_scheme": "sr25519",
                }
            return _FakeResponse(self._register_status, body)
        raise AssertionError(f"unexpected route {path}")


def _client(transport: Any) -> OrmasMinerClient:
    return OrmasMinerClient(base_url="https://fake.invalid", token="ormr_test", http_client=transport)


def test_hotkey_challenge_body_is_schema_runner_hotkey() -> None:
    transport = _HotkeyTransport()
    out = _client(transport).hotkey_challenge("runner-1", hotkey_ss58=HOTKEY)
    path, body = transport.calls[0]
    assert path == "/api/runner/v1/hotkey/challenge"
    assert body == {
        "schema_version": RUNNER_PROTOCOL_V1,
        "runner_id": "runner-1",
        "hotkey_ss58": HOTKEY,
    }
    assert out["challenge"] == CHALLENGE
    assert out["miner_identity"] == "miner:t1"


def test_hotkey_challenge_accepts_gateway_tenant_fallback_identity() -> None:
    identity = "miner:user_example_123"
    challenge = f"ormas-hotkey:v1:{identity}:{HOTKEY}:{NONCE}:{EXPIRES_TEXT}"
    transport = _HotkeyTransport(challenge_body={
        "challenge": challenge,
        "expires_at": EXPIRES_TEXT,
        "now": NOW_TEXT,
        "miner_identity": identity,
    })
    assert _client(transport).hotkey_challenge(
        "runner-1", hotkey_ss58=HOTKEY,
    )["miner_identity"] == identity


def test_register_hotkey_signs_exact_challenge_bytes_with_injected_signer() -> None:
    seen: list[bytes] = []

    def sign_fn(message: bytes) -> str:
        seen.append(message)
        return "ab" * 64

    transport = _HotkeyTransport()
    result = _client(transport).register_hotkey("runner-1", hotkey_ss58=HOTKEY, sign_fn=sign_fn)

    # Signed exactly the UTF-8 bytes of the challenge string the gateway returned.
    assert seen == [CHALLENGE.encode("utf-8")]
    paths = [p for p, _ in transport.calls]
    assert paths == ["/api/runner/v1/hotkey/challenge", "/api/runner/v1/hotkey"]
    _, body = transport.calls[1]
    assert body == {
        "schema_version": RUNNER_PROTOCOL_V1,
        "runner_id": "runner-1",
        "hotkey_ss58": HOTKEY,
        "challenge": CHALLENGE,
        "signature_hex": "ab" * 64,
    }
    assert result["verified"] is True
    assert result["hotkey_ss58"] == HOTKEY
    assert result["signature_scheme"] == "sr25519"


def test_register_hotkey_never_declares_verified_on_refusal() -> None:
    """A gateway refusal (e.g. hotkey_claimed 409) surfaces as an error, never a verified result."""
    transport = _HotkeyTransport(
        register_status=409,
        register_body={"error": {"type": "conflict_error", "message": "hotkey_claimed"}},
    )
    with pytest.raises(Exception) as excinfo:
        _client(transport).register_hotkey(
            "runner-1", hotkey_ss58=HOTKEY, sign_fn=lambda m: "ab" * 64,
        )
    assert "hotkey_claimed" in str(excinfo.value)


@pytest.mark.parametrize(
    "body",
    [
        {"challenge": "sign arbitrary bytes", "expires_at": EXPIRES_TEXT,
         "now": NOW_TEXT, "miner_identity": "miner:t1"},
        {"challenge": CHALLENGE.replace("ormas-hotkey:v1", "other:v1"),
         "expires_at": EXPIRES_TEXT, "now": NOW_TEXT, "miner_identity": "miner:t1"},
        {"challenge": CHALLENGE.replace(NONCE, "ab"), "expires_at": EXPIRES_TEXT,
         "now": NOW_TEXT, "miner_identity": "miner:t1"},
        {"challenge": CHALLENGE, "expires_at": EXPIRES_TEXT, "now": NOW_TEXT,
         "miner_identity": "miner:other"},
        {"challenge": CHALLENGE, "expires_at": EXPIRES_TEXT,
         "now": (NOW - timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
         "miner_identity": "miner:t1"},
        {"challenge": CHALLENGE, "expires_at": EXPIRES_TEXT,
         "now": (NOW + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
         "miner_identity": "miner:t1"},
        {"challenge": (
             f"ormas-hotkey:v1:miner:t1:{HOTKEY}:{NONCE}:"
             f"{(NOW - timedelta(seconds=1)).isoformat().replace('+00:00', 'Z')}"
         ),
         "expires_at": (NOW - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
         "now": (NOW - timedelta(seconds=60)).isoformat().replace("+00:00", "Z"),
         "miner_identity": "miner:t1"},
    ],
    ids=(
        "opaque", "wrong-domain", "short-nonce", "identity-mismatch",
        "long-ttl", "future-clock", "locally-expired",
    ),
)
def test_malformed_challenge_never_reaches_signer(body: Any) -> None:
    seen: list[bytes] = []
    transport = _HotkeyTransport(challenge_body=body)
    with pytest.raises(ValueError, match="challenge"):
        _client(transport).register_hotkey(
            "runner-1", hotkey_ss58=HOTKEY,
            sign_fn=lambda message: seen.append(message) or "ab" * 64,
        )
    assert seen == []
    assert [path for path, _ in transport.calls] == ["/api/runner/v1/hotkey/challenge"]


@pytest.mark.parametrize(
    "body",
    [
        {"miner_identity": "miner:t1", "hotkey_ss58": "5" + "A" * 47,
         "verified": True, "signature_scheme": "sr25519"},
        {"miner_identity": "miner:other", "hotkey_ss58": HOTKEY,
         "verified": True, "signature_scheme": "sr25519"},
        {"miner_identity": "miner:t1", "hotkey_ss58": HOTKEY,
         "verified": False, "signature_scheme": "sr25519"},
    ],
    ids=("hotkey", "identity", "verified"),
)
def test_registration_response_must_repeat_signed_identity(body: dict[str, Any]) -> None:
    transport = _HotkeyTransport(register_body=body)
    with pytest.raises(ValueError, match="registration response"):
        _client(transport).register_hotkey(
            "runner-1", hotkey_ss58=HOTKEY, sign_fn=lambda _message: "ab" * 64,
        )


@pytest.mark.parametrize("bad_sig", ["", "zz", 12, None, "abc"])
def test_register_hotkey_rejects_non_hex_or_empty_signature_locally(bad_sig: Any) -> None:
    """The signer must return a non-empty even-length hex string; anything else is refused
    before the register request is sent (the challenge request has already happened)."""
    transport = _HotkeyTransport()
    with pytest.raises(ValueError):
        _client(transport).register_hotkey(
            "runner-1", hotkey_ss58=HOTKEY, sign_fn=lambda m: bad_sig,
        )
    assert [p for p, _ in transport.calls] == ["/api/runner/v1/hotkey/challenge"]


def test_miner_cli_has_register_hotkey_subcommand() -> None:
    """``python -m neurons.miner register-hotkey --gateway … --runner-id … --hotkey-ss58 …``
    exists and drives the same client flow; signing is supplied by ``--sign-command``,
    a program that reads the challenge bytes on stdin and prints the signature hex — the
    miner keeps its key in its own tooling, never in this package."""
    from neurons import miner

    parser = miner.build_parser()
    ns = parser.parse_args([
        "register-hotkey",
        "--gateway", "https://fake.invalid",
        "--token-env", "ORMAS_MINER_TOKEN",
        "--runner-id", "runner-1",
        "--hotkey-ss58", HOTKEY,
        "--sign-command", "my-signer --hotkey alice",
    ])
    assert ns.command == "register-hotkey"
    assert ns.hotkey_ss58 == HOTKEY
    assert ns.sign_command == "my-signer --hotkey alice"
