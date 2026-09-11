"""Ormas miner wire protocol — the ``ormas-runner-v1`` DTOs.

This is a **copy**, not an import, of the DTOs defined in
``tensorbox_spec.mcp_server.ormas_http_client`` (the private monorepo). The two must
stay byte-compatible on field names, types, and validation because they serialize to
the same wire format accepted by the gateway's ``/api/runner/v1`` routes
(``tensorbox_spec/customer_api/runner_api.py``). ``public_subnet/tests/test_protocol_parity.py``
(in the private repo, not shipped here) asserts the two stay in sync; if you change a
field here, the private copy must change too, and vice versa.

See ``public_subnet/docs/protocol.md`` for the full wire contract and
``docs/DECISIONS.md`` for why this package
exists at all: the miner posts a firm bid for a whole task and is paid only on
accepted delivery — this module is the shape of that conversation, not the mining
logic itself.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, fields
from types import MappingProxyType
from typing import Any, Mapping, TypeVar

RUNNER_PROTOCOL_V1 = "ormas-runner-v1"
RUNNER_DEVICE_HEADER = "X-Ormas-Runner-Device"

# Miner-reported terminal state after a solve attempt. Advisory only — the
# validator's own re-run of the verify command is what determines payment
# (decision doc §8); a miner claiming "verified" is not the acceptance oracle.
VERIFICATION_STATES = frozenset(
    {
        "verified",
        "failed",
        "scope_violation",
        "setup_failure",
        "repair_refused",
        "publish_failed",
        "budget_exceeded",
        "aborted",
    }
)

__all__ = [
    "RUNNER_PROTOCOL_V1",
    "RUNNER_DEVICE_HEADER",
    "VERIFICATION_STATES",
    "RunnerRegistration",
    "RepoRegistration",
    "TaskDraft",
    "TaskLease",
    "TaskEvent",
    "TaskReceipt",
    "TaskTerminal",
]

_T = TypeVar("_T", bound="_RunnerWireDTO")

# Fields the wire protocol refuses everywhere — client-local secrets and paths
# must never cross to the gateway. Matches
# ``tensorbox_spec.mcp_server.ormas_http_client._FORBIDDEN_RUNNER_WIRE_FIELDS``.
_FORBIDDEN_RUNNER_WIRE_FIELDS = frozenset(
    {
        "provider_key",
        "repo_path",
        "raw_source",
        "raw_prompt",
        "raw_output",
        "raw_diff",
        "tenant_id",
        "client_0",
        "rao",
    }
)

# Landed tb-web path-segment grammar for runner lease task IDs (max 128 chars).
_RUNNER_TASK_ID_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?$"
)


def _wire_value(value: Any) -> Any:
    """Return a JSON-shaped copy of one DTO field without exposing internals."""
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, tuple):
        return list(value)
    return value


def require_task_id(task_id: str) -> str:
    """Reject untrusted path segments before they reach a dynamic runner URL."""
    if not isinstance(task_id, str) or _RUNNER_TASK_ID_RE.fullmatch(task_id) is None:
        raise ValueError("invalid task_id")
    return task_id


def snapshot_evidence(value: Any) -> Any:
    """Deep-copy evidence into plain JSON-shaped containers; fail closed on risk.

    Caller-owned mappings/lists must not ride the wire by reference. Nested keys
    in ``_FORBIDDEN_RUNNER_WIRE_FIELDS`` and non-JSON values are rejected — never
    stringified or dropped.
    """
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (str, int, float)):
        return value
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("evidence mapping keys must be strings")
            if key in _FORBIDDEN_RUNNER_WIRE_FIELDS:
                raise ValueError(f"forbidden runner V1 field: {key}")
            out[key] = snapshot_evidence(item)
        return out
    if isinstance(value, (list, tuple)):
        return [snapshot_evidence(item) for item in value]
    raise ValueError("unsupported evidence value")


class _RunnerWireDTO:
    """Shared strict serialization for the V1 miner control-plane DTOs.

    The DTOs intentionally carry sanitized task metadata only. A provider
    credential, absolute local repository path, raw source, or any
    caller-selected tenant identifier must never cross this seam.
    """

    def to_wire(self) -> dict[str, Any]:
        return {
            "schema_version": RUNNER_PROTOCOL_V1,
            **{field.name: _wire_value(getattr(self, field.name)) for field in fields(self)},
        }

    @classmethod
    def from_wire(cls: type[_T], payload: Mapping[str, Any]) -> _T:
        if not isinstance(payload, Mapping):
            raise ValueError("payload must be a mapping")

        keys = set(payload)
        forbidden = sorted(keys & _FORBIDDEN_RUNNER_WIRE_FIELDS)
        if forbidden:
            raise ValueError(f"forbidden runner V1 field: {forbidden[0]}")

        expected = {field.name for field in fields(cls)}
        allowed = expected | {"schema_version"}
        unknown = sorted(keys - allowed)
        if unknown:
            raise ValueError(f"unknown runner V1 field: {unknown[0]}")

        if payload.get("schema_version") != RUNNER_PROTOCOL_V1:
            raise ValueError("schema_version must be 'ormas-runner-v1'")

        missing = sorted(expected - keys)
        if missing:
            raise ValueError(f"missing runner V1 field: {missing[0]}")

        return cls(**{field.name: payload[field.name] for field in fields(cls)})


@dataclass(frozen=True)
class RunnerRegistration(_RunnerWireDTO):
    runner_id: str
    runner_version: str
    platform: str
    capacity: int
    health: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "health", MappingProxyType(dict(self.health)))


@dataclass(frozen=True)
class RepoRegistration(_RunnerWireDTO):
    repo_id: str
    display_alias: str
    base_commit: str
    preflight_state: str


@dataclass(frozen=True)
class TaskDraft(_RunnerWireDTO):
    task_id: str
    runner_id: str
    repo_id: str
    base_commit: str
    brief: str
    verify_command: str
    allowed_paths: tuple[str, ...] | list[str]
    budget_usd: float | None
    work_packet: Mapping[str, Any]
    work_packet_sha256: str
    attempt: int
    parent_job_id: str
    repair_findings: tuple[Any, ...] | list[Any]
    repair_evidence: Mapping[str, Any] | None = None
    # Credential-free clone source for a miner with no local bind (https/ssh URL,
    # never a token); empty when a bound repo_id already covers it.
    repo_url: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowed_paths", tuple(self.allowed_paths))
        object.__setattr__(self, "work_packet", MappingProxyType(dict(self.work_packet)))
        object.__setattr__(self, "repair_findings", tuple(self.repair_findings))
        evidence = self.repair_evidence
        if evidence is None:
            object.__setattr__(self, "repair_evidence", None)
            return
        snapshotted = snapshot_evidence(evidence)
        if not isinstance(snapshotted, dict):
            raise ValueError("repair_evidence must be a mapping")
        object.__setattr__(self, "repair_evidence", MappingProxyType(snapshotted))

    def to_wire(self) -> dict[str, Any]:
        payload = super().to_wire()
        evidence = self.repair_evidence
        if evidence is None:
            payload.pop("repair_evidence", None)
            return payload
        payload["repair_evidence"] = snapshot_evidence(dict(evidence))
        return payload

    @classmethod
    def from_wire(cls, payload: Mapping[str, Any]) -> TaskDraft:
        if not isinstance(payload, Mapping):
            raise ValueError("payload must be a mapping")
        data = dict(payload)
        data.setdefault("repair_evidence", None)
        data.setdefault("repo_url", "")
        return super().from_wire(data)


@dataclass(frozen=True)
class TaskLease(_RunnerWireDTO):
    lease_id: str
    task_id: str
    expires_at: str
    selected_cell: str
    provider_pin: str
    fallback_policy: str
    hold_ref: str
    now: str
    outcome_price_usd: float


@dataclass(frozen=True)
class TaskEvent(_RunnerWireDTO):
    lease_id: str
    state: str
    occurred_at: str
    error_category: str | None


@dataclass(frozen=True)
class TaskReceipt(_RunnerWireDTO):
    lease_id: str
    generation_ids: tuple[str, ...] | list[str]
    actual_provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    cache_read_input_tokens: int
    cache_creation_input_tokens: int
    reasoning_tokens: int
    upstream_cost_usd: float | None
    finish_reason: str | None
    metering_complete: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "generation_ids", tuple(self.generation_ids))


@dataclass(frozen=True)
class TaskTerminal(_RunnerWireDTO):
    lease_id: str
    verification_state: str
    result_ref: str | None
    settlement_state: str
    rating: str | None
    result_commit: str
