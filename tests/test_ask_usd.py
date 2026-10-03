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


@pytest.mark.parametrize("offers", [
    [{"job_id": "job-firm", "kind": "firm", "price_usd": 0.75},
     {"job_id": "job-limit", "kind": "limit", "estimate_usd": 0.9, "limit_usd": 1.25}],
    [],
])
def test_offers_body_matches_private_client(offers: list[dict]) -> None:
    private = pytest.importorskip("tensorbox_spec.mcp_server.ormas_http_client")
    pub_transport, priv_transport = _RecordingTransport(), _RecordingTransport()
    OrmasMinerClient(
        base_url="https://fake.invalid", token="ormr_test", http_client=pub_transport,
    ).claim_task("runner-1", offers=offers, claim_request_id="req-offers")
    private.OrmAsGatewayClient(
        base_url="https://fake.invalid", api_key="ormr_test", http_client=priv_transport,
    ).claim_task("runner-1", offers=offers, claim_request_id="req-offers")
    assert pub_transport.calls == priv_transport.calls
    assert pub_transport.calls[0] == ("/api/runner/v1/leases", {
        "schema_version": RUNNER_PROTOCOL_V1, "runner_id": "runner-1",
        "offers": offers, "claim_request_id": "req-offers",
    })


_BAD_OFFERS = [
    ({"job_id": "job-1", "kind": "firm", "price_usd": 1.0},),
    [{"job_id": f"job-{i}", "kind": "firm", "price_usd": 1.0} for i in range(21)],
    ["job-1"],
    [{"kind": "firm", "price_usd": 1.0}],
    [{"job_id": "", "kind": "firm", "price_usd": 1.0}],
    [{"job_id": 7, "kind": "firm", "price_usd": 1.0}],
    [{"job_id": "job-1", "kind": "firm", "price_usd": 1.0},
     {"job_id": "job-1", "kind": "limit", "estimate_usd": 0.5, "limit_usd": 1.0}],
    [{"job_id": "job-1", "kind": "bid", "price_usd": 1.0}],
    [{"job_id": "job-1", "kind": "firm"}],
    [{"job_id": "job-1", "kind": "firm", "limit_usd": 1.0}],
    [{"job_id": "job-1", "kind": "limit", "price_usd": 1.0}],
    [{"job_id": "job-1", "kind": "limit", "limit_usd": 1.0}],
    [{"job_id": "job-1", "kind": "limit", "estimate_usd": 0.5}],
    [{"job_id": "job-1", "kind": "limit", "estimate_usd": 0.5, "price_usd": 1.0}],
    [{"job_id": "job-1", "kind": "firm", "price_usd": 1.0, "limit_usd": 1.0}],
    [{"job_id": "job-1", "kind": "firm", "price_usd": 1.0, "estimate_usd": 0.5}],
    [{"job_id": "job-1", "kind": "limit", "estimate_usd": 0.5, "limit_usd": 1.0, "note": "x"}],
    [{"job_id": "job-1", "kind": "firm", "price_usd": 0}],
    [{"job_id": "job-1", "kind": "firm", "price_usd": -1.0}],
    [{"job_id": "job-1", "kind": "firm", "price_usd": True}],
    [{"job_id": "job-1", "kind": "firm", "price_usd": "1.0"}],
    [{"job_id": "job-1", "kind": "limit", "estimate_usd": 0.5, "limit_usd": float("nan")}],
    [{"job_id": "job-1", "kind": "limit", "estimate_usd": 0.5, "limit_usd": float("inf")}],
    [{"job_id": "job-1", "kind": "limit", "estimate_usd": 0.5, "limit_usd": 10 ** 400}],
    [{"job_id": "job-1", "kind": "limit", "estimate_usd": "0.5", "limit_usd": 1.0}],
    [{"job_id": "job-1", "kind": "limit", "estimate_usd": float("inf"), "limit_usd": 1.0}],
    [{"job_id": "job-1", "kind": "limit", "estimate_usd": -0.5, "limit_usd": 1.0}],
    [{"job_id": "job-1", "kind": "limit", "limit_usd": 1.0, "estimate_usd": 0}],
    [{"job_id": "job-1", "kind": "limit", "limit_usd": 1.0, "estimate_usd": 1.5}],
    [{"job_id": "job-1", "kind": "limit", "limit_usd": 1.0, "estimate_usd": False}],
    [{"job_id": "job-1", "kind": "limit", "limit_usd": 1.0, "estimate_usd": float("nan")}],
]


@pytest.mark.parametrize("offers", _BAD_OFFERS)
def test_malformed_offers_raise_before_any_request(offers: Any) -> None:
    transport = _RecordingTransport()
    client = OrmasMinerClient("https://fake.invalid", "ormr_test", http_client=transport)
    with pytest.raises(ValueError):
        client.claim_task("runner-1", offers=offers)
    assert transport.calls == []


def test_limit_offers_and_full_page_are_sent_unchanged() -> None:
    offers = [{"job_id": "job-0", "kind": "limit", "limit_usd": 1.25, "estimate_usd": 0.8},
              {"job_id": "job-1", "kind": "limit", "limit_usd": 1.25, "estimate_usd": 1.25}]
    offers += [{"job_id": f"job-{i}", "kind": "firm", "price_usd": 1} for i in range(2, 20)]
    transport = _RecordingTransport()
    client = OrmasMinerClient("https://fake.invalid", "ormr_test", http_client=transport)
    assert client.claim_task("runner-1", offers=offers) is None
    assert transport.calls == [("/api/runner/v1/leases", {
        "schema_version": RUNNER_PROTOCOL_V1, "runner_id": "runner-1", "offers": offers,
    })]


@pytest.mark.parametrize("offers", [[], [{"job_id": "job-1", "kind": "limit", "estimate_usd": 1, "limit_usd": 1}]])
@pytest.mark.parametrize("ask", [0.0, 1.25])
def test_offers_and_ask_are_mutually_exclusive(offers: list[dict], ask: float) -> None:
    transport = _RecordingTransport()
    client = OrmasMinerClient("https://fake.invalid", "ormr_test", http_client=transport)
    with pytest.raises(ValueError, match="offers cannot be combined"):
        client.claim_task("runner-1", ask_usd=ask, offers=offers)
    assert transport.calls == []


class _QueueTransport(_RecordingTransport):
    def __init__(self, response: _FakeResponse) -> None:
        super().__init__()
        self.response = response
        self.gets: list[tuple[str, Any]] = []

    def get(self, path: str, headers: Any = None) -> _FakeResponse:
        self.gets.append((path, headers))
        return self.response


@pytest.mark.parametrize("device_nonce", [None, "device-1"])
def test_queue_payload_and_request_match_private_client(device_nonce: str | None) -> None:
    private = pytest.importorskip("tensorbox_spec.mcp_server.ormas_http_client")
    payload = {"schema_version": "ormas.runner-queue.v1", "jobs": [{
        "job_id": "job-1", "created_at": "2026-10-02T19:00:00Z",
        "envelope": {"size_class": "small", "turn_budget": 24, "languages": ["python"],
                     "service_level": "standard", "repository_visibility": "public"},
    }]}
    pub_transport = _QueueTransport(_FakeResponse(200, payload))
    priv_transport = _QueueTransport(_FakeResponse(200, payload))
    public = OrmasMinerClient(
        "https://fake.invalid", "ormr_test", http_client=pub_transport, device_nonce=device_nonce,
    )
    internal = private.OrmAsGatewayClient(
        "https://fake.invalid", "ormr_test", http_client=priv_transport, device_nonce=device_nonce,
    )
    assert public.list_queue("runner /&?") == internal.list_queue("runner /&?") == payload
    assert pub_transport.gets == priv_transport.gets
    assert pub_transport.gets[0][0] == "/api/runner/v1/queue?runner_id=runner%20%2F%26%3F"


@pytest.mark.parametrize("payload", [None, [], "invalid"])
def test_queue_rejects_non_dict_payload(payload: Any) -> None:
    client = OrmasMinerClient(
        "https://fake.invalid", "ormr_test",
        http_client=_QueueTransport(_FakeResponse(200, payload)),
    )
    with pytest.raises(ValueError, match="invalid queue payload"):
        client.list_queue("runner-1")


@pytest.mark.parametrize("status", [404, 403, 503])
def test_queue_http_errors_carry_status(status: int) -> None:
    from ormas_subnet.client import OrmasGatewayError

    client = OrmasMinerClient(
        "https://fake.invalid", "ormr_test",
        http_client=_QueueTransport(_FakeResponse(status, {"error": {
            "type": "queue_unavailable", "message": "queue unavailable",
        }})),
    )
    with pytest.raises(OrmasGatewayError) as raised:
        client.list_queue("runner-1")
    assert raised.value.status_code == status


@pytest.mark.parametrize("settled", [None, 0.0, 0.75])
def test_completion_forwards_terminal_settlement_unchanged(settled: float | None) -> None:
    from ormas_subnet.protocol import TaskReceipt, TaskTerminal

    transport = _RecordingTransport()
    client = OrmasMinerClient("https://fake.invalid", "ormr_test", http_client=transport)
    terminal = TaskTerminal(
        lease_id="lease-1", verification_state="verified", result_ref="local:ormas/job/job-1",
        settlement_state="unset", rating=None, result_commit="a" * 40,
        settled_price_usd=settled,
    )
    receipt = TaskReceipt(
        lease_id="lease-1", generation_ids=(), actual_provider="unknown", model=None,
        prompt_tokens=0, completion_tokens=0, cache_read_input_tokens=0,
        cache_creation_input_tokens=0, reasoning_tokens=0, upstream_cost_usd=None,
        finish_reason=None, metering_complete=False,
    )
    client.complete_task("job-1", "runner-1", "lease-1", receipt=receipt, terminal=terminal)
    assert transport.calls[0][1]["terminal"] == terminal.to_wire()
    assert ("settled_price_usd" in transport.calls[0][1]["terminal"]) == (settled is not None)


def test_limit_estimate_compares_float_normalized_like_gateway() -> None:
    offers = [{"job_id": "job-1", "kind": "limit", "estimate_usd": 2 ** 53 + 1,
               "limit_usd": 2 ** 53}]
    transport = _RecordingTransport()
    client = OrmasMinerClient("https://fake.invalid", "ormr_test", http_client=transport)
    assert client.claim_task("runner-1", offers=offers) is None
    assert transport.calls[0][1]["offers"] == offers
    runner_api = pytest.importorskip("tensorbox_spec.customer_api.runner_api")
    assert runner_api._parse_offers(offers) is not None
