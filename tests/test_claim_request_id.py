"""Reference and private clients send claim_request_id only when given."""
from __future__ import annotations

from typing import Any

import pytest

from ormas_subnet.client import OrmasMinerClient
from ormas_subnet.protocol import RUNNER_PROTOCOL_V1, TaskDraft, TaskLease


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
    def __init__(self, response: _FakeResponse | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._response = response or _FakeResponse(204)

    def post(
        self, path: str, json: dict[str, Any] | None = None, headers: Any = None,
    ) -> _FakeResponse:
        self.calls.append((path, json or {}))
        return self._response


def _draft_wire() -> dict[str, Any]:
    return TaskDraft(
        task_id="task1",
        runner_id="runner-1",
        repo_id="repo1",
        base_commit="a" * 40,
        brief="do the thing",
        verify_command="pytest -q",
        allowed_paths=["src/x.py"],
        budget_usd=1.5,
        work_packet={"task": "do the thing"},
        work_packet_sha256="b" * 64,
        attempt=0,
        parent_job_id="",
        repair_findings=[],
    ).to_wire()


def _lease_wire(**extra: Any) -> dict[str, Any]:
    body = {
        "schema_version": RUNNER_PROTOCOL_V1,
        "lease_id": "lease1",
        "task_id": "task1",
        "expires_at": "2026-01-01T00:00:00Z",
        "selected_cell": "code-edit-small",
        "provider_pin": "unset",
        "fallback_policy": "unset",
        "hold_ref": "unset",
        "now": "2026-01-01T00:00:00Z",
        "outcome_price_usd": 0.05,
    }
    body.update(extra)
    return body


def test_claim_request_id_is_sent_only_when_given() -> None:
    private = pytest.importorskip("tensorbox_spec.mcp_server.ormas_http_client")
    OrmAsGatewayClient = private.OrmAsGatewayClient
    for claim_request_id in (None, "req-1"):
        pub_transport, priv_transport = _RecordingTransport(), _RecordingTransport()
        kwargs: dict[str, Any] = {}
        if claim_request_id is not None:
            kwargs["claim_request_id"] = claim_request_id
        OrmasMinerClient(
            base_url="https://fake.invalid", token="ormr_test", http_client=pub_transport,
        ).claim_task("runner-1", **kwargs)
        OrmAsGatewayClient(
            base_url="https://fake.invalid", api_key="ormr_test", http_client=priv_transport,
        ).claim_task("runner-1", **kwargs)
        assert pub_transport.calls == priv_transport.calls
        body = pub_transport.calls[0][1]
        if claim_request_id is None:
            assert "claim_request_id" not in body
            assert body == {"schema_version": RUNNER_PROTOCOL_V1, "runner_id": "runner-1"}
        else:
            assert body["claim_request_id"] == "req-1"


def test_clients_parse_the_echo_and_a_missing_id_is_none() -> None:
    private = pytest.importorskip("tensorbox_spec.mcp_server.ormas_http_client")
    echoed = _lease_wire(claim_request_id="req-1")
    absent = _lease_wire()
    payload = {"lease": echoed, "draft": _draft_wire()}
    bare = {"lease": absent, "draft": _draft_wire()}
    clients = [
        OrmasMinerClient(
            base_url="https://fake.invalid",
            token="ormr_test",
            http_client=_RecordingTransport(_FakeResponse(200, payload)),
        ),
        private.OrmAsGatewayClient(
            base_url="https://fake.invalid",
            api_key="ormr_test",
            http_client=_RecordingTransport(_FakeResponse(200, payload)),
        ),
    ]
    for client in clients:
        claimed = client.claim_task("runner-1")
        assert claimed is not None
        lease, _draft = claimed
        assert lease.claim_request_id == "req-1"
    bare_clients = [
        OrmasMinerClient(
            base_url="https://fake.invalid",
            token="ormr_test",
            http_client=_RecordingTransport(_FakeResponse(200, bare)),
        ),
        private.OrmAsGatewayClient(
            base_url="https://fake.invalid",
            api_key="ormr_test",
            http_client=_RecordingTransport(_FakeResponse(200, bare)),
        ),
    ]
    for client in bare_clients:
        claimed = client.claim_task("runner-1")
        assert claimed is not None
        lease, _draft = claimed
        assert lease.claim_request_id is None
        assert "claim_request_id" not in lease.to_wire()


def test_unknown_lease_field_is_still_rejected() -> None:
    """An unrecognized lease key stays a decode error. Do not drop it."""
    private = pytest.importorskip("tensorbox_spec.mcp_server.ormas_http_client")
    with pytest.raises(ValueError, match="future_field"):
        TaskLease.from_wire(_lease_wire(future_field="contract-body-marker"))
    with pytest.raises(ValueError, match="future_field"):
        private.TaskLease.from_wire(_lease_wire(future_field="contract-body-marker"))
    with pytest.raises(ValueError, match="provider_key"):
        TaskLease.from_wire(_lease_wire(provider_key="sk-never"))
