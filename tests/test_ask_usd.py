"""The public miner's firm-bid (``ask_usd``) claim wire delta.

Assert the public client's claim body equals the private ``OrmasHttpClient``'s
for the same inputs — the private module is ground truth (it is what the
server enforces) and imports ONLY here, in this test, nowhere else in
``ormas_subnet`` (see ``test_import_graph.py``). Also assert the local
validation mirror: a negative / non-finite / bool / non-numeric ask raises
``ValueError`` before any request crosses the transport.
"""
from __future__ import annotations

from typing import Any

import pytest

from ormas_subnet.client import OrmasMinerClient
from ormas_subnet.protocol import RUNNER_PROTOCOL_V1


class _FakeResponse:
    def __init__(self, status_code: int, body: Any = None) -> None:
        self.status_code = status_code
        self._body = body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}: {self._body}")

    def json(self) -> Any:
        return self._body


class _RecordingTransport:
    """Captures POST bodies; answers 204 (idle) to every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def post(
        self, path: str, json: dict[str, Any] | None = None, headers: Any = None,
    ) -> _FakeResponse:
        self.calls.append((path, json or {}))
        return _FakeResponse(204)


def test_claim_body_with_ask_matches_private_client() -> None:
    """ask_usd=1.25 rides onto the wire identically in both client halves."""
    private = pytest.importorskip("tensorbox_spec.mcp_server.ormas_http_client")
    OrmAsGatewayClient = private.OrmAsGatewayClient

    pub_transport, priv_transport = _RecordingTransport(), _RecordingTransport()
    OrmasMinerClient(
        base_url="https://fake.invalid", token="ormr_test", http_client=pub_transport,
    ).claim_task("runner-1", ask_usd=1.25)
    OrmAsGatewayClient(
        base_url="https://fake.invalid", api_key="ormr_test", http_client=priv_transport,
    ).claim_task("runner-1", ask_usd=1.25)

    assert pub_transport.calls == priv_transport.calls
    path, body = pub_transport.calls[0]
    assert path == "/api/runner/v1/leases"
    assert body["ask_usd"] == 1.25


def test_claim_body_without_ask_is_schema_and_runner_only() -> None:
    """No ask configured keeps the byte-identical two-field body."""
    transport = _RecordingTransport()
    OrmasMinerClient(
        base_url="https://fake.invalid", token="ormr_test", http_client=transport,
    ).claim_task("runner-1")

    _, body = transport.calls[0]
    assert body == {"schema_version": RUNNER_PROTOCOL_V1, "runner_id": "runner-1"}


@pytest.mark.parametrize(
    "bad",
    [-0.01, -1, float("nan"), float("inf"), float("-inf"), True, False, "1.25"],
)
def test_bad_ask_raises_before_any_request(bad: Any) -> None:
    """The server's claim_lease validation is mirrored locally: finite, >=0, not bool."""
    transport = _RecordingTransport()
    client = OrmasMinerClient(base_url="https://fake.invalid", token="ormr_test", http_client=transport)
    with pytest.raises(ValueError, match="ask_usd"):
        client.claim_task("runner-1", ask_usd=bad)
    assert transport.calls == []
