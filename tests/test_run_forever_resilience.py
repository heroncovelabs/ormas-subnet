"""Claim refusals idle the daemon; library and post-claim failures still propagate."""
from __future__ import annotations

import logging

import httpx
import pytest

from ormas_subnet.client import OrmasGatewayError, OrmasMinerClient
from ormas_subnet.localnet import LocalResponse
from ormas_subnet.skeleton import MinerConfig, MinerSkeleton, RecoveryRequired


class _ClaimTransport:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.claims = 0

    def post(self, path, **kwargs):
        assert path.endswith("/leases")
        self.claims += 1
        outcome = next(self.outcomes)
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome == 204:
            return LocalResponse(204, {})
        return LocalResponse(outcome, {
            "error": {"type": "daily_claim_cap" if outcome == 429 else "refused",
                      "message": "claim unavailable"},
        })


def _miner(tmp_path, outcomes):
    transport = _ClaimTransport(outcomes)
    client = OrmasMinerClient("https://fake.invalid", "ormr_test", http_client=transport)
    config = MinerConfig(
        runner_id="miner-1", runner_version="test", platform="test", capacity=1,
        cells=("task:code",), workdir_root=tmp_path, repo_id="repo-1", repo_url="unused",
    )
    return MinerSkeleton(client, config, lambda *_: pytest.fail("idle claim ran solver")), transport


@pytest.mark.parametrize("status", [429, 503])
def test_refusal_then_204_continues_claiming(tmp_path, caplog, status):
    miner, transport = _miner(tmp_path, [status, 204])
    sleeps = []
    with caplog.at_level(logging.WARNING):
        miner.run_forever(max_iterations=2, idle_sleep=sleeps.append)
    assert transport.claims == 2
    assert sleeps == [miner.poll_interval_s, miner.poll_interval_s]
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.WARNING
    assert str(status) in caplog.text
    assert ("daily_claim_cap" if status == 429 else "refused") in caplog.text
    assert "claim unavailable" in caplog.text


@pytest.mark.parametrize("status", [401, 403])
def test_credential_refusal_propagates(tmp_path, caplog, status):
    miner, transport = _miner(tmp_path, [status])
    sleeps = []
    with pytest.raises(OrmasGatewayError) as caught:
        miner.run_forever(max_iterations=2, idle_sleep=sleeps.append)
    assert caught.value.status_code == status
    assert transport.claims == 1
    assert sleeps == []
    assert caplog.records == []


@pytest.mark.parametrize("error", [httpx.ConnectError("connection refused"), OSError("offline")])
def test_transport_failure_continues_at_idle_cadence(tmp_path, caplog, error):
    miner, transport = _miner(tmp_path, [error, 204])
    sleeps = []
    with caplog.at_level(logging.WARNING):
        miner.run_forever(max_iterations=2, idle_sleep=sleeps.append)
    assert transport.claims == 2
    assert sleeps == [miner.poll_interval_s, miner.poll_interval_s]
    assert len(caplog.records) == 1
    assert type(error).__name__ in caplog.text
    assert str(error) in caplog.text


def test_consecutive_failures_use_poll_override(tmp_path, caplog):
    miner, transport = _miner(tmp_path, [503, 429, 503, 204])
    sleeps = []
    with caplog.at_level(logging.WARNING):
        miner.run_forever(poll_interval_s=7, max_iterations=4, idle_sleep=sleeps.append)
    assert transport.claims == 4
    assert sleeps == [7, 7, 7, 7]
    assert len(caplog.records) == 3


@pytest.mark.parametrize("outcome", [429, 503, httpx.ConnectError("offline"), OSError("offline")])
def test_run_once_still_propagates_claim_failures(tmp_path, outcome):
    miner, transport = _miner(tmp_path, [outcome])
    expected = OrmasGatewayError if isinstance(outcome, int) else type(outcome)
    with pytest.raises(expected):
        miner.run_once()
    assert transport.claims == 1


@pytest.mark.parametrize("error", [KeyboardInterrupt(), RecoveryRequired("recover first")])
def test_interrupt_and_recovery_required_propagate(tmp_path, error):
    miner, transport = _miner(tmp_path, [error])
    sleeps = []
    with pytest.raises(type(error)):
        miner.run_forever(max_iterations=2, idle_sleep=sleeps.append)
    assert transport.claims == 1
    assert sleeps == []


@pytest.mark.parametrize("error", [
    OrmasGatewayError(status_code=503, error_type="refused", message="recovery failed"),
    httpx.ConnectError("recovery failed"), OSError("recovery failed"),
])
def test_non_claim_failure_is_not_absorbed(tmp_path, monkeypatch, error):
    miner, _ = _miner(tmp_path, [204])

    def fail():
        raise error

    monkeypatch.setattr(miner, "run_once", fail)
    with pytest.raises(type(error)) as caught:
        miner.run_forever(max_iterations=1, idle_sleep=lambda _: None)
    assert caught.value is error
