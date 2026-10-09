"""Standalone offer-window transport and real reference miner lifecycle tests."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from urllib.parse import unquote

import pytest

from ormas_subnet.client import OrmasGatewayError, OrmasMinerClient
from ormas_subnet.protocol import RUNNER_DEVICE_HEADER
from ormas_subnet.skeleton import MinerSkeleton
from ormas_subnet._recovery import RecoveryRequired
from .test_ask_usd import _BAD_OFFERS, _FakeResponse
from .test_skeleton import _offer_skeleton
from .test_skeleton_public_recovery import _config, _lease_and_draft


class OfferTransport:
    """In-process contract: POST replaces, queue/claim award, GET reads own status."""

    def __init__(self):
        self.jobs = [{"job_id": "task_1", "created_at": "2026-10-08T00:00:00Z", "envelope": {}}]
        self.calls = []
        self.bids = {}
        self.award = None
        self.award_on_claim = False
        self.race = None
        self.error = None
        self.legacy = set()
        self.queue_response = None
        self.lease_response = None
        self.source = None

    def conflict(self, message):
        return _FakeResponse(409, {"error": {"type": "conflict_error", "message": message}})

    def post(self, path, json=None, headers=None):
        body = deepcopy(json)
        self.calls.append(("POST", path, body, headers))
        if self.error is not None:
            if isinstance(self.error, BaseException):
                raise self.error
            return self.error
        if path.endswith("/offers"):
            if body["job_id"] in self.legacy:
                return self.conflict("not_offering")
            if self.race == "window_closed":
                if self.bids:
                    self.set_award(next(reversed(self.bids)))
                return self.conflict("window_closed")
            if self.award:
                return self.conflict("window_closed")
            for bid in self.bids.values():
                if bid["terms"]["job_id"] == body["job_id"] and bid["status"] == "open":
                    bid["status"] = "replaced"
            bid_id = f"bid_{len(self.bids) + 1}"
            self.bids[bid_id] = {"terms": {k: v for k, v in body.items() if k != "runner_id"},
                                 "status": "open"}
            return _FakeResponse(200, {"bid_id": bid_id, "window_closes_at": "2026-10-08T00:10:00Z"})
        if path.endswith("/leases"):
            if self.lease_response is not None:
                return self.lease_response
            if self.award_on_claim and self.bids:
                self.set_award(next(reversed(self.bids)))
            if self.source is None:
                return _FakeResponse(204)
            if self.award:
                terms = self.bids[self.award]["terms"]
                response = self.source.post(path, json={**body, "offers": [terms]}, headers=headers)
                if response.status_code == 200:
                    response._body["lease"].update(bid_id=self.award,
                        outcome_price_usd=terms.get("price_usd", terms.get("limit_usd")),
                        offer_kind=terms["kind"], estimate_usd=terms.get("estimate_usd"),
                        limit_usd=terms.get("limit_usd"))
                return response
            if body.get("offers"):
                response = self.source.post(path, json=body, headers=headers)
                if response.status_code == 200:
                    terms = next(t for t in body["offers"] if t["job_id"] == response._body["lease"]["task_id"])
                    response._body["lease"]["outcome_price_usd"] = terms.get("price_usd", terms.get("limit_usd"))
                return response
            return _FakeResponse(204)
        assert self.source is not None
        return self.source.post(path, json=body, headers=headers)

    def get(self, path, headers=None):
        self.calls.append(("GET", path, None, headers))
        if "/queue?" in path:
            if self.queue_response:
                return self.queue_response
            jobs = deepcopy(self.jobs)
            if self.award:
                for job in jobs:
                    if job["job_id"] == self.bids[self.award]["terms"]["job_id"]:
                        job.update(awarded_to_you=True, bid_id=self.award, award_id="awd_1")
            return _FakeResponse(200, {"jobs": jobs})
        bid_id = unquote(path.rsplit("/", 1)[1])
        if self.error is not None:
            if isinstance(self.error, BaseException):
                raise self.error
            return self.error
        payload = {"bid_id": bid_id, "status": self.bids[bid_id]["status"]}
        if self.award == bid_id:
            payload["award_id"] = "awd_1"
        return _FakeResponse(200, payload)

    def delete(self, path, headers=None):
        self.calls.append(("DELETE", path, None, headers))
        if self.error is not None:
            if isinstance(self.error, BaseException):
                raise self.error
            return self.error
        bid_id = unquote(path.rsplit("/", 1)[1])
        if self.race == "not_open":
            self.set_award(bid_id)
            return self.conflict("not_open")
        if self.bids[bid_id]["status"] != "open":
            return self.conflict("not_open")
        self.bids[bid_id]["status"] = "withdrawn"
        return _FakeResponse(200, {"bid_id": bid_id, "status": "withdrawn"})

    def set_award(self, bid_id):
        self.award = bid_id
        for key, bid in self.bids.items():
            bid["status"] = "awarded" if key == bid_id else "not_awarded"


def client_for(transport, **kwargs):
    return OrmasMinerClient("https://fake.invalid", "ormr_test", http_client=transport, **kwargs)


def require_offer_api(client):
    for method in ("submit_offer", "get_offer", "withdraw_offer"):
        assert callable(getattr(client, method, None)), f"public client needs {method} behavior"


@pytest.mark.parametrize("terms", [
    {"job_id": "task_1", "kind": "firm", "price_usd": 1.25},
    {"job_id": "task_1", "kind": "limit", "estimate_usd": 0.9, "limit_usd": 1.25},
])
@pytest.mark.parametrize("device", [None, "device-1"])
def test_documented_submit_read_withdraw_exact_wire(terms, device):
    transport = OfferTransport()
    client = client_for(transport, device_nonce=device)
    require_offer_api(client)
    posted = client.submit_offer("runner-1", terms)
    assert posted == {"bid_id": "bid_1", "window_closes_at": "2026-10-08T00:10:00Z"}
    assert client.get_offer(posted["bid_id"]) == {"bid_id": "bid_1", "status": "open"}
    assert client.withdraw_offer(posted["bid_id"]) == {"bid_id": "bid_1", "status": "withdrawn"}
    headers = None if device is None else {RUNNER_DEVICE_HEADER: device}
    assert transport.calls == [
        ("POST", "/api/runner/v1/offers", {"runner_id": "runner-1", **terms}, headers),
        ("GET", "/api/runner/v1/offers/bid_1", None, headers),
        ("DELETE", "/api/runner/v1/offers/bid_1", None, headers),
    ]


@pytest.mark.parametrize("offer", [None, [], *[v[0] for v in _BAD_OFFERS if isinstance(v, list) and len(v) == 1],
                                   {"job_id": "task_1", "kind": "firm", "price_usd": float("nan")},
                                   {"job_id": "task_1", "kind": "firm", "price_usd": float("inf")},
                                   {"job_id": "task_1", "kind": "firm", "price_usd": 10 ** 400},
                                   {"job_id": "task_1", "kind": "firm", "price_usd": 1, "schema_version": "x"}])
def test_submit_validates_locally(offer):
    transport = OfferTransport()
    client = client_for(transport)
    require_offer_api(client)
    with pytest.raises(ValueError):
        client.submit_offer("runner-1", offer)
    assert transport.calls == []


@pytest.mark.parametrize("identity", [None, "", " ", 7, True])
def test_offer_identities_validate_locally(identity):
    transport = OfferTransport()
    client = client_for(transport)
    require_offer_api(client)
    for operation in [lambda: client.submit_offer(identity, {"job_id": "task_1", "kind": "firm", "price_usd": 1}),
                      lambda: client.get_offer(identity), lambda: client.withdraw_offer(identity)]:
        with pytest.raises(ValueError):
            operation()
    assert transport.calls == []


def test_submit_limit_compares_original_amounts_like_offer_backend():
    transport = OfferTransport()
    client = client_for(transport)
    require_offer_api(client)
    with pytest.raises(ValueError, match="estimate_usd"):
        client.submit_offer("runner-1", {"job_id": "task_1", "kind": "limit",
            "estimate_usd": 2 ** 53 + 1, "limit_usd": 2 ** 53})
    assert transport.calls == []


def test_bid_identity_is_one_url_segment():
    transport = OfferTransport()
    bid_id = "bid/ ../?&#%"
    transport.bids[bid_id] = {"status": "open"}
    client = client_for(transport)
    require_offer_api(client)
    client.get_offer(bid_id)
    client.withdraw_offer(bid_id)
    for _, path, _, _ in transport.calls:
        assert path.startswith("/api/runner/v1/offers/bid%2F%20")
        assert unquote(path.rsplit("/", 1)[1]) == bid_id
        assert path.count("/") == 5


@pytest.mark.parametrize("method", ["submit_offer", "get_offer", "withdraw_offer"])
@pytest.mark.parametrize("code,kind,message", [(409, "conflict_error", "not_open"),
    (409, "conflict_error", "window_closed"), (401, "authentication_error", "device mismatch"),
    (503, "service_unavailable", "unavailable")])
def test_offer_structured_errors(method, code, kind, message):
    transport = OfferTransport()
    transport.error = _FakeResponse(code, {"error": {"type": kind, "message": message}})
    client = client_for(transport, device_nonce="device-1")
    require_offer_api(client)
    args = ("runner-1", {"job_id": "task_1", "kind": "firm", "price_usd": 1}) if method == "submit_offer" else ("bid_1",)
    with pytest.raises(OrmasGatewayError) as raised:
        getattr(client, method)(*args)
    assert (raised.value.status_code, raised.value.error_type, raised.value.message) == (code, kind, message)
    assert transport.calls[-1][3] == {RUNNER_DEVICE_HEADER: "device-1"}


def window_miner(tmp_path, offer_fn=None, ask_usd=1.25):
    # Reuse the real checkout/solver/complete fixtures from the legacy offers tests.
    source, miner = _offer_skeleton(tmp_path, offer_fn=offer_fn, ask_usd=ask_usd)
    transport = OfferTransport()
    transport.source = source
    miner.client = client_for(transport)
    # Dynamic assignment keeps the base tests collectible and tests behavior on base.
    miner.config.offer_window = True
    return transport, miner


def offers(transport):
    return [call for call in transport.calls if call[0] == "POST" and call[1].endswith("/offers")]


def claims(transport):
    return [call[2] for call in transport.calls if call[1].endswith("/leases")]


def test_default_and_cli_are_explicit_opt_in(tmp_path):
    from neurons import miner as cli
    assert getattr(_config(tmp_path), "offer_window", None) is False
    parser = cli.build_parser()
    assert parser.parse_args([]).offer_window is False
    assert parser.parse_args(["--offer-window"]).offer_window is True


@pytest.mark.parametrize("enabled", [False, True])
def test_cli_constructs_opt_in_config(tmp_path, monkeypatch, enabled):
    from neurons import miner as cli
    configs = []

    class Miner:
        def __init__(self, _client, config, _solve):
            self.config = config
            configs.append(config)

        def register(self):
            return {}

        def run_once(self):
            return False

    monkeypatch.setattr(cli, "MinerSkeleton", Miner)
    monkeypatch.setattr(cli, "OrmasMinerClient", lambda **_: object())
    args = ["--gateway", "https://fake.invalid", "--runner-id", "runner-1",
            "--repo-id", "repo1", "--repo-url", "local", "--cell", "task:code",
            "--solve-command", "true", "--once", "--workdir-root", str(tmp_path), "--ask-usd", "1.25"]
    if enabled:
        args.append("--offer-window")
    assert cli.main(args, token="ormr_test") == 3
    assert configs[0].offer_window is enabled
    assert configs[0].ask_usd == 1.25


def test_httpx_offer_wire_preserves_bearer_and_device():
    import httpx
    requests = []
    responses = [{"bid_id": "bid_1", "window_closes_at": "2026-10-08T00:10:00Z"},
                 {"bid_id": "bid_1", "status": "open"}, {"bid_id": "bid_1", "status": "withdrawn"}]

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=responses[len(requests) - 1])

    with httpx.Client(base_url="https://fake.invalid", headers={"Authorization": "Bearer ormr_test"},
                      transport=httpx.MockTransport(handle)) as transport:
        client = client_for(transport, device_nonce="device-1")
        posted = client.submit_offer("runner-1", {"job_id": "job-1", "kind": "limit",
                                                "estimate_usd": 0.20, "limit_usd": 0.30})
        assert client.get_offer(posted["bid_id"])["status"] == "open"
        assert client.withdraw_offer(posted["bid_id"])["status"] == "withdrawn"
    assert [request.method for request in requests] == ["POST", "GET", "DELETE"]
    assert requests[0].content == b'{"runner_id":"runner-1","job_id":"job-1","kind":"limit","estimate_usd":0.2,"limit_usd":0.3}'
    for request in requests:
        assert request.headers["Authorization"] == "Bearer ormr_test"
        assert request.headers[RUNNER_DEVICE_HEADER] == "device-1"


def test_repeated_polls_freeze_terms_replace_and_withdraw(tmp_path):
    terms = {"job_id": "task_1", "kind": "limit", "estimate_usd": 0.9, "limit_usd": 1.25}
    desired = [terms]
    transport, miner = window_miner(tmp_path, lambda _: desired[0])
    assert miner.run_once() is False
    assert len(offers(transport)) == 1, "window terms must be POSTed before any lease claim"
    assert miner.run_once() is False
    assert len(offers(transport)) == 1, "unchanged polls retain their bid identity"
    terms["limit_usd"] = 1.5
    assert miner.run_once() is False
    assert len(offers(transport)) == 2
    assert transport.bids["bid_1"]["terms"]["limit_usd"] == 1.25
    assert transport.bids["bid_1"]["status"] == "replaced"
    desired[0] = None
    assert miner.run_once() is False
    assert transport.bids["bid_2"]["status"] == "withdrawn"
    assert miner.run_once() is False
    assert sum(c[0] == "DELETE" for c in transport.calls) == 1
    assert all(body["offers"] == [] and "ask_usd" not in body for body in claims(transport))


@pytest.mark.parametrize("bad", [None, 0, -1, True, float("nan"), float("inf")])
def test_window_refuses_missing_static_pricing(tmp_path, bad):
    transport, miner = window_miner(tmp_path, ask_usd=bad)
    with pytest.raises(ValueError, match="ask_usd|pricing"):
        miner.run_once()
    assert not claims(transport)


def test_static_firm_award_executes_real_solver_once(tmp_path):
    transport, miner = window_miner(tmp_path)
    assert miner.run_once() is False
    assert len(offers(transport)) == 1, "static ask must produce a standalone firm offer"
    transport.set_award("bid_1")
    miner.config.ask_usd = 99
    assert miner.run_once() is True
    assert transport.source.completed["terminal"]["verification_state"] == "verified"
    assert len(offers(transport)) == 1
    assert all(body["offers"] == [] for body in claims(transport))
    assert (miner.config.workdir_root / "task_1" / "out.txt").read_text() == "base\nmined\n"


@pytest.mark.parametrize("queue_view", ["awarded", "ordinary", "absent"])
def test_new_award_recovers_without_static_pricing(tmp_path, queue_view):
    transport, miner = window_miner(tmp_path)
    assert miner.run_once() is False
    transport.set_award("bid_1")
    miner.config.ask_usd = None
    if queue_view == "ordinary":
        transport.queue_response = _FakeResponse(200, {"jobs": deepcopy(transport.jobs)})
    elif queue_view == "absent":
        transport.jobs = []
    transport.calls.clear()
    assert miner.run_once() is True
    assert transport.source.completed["terminal"]["verification_state"] == "verified"
    assert transport.bids["bid_1"]["terms"]["price_usd"] == 1.25
    assert offers(transport) == []
    assert not any(call[0] == "DELETE" for call in transport.calls)
    reads = [call for call in transport.calls if call[0] == "GET" and "/offers/" in call[1]]
    assert len(reads) == (1 if queue_view == "absent" else 2)
    assert all(body["offers"] == [] for body in claims(transport))


def test_not_awarded_can_reopen_and_win_at_frozen_terms(tmp_path):
    """e8b25616 allows a fresh bid while retaining recovery of an older award."""
    transport, miner = window_miner(tmp_path)
    assert miner.run_once() is False
    transport.bids["bid_1"]["status"] = "not_awarded"
    assert miner.run_once() is False
    assert len(offers(transport)) == 2
    transport.bids["bid_1"]["status"] = "open"
    assert miner.run_once() is False
    assert len(offers(transport)) == 2
    transport.set_award("bid_1")
    miner.config.offer_fn = lambda _: pytest.fail("a reopened award must retain its frozen terms")
    assert miner.run_once() is True
    assert len(offers(transport)) == 2
    assert transport.source.completed["terminal"]["verification_state"] == "verified"


@pytest.mark.parametrize("status", ["not_awarded", "withdrawn", "replaced", "no_capacity", "award_lapsed"])
def test_losing_status_never_executes(tmp_path, status):
    transport, miner = window_miner(tmp_path)
    assert miner.run_once() is False
    assert len(offers(transport)) == 1
    transport.bids["bid_1"]["status"] = status
    miner.solve_fn = lambda *_: pytest.fail("a losing bid cannot execute")
    assert miner.run_once() is False
    assert transport.source.completed is None
    assert len(offers(transport)) == 2
    assert transport.bids["bid_2"]["status"] == "open"
    assert transport.bids["bid_1"]["status"] == status


@pytest.mark.parametrize("status", ["not_awarded", "withdrawn", "replaced", "no_capacity", "award_lapsed"])
def test_explicitly_changed_terms_can_submit_after_terminal_bid(tmp_path, status):
    terms = {"job_id": "task_1", "kind": "firm", "price_usd": 1.25}
    transport, miner = window_miner(tmp_path, lambda _: dict(terms))
    assert miner.run_once() is False
    transport.bids["bid_1"]["status"] = status
    terms["price_usd"] = 1.5
    assert miner.run_once() is False
    assert len(offers(transport)) == 2
    assert transport.bids["bid_2"]["terms"]["price_usd"] == 1.5
    assert transport.source.completed is None


@pytest.mark.parametrize("race", ["claim", "not_open", "window_closed"])
def test_award_race_keeps_original_binding(tmp_path, race):
    desired = [{"job_id": "task_1", "kind": "limit", "estimate_usd": 0.9, "limit_usd": 1.25}]
    transport, miner = window_miner(tmp_path, lambda _: desired[0])
    assert miner.run_once() is False
    assert len(offers(transport)) == 1
    if race == "claim":
        transport.award_on_claim = True
    else:
        transport.race = race
        desired[0] = None if race == "not_open" else {**desired[0], "limit_usd": 2}
    assert miner.run_once() is True
    assert transport.source.completed["terminal"]["settled_price_usd"] == 1.25
    assert transport.bids["bid_1"]["terms"]["limit_usd"] == 1.25
    assert all(body["offers"] == [] for body in claims(transport))


@pytest.mark.parametrize("status", ["not_awarded", "withdrawn", "replaced", "no_capacity", "award_lapsed"])
def test_terminal_bids_outside_queue_are_not_refreshed(tmp_path, status):
    transport, miner = window_miner(tmp_path)
    assert miner.run_once() is False
    transport.bids["bid_1"]["status"] = status
    import json
    journal = next(tmp_path.rglob("offers.json"))
    state = json.loads(journal.read_text())
    state["bids"]["bid_1"]["status"] = status
    journal.write_text(json.dumps(state))
    transport.jobs = []
    transport.calls.clear()
    assert miner.run_once() is False
    assert not any(call[0] == "GET" and "/offers/" in call[1] for call in transport.calls)
    assert claims(transport) == [{"schema_version": "ormas-runner-v1", "runner_id": miner.config.runner_id,
                                 "offers": []}]


def test_restart_open_bid_is_retained(tmp_path):
    transport, miner = window_miner(tmp_path)
    assert miner.run_once() is False
    restarted = MinerSkeleton(miner.client, replace(miner.config), miner.solve_fn)
    assert restarted.run_once() is False
    assert len(offers(transport)) == 1
    assert any(call[0] == "GET" and call[1].endswith("/offers/bid_1") for call in transport.calls)


def test_award_uses_gateway_float_normalized_amounts(tmp_path):
    terms = {"job_id": "task_1", "kind": "limit", "estimate_usd": 2 ** 53 + 1, "limit_usd": 2 ** 54}
    transport, miner = window_miner(tmp_path, lambda _: terms)
    assert miner.run_once() is False
    transport.set_award("bid_1")
    # The real POST handler stores float-normalized accepted terms.
    original = transport.post

    def normalized_lease(path, json=None, headers=None):
        response = original(path, json=json, headers=headers)
        if path.endswith("/leases") and response.status_code == 200:
            response._body["lease"]["estimate_usd"] = float(terms["estimate_usd"])
            response._body["lease"]["limit_usd"] = float(terms["limit_usd"])
        return response

    transport.post = normalized_lease
    assert miner.run_once() is True
    assert transport.source.completed["terminal"]["settled_price_usd"] == float(terms["limit_usd"])


def test_restart_recovers_bid_and_namespace_binding(tmp_path):
    transport, miner = window_miner(tmp_path)
    assert miner.run_once() is False
    assert len(offers(transport)) == 1
    # Returned identity and immutable terms must already be durable when claiming.
    saved = [p for p in miner.config.workdir_root.rglob("*.json") if "bid_1" in p.read_text()]
    assert saved and any("window_closes_at" in p.read_text() for p in saved)
    transport.set_award("bid_1")
    restarted = MinerSkeleton(miner.client, replace(miner.config), miner.solve_fn)
    restarted.config.offer_fn = lambda _: pytest.fail("an award cannot be repriced on restart")
    assert restarted.run_once() is True
    assert len(offers(transport)) == 1
    for gateway, runner in [("https://other.invalid", "miner-1"), ("https://fake.invalid", "other-runner")]:
        client = client_for(transport)
        client.base_url = gateway
        config = replace(miner.config, runner_id=runner)
        stranger = MinerSkeleton(client, config, lambda *_: pytest.fail("foreign identity cannot execute"))
        with pytest.raises(RecoveryRequired, match="award|bid"):
            stranger.run_once()


@pytest.mark.parametrize("mismatch", ["unknown", "task", "draft", "price", "kind", "estimate", "limit"])
def test_unknown_or_mismatched_lease_refuses_before_solver(tmp_path, mismatch):
    desired = {"job_id": "task_1", "kind": "limit", "estimate_usd": 0.9, "limit_usd": 1.25}
    transport, miner = window_miner(tmp_path, lambda _: desired)
    assert miner.run_once() is False
    assert len(offers(transport)) == 1
    transport.set_award("bid_1")
    lease, _ = _lease_and_draft()
    lease = replace(lease, task_id="task_1", bid_id="bid_1", offer_kind="limit", estimate_usd=0.9, limit_usd=1.25)
    values = {"unknown": {"bid_id": "foreign"}, "task": {"task_id": "other"},
              "price": {"outcome_price_usd": 2}, "kind": {"offer_kind": "firm"},
              "estimate": {"estimate_usd": 1}, "limit": {"limit_usd": 2}}
    if mismatch != "draft":
        lease = replace(lease, **values[mismatch])
    # Get a valid TaskDraft wire from the real local gateway fixture.
    raw = transport.source.post("/api/runner/v1/leases", json={"schema_version": "ormas-runner-v1", "runner_id": "miner-1", "offers": [desired]}).json()
    raw["lease"] = lease.to_wire()
    if mismatch == "draft":
        raw["draft"]["task_id"] = "other"
    transport.lease_response = _FakeResponse(200, raw)
    miner.solve_fn = lambda *_: pytest.fail("mismatched award cannot execute")
    with pytest.raises(RecoveryRequired, match="award|bid|terms"):
        miner.run_once()
    assert transport.source.completed is None


def test_unknown_awarded_queue_refuses_without_claim(tmp_path):
    transport, miner = window_miner(tmp_path)
    transport.queue_response = _FakeResponse(200, {"jobs": [
        {**transport.jobs[0], "awarded_to_you": True, "bid_id": "foreign", "award_id": "awd_foreign"}]})
    with pytest.raises(RecoveryRequired, match="award|bid"):
        miner.run_once()
    assert claims(transport) == []


def test_typed_not_offering_keeps_per_job_legacy_claim(tmp_path):
    transport, miner = window_miner(tmp_path)
    transport.legacy.add("task_1")
    assert miner.run_once() is True
    assert len(offers(transport)) == 1
    assert claims(transport)[0]["offers"] == []
    assert claims(transport)[1]["offers"] == [{"job_id": "task_1", "kind": "firm", "price_usd": 1.25}]
    assert transport.source.completed["terminal"]["verification_state"] == "verified"


@pytest.mark.parametrize("error", [RuntimeError("transport broke"),
    _FakeResponse(401, {"error": {"type": "authentication_error", "message": "bad device"}})])
def test_window_ordinary_errors_propagate(tmp_path, error):
    transport, miner = window_miner(tmp_path)
    transport.error = error
    with pytest.raises((RuntimeError, OrmasGatewayError)):
        miner.run_once()
    assert claims(transport) == []


def test_ambiguous_post_restart_refuses_unbound_execution(tmp_path):
    transport, miner = window_miner(tmp_path)
    transport.error = RuntimeError("response lost")
    with pytest.raises(RuntimeError, match="response lost"):
        miner.run_once()
    transport.error = None
    restarted = MinerSkeleton(miner.client, miner.config, miner.solve_fn)
    with pytest.raises(RecoveryRequired, match="offer|bid"):
        restarted.run_once()
    assert claims(transport) == []


def test_public_recovery_precedes_all_offer_calls(tmp_path, monkeypatch):
    transport, miner = window_miner(tmp_path)
    from ormas_subnet._recovery import PublicRecovery
    monkeypatch.setattr(PublicRecovery, "pending", lambda _: {"stage": "completion"})
    monkeypatch.setattr(miner, "_recover_public", lambda _: True)
    assert miner.run_once() is True
    assert transport.calls == []


def test_offer_identity_is_durable_before_claim(tmp_path, monkeypatch):
    transport, miner = window_miner(tmp_path)
    original = transport.post

    def checked_post(path, json=None, headers=None):
        if path.endswith("/leases"):
            saved = [p for p in miner.config.workdir_root.rglob("*.json") if "bid_1" in p.read_text()]
            assert saved, "save the returned bid identity before issuing a claim"
        return original(path, json=json, headers=headers)

    monkeypatch.setattr(transport, "post", checked_post)
    assert miner.run_once() is False
    assert len(offers(transport)) == 1


def test_withdrawn_offer_can_return_at_identical_terms(tmp_path):
    price = {'value': 1.25}
    def quote(_):
        return None if price['value'] is None else {
            'job_id': 'task_1', 'kind': 'firm', 'price_usd': price['value']}
    transport, miner = window_miner(tmp_path, quote)
    assert miner.run_once() is False
    price['value'] = None
    assert miner.run_once() is False
    assert transport.bids['bid_1']['status'] == 'withdrawn'
    price['value'] = 1.25
    assert miner.run_once() is False
    assert len(offers(transport)) == 2
    assert transport.bids['bid_2']['status'] == 'open'


@pytest.mark.parametrize('status', ['withdrawn', 'not_awarded', 'no_capacity', 'replaced'])
def test_terminal_unawarded_bid_can_reoffer_same_terms_after_restart(tmp_path, status):
    transport, miner = window_miner(tmp_path)
    assert miner.run_once() is False
    transport.bids['bid_1']['status'] = status
    restarted = MinerSkeleton(miner.client, replace(miner.config), miner.solve_fn)
    assert restarted.run_once() is False
    assert len(offers(transport)) == 2
    assert transport.bids['bid_1']['terms'] == transport.bids['bid_2']['terms']


@pytest.mark.parametrize('kind', ['firm', 'limit'])
def test_saved_lapsed_award_reoffers_identical_terms_after_restart(tmp_path, kind):
    terms = {'job_id': 'task_1', 'kind': kind,
             **({'price_usd': 1.25} if kind == 'firm' else {'estimate_usd': .9, 'limit_usd': 1.25})}
    transport, miner = window_miner(tmp_path, lambda _: dict(terms))
    assert miner.run_once() is False
    transport.set_award('bid_1')
    import json
    journal = next(tmp_path.rglob('offers.json'))
    state = json.loads(journal.read_text())
    state['bids']['bid_1'].update(status='awarded', award_id='awd_1')
    journal.write_text(json.dumps(state))
    transport.award = None
    transport.bids['bid_1']['status'] = 'award_lapsed'
    restarted = MinerSkeleton(miner.client, replace(miner.config), miner.solve_fn)
    assert restarted.run_once() is False
    assert len(offers(transport)) == 2
    assert transport.bids['bid_1']['terms'] == transport.bids['bid_2']['terms'] == terms
    assert json.loads(journal.read_text())['bids']['bid_1']['status'] == 'award_lapsed'
    transport.set_award('bid_2')
    restarted.config.offer_fn = lambda _: pytest.fail('fresh award terms must stay frozen')
    assert restarted.run_once() is True
    assert transport.source.completed['terminal']['verification_state'] == 'verified'


@pytest.mark.parametrize('status', ['open', 'not_awarded', 'no_capacity', 'awarded'])
def test_saved_award_mismatch_refuses_before_claim(tmp_path, status):
    import json
    transport, miner = window_miner(tmp_path)
    assert miner.run_once() is False
    journal = next(tmp_path.rglob('offers.json'))
    state = json.loads(journal.read_text())
    state['bids']['bid_1'].update(status='awarded', award_id='old-award')
    journal.write_text(json.dumps(state))
    transport.bids['bid_1']['status'] = status
    if status == 'awarded':
        transport.set_award('bid_1')
    transport.calls.clear()
    with pytest.raises(RecoveryRequired, match='award|bid'):
        miner.run_once()
    assert offers(transport) == claims(transport) == []
    assert transport.source.completed is None
