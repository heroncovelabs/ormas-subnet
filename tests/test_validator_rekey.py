"""The reference checker retains its old key until the challenge retry succeeds."""
from __future__ import annotations

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ormas_subnet.validator import OrmasValidatorClient, make_ed25519_signer


def test_validator_reference_rekey_challenge_flow():
    current = Ed25519PrivateKey.generate()
    new = Ed25519PrivateKey.generate()
    sign, _ = make_ed25519_signer(current.private_bytes_raw().hex())
    pubkey = new.public_key().public_bytes_raw().hex()
    nonce = bytes(range(32))
    calls = []

    def gateway(request):
        import json
        body = json.loads(request.content)
        calls.append((request.url.path, body))
        if len(calls) == 1:
            assert "rekey_proof" not in body
            return httpx.Response(409, json={"error": {"type": "conflict_error",
                                                      "message": "validator_rekey_proof_required"}})
        if len(calls) == 2:
            assert request.url.path == "/api/validator/v1/rekey-challenge"
            return httpx.Response(200, json={"challenge_id": "vrk_1", "nonce_hex": nonce.hex(),
                                            "expires_at": "2999-01-01T00:00:00Z"})
        assert request.url.path == "/api/validator/v1/registrations"
        assert body["pubkey_hex"] == pubkey
        assert body["acceptance_contracts"] == calls[0][1]["acceptance_contracts"]
        assert body["task_cells"] == ["task:code/protected"]
        proof = body["rekey_proof"]
        assert proof["challenge_id"] == "vrk_1"
        current.public_key().verify(bytes.fromhex(proof["signature"]),
                                    b"ormas-validator-rekey-v1" + nonce + bytes.fromhex(pubkey))
        return httpx.Response(200, json={"validator_id": "val_1"})

    with httpx.Client(base_url="https://fake.invalid", transport=httpx.MockTransport(gateway)) as http:
        client = OrmasValidatorClient("https://fake.invalid", "ormv_test", http_client=http)
        assert client.register(pubkey_hex=pubkey, task_cells=["task:code/protected"],
                               current_sign_fn=sign) == {"validator_id": "val_1"}
    assert len(calls) == 3


@pytest.mark.parametrize("message", ["validator_rekey_proof_required", "other_conflict"])
def test_validator_reference_rekey_retries_only_once(message):
    current = Ed25519PrivateKey.generate()
    sign, pubkey = make_ed25519_signer(current.private_bytes_raw().hex())
    calls = []

    def gateway(request):
        calls.append(request.url.path)
        if request.url.path.endswith("rekey-challenge"):
            return httpx.Response(200, json={"challenge_id": "vrk_1", "nonce_hex": "aa" * 32,
                                            "expires_at": "2999-01-01T00:00:00Z"})
        return httpx.Response(409, json={"error": {"type": "conflict_error", "message": message}})

    with httpx.Client(base_url="https://fake.invalid", transport=httpx.MockTransport(gateway)) as http:
        client = OrmasValidatorClient("https://fake.invalid", "ormv_test", http_client=http)
        with pytest.raises(httpx.HTTPStatusError):
            client.register(pubkey_hex=pubkey, current_sign_fn=sign)
    assert len(calls) == (3 if message == "validator_rekey_proof_required" else 1)


def test_validator_reference_without_current_key_keeps_refusal():
    def gateway(request):
        assert request.url.path.endswith("registrations")
        return httpx.Response(409, json={"error": {"type": "conflict_error",
                                                  "message": "validator_rekey_proof_required"}})
    with httpx.Client(base_url="https://fake.invalid", transport=httpx.MockTransport(gateway)) as http:
        client = OrmasValidatorClient("https://fake.invalid", "ormv_test", http_client=http)
        with pytest.raises(httpx.HTTPStatusError):
            client.register(pubkey_hex="aa" * 32)
