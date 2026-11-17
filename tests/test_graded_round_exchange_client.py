"""Graded round exchange: HTTP contract and an offline reference-miner loop."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from ormas_subnet import outcomes_support, skeleton
from ormas_subnet.client import OrmasGatewayError, OrmasMinerClient
from ormas_subnet.protocol import RUNNER_DEVICE_HEADER, TaskDraft, TaskLease
from ormas_subnet.skeleton import MinerConfig, MinerSkeleton, SolveResult


def client_for(handler):
    return OrmasMinerClient(
        "https://gateway.invalid", "ormr_test", device_nonce="device-test",
        http_client=httpx.Client(
            base_url="https://gateway.invalid",
            headers={"Authorization": "Bearer ormr_test"},
            transport=httpx.MockTransport(handler),
        ),
    )


def assert_auth(request):
    assert request.headers["Authorization"] == "Bearer ormr_test"
    assert request.headers[RUNNER_DEVICE_HEADER] == "device-test"


def round_request(**overrides):
    return {
        "job_id": "parent-job", "request_id": "request-1", "kind": "revision",
        "status": "pending", "contract_digest": "original-digest",
        "amended_contract_digest": "amended-digest", "acceptance_window_s": 86400,
        # Gateway numbering: outcomes_validators.py revision_request, count + 1.
        "revision_index": 1, "revision_allowance": 1,
        "revision_delivery_deadline": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        "window_closes_at": (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        **overrides,
    }


def test_inbox_get_returns_requests_with_runner_and_auth():
    seen = []
    entries = [round_request()]

    def gateway(request):
        seen.append(request)
        return httpx.Response(200, json={"requests": entries})

    client = client_for(gateway)
    assert client.list_graded_rounds("miner /1") == entries
    assert len(seen) == 1
    request = seen[0]
    assert (request.method, request.url.path) == ("GET", "/api/runner/v1/graded-rounds")
    assert dict(request.url.params) == {"runner_id": "miner /1"}
    assert_auth(request)


@pytest.mark.parametrize("decision", ["revision_agree", "revision_decline", "change_order_decline"])
def test_answer_has_exact_gateway_body(decision):
    seen = []

    def gateway(request):
        seen.append(request)
        return httpx.Response(200, json={"decision": decision})

    client = client_for(gateway)
    assert client.answer_graded_round(
        "miner-1", "request-1", job_id="parent-job", decision=decision,
        contract_digest="original-digest",
    ) == {"decision": decision}
    assert len(seen) == 1
    request = seen[0]
    assert (request.method, request.url.path) == (
        "POST", "/api/runner/v1/graded-rounds/request-1")
    assert json.loads(request.read()) == {
        "runner_id": "miner-1", "job_id": "parent-job", "decision": decision,
        "contract_digest": "original-digest",
    }
    assert_auth(request)


@pytest.mark.parametrize("operation", ["inbox", "answer", "quote"])
def test_exchange_http_errors_keep_gateway_error_type(operation):
    def gateway(request):
        return httpx.Response(409, json={"error": {
            "type": "graded_round_reply_refused", "message": "refused",
        }})

    client = client_for(gateway)
    with pytest.raises(OrmasGatewayError) as caught:
        if operation == "inbox":
            client.list_graded_rounds("miner-1")
        elif operation == "answer":
            client.answer_graded_round("miner-1", "request-1", job_id="parent-job",
                                       decision="revision_agree", contract_digest="original")
        else:
            client.submit_change_order_quote(
                "miner-1", "parent-job", client_request_id="request-1",
                contract_digest="amended", terms={"kind": "firm", "price_usd": 2.0})
    assert (caught.value.status_code, caught.value.error_type) == (
        409, "graded_round_reply_refused")


def make_miner(tmp_path, gateway, **config):
    return MinerSkeleton(
        client_for(gateway),
        MinerConfig(
            runner_id="miner-1", runner_version="test", platform="linux", capacity=1,
            cells=("task:code",), workdir_root=tmp_path, repo_id="", repo_url="",
            **config,
        ),
        lambda *_: pytest.fail("idle miner ran solver"),
    )


@pytest.mark.parametrize("overrides,decision", [
    ({}, "revision_agree"),
    ({"revision_index": 0}, "revision_decline"),
    ({"revision_index": 2}, "revision_decline"),
    ({"revision_delivery_deadline": "2000-01-01T00:00:00Z"}, "revision_decline"),
    ({"revision_delivery_deadline": "invalid"}, "revision_decline"),
    ({"status": "expired"}, None),
    ({"window_closes_at": "2000-01-01T00:00:00Z"}, None),
    ({"window_closes_at": None}, None),
    ({"revision_index": 1, "revision_allowance": 2, "window_closes_at": "2000-01-01T00:00:00Z"}, None),
    ({"status": "agreed"}, None),
    ({"status": "revision_agree"}, None),
])
def test_revision_decision_and_second_pass(tmp_path, overrides, decision):
    entry = round_request(**overrides)
    posts = []

    def gateway(request):
        if request.url.path.endswith("/leases"):
            return httpx.Response(204)
        if request.method == "GET":
            return httpx.Response(200, json={"requests": [entry]})
        posts.append(json.loads(request.read()))
        entry["status"] = "agreed" if decision == "revision_agree" else decision
        return httpx.Response(200, json={"decision": decision})

    miner = make_miner(tmp_path, gateway)
    assert miner.run_once() is False
    assert miner.run_once() is False
    assert posts == ([] if decision is None else [{
        "runner_id": "miner-1", "job_id": "parent-job", "decision": decision,
        "contract_digest": "original-digest",
    }])


@pytest.mark.parametrize("terms", [None, {"kind": "firm", "price_usd": 2.5},
                                  {"kind": "limit", "estimate_usd": 2.0, "limit_usd": 3.0}])
def test_change_order_default_decline_or_configured_quote(tmp_path, terms):
    entry = round_request(kind="change_order")
    seen = []

    def gateway(request):
        if request.url.path.endswith("/leases"):
            return httpx.Response(204)
        if request.method == "GET":
            return httpx.Response(200, json={"requests": [entry]})
        seen.append(request)
        entry["status"] = "change_order_quote" if terms else "change_order_decline"
        return httpx.Response(200, json={"job_id": "child-job", "bid_id": "bid-1"})

    config = {} if terms is None else {"change_order_terms": terms}
    miner = make_miner(tmp_path, gateway, **config)
    assert miner.run_once() is False
    assert miner.run_once() is False
    assert len(seen) == 1
    request = seen[0]
    assert request.method == "POST"
    assert_auth(request)
    if terms is None:
        assert request.url.path == "/api/runner/v1/graded-rounds/request-1"
        assert json.loads(request.read()) == {
            "runner_id": "miner-1", "job_id": "parent-job",
            "decision": "change_order_decline", "contract_digest": "original-digest",
        }
    else:
        assert request.url.path == "/api/runner/v1/offers"
        assert json.loads(request.read()) == {
            "runner_id": "miner-1", "job_id": "parent-job", "client_request_id": "request-1",
            "contract_digest": "amended-digest", **terms,
        }
        assert miner.config.change_order_terms == terms


def test_agreed_revision_uses_existing_graded_publication_and_completion(tmp_path, monkeypatch):
    """Stub solving/git/artifact construction; exercise the real custody HTTP path."""
    base = "a" * 40
    entry = round_request()
    lease = TaskLease(
        lease_id="lease-revision", task_id="revision-job", expires_at="2099-01-01T00:00:00Z",
        selected_cell="task:code", provider_pin="unset", fallback_policy="unset",
        hold_ref="unset", now="2026-11-13T00:00:00Z", outcome_price_usd=1.0,
    )
    draft = TaskDraft(
        task_id=lease.task_id, runner_id="miner-1", repo_id="repo-1", base_commit=base,
        brief="Revise criterion", verify_command="unused", allowed_paths=["value.txt"],
        budget_usd=1.0, work_packet={
            "acceptance_level": "graded",
            "graded_contract": {"base_sha": base, "immutable_paths": []},
            "execution_policy": {"repo_base_sha": base, "immutable_paths": []},
            "delivery_limits": {"max_bytes": 4096, "max_files": 2},
        }, work_packet_sha256="c" * 64, attempt=1, parent_job_id="", repair_findings=[],
        graded_round_kind="revision", graded_parent_job_id="parent-job",
    )
    seen = []
    artifact = b"frozen revision artifact"
    claimed = False

    def gateway(request):
        nonlocal claimed
        seen.append(request)
        path = request.url.path
        if path.endswith("/leases"):
            # runner_api.py:_claim_lease_impl answers an empty-offer claim 204
            # unless an own award is in 'offering'; the revision child is queued.
            if entry["status"] == "pending" or claimed or json.loads(request.read()).get("offers") == []:
                return httpx.Response(204)
            claimed = True
            return httpx.Response(200, json={"lease": lease.to_wire(), "draft": draft.to_wire()})
        if request.method == "GET":
            return httpx.Response(200, json={"requests": [entry]})
        if "/graded-rounds/" in path:
            assert json.loads(request.read())["decision"] == "revision_agree"
            entry["status"] = "revision_agree"
            return httpx.Response(200, json={"child_job_id": draft.task_id})
        if path.endswith("/heartbeat"):
            return httpx.Response(200, json={"ok": True})
        if path.endswith("/graded-artifact"):
            assert request.read() == artifact
            return httpx.Response(200, json={
                "status": "held", "artifact_digest": hashlib.sha256(artifact).hexdigest(),
            })
        assert path.endswith("/complete")
        return httpx.Response(200, json={"status": "done", "receipt": {"receipt_id": "receipt-revision", "settlement": "pending_acceptance"}})

    miner = make_miner(tmp_path, gateway)
    monkeypatch.setattr(miner, "_clone_and_checkout", lambda _: tmp_path)
    monkeypatch.setattr(miner, "_heartbeat_loop", lambda *_: None)
    monkeypatch.setattr(miner, "_material_diff_sha256", lambda *_: None)
    monkeypatch.setattr(miner, "_compute_scope", lambda *_: (True, ("value.txt",)))
    monkeypatch.setattr(skeleton, "_run_verify_command", lambda *_args, **_kwargs: 0)

    def solve(received, workdir):
        assert received.graded_round_kind == "revision"
        assert received.graded_parent_job_id == "parent-job"
        return SolveResult("b" * 40, ("value.txt",))

    def build(packet, workdir, commit, target):
        target.write(artifact)
        return len(artifact), hashlib.sha256(artifact).hexdigest(), "d" * 64

    miner.solve_fn = solve
    monkeypatch.setattr(outcomes_support, "build_graded_artifact", build)
    publish = miner._publish_graded
    published = []

    def tracked_publish(*args, **kwargs):
        published.append(args[1].graded_round_kind)
        return publish(*args, **kwargs)

    monkeypatch.setattr(miner, "_publish_graded", tracked_publish)
    assert miner.run_once() is True
    assert published == ["revision"]
    uploads = [r for r in seen if r.url.path.endswith("/graded-artifact")]
    assert len(uploads) == 1
    assert uploads[0].url.path == "/api/runner/v1/leases/revision-job/graded-artifact"
    assert_auth(uploads[0])
    complete = [r for r in seen if r.url.path.endswith("/complete")]
    assert len(complete) == 1
    body = json.loads(complete[0].read())
    assert body["terminal"]["verification_state"] == "verified"
    assert body["terminal"]["result_ref"] is None
    assert body["terminal"]["result_commit"] == ""


@pytest.mark.parametrize("status,error", [(409, "graded_round_reply_refused"), (500, "internal_error")])
def test_refused_reply_keeps_the_miner_claiming(tmp_path, status, error):
    entry = round_request()
    calls = []

    def gateway(request):
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("/leases"):
            return httpx.Response(204)
        if request.method == "GET":
            return httpx.Response(200, json={"requests": [entry]})
        return httpx.Response(status, json={"error": {"type": error, "message": error}})

    miner = make_miner(tmp_path, gateway)
    assert miner.run_once() is False
    assert miner.run_once() is False
    posts = [c for c in calls if c[0] == "POST" and "graded-rounds" in c[1]]
    claims = [c for c in calls if c[1].endswith("/leases")]
    assert len(posts) == 2 and len(claims) == 2
