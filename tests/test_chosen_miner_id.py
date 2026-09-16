"""Optional chosen miner_id on runner registration (gateway-2026.09.16.2).

When present, claims, receipts (worker_id) and the hotkey identity bind to
``miner:<miner_id>`` instead of ``miner:<tenant>``. Absent, the registration
body is byte-identical to today.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ormas_subnet.client import OrmasMinerClient
from ormas_subnet.protocol import RUNNER_PROTOCOL_V1, RunnerRegistration
from ormas_subnet.skeleton import MinerConfig, MinerSkeleton, SolveResult


def _registration(**kwargs: Any) -> RunnerRegistration:
    fields = dict(
        runner_id="r1",
        runner_version="0.1",
        platform="linux",
        capacity=1,
        health={"cells": ["task:code"]},
    )
    fields.update(kwargs)
    return RunnerRegistration(**fields)


def test_to_wire_includes_miner_id_when_set() -> None:
    wire = _registration(miner_id="jake-miner").to_wire()
    assert wire["miner_id"] == "jake-miner"
    assert wire["schema_version"] == RUNNER_PROTOCOL_V1


def test_to_wire_omits_miner_id_when_absent() -> None:
    wire = _registration().to_wire()
    assert "miner_id" not in wire


def test_from_wire_round_trips_chosen_and_absent_miner_id() -> None:
    chosen = _registration(miner_id="jake-miner")
    restored = RunnerRegistration.from_wire(chosen.to_wire())
    assert restored.miner_id == "jake-miner"
    assert restored.to_wire()["miner_id"] == "jake-miner"

    absent = _registration()
    restored_absent = RunnerRegistration.from_wire(absent.to_wire())
    assert restored_absent.miner_id is None
    assert "miner_id" not in restored_absent.to_wire()


class _FakeResponse:
    def __init__(self, status_code: int, body: Any = None) -> None:
        self.status_code = status_code
        self._body = body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}: {self._body}")

    def json(self) -> Any:
        return self._body


class _AssigningGateway:
    """Empty runner_id assigns runr_assigned01; echoes miner_id when sent."""

    KNOWN = "runr_assigned01"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def post(self, path: str, json: dict[str, Any] | None = None, headers: Any = None) -> _FakeResponse:
        body = json or {}
        self.calls.append((path, body))
        if path == "/api/runner/v1/registrations":
            rid = body.get("runner_id", "")
            if rid in ("", self.KNOWN):
                out: dict[str, Any] = {
                    "runner_id": self.KNOWN,
                    "poll_interval_s": 5,
                    "lease_ttl_s": 60,
                    "heartbeat_s": 20,
                    "protocol": "ormas-runner-v1",
                }
                if "miner_id" in body:
                    out["miner_id"] = body["miner_id"]
                return _FakeResponse(200, out)
            return _FakeResponse(404, {"error": {"type": "not_found_error", "message": "not found"}})
        if path == "/api/runner/v1/leases":
            return _FakeResponse(204)
        raise AssertionError(f"unexpected route {path}")


def _skeleton(transport: Any, tmp_path: Path, **config_kw: Any) -> MinerSkeleton:
    client = OrmasMinerClient(base_url="https://fake.invalid", token="ormr_test", http_client=transport)
    config = MinerConfig(
        runner_id="",
        runner_version="test",
        platform="test",
        capacity=1,
        cells=("task:code",),
        workdir_root=tmp_path,
        repo_id="third-party",
        repo_url="https://fake.invalid/repo.git",
        push_remote=None,
        **config_kw,
    )

    def solve(draft: Any, workdir: Path) -> SolveResult:  # never called here
        raise AssertionError("solve must not run")

    return MinerSkeleton(client, config, solve)


def test_register_sends_configured_miner_id_and_adopts_runner_id(tmp_path: Path) -> None:
    gw = _AssigningGateway()
    sk = _skeleton(gw, tmp_path, miner_id="jake-miner")
    resp = sk.register()
    path, body = gw.calls[0]
    assert path == "/api/runner/v1/registrations"
    assert body["miner_id"] == "jake-miner"
    assert resp["runner_id"] == "runr_assigned01"
    assert resp["miner_id"] == "jake-miner"
    assert sk.config.runner_id == "runr_assigned01"


def test_register_default_sends_no_miner_id(tmp_path: Path) -> None:
    gw = _AssigningGateway()
    sk = _skeleton(gw, tmp_path)
    resp = sk.register()
    path, body = gw.calls[0]
    assert path == "/api/runner/v1/registrations"
    assert "miner_id" not in body
    assert "miner_id" not in resp
    assert sk.config.runner_id == "runr_assigned01"
