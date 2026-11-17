"""Recover a lost offer POST by reading the runner's own bid for that job.

The reference miner must not resubmit the POST. Closure comes from the
gateway's ``window_closed`` answer.
"""
from __future__ import annotations

import json
import logging

import httpx
import pytest

from ormas_subnet._recovery import PublicRecovery, RecoveryRequired
from ormas_subnet.client import OrmasGatewayError, OrmasMinerClient
from ormas_subnet.protocol import RUNNER_DEVICE_HEADER
from ormas_subnet.skeleton import MinerSkeleton
from .test_ask_usd import _FakeResponse
from .test_offer_window import offers, window_miner


def _lost_offer(tmp_path):
    """A poll whose offer POST raised after the intent was saved."""
    transport, miner = window_miner(tmp_path)
    transport.error = RuntimeError("response lost")
    with pytest.raises(RuntimeError, match="response lost"):
        miner.run_once()
    transport.error = None
    transport.calls.clear()
    journal = next(tmp_path.rglob("offers.json"))
    state = json.loads(journal.read_text())
    restarted = MinerSkeleton(miner.client, miner.config, miner.solve_fn)
    return transport, restarted, journal, state


def _recovery_reads(transport):
    return [call for call in transport.calls
            if call[0] == "GET" and call[1].startswith("/api/runner/v1/offers?")]


def test_get_offer_for_job_encodes_query_and_rejects_blank_ids():
    seen = []

    class Transport:
        def get(self, path, headers=None):
            seen.append(("GET", path, headers))
            return _FakeResponse(200, {"job_id": "job/1", "window_closed": False, "bid": None})

        def post(self, path, json=None, headers=None):
            seen.append(("POST", path))
            raise AssertionError("get_offer_for_job must not POST")

    client = OrmasMinerClient(
        "https://fake.invalid", "ormr_test", http_client=Transport(), device_nonce="device-1")
    assert client.get_offer_for_job("run r", "job/1") == {
        "job_id": "job/1", "window_closed": False, "bid": None}
    assert seen == [(
        "GET",
        "/api/runner/v1/offers?runner_id=run%20r&job_id=job%2F1",
        {RUNNER_DEVICE_HEADER: "device-1"},
    )]
    seen.clear()
    for runner_id, job_id in [("", "job"), ("  ", "job"), ("run", ""), ("run", " "),
                              (None, "job"), ("run", None), (1, "job"), ("run", 1)]:
        with pytest.raises(ValueError):
            client.get_offer_for_job(runner_id, job_id)
    assert seen == []


def test_get_offer_for_job_shares_bearer_headers_and_raises_on_http_error():
    seen = []

    def handle(request):
        seen.append(request)
        if request.url.params.get("job_id") == "missing":
            return httpx.Response(404, json={"detail": "Not Found"})
        if request.url.params.get("job_id") == "down":
            return httpx.Response(503, json={"error": {"type": "service_unavailable", "message": "down"}})
        return httpx.Response(200, json={"job_id": request.url.params["job_id"],
                                          "window_closed": True, "bid": None})

    with httpx.Client(
        base_url="https://fake.invalid",
        headers={"Authorization": "Bearer ormr_test"},
        transport=httpx.MockTransport(handle),
    ) as transport:
        client = OrmasMinerClient(
            "https://fake.invalid", "ormr_test", http_client=transport, device_nonce="device-1")
        assert client.get_offer_for_job("runner-1", "job-1")["window_closed"] is True
        with pytest.raises(OrmasGatewayError) as missing:
            client.get_offer_for_job("runner-1", "missing")
        with pytest.raises(OrmasGatewayError) as down:
            client.get_offer_for_job("runner-1", "down")
    assert missing.value.status_code == 404 and missing.value.error_type is None
    assert (down.value.status_code, down.value.error_type, down.value.message) == (
        503, "service_unavailable", "down")
    assert [request.method for request in seen] == ["GET", "GET", "GET"]
    for request in seen:
        assert request.url.path == "/api/runner/v1/offers"
        assert request.headers["Authorization"] == "Bearer ormr_test"
        assert request.headers[RUNNER_DEVICE_HEADER] == "device-1"


@pytest.mark.parametrize("status", ["open", "awarded"])
def test_pending_offer_adopts_matching_bid(tmp_path, status):
    transport, miner, journal, state = _lost_offer(tmp_path)
    intent = state["pending"]
    bid = {
        "bid_id": "bid_recovered",
        "status": status,
        "terms": intent,
        "ack_by_at": "2026-10-08T00:00:01Z",
        "window_closes_at": "2026-10-08T00:10:00Z",
    }
    if status == "awarded":
        bid.update(award_id="awd_recovered", job_status="offering")
    transport.recovery_response = _FakeResponse(200, {
        "job_id": intent["job_id"], "window_closed": status == "awarded", "bid": bid})
    assert miner.run_once() is False
    saved = json.loads(journal.read_text())
    assert saved["pending"] is None
    assert saved["current"]["task_1"] == "bid_recovered"
    recorded = saved["bids"]["bid_recovered"]
    assert recorded["status"] == status
    assert recorded["terms"] == intent
    assert recorded["window_closes_at"] == "2026-10-08T00:10:00Z"
    if status == "awarded":
        assert recorded["award_id"] == "awd_recovered"
    else:
        assert "award_id" not in recorded
    assert offers(transport) == []
    assert len(_recovery_reads(transport)) == 1
    assert transport.calls[0][1].startswith("/api/runner/v1/offers?")
    queue_at = next(i for i, call in enumerate(transport.calls) if "/queue?" in call[1])
    assert transport.calls.index(_recovery_reads(transport)[0]) < queue_at
    assert transport.source.completed is None


def test_closed_window_clears_null_bid_and_offers_other_work(tmp_path, caplog):
    transport, miner, journal, state = _lost_offer(tmp_path)
    transport.jobs.append(
        {"job_id": "task_2", "created_at": "2026-10-08T00:00:00Z", "envelope": {}})
    transport.recovery_response = _FakeResponse(200, {
        "job_id": "task_1", "window_closed": True, "bid": None})
    assert miner.run_once() is False
    saved = json.loads(journal.read_text())
    assert saved["pending"] is None
    assert "task_1" not in saved["current"]
    assert all(bid["terms"]["job_id"] != "task_1" for bid in saved["bids"].values())
    assert saved["current"]["task_2"]
    assert [call[2]["job_id"] for call in offers(transport)] == ["task_2"]
    assert len(_recovery_reads(transport)) == 1
    assert any(record.levelno == logging.WARNING and "task_1" in record.message
               for record in caplog.records)


@pytest.mark.parametrize("already_saved", [False, True])
def test_closed_window_drops_terminal_bid_that_cannot_be_adopted(tmp_path, caplog, already_saved):
    if already_saved:
        transport, miner = window_miner(tmp_path)
        assert miner.run_once() is False
        journal = next(tmp_path.rglob("offers.json"))
        state = json.loads(journal.read_text())
        original_terms = state["bids"]["bid_1"]["terms"]
        state["bids"]["bid_1"]["status"] = "not_awarded"
        state["pending"] = {"job_id": "task_1", "kind": "firm", "price_usd": 9}
        journal.write_text(json.dumps(state))
        transport.bids["bid_1"]["status"] = "not_awarded"
        transport.calls.clear()
        restarted = MinerSkeleton(miner.client, miner.config, miner.solve_fn)
        response_bid = {
            "bid_id": "bid_1",
            "status": "not_awarded",
            "terms": original_terms,
            "ack_by_at": "ack",
            "window_closes_at": "2026-10-08T00:10:00Z",
        }
    else:
        transport, restarted, journal, state = _lost_offer(tmp_path)
        original_terms = state["pending"]
        response_bid = {
            "bid_id": "bid_other",
            "status": "withdrawn",
            "terms": {**original_terms, "price_usd": 9},
            "ack_by_at": "ack",
            "window_closes_at": "2026-10-08T00:10:00Z",
        }
    transport.recovery_response = _FakeResponse(200, {
        "job_id": "task_1", "window_closed": True, "bid": response_bid})
    assert restarted.run_once() is False
    saved = json.loads(journal.read_text())
    assert saved["pending"] is None
    assert offers(transport) == []
    assert len(_recovery_reads(transport)) == 1
    assert any(record.levelno == logging.WARNING and "task_1" in record.message
               for record in caplog.records)
    if already_saved:
        assert saved["bids"]["bid_1"]["terms"] == original_terms
        assert saved["bids"]["bid_1"]["status"] == "not_awarded"
        assert "bid_other" not in saved["bids"]
    else:
        assert saved["bids"] == {}
        assert "bid_other" not in saved["bids"]


def test_open_window_without_bid_keeps_pending(tmp_path):
    transport, miner, journal, _state = _lost_offer(tmp_path)
    before = journal.read_bytes()
    transport.recovery_response = _FakeResponse(200, {
        "job_id": "task_1", "window_closed": False, "bid": None})
    with pytest.raises(RecoveryRequired, match="offer|bid"):
        miner.run_once()
    assert journal.read_bytes() == before
    assert offers(transport) == []
    assert _recovery_reads(transport) and not any("/queue?" in call[1] for call in transport.calls)


@pytest.mark.parametrize("failure", [
    "http_500",
    "transport",
    "untyped_404",
    "missing_job_id",
    "missing_window_closed",
    "missing_bid",
    "wrong_job_id",
    "window_closed_not_bool",
    "bid_without_id",
    "empty_bid_id",
    "bad_status",
    "non_object",
])
def test_unreadable_pending_offer_keeps_intent(tmp_path, failure):
    transport, miner, journal, _state = _lost_offer(tmp_path)
    before = journal.read_bytes()
    if failure == "http_500":
        transport.recovery_response = _FakeResponse(
            503, {"error": {"type": "service_unavailable", "message": "down"}})
    elif failure == "transport":
        transport.recovery_response = RuntimeError("connection reset")
    elif failure == "untyped_404":
        transport.recovery_response = _FakeResponse(404, {"detail": "Not Found"})
    elif failure == "non_object":
        transport.recovery_response = _FakeResponse(200, ["nope"])
    else:
        body = {"job_id": "task_1", "window_closed": True, "bid": {
            "bid_id": "bid_x", "status": "open"}}
        if failure == "missing_job_id":
            body.pop("job_id")
        elif failure == "missing_window_closed":
            body.pop("window_closed")
        elif failure == "missing_bid":
            body.pop("bid")
        elif failure == "wrong_job_id":
            body["job_id"] = "other"
        elif failure == "window_closed_not_bool":
            body["window_closed"] = "true"
        elif failure == "bid_without_id":
            body["bid"] = {"status": "open"}
        elif failure == "empty_bid_id":
            body["bid"] = {"bid_id": "", "status": "open"}
        elif failure == "bad_status":
            body["bid"] = {"bid_id": "bid_x", "status": "mystery"}
        transport.recovery_response = _FakeResponse(200, body)
    with pytest.raises(RecoveryRequired, match="offer|bid"):
        miner.run_once()
    assert journal.read_bytes() == before
    assert json.loads(before)["pending"]["job_id"] == "task_1"
    assert offers(transport) == []
    assert len(_recovery_reads(transport)) == 1
    assert not any("/queue?" in call[1] for call in transport.calls)


def test_direct_offer_state_stays_fail_closed_when_recovery_is_bypassed(tmp_path):
    _transport, miner, _journal, state = _lost_offer(tmp_path)
    miner._public_recovery = PublicRecovery(
        miner.config.workdir_root, miner.client.base_url, miner.config.runner_id)
    with pytest.raises(RecoveryRequired, match="offer POST outcome unknown"):
        miner._offer_state()
    loaded = miner._offer_state(allow_pending=True)
    assert loaded["pending"] == state["pending"]
    assert loaded["bids"] == {}
