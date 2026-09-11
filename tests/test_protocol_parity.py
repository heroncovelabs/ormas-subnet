"""Assert ormas_subnet.protocol stays byte-compatible with the private wire DTOs.

Both modules import ONLY here, in this test — nowhere else in ``ormas_subnet``
does a private ``tensorbox_spec`` module get imported (see
``test_import_graph.py``). If this test fails, one of the two copies drifted;
fix the public copy to match the server's actual accepted contract (the
private module is ground truth, since it's what the server enforces).
"""
from __future__ import annotations

from dataclasses import fields

import pytest

from ormas_subnet import protocol as public

# Ground truth lives in the private monorepo; standalone checkouts of the public
# repo skip this guard, the monorepo runs it on every change.
private = pytest.importorskip("tensorbox_spec.mcp_server.ormas_http_client")

DTO_NAMES = [
    "RunnerRegistration",
    "RepoRegistration",
    "TaskDraft",
    "TaskLease",
    "TaskEvent",
    "TaskReceipt",
    "TaskTerminal",
]


@pytest.mark.parametrize("name", DTO_NAMES)
def test_dto_field_names_and_types_match(name: str) -> None:
    pub_cls = getattr(public, name)
    priv_cls = getattr(private, name)
    pub_fields = {f.name: f.type for f in fields(pub_cls)}
    priv_fields = {f.name: f.type for f in fields(priv_cls)}
    assert pub_fields == priv_fields, f"{name} field shape drifted: {pub_fields} != {priv_fields}"


def test_schema_version_constant_matches() -> None:
    assert public.RUNNER_PROTOCOL_V1 == private.RUNNER_PROTOCOL_V1


def test_device_header_constant_matches() -> None:
    assert public.RUNNER_DEVICE_HEADER == private.RUNNER_DEVICE_HEADER


def test_verification_states_match() -> None:
    assert public.VERIFICATION_STATES == private.VERIFICATION_STATES


def test_forbidden_fields_match() -> None:
    assert public._FORBIDDEN_RUNNER_WIRE_FIELDS == private._FORBIDDEN_RUNNER_WIRE_FIELDS


@pytest.mark.parametrize("name", DTO_NAMES)
def test_to_wire_round_trips_identically(name: str) -> None:
    """Build one instance of each DTO and assert both copies serialize the same wire dict."""
    pub_cls = getattr(public, name)
    priv_cls = getattr(private, name)
    sample = _sample_for(name)
    pub_wire = pub_cls(**sample).to_wire()
    priv_wire = priv_cls(**sample).to_wire()
    assert pub_wire == priv_wire


def _sample_for(name: str) -> dict:
    if name == "RunnerRegistration":
        return dict(
            runner_id="r1", runner_version="0.1", platform="linux",
            capacity=1, health={"cells": ["code-edit-small"]},
        )
    if name == "RepoRegistration":
        return dict(
            repo_id="repo1", display_alias="repo1", base_commit="a" * 40,
            preflight_state="ready",
        )
    if name == "TaskDraft":
        return dict(
            task_id="task1", runner_id="r1", repo_id="repo1", base_commit="a" * 40,
            brief="do the thing", verify_command="pytest -q", allowed_paths=["src/x.py"],
            budget_usd=1.5, work_packet={"task": "do the thing"},
            work_packet_sha256="b" * 64, attempt=0, parent_job_id="",
            repair_findings=[],
        )
    if name == "TaskLease":
        return dict(
            lease_id="lease1", task_id="task1", expires_at="2026-01-01T00:00:00Z",
            selected_cell="code-edit-small", provider_pin="unset",
            fallback_policy="unset", hold_ref="unset", now="2026-01-01T00:00:00Z",
            outcome_price_usd=0.05,
        )
    if name == "TaskEvent":
        return dict(
            lease_id="lease1", state="executing", occurred_at="2026-01-01T00:00:00Z",
            error_category=None,
        )
    if name == "TaskReceipt":
        return dict(
            lease_id="lease1", generation_ids=["gen-1"], actual_provider="reference",
            model="reference-shell-solver", prompt_tokens=0, completion_tokens=0,
            cache_read_input_tokens=0, cache_creation_input_tokens=0,
            reasoning_tokens=0, upstream_cost_usd=0.0, finish_reason="stop",
            metering_complete=True,
        )
    if name == "TaskTerminal":
        return dict(
            lease_id="lease1", verification_state="verified",
            result_ref="refs/heads/ormas/job/task1", settlement_state="unset",
            rating=None, result_commit="c" * 40,
        )
    raise AssertionError(f"no sample for {name}")
