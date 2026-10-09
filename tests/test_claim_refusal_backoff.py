"""Claim refusals back off on a configurable, bounded schedule and are visible in claim health."""
from __future__ import annotations

import time

import httpx
import pytest

from ormas_subnet.client import OrmasMinerClient
from ormas_subnet.localnet import LocalResponse
from ormas_subnet.skeleton import MinerConfig, MinerSkeleton


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


def _miner(tmp_path, outcomes, **backoff):
    transport = _ClaimTransport(outcomes)
    client = OrmasMinerClient("https://fake.invalid", "ormr_test", http_client=transport)
    try:
        config = MinerConfig(
            runner_id="miner-1", runner_version="test", platform="test", capacity=1,
            cells=("task:code",), workdir_root=tmp_path, repo_id="repo-1", repo_url="unused",
            **backoff,
        )
    except TypeError as exc:
        raise AssertionError(f"MinerConfig lacks claim refusal backoff fields: {exc}") from None
    miner = MinerSkeleton(client, config, lambda *_: pytest.fail("idle claim ran solver"))
    return miner, transport


def _health(miner):
    assert callable(getattr(miner, "claim_health", None)), "MinerSkeleton.claim_health() missing"
    health = miner.claim_health()
    assert isinstance(health, dict), health
    return health


def test_refusals_double_up_to_the_maximum_and_reset_on_2xx(tmp_path):
    miner, transport = _miner(
        tmp_path, [503, 503, 503, 503, 204, 503],
        claim_refusal_backoff_s=2.0, claim_refusal_backoff_max_s=5.0,
    )
    sleeps = []
    miner.run_forever(max_iterations=6, idle_sleep=sleeps.append)
    assert transport.claims == 6
    assert sleeps == [2.0, 4.0, 5.0, 5.0, miner.poll_interval_s, 2.0]


def test_default_backoff_is_the_poll_interval(tmp_path):
    miner, transport = _miner(tmp_path, [429, 429, 204])
    sleeps = []
    miner.run_forever(max_iterations=3, idle_sleep=sleeps.append)
    assert sleeps == [miner.poll_interval_s] * 3
    assert _health(miner)["consecutive_claim_refusals"] == 0


def test_claim_health_records_the_last_refusal(tmp_path):
    miner, _ = _miner(tmp_path, [429, 204], claim_refusal_backoff_s=3.0,
                      claim_refusal_backoff_max_s=3.0)
    assert _health(miner) == {"last_claim_refusal": None, "consecutive_claim_refusals": 0}
    before = time.time()
    miner.run_forever(max_iterations=1, idle_sleep=lambda _: None)
    after = time.time()
    health = _health(miner)
    refusal = health["last_claim_refusal"]
    assert health["consecutive_claim_refusals"] == 1
    assert refusal["status"] == 429
    assert refusal["type"] == "daily_claim_cap"
    assert isinstance(refusal["at"], float) and before <= refusal["at"] <= after
    miner.run_forever(max_iterations=1, idle_sleep=lambda _: None)
    health = _health(miner)
    assert health["consecutive_claim_refusals"] == 0
    assert health["last_claim_refusal"] == refusal


def test_transport_errors_are_not_refusals(tmp_path):
    miner, _ = _miner(tmp_path, [httpx.ConnectError("offline"), 204],
                      claim_refusal_backoff_s=2.0, claim_refusal_backoff_max_s=8.0)
    sleeps = []
    miner.run_forever(max_iterations=2, idle_sleep=sleeps.append)
    assert sleeps == [miner.poll_interval_s, miner.poll_interval_s]
    assert _health(miner) == {"last_claim_refusal": None, "consecutive_claim_refusals": 0}
