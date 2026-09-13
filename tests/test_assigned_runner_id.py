"""First registration: the gateway ASSIGNS the runner id; the skeleton must adopt it.

Found live on api.ormas.ai (gateway-2026.09.13) during the Loop A acceptance rehearsal:
``runner_api`` registration with a self-chosen, never-seen ``runner_id`` returns 404
(``outcomes_runners.upsert_runner`` refuses an unknown assigned id); an EMPTY ``runner_id``
makes the gateway mint ``runr_<12hex>`` and return it. The skeleton sent the configured id
verbatim and never read ``runner_id`` back, so a newcomer could not register at all.
Rule: ``MinerSkeleton.register`` adopts the response's ``runner_id`` into ``config.runner_id``
(so claim/heartbeat/complete use it), and the miner CLI accepts a missing ``--runner-id``
(first run) and prints the assigned id so the operator can pass it next time.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ormas_subnet.client import OrmasMinerClient
from ormas_subnet.skeleton import MinerConfig, MinerSkeleton, SolveResult


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
    """Registration with empty runner_id assigns runr_assigned01; a known id refreshes;
    an unknown non-empty id is refused 404 (the live gateway rule)."""

    KNOWN = "runr_assigned01"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def post(self, path: str, json: dict[str, Any] | None = None, headers: Any = None) -> _FakeResponse:
        body = json or {}
        self.calls.append((path, body))
        if path == "/api/runner/v1/registrations":
            rid = body.get("runner_id", "")
            if rid in ("", self.KNOWN):
                return _FakeResponse(200, {"runner_id": self.KNOWN, "poll_interval_s": 5,
                                           "lease_ttl_s": 60, "heartbeat_s": 20,
                                           "protocol": "ormas-runner-v1"})
            return _FakeResponse(404, {"error": {"type": "not_found_error", "message": "not found"}})
        if path == "/api/runner/v1/leases":
            return _FakeResponse(204)
        raise AssertionError(f"unexpected route {path}")


def _skeleton(transport: Any, runner_id: str, tmp_path: Path) -> MinerSkeleton:
    client = OrmasMinerClient(base_url="https://fake.invalid", token="ormr_test", http_client=transport)
    config = MinerConfig(
        runner_id=runner_id, runner_version="test", platform="test", capacity=1,
        cells=("task:code",), workdir_root=tmp_path,
        repo_id="third-party", repo_url="https://fake.invalid/repo.git", push_remote=None,
    )

    def solve(draft: Any, workdir: Path) -> SolveResult:  # never called here
        raise AssertionError("solve must not run")

    return MinerSkeleton(client, config, solve)


def test_register_with_empty_id_adopts_the_assigned_runner_id(tmp_path: Path) -> None:
    gw = _AssigningGateway()
    sk = _skeleton(gw, "", tmp_path)
    resp = sk.register()
    assert resp["runner_id"] == "runr_assigned01"
    assert sk.config.runner_id == "runr_assigned01"
    # The next claim carries the ASSIGNED id, not the empty string.
    assert sk.run_once() is False  # 204 idle
    path, body = gw.calls[-1]
    assert path == "/api/runner/v1/leases" and body["runner_id"] == "runr_assigned01"


def test_register_with_known_id_keeps_it(tmp_path: Path) -> None:
    gw = _AssigningGateway()
    sk = _skeleton(gw, "runr_assigned01", tmp_path)
    sk.register()
    assert sk.config.runner_id == "runr_assigned01"
    assert gw.calls[0][1]["runner_id"] == "runr_assigned01"


def test_miner_cli_runner_id_is_optional_on_first_run() -> None:
    from neurons import miner

    ns = miner.build_parser().parse_args([
        "--gateway", "https://fake.invalid", "--token-env", "T",
        "--repo-id", "third-party", "--repo-url", "https://fake.invalid/r.git",
        "--cell", "task:code", "--solve-command", "true",
    ])
    assert ns.runner_id in (None, "")
    # _parse must not demand --runner-id (the gateway assigns it on first registration).
    ns2 = miner._parse([
        "--gateway", "https://fake.invalid", "--token-env", "T",
        "--repo-id", "third-party", "--repo-url", "https://fake.invalid/r.git",
        "--cell", "task:code", "--solve-command", "true",
    ])
    assert ns2.command is None
