"""Completion sends voluntary effort only when supplied."""
from __future__ import annotations

import pytest

from ormas_subnet.client import OrmasMinerClient, _validate_effort
from ormas_subnet.protocol import TaskReceipt, TaskTerminal
from tests.test_claim_request_id import _FakeResponse, _RecordingTransport


@pytest.mark.parametrize("supplied", [False, True])
def test_complete_effort_is_optional(supplied):
    effort = {
        "attempts": 2, "model_turns": 8, "models_used": 2,
        "output_tokens": 40, "total_tokens": 100,
    }
    transport = _RecordingTransport(_FakeResponse(200, {"ok": True}))
    client = OrmasMinerClient(
        base_url="https://fake.invalid", token="ormr_test", http_client=transport,
    )
    receipt = TaskReceipt.from_wire({
        "schema_version": "ormas-runner-v1", "lease_id": "lease-1",
        "generation_ids": [], "actual_provider": "reference", "child_model_ids": [],
        "prompt_tokens": 0, "completion_tokens": 0,
        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
        "reasoning_tokens": 0, "upstream_cost_usd": None, "finish_reason": "stop",
        "metering_complete": True,
    })
    terminal = TaskTerminal(
        lease_id="lease-1", verification_state="verified", result_ref="refs/heads/result",
        settlement_state="pending", rating=None, result_commit="a" * 40,
    )
    kwargs = {"effort": effort} if supplied else {}
    assert client.complete_task(
        "task-1", "runner-1", "lease-1", receipt=receipt, terminal=terminal, **kwargs,
    ) == {"ok": True}
    path, body = transport.calls[0]
    assert path == "/api/runner/v1/leases/task-1/complete"
    if supplied:
        assert body["effort"] == effort
    else:
        assert "effort" not in body


@pytest.mark.parametrize("bad", [
    [], {}, {"attempts": 1}, {"attempts": 1, "model_turns": 1, "models_used": 1,
                             "output_tokens": 1, "total_tokens": 1, "model": "x"},
    {"attempts": True, "model_turns": 1, "models_used": 1, "output_tokens": 1, "total_tokens": 1},
    {"attempts": -1, "model_turns": 1, "models_used": 1, "output_tokens": 1, "total_tokens": 1},
    {"attempts": 1.5, "model_turns": 1, "models_used": 1, "output_tokens": 1, "total_tokens": 1},
    {"attempts": "1", "model_turns": 1, "models_used": 1, "output_tokens": 1, "total_tokens": 1},
])
def test_malformed_effort_raises_before_sending(bad):
    with pytest.raises(ValueError):
        _validate_effort(bad)
    transport = _RecordingTransport(_FakeResponse(200, {"ok": True}))
    client = OrmasMinerClient(
        base_url="https://fake.invalid", token="ormr_test", http_client=transport,
    )
    receipt = TaskReceipt.from_wire({
        "schema_version": "ormas-runner-v1", "lease_id": "lease-1",
        "generation_ids": [], "actual_provider": "reference", "child_model_ids": [],
        "prompt_tokens": 0, "completion_tokens": 0,
        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
        "reasoning_tokens": 0, "upstream_cost_usd": None, "finish_reason": "stop",
        "metering_complete": True,
    })
    terminal = TaskTerminal(
        lease_id="lease-1", verification_state="verified", result_ref="refs/heads/result",
        settlement_state="pending", rating=None, result_commit="a" * 40,
    )
    with pytest.raises(ValueError):
        client.complete_task(
            "task-1", "runner-1", "lease-1", receipt=receipt, terminal=terminal, effort=bad,
        )
    assert transport.calls == []
