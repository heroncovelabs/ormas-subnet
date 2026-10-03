"""A public task receipt does not require the miner to name a model."""
from __future__ import annotations

from ormas_subnet.protocol import RUNNER_PROTOCOL_V1, TaskReceipt


def test_task_receipt_does_not_require_model():
    payload = {
        "schema_version": RUNNER_PROTOCOL_V1,
        "lease_id": "lease1",
        "generation_ids": ["gen-1"],
        "actual_provider": "reference",
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "reasoning_tokens": 0,
        "upstream_cost_usd": None,
        "finish_reason": "stop",
        "metering_complete": True,
        "child_model_ids": [],
    }
    receipt = TaskReceipt.from_wire(payload)
    assert receipt.model is None
    assert "model" not in receipt.to_wire()
