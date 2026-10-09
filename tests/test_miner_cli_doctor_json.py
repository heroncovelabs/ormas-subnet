"""`ormas-miner doctor --json`: one JSON object carrying the doctor's facts."""
from __future__ import annotations

import importlib
import json
import os

import httpx
import pytest

TOKEN = "ormr_test_private_abcd"
GATEWAY = "https://api.ormas.ai"
STANDARD_CELLS = [
    "task:code", "task:code/small", "task:lang/python",
    "task:publication/github-artifact-v1", "task:acceptance/operator-run-v3",
    "task:preflight/deferred-v1",
]


@pytest.fixture
def cli(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("ORMAS_MINER_TOKEN", raising=False)
    module = importlib.import_module("ormas_subnet.miner_cli")
    # The doctor only looks git up on PATH; keep these checks independent of the host.
    monkeypatch.setattr(module.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setenv("PATH", str(tmp_path / ".local" / "bin") + os.pathsep + os.defpath)
    path = tmp_path / ".ormas-miner" / "credentials.json"
    path.parent.mkdir(mode=0o700)
    path.write_text(json.dumps({"gateway": GATEWAY, "token": TOKEN}))
    path.chmod(0o600)
    return module


@pytest.fixture
def gateway(monkeypatch):
    responses = {
        "/health": (200, {"status": "ok"}),
        "/api/runner/v1/queue": (200, {"jobs": [{"job_id": "job_a"}, {"job_id": "job_b"}]}),
        "/api/runner/v1/runners/me": (200, {
            "runner_id": "runr_test", "cells": list(STANDARD_CELLS), "claim_cap": 1,
            "hotkey": {"bound": True, "verified": True},
            "qualification": {"job_status": "done"},
        }),
    }
    client_class = httpx.Client

    def handler(request):
        status, payload = responses[request.url.path]
        return httpx.Response(status, json=payload)

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return client_class(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", client)
    return responses


def _doctor_json(cli, capsys, *extra):
    try:
        code = cli.main(["doctor", "--json", *extra])
    except SystemExit as exc:
        raise AssertionError(f"doctor rejected --json (exit {exc.code})") from None
    out = capsys.readouterr().out
    assert TOKEN not in out
    try:
        report = json.loads(out)
    except ValueError:
        raise AssertionError(f"doctor --json stdout is not one JSON object: {out[:500]!r}")
    assert isinstance(report, dict), report
    return code, report


def test_doctor_json_success_reports_all_facts(cli, gateway, capsys):
    code, report = _doctor_json(cli, capsys)
    assert code == 0
    assert report["ok"] is True
    assert report["gateway"] == {"url": GATEWAY, "reachable": True}
    assert report["runner_id"] == "runr_test"
    assert report["qualification"] == {"cap": 1, "status": "done"}
    assert report["cells"] == {"ok": True, "sizes": ["small"], "missing": []}
    assert report["queue"] == {"visible": 2}


def test_doctor_json_reports_missing_cells_and_fails(cli, gateway, capsys):
    gateway["/api/runner/v1/runners/me"][1]["cells"].remove("task:preflight/deferred-v1")
    code, report = _doctor_json(cli, capsys)
    assert code == 1
    assert report["ok"] is False
    assert report["cells"]["ok"] is False
    assert "task:preflight/deferred-v1" in report["cells"]["missing"]


def test_doctor_json_unreachable_gateway(cli, gateway, capsys):
    gateway["/health"] = (503, {"status": "down"})
    code, report = _doctor_json(cli, capsys)
    assert code == 1
    assert report["ok"] is False
    assert report["gateway"] == {"url": GATEWAY, "reachable": False}
    assert report["runner_id"] is None
    assert report["queue"] == {"visible": None}


def test_doctor_without_json_keeps_human_output(cli, gateway, capsys):
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("ok python:")
    assert "ok gateway: https://api.ormas.ai/health reachable" in out
    assert "cells: ok (sizes: small)" in out
    assert "qualification: cap 1 (job: done)" in out
    assert not out.lstrip().startswith("{")
