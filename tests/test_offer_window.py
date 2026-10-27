"""Standalone offer-window transport and real reference miner lifecycle tests."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from ormas_subnet.client import (
    OrmasGatewayError,
    OrmasMinerClient,
    _offer_terms_sha256,
    _validate_offer_request_id,
)
from ormas_subnet.protocol import RUNNER_DEVICE_HEADER
from ormas_subnet.skeleton import MinerSkeleton
from ormas_subnet._recovery import RecoveryRequired
from .test_ask_usd import _BAD_OFFERS, _FakeResponse
from .test_skeleton import _offer_skeleton
from .test_skeleton_public_recovery import _config, _lease_and_draft


def offer_request_id(window_id, terms, random_hex="c" * 32):
    return f"ofr_{window_id}{_offer_terms_sha256(terms)}{random_hex}"


class OfferTransport:
    """In-process contract: POST replaces, queue/claim award, GET reads own status."""

    def __init__(self):
        self.offer_window_id = "b" * 32
        self.jobs = [{
            "job_id": "task_1",
            "created_at": "2026-10-08T00:00:00Z",
            "envelope": {},
            "offer_window_id": self.offer_window_id,
        }]
        self.calls = []
        self.bids = {}
        self.offer_requests = {}
        self.advertise_offer_idempotency = True
        self.offer_authority_epoch = "a" * 32
        self.supports_offer_request_id = True
        self.offer_request_echo = None
        self.offer_refusal_echo = None
        self.offer_epoch_echo = None
        self.offer_digest_echo = None
        self.offer_job_echo = None
        self.offer_window_echo = None
        self.offer_refusal_reason = None
        self.award = None
        self.award_on_claim = False
        self.race = None
        self.error = None
        self.lose_offer_before_commit = False
        self.lose_offer_ack = False
        self.window_closed = False
        self.legacy = set()
        self.queue_response = None
        self.lease_response = None
        self.source = None

    def conflict(self, message):
        return _FakeResponse(409, {"error": {"type": "conflict_error", "message": message}})

    def request_conflict(self, request_id):
        return _FakeResponse(409, {"error": {
            "type": "offer_request_conflict", "message": "offer_request_id reused",
            "offer_request_id": request_id,
        }})

    @staticmethod
    def request_terms(body):
        return {key: value for key, value in body.items()
                if key not in {
                    "runner_id", "offer_request_id", "offer_authority_epoch", "offer_window_id",
                }}

    def request_refusal(self, request_id, reason, body):
        terms = self.request_terms(body)
        return _FakeResponse(409, {"error": {
            "type": "offer_request_refused", "message": reason,
            "offer_request_id": (request_id if self.offer_refusal_echo is None
                                 else self.offer_refusal_echo),
            "job_id": (terms["job_id"] if self.offer_job_echo is None
                       else self.offer_job_echo),
            "offer_terms_sha256": (
                _offer_terms_sha256(terms) if self.offer_digest_echo is None
                else self.offer_digest_echo
            ),
            "offer_authority_epoch": (
                body["offer_authority_epoch"] if self.offer_epoch_echo is None
                else self.offer_epoch_echo
            ),
            "offer_window_id": (
                body["offer_window_id"] if self.offer_window_echo is None
                else self.offer_window_echo
            ),
        }})

    def post(self, path, json=None, headers=None):
        body = deepcopy(json)
        self.calls.append(("POST", path, body, headers))
        if self.error is not None:
            if isinstance(self.error, BaseException):
                raise self.error
            return self.error
        if path.endswith("/offers"):
            request_id = body.get("offer_request_id")
            if request_id is not None and not self.supports_offer_request_id:
                return _FakeResponse(400, {"error": {
                    "type": "unknown_field", "message": "unknown field: offer_request_id",
                }})
            if (request_id is not None
                    and body.get("offer_authority_epoch") != self.offer_authority_epoch):
                return _FakeResponse(409, {"error": {
                    "type": "conflict_error",
                    "message": "offer_authority_epoch_mismatch",
                }})
            if request_id in self.offer_requests:
                saved_body, status, payload = self.offer_requests[request_id]
                if saved_body != body:
                    return self.request_conflict(request_id)
                return _FakeResponse(status, deepcopy(payload))
            if request_id is not None:
                terms = self.request_terms(body)
                try:
                    _validate_offer_request_id(
                        request_id,
                        offer_window_id=body.get("offer_window_id"),
                        offer_terms_sha256=_offer_terms_sha256(terms),
                    )
                except ValueError:
                    return self.conflict("offer_request_binding_mismatch")
            if self.lose_offer_before_commit:
                self.lose_offer_before_commit = False
                raise RuntimeError("replacement outcome unknown")

            def finish(response):
                if request_id is not None:
                    self.offer_requests[request_id] = (
                        deepcopy(body), response.status_code, deepcopy(response._body),
                    )
                return response

            def refuse(reason):
                if request_id is None:
                    return self.conflict(reason)
                return finish(self.request_refusal(request_id, reason, body))

            if self.offer_refusal_reason is not None:
                return refuse(self.offer_refusal_reason)
            current_window = next((
                job.get("offer_window_id") for job in self.jobs
                if job.get("job_id") == body["job_id"]
            ), None)
            if request_id is not None and body.get("offer_window_id") != current_window:
                return refuse("window_closed")
            if body["job_id"] in self.legacy:
                return refuse("not_offering")
            if self.race == "window_closed":
                if self.bids:
                    self.set_award(next(reversed(self.bids)))
                return refuse("window_closed")
            if self.award:
                return refuse("window_closed")
            for bid in self.bids.values():
                if bid["terms"]["job_id"] == body["job_id"] and bid["status"] == "open":
                    bid["status"] = "replaced"
            bid_id = f"bid_{len(self.bids) + 1}"
            terms = self.request_terms(body)
            self.bids[bid_id] = {"terms": terms,
                                 "status": "open"}
            payload = {"bid_id": bid_id, "window_closes_at": "2026-10-08T00:10:00Z"}
            if request_id is not None:
                payload.update(
                    job_id=(terms["job_id"] if self.offer_job_echo is None
                            else self.offer_job_echo),
                    ack_by_at="2026-10-08T00:01:00Z",
                    offer_terms_sha256=(
                        _offer_terms_sha256(terms) if self.offer_digest_echo is None
                        else self.offer_digest_echo
                    ),
                    offer_authority_epoch=(
                        body["offer_authority_epoch"] if self.offer_epoch_echo is None
                        else self.offer_epoch_echo
                    ),
                    offer_window_id=(
                        body["offer_window_id"] if self.offer_window_echo is None
                        else self.offer_window_echo
                    ),
                )
                if self.offer_request_echo is not False:
                    payload["offer_request_id"] = (
                        request_id if self.offer_request_echo is None else self.offer_request_echo
                    )
            response = finish(_FakeResponse(200, payload))
            if self.lose_offer_ack:
                self.lose_offer_ack = False
                raise RuntimeError("response lost after commit")
            return response
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
            payload = {"jobs": jobs}
            if self.advertise_offer_idempotency:
                payload["offer_idempotency"] = {
                    "protocol": "ormas-offer-request-v1",
                    "authority_epoch": self.offer_authority_epoch,
                }
            return _FakeResponse(200, payload)
        if "/offers?" in path:
            if self.error is not None:
                if isinstance(self.error, BaseException):
                    raise self.error
                return self.error
            query = parse_qs(urlsplit(path).query)
            job_id = query.get("job_id", [None])[0]
            matches = [(bid_id, bid) for bid_id, bid in self.bids.items()
                       if bid["terms"]["job_id"] == job_id]
            bid_payload = None
            if matches:
                bid_id, bid = matches[-1]
                bid_payload = {
                    "bid_id": bid_id,
                    "status": bid["status"],
                    "terms": deepcopy(bid["terms"]),
                    "ack_by_at": "2026-10-08T00:01:00Z",
                    "window_closes_at": "2026-10-08T00:10:00Z",
                }
                if bid["status"] == "awarded":
                    bid_payload["award_id"] = "awd_1"
            return _FakeResponse(200, {"job_id": job_id,
                "window_closed": self.window_closed, "bid": bid_payload})
        bid_id = unquote(path.rsplit("/", 1)[1])
        if self.error is not None:
            if isinstance(self.error, BaseException):
                raise self.error
            return self.error
        payload = {"bid_id": bid_id, "status": self.bids[bid_id]["status"]}
        if self.bids[bid_id]["status"] == "awarded":
            payload["award_id"] = self.bids[bid_id].get("award_id", "awd_1")
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
            if key == bid_id:
                bid["award_id"] = "awd_1"


def client_for(transport, **kwargs):
    return OrmasMinerClient("https://fake.invalid", "ormr_test", http_client=transport, **kwargs)


def require_offer_api(client):
    for method in ("submit_offer", "get_offer_for_job", "get_offer", "withdraw_offer"):
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
    assert client.get_offer_for_job("runner-1", terms["job_id"])["bid"]["bid_id"] == "bid_1"
    assert client.get_offer(posted["bid_id"]) == {"bid_id": "bid_1", "status": "open"}
    assert client.withdraw_offer(posted["bid_id"]) == {"bid_id": "bid_1", "status": "withdrawn"}
    headers = None if device is None else {RUNNER_DEVICE_HEADER: device}
    assert transport.calls == [
        ("POST", "/api/runner/v1/offers", {"runner_id": "runner-1", **terms}, headers),
        ("GET", "/api/runner/v1/offers?runner_id=runner-1&job_id=task_1", None, headers),
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
                      lambda: client.get_offer_for_job(identity, "task_1"),
                      lambda: client.get_offer_for_job("runner-1", identity),
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


def test_offer_request_id_is_bounded_echoed_and_reuse_conflicts():
    transport = OfferTransport()
    client = client_for(transport)
    authority_epoch = transport.offer_authority_epoch
    window_id = transport.offer_window_id
    terms = {"job_id": "task_1", "kind": "firm", "price_usd": 1.25}
    request_id = offer_request_id(window_id, terms)
    posted = client.submit_offer(
        "runner-1", terms,
        offer_request_id=request_id,
        offer_authority_epoch=authority_epoch,
        offer_window_id=window_id,
    )
    assert posted["offer_request_id"] == request_id
    assert posted["offer_terms_sha256"] == _offer_terms_sha256(terms)
    assert posted["offer_authority_epoch"] == authority_epoch
    assert posted["offer_window_id"] == window_id
    assert transport.calls[0][2] == {
        "runner_id": "runner-1", **terms, "offer_request_id": request_id,
        "offer_authority_epoch": authority_epoch, "offer_window_id": window_id,
    }
    with pytest.raises(ValueError, match="terms binding mismatch"):
        client.submit_offer(
            "runner-1", {**terms, "price_usd": 2.5}, offer_request_id=request_id,
            offer_authority_epoch=authority_epoch,
            offer_window_id=window_id,
        )
    assert len(transport.bids) == 1 and len(offers(transport)) == 1


@pytest.mark.parametrize("request_id", [
    "", "x" * 132, "ofr_" + "A" * 128, "ofr_" + "a" * 127,
    "has space", "has/slash", True, b"bytes",
])
def test_offer_request_id_rejects_unsafe_or_unbounded_values_before_post(request_id):
    transport = OfferTransport()
    with pytest.raises(ValueError, match="offer_request_id"):
        client_for(transport).submit_offer(
            "runner-1", {"job_id": "task_1", "kind": "firm", "price_usd": 1},
            offer_request_id=request_id,
            offer_authority_epoch="a" * 32,
            offer_window_id="b" * 32,
        )
    assert transport.calls == []


@pytest.mark.parametrize("request_id,authority_epoch,window_id", [
    ("ofr_" + "a" * 128, None, "b" * 32),
    (None, "a" * 32, "b" * 32),
    (None, None, "b" * 32),
    ("ofr_" + "a" * 128, "A" * 32, "b" * 32),
    ("ofr_" + "a" * 128, "a" * 31, "b" * 32),
    ("ofr_" + "a" * 128, "a" * 32, "B" * 32),
])
def test_offer_request_identity_requires_exact_bound_triple(
    request_id, authority_epoch, window_id,
):
    transport = OfferTransport()
    with pytest.raises(ValueError, match="offer_(request_id|authority_epoch|window_id)"):
        client_for(transport).submit_offer(
            "runner-1", {"job_id": "task_1", "kind": "firm", "price_usd": 1},
            offer_request_id=request_id,
            offer_authority_epoch=authority_epoch,
            offer_window_id=window_id,
        )
    assert transport.calls == []


def test_offer_terms_digest_uses_gateway_float_normalization_and_canonical_json():
    first = {"price_usd": 1, "kind": "firm", "job_id": "task_1"}
    second = {"job_id": "task_1", "kind": "firm", "price_usd": 1.0}
    expected = "75446b10a33e374110a2de8909da965a20c11433033eae9c545233a8cd444d45"
    assert _offer_terms_sha256(first) == expected
    assert _offer_terms_sha256(second) == expected
    assert _offer_terms_sha256({
        "job_id": "job-1", "kind": "firm", "price_usd": .2,
    }) == "1767f3a9a2bdc2fc894b9fe73c3e5fafb0abf327166c6bc6376ffcfaecb3c602"
    assert _offer_terms_sha256({
        "job_id": "task_1", "kind": "limit", "estimate_usd": .2, "limit_usd": .3,
    }) == "41d910a896fe625104f533d8616dc7a56bcfc006b5d84899edb3dd36a07857b2"
    assert _offer_terms_sha256({
        "job_id": "tâche-雪", "kind": "limit", "estimate_usd": .2, "limit_usd": 1,
    }) == "88dff061f258375e70cc280d58043d4676b9dab75dc1f9d869bd8f354008cefb"


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


@pytest.mark.parametrize("method", ["submit_offer", "get_offer_for_job", "get_offer", "withdraw_offer"])
@pytest.mark.parametrize("code,kind,message", [(409, "conflict_error", "not_open"),
    (409, "conflict_error", "window_closed"), (401, "authentication_error", "device mismatch"),
    (503, "service_unavailable", "unavailable")])
def test_offer_structured_errors(method, code, kind, message):
    transport = OfferTransport()
    transport.error = _FakeResponse(code, {"error": {"type": kind, "message": message}})
    client = client_for(transport, device_nonce="device-1")
    require_offer_api(client)
    if method == "submit_offer":
        args = ("runner-1", {"job_id": "task_1", "kind": "firm", "price_usd": 1})
    elif method == "get_offer_for_job":
        args = ("runner-1", "task_1")
    else:
        args = ("bid_1",)
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
                 {"job_id": "job-1", "window_closed": False, "bid": {
                     "bid_id": "bid_1", "status": "open",
                     "terms": {"job_id": "job-1", "kind": "limit",
                               "estimate_usd": 0.2, "limit_usd": 0.3},
                     "ack_by_at": "2026-10-08T00:01:00Z",
                     "window_closes_at": "2026-10-08T00:10:00Z"}},
                 {"bid_id": "bid_1", "status": "open"}, {"bid_id": "bid_1", "status": "withdrawn"}]

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=responses[len(requests) - 1])

    with httpx.Client(base_url="https://fake.invalid", headers={"Authorization": "Bearer ormr_test"},
                      transport=httpx.MockTransport(handle)) as transport:
        client = client_for(transport, device_nonce="device-1")
        posted = client.submit_offer("runner-1", {"job_id": "job-1", "kind": "limit",
                                                "estimate_usd": 0.20, "limit_usd": 0.30})
        assert client.get_offer_for_job("runner-1", "job-1")["bid"]["bid_id"] == "bid_1"
        assert client.get_offer(posted["bid_id"])["status"] == "open"
        assert client.withdraw_offer(posted["bid_id"])["status"] == "withdrawn"
    assert [request.method for request in requests] == ["POST", "GET", "GET", "DELETE"]
    assert requests[0].content == b'{"runner_id":"runner-1","job_id":"job-1","kind":"limit","estimate_usd":0.2,"limit_usd":0.3}'
    assert requests[1].url.query == b"runner_id=runner-1&job_id=job-1"
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
        transport.queue_response = _FakeResponse(200, {
            "jobs": deepcopy(transport.jobs),
            "offer_idempotency": {
                "protocol": "ormas-offer-request-v1",
                "authority_epoch": transport.offer_authority_epoch,
            },
        })
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


def test_request_id_not_awarded_cannot_reopen_into_a_later_bid(tmp_path):
    transport, miner = window_miner(tmp_path)
    assert miner.run_once() is False
    transport.bids["bid_1"]["status"] = "not_awarded"
    assert miner.run_once() is False
    assert len(offers(transport)) == 2
    transport.set_award("bid_1")
    miner.solve_fn = lambda *_: pytest.fail("a terminal request-ID bid cannot execute")

    with pytest.raises(RecoveryRequired, match="superseded offer generation"):
        miner.run_once()

    assert len(offers(transport)) == 2
    assert transport.source.completed is None


def test_requestless_rollout_bid_can_reopen_at_frozen_terms(tmp_path):
    transport, miner = window_miner(tmp_path)
    assert miner.run_once() is False

    # Model a journal written before request IDs while preserving the adopted
    # protocol floor.  Rollout gateways may still reopen exactly this class of
    # frozen bid; the request-bound bid above must remain terminal.
    journal = next(tmp_path.rglob("offers.json"))
    state = json.loads(journal.read_text())
    saved = state["bids"]["bid_1"]
    for field in (
        "offer_request_id", "offer_authority_epoch", "offer_window_id",
        "offer_terms_sha256", "ack_by_at",
    ):
        saved.pop(field, None)
    state.update(
        pending=None, offer_protocol_floor=None, offer_protocol_poisoned=False,
        offer_authority_epoch=None, request_outcomes={}, window_markers={}, offer_window_history={},
    )
    journal.write_text(json.dumps(state))

    transport.bids["bid_1"]["status"] = "not_awarded"
    idle = MinerSkeleton(miner.client, replace(miner.config), miner.solve_fn)
    idle.config.offer_fn = lambda _: None
    assert idle.run_once() is False
    saved = json.loads(journal.read_text())["bids"]["bid_1"]
    assert saved["status"] == "not_awarded"
    assert len(offers(transport)) == 1

    transport.set_award("bid_1")
    restarted = MinerSkeleton(miner.client, replace(miner.config), miner.solve_fn)
    restarted.config.offer_fn = lambda _: pytest.fail(
        "a reopened requestless award must retain its frozen terms",
    )
    assert restarted.run_once() is True
    assert len(offers(transport)) == 1
    assert transport.source.completed["terminal"]["verification_state"] == "verified"


def test_request_id_not_awarded_rejects_hidden_stale_lease(tmp_path):
    transport, miner = window_miner(tmp_path)
    assert miner.run_once() is False
    transport.bids["bid_1"]["status"] = "not_awarded"

    idle = MinerSkeleton(miner.client, replace(miner.config), miner.solve_fn)
    idle.config.offer_fn = lambda _: None
    assert idle.run_once() is False
    assert len(offers(transport)) == 1

    transport.set_award("bid_1")
    transport.queue_response = _FakeResponse(200, {
        "jobs": deepcopy(transport.jobs),
        "offer_idempotency": {
            "protocol": "ormas-offer-request-v1",
            "authority_epoch": transport.offer_authority_epoch,
        },
    })
    restarted = MinerSkeleton(miner.client, replace(miner.config), miner.solve_fn)
    restarted.config.offer_fn = lambda _: None
    restarted.solve_fn = lambda *_: pytest.fail("a terminal request-ID bid cannot execute")
    with pytest.raises(RecoveryRequired, match="lease award bid identity"):
        restarted.run_once()
    assert transport.source.completed is None


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


def test_fresh_window_reoffers_same_terms_open_bid_after_restart(tmp_path):
    transport, miner = window_miner(tmp_path)
    assert miner.run_once() is False
    first = offers(transport)[0][2]

    transport.offer_window_id = "c" * 32
    transport.jobs[0]["offer_window_id"] = transport.offer_window_id
    restarted = MinerSkeleton(miner.client, replace(miner.config), miner.solve_fn)
    assert restarted.run_once() is False

    posted = offers(transport)
    assert len(posted) == 2
    assert OfferTransport.request_terms(posted[1][2]) == OfferTransport.request_terms(first)
    assert posted[1][2]["offer_window_id"] == transport.offer_window_id
    assert posted[1][2]["offer_request_id"] != first["offer_request_id"]


def test_fresh_window_reoffers_historical_award_after_restart(tmp_path):
    transport, miner = window_miner(tmp_path)
    assert miner.run_once() is False
    first = offers(transport)[0][2]
    transport.set_award("bid_1")
    transport.award = None
    transport.offer_window_id = "c" * 32
    transport.jobs[0]["offer_window_id"] = transport.offer_window_id

    restarted = MinerSkeleton(miner.client, replace(miner.config), miner.solve_fn)
    assert restarted.run_once() is False

    posted = offers(transport)
    assert len(posted) == 2
    assert posted[1][2]["offer_window_id"] == transport.offer_window_id
    assert posted[1][2]["offer_request_id"] != first["offer_request_id"]


def test_fresh_window_refuses_a_lease_for_a_historical_generation(tmp_path):
    transport, miner = window_miner(tmp_path)
    assert miner.run_once() is False
    transport.offer_window_id = "c" * 32
    transport.jobs[0]["offer_window_id"] = transport.offer_window_id
    assert MinerSkeleton(miner.client, replace(miner.config), miner.solve_fn).run_once() is False
    assert len(offers(transport)) == 2

    transport.bids["bid_1"].update(status="awarded", award_id="awd_1")
    transport.award = "bid_1"
    transport.queue_response = _FakeResponse(200, {
        "jobs": deepcopy(transport.jobs),
        "offer_idempotency": {
            "protocol": "ormas-offer-request-v1",
            "authority_epoch": transport.offer_authority_epoch,
        },
    })
    restarted = MinerSkeleton(miner.client, replace(miner.config), miner.solve_fn)
    with pytest.raises(RecoveryRequired, match="superseded offer generation"):
        restarted.run_once()
    assert transport.source.completed is None


@pytest.mark.parametrize("terminal", ["withdrawn", "replaced", "no_capacity", "award_lapsed"])
def test_same_window_refuses_terminal_historical_bid_reaward(tmp_path, terminal):
    transport, miner = window_miner(tmp_path)
    assert miner.run_once() is False
    transport.bids["bid_1"]["status"] = terminal
    assert MinerSkeleton(miner.client, replace(miner.config), miner.solve_fn).run_once() is False
    assert len(offers(transport)) == 2

    transport.bids["bid_1"].update(status="awarded", award_id="awd_1")
    transport.award = "bid_1"
    transport.queue_response = _FakeResponse(200, {
        "jobs": deepcopy(transport.jobs),
        "offer_idempotency": {
            "protocol": "ormas-offer-request-v1",
            "authority_epoch": transport.offer_authority_epoch,
        },
    })
    restarted = MinerSkeleton(miner.client, replace(miner.config), miner.solve_fn)
    with pytest.raises(RecoveryRequired, match="superseded offer generation"):
        restarted.run_once()
    assert transport.source.completed is None


def test_protocol_rollout_can_claim_known_pre_request_id_award(tmp_path):
    transport, miner = window_miner(tmp_path)
    assert miner.run_once() is False
    journal = next(Path(tmp_path).rglob("offers.json"))
    state = json.loads(journal.read_text())
    for field in (
        "offer_request_id", "offer_authority_epoch", "offer_window_id",
        "offer_terms_sha256", "ack_by_at",
    ):
        state["bids"]["bid_1"].pop(field, None)
    state.update(
        pending=None, offer_protocol_floor=None, offer_protocol_poisoned=False,
        offer_authority_epoch=None, request_outcomes={}, window_markers={}, offer_window_history={},
    )
    journal.write_text(json.dumps(state))
    transport.set_award("bid_1")

    restarted = MinerSkeleton(miner.client, replace(miner.config), miner.solve_fn)
    assert restarted.run_once() is True
    assert transport.source.completed["terminal"]["verification_state"] == "verified"
    assert len(offers(transport)) == 1


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
    transport.queue_response = _FakeResponse(200, {
        "jobs": [{**transport.jobs[0], "awarded_to_you": True,
                  "bid_id": "foreign", "award_id": "awd_foreign"}],
        "offer_idempotency": {
            "protocol": "ormas-offer-request-v1",
            "authority_epoch": transport.offer_authority_epoch,
        },
    })
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


def test_stable_legacy_window_uses_one_request_across_32_idle_polls_and_restart(tmp_path):
    transport, miner = window_miner(tmp_path)
    transport.source = None
    transport.legacy.add("task_1")
    assert miner.run_once() is False
    for _ in range(31):
        restarted = MinerSkeleton(miner.client, replace(miner.config), miner.solve_fn)
        assert restarted.run_once() is False

    posted = offers(transport)
    assert len(posted) == 1
    assert len(transport.offer_requests) == 1
    assert len(claims(transport)) == 64
    assert all(body["offers"] == [] for body in claims(transport)[::2])
    assert all(body["offers"] == [{
        "job_id": "task_1", "kind": "firm", "price_usd": 1.25,
    }] for body in claims(transport)[1::2])
    state = json.loads(next(Path(tmp_path).rglob("offers.json")).read_text())
    assert state["pending"] == "ormas-offer-request-v1"
    assert len(state["request_outcomes"]) == 1
    assert state["window_markers"]["task_1"]["reason"] == "not_offering"


def test_stable_legacy_window_passes_changed_price_to_claim_without_new_post(tmp_path):
    desired = [{"job_id": "task_1", "kind": "firm", "price_usd": 1.25}]
    transport, miner = window_miner(tmp_path, lambda _: dict(desired[0]))
    transport.source = None
    transport.legacy.add("task_1")
    assert miner.run_once() is False
    desired[0]["price_usd"] = 2.5
    restarted = MinerSkeleton(miner.client, replace(miner.config), miner.solve_fn)
    assert restarted.run_once() is False
    assert len(offers(transport)) == 1
    assert claims(transport)[-1]["offers"] == [desired[0]]


def test_legacy_classification_epoch_change_stays_fenced(tmp_path):
    transport, miner = window_miner(tmp_path)
    transport.source = None
    transport.legacy.add("task_1")
    assert miner.run_once() is False
    transport.offer_authority_epoch = "c" * 32
    transport.calls.clear()
    restarted = MinerSkeleton(miner.client, replace(miner.config), miner.solve_fn)
    with pytest.raises(RecoveryRequired, match="authority epoch changed"):
        restarted.run_once()
    assert offers(transport) == claims(transport) == []


def test_changed_window_generation_gets_one_fresh_bound_request(tmp_path):
    transport, miner = window_miner(tmp_path)
    transport.source = None
    transport.legacy.add("task_1")
    assert miner.run_once() is False
    first_request = offers(transport)[0][2]["offer_request_id"]
    transport.offer_window_id = "c" * 32
    transport.jobs[0]["offer_window_id"] = transport.offer_window_id
    restarted = MinerSkeleton(miner.client, replace(miner.config), miner.solve_fn)
    assert restarted.run_once() is False
    posted = offers(transport)
    assert len(posted) == 2
    assert posted[1][2]["offer_request_id"] != first_request
    assert posted[1][2]["offer_request_id"][4:36] == transport.offer_window_id
    state = json.loads(next(Path(tmp_path).rglob("offers.json")).read_text())
    assert len(state["request_outcomes"]) == 2
    assert state["window_markers"]["task_1"]["offer_window_id"] == transport.offer_window_id


@pytest.mark.parametrize("reason", ["window_closed", "request_limit"])
def test_terminal_window_marker_prevents_new_request_until_window_changes(tmp_path, reason):
    transport, miner = window_miner(tmp_path)
    transport.source = None
    transport.offer_refusal_reason = reason
    assert miner.run_once() is False
    transport.offer_refusal_reason = None
    for _ in range(3):
        restarted = MinerSkeleton(miner.client, replace(miner.config), miner.solve_fn)
        assert restarted.run_once() is False
    assert len(offers(transport)) == 1
    assert len(transport.offer_requests) == 1
    state = json.loads(next(Path(tmp_path).rglob("offers.json")).read_text())
    assert state["window_markers"]["task_1"]["reason"] == reason
    assert next(iter(state["request_outcomes"].values()))["message"] == reason
    transport.offer_window_id = "d" * 32
    transport.jobs[0]["offer_window_id"] = transport.offer_window_id
    restarted = MinerSkeleton(miner.client, replace(miner.config), miner.solve_fn)
    assert restarted.run_once() is False
    assert len(offers(transport)) == 2
    assert offers(transport)[-1][2]["offer_window_id"] == transport.offer_window_id


def test_request_limit_binding_mismatch_retains_pending(tmp_path):
    transport, miner = window_miner(tmp_path)
    transport.offer_refusal_reason = "request_limit"
    transport.offer_window_echo = "f" * 32
    with pytest.raises(RecoveryRequired, match="offer POST outcome unknown"):
        miner.run_once()
    state = json.loads(next(Path(tmp_path).rglob("offers.json")).read_text())
    assert state["pending"] is not None
    assert state["request_outcomes"] == state["window_markers"] == {}


def test_terminal_refusal_replay_cannot_later_create_a_bid(tmp_path):
    transport, miner = window_miner(tmp_path)
    transport.offer_refusal_reason = "request_limit"
    assert miner.run_once() is False
    posted = offers(transport)[0][2]
    transport.offer_refusal_reason = None
    response = transport.post("/api/runner/v1/offers", json=posted, headers={})
    assert response.status_code == 409
    assert response.json()["error"]["message"] == "request_limit"
    assert transport.bids == {} and len(transport.offer_requests) == 1


@pytest.mark.parametrize("error,expected", [
    (RuntimeError("transport broke"), RecoveryRequired),
    (_FakeResponse(401, {"error": {
        "type": "authentication_error", "message": "bad device",
    }}), OrmasGatewayError),
    (_FakeResponse(503, {"error": {
        "type": "service_unavailable", "message": "retry",
    }}), OrmasGatewayError),
    (_FakeResponse(409, {"error": {
        "type": "conflict_error", "message": "request_limit",
    }}), RecoveryRequired),
])
def test_window_nonfenced_errors_keep_request_pending(tmp_path, error, expected):
    transport, miner = window_miner(tmp_path)
    transport.error = error
    with pytest.raises(expected):
        miner.run_once()
    assert json.loads(next(Path(tmp_path).rglob("offers.json")).read_text())["pending"]
    assert claims(transport) == []


def test_daemon_replays_same_request_after_transient_gateway_failure(tmp_path):
    transport, miner = window_miner(tmp_path)
    transport.error = _FakeResponse(503, {"error": {
        "type": "service_unavailable", "message": "retry",
    }})
    sleeps = []

    def recover_gateway(delay):
        sleeps.append(delay)
        transport.error = None

    miner.run_forever(max_iterations=2, idle_sleep=recover_gateway)
    posted = offers(transport)
    assert len(posted) == 2 and posted[0][2] == posted[1][2]
    assert len(transport.bids) == 1
    assert json.loads(next(Path(tmp_path).rglob("offers.json")).read_text())["pending"] == "ormas-offer-request-v1"
    assert len(sleeps) == 2


def test_lost_ack_restart_replays_exact_request_and_recovers_one_bid(tmp_path):
    transport, miner = window_miner(tmp_path)
    transport.lose_offer_ack = True
    with pytest.raises(RecoveryRequired, match="offer POST outcome unknown"):
        miner.run_once()
    journal = next(Path(tmp_path).rglob("offers.json"))
    saved = json.loads(journal.read_text())["pending"]
    assert saved["version"] == 2
    assert saved["offer_request_id"].startswith("ofr_")
    assert len(saved["offer_request_id"]) == 132
    assert saved["offer_authority_epoch"] == transport.offer_authority_epoch
    assert saved["offer_window_id"] == transport.offer_window_id
    assert saved["offer_terms_sha256"] == _offer_terms_sha256(saved["terms"])
    assert saved["offer_request_id"][4:36] == saved["offer_window_id"]
    assert saved["offer_request_id"][36:100] == saved["offer_terms_sha256"]

    restarted = MinerSkeleton(miner.client, replace(miner.config), miner.solve_fn)
    assert restarted.run_once() is False
    posted = offers(transport)
    assert len(posted) == 2 and posted[0][2] == posted[1][2]
    assert len(transport.bids) == 1
    state = json.loads(journal.read_text())
    assert state["pending"] == "ormas-offer-request-v1"
    assert state["current"] == {"task_1": "bid_1"}
    assert state["bids"]["bid_1"]["offer_request_id"] == saved["offer_request_id"]
    assert not any(call[0] == "GET" and "/offers?" in call[1] for call in transport.calls)


def test_lost_before_commit_restart_replays_same_request_and_can_succeed(tmp_path):
    transport, miner = window_miner(tmp_path)
    transport.lose_offer_before_commit = True
    with pytest.raises(RecoveryRequired, match="offer POST outcome unknown"):
        miner.run_once()
    restarted = MinerSkeleton(miner.client, replace(miner.config), miner.solve_fn)
    assert restarted.run_once() is False
    posted = offers(transport)
    assert len(posted) == 2 and posted[0][2] == posted[1][2]
    assert list(transport.bids) == ["bid_1"]
    assert json.loads(next(Path(tmp_path).rglob("offers.json")).read_text())["pending"] == "ormas-offer-request-v1"


def test_replayed_success_preserves_award_identity_before_execution(tmp_path):
    transport, miner = window_miner(tmp_path)
    transport.lose_offer_ack = True
    with pytest.raises(RecoveryRequired, match="offer POST outcome unknown"):
        miner.run_once()
    transport.set_award("bid_1")
    restarted = MinerSkeleton(miner.client, replace(miner.config), miner.solve_fn)
    assert restarted.run_once() is True
    assert len(offers(transport)) == 2 and len(transport.bids) == 1
    assert transport.source.completed["terminal"]["verification_state"] == "verified"
    state = json.loads(next(Path(tmp_path).rglob("offers.json")).read_text())
    assert state["bids"]["bid_1"]["award_id"] == "awd_1"


@pytest.mark.parametrize(("attribute", "echo"), [
    ("offer_request_echo", False),
    ("offer_request_echo", "ofr_" + "f" * 128),
    ("offer_epoch_echo", "b" * 32),
    ("offer_digest_echo", "f" * 64),
    ("offer_job_echo", "other-task"),
    ("offer_window_echo", "f" * 32),
])
def test_missing_or_mismatched_success_binding_keeps_request_pending(
    tmp_path, attribute, echo,
):
    transport, miner = window_miner(tmp_path)
    setattr(transport, attribute, echo)
    with pytest.raises(RecoveryRequired, match="offer POST outcome unknown"):
        miner.run_once()
    before = json.loads(next(Path(tmp_path).rglob("offers.json")).read_text())["pending"]
    restarted = MinerSkeleton(miner.client, replace(miner.config), miner.solve_fn)
    with pytest.raises(RecoveryRequired, match="offer POST outcome unknown"):
        restarted.run_once()
    after = json.loads(next(Path(tmp_path).rglob("offers.json")).read_text())["pending"]
    assert after == before
    assert len(offers(transport)) == 2 and offers(transport)[0][2] == offers(transport)[1][2]
    assert len(transport.bids) == 1 and claims(transport) == []


def test_exact_typed_durable_refusal_is_the_only_error_that_clears_pending(tmp_path):
    transport, miner = window_miner(tmp_path)
    transport.offer_refusal_reason = "window_closed"
    assert miner.run_once() is False
    state = json.loads(next(Path(tmp_path).rglob("offers.json")).read_text())
    assert state["pending"] == "ormas-offer-request-v1" and state["bids"] == {}
    assert len(state["request_outcomes"]) == 1
    request_id, refusal = next(iter(state["request_outcomes"].items()))
    assert refusal == {
        "version": 2,
        "offer_request_id": request_id,
        "offer_authority_epoch": transport.offer_authority_epoch,
        "offer_window_id": transport.offer_window_id,
        "offer_terms_sha256": _offer_terms_sha256(refusal["terms"]),
        "terms": {"job_id": "task_1", "kind": "firm", "price_usd": 1.25},
        "outcome": "refused",
        "http_status": 409,
        "error_type": "offer_request_refused",
        "message": "window_closed",
    }
    assert state["window_markers"] == {"task_1": {
        "offer_request_id": request_id,
        "offer_authority_epoch": transport.offer_authority_epoch,
        "offer_window_id": transport.offer_window_id,
        "reason": "window_closed",
    }}


def test_typed_refusal_with_unknown_message_stays_pending(tmp_path):
    transport, miner = window_miner(tmp_path)
    transport.offer_refusal_reason = "try_again"
    with pytest.raises(RecoveryRequired, match="offer POST outcome unknown"):
        miner.run_once()
    assert json.loads(next(Path(tmp_path).rglob("offers.json")).read_text())["pending"]
    assert claims(transport) == []


@pytest.mark.parametrize(("attribute", "echo"), [
    ("offer_refusal_echo", "ofr_" + "f" * 128),
    ("offer_epoch_echo", "b" * 32),
    ("offer_digest_echo", "f" * 64),
    ("offer_job_echo", "other-task"),
    ("offer_window_echo", "f" * 32),
])
def test_typed_refusal_with_wrong_binding_stays_pending(tmp_path, attribute, echo):
    transport, miner = window_miner(tmp_path)
    transport.offer_refusal_reason = "window_closed"
    setattr(transport, attribute, echo)
    with pytest.raises(RecoveryRequired, match="offer POST outcome unknown"):
        miner.run_once()
    journal = next(Path(tmp_path).rglob("offers.json"))
    before = json.loads(journal.read_text())["pending"]
    restarted = MinerSkeleton(miner.client, replace(miner.config), miner.solve_fn)
    with pytest.raises(RecoveryRequired, match="offer POST outcome unknown"):
        restarted.run_once()
    assert json.loads(journal.read_text())["pending"] == before
    assert len(offers(transport)) == 2 and offers(transport)[0][2] == offers(transport)[1][2]
    assert claims(transport) == []


def test_unadvertised_old_gateway_gets_no_idempotent_offer_post(tmp_path):
    transport, miner = window_miner(tmp_path)
    transport.advertise_offer_idempotency = False
    with pytest.raises(RecoveryRequired, match="no usable offer idempotency capability"):
        miner.run_once()
    assert offers(transport) == []
    assert transport.bids == {} and claims(transport) == []
    journals = list(Path(tmp_path).rglob("offers.json"))
    assert not journals or json.loads(journals[0].read_text())["pending"] is None


@pytest.mark.parametrize("queue_view", ["empty", "legacy_row", "declined"])
def test_unadvertised_gateway_never_claims_in_window_mode(tmp_path, queue_view):
    transport, miner = window_miner(tmp_path)
    transport.advertise_offer_idempotency = False
    if queue_view == "empty":
        transport.jobs = []
    elif queue_view == "legacy_row":
        transport.jobs[0].pop("offer_window_id")
    else:
        miner.config.offer_fn = lambda _: None
    with pytest.raises(RecoveryRequired, match="no usable offer idempotency capability"):
        miner.run_once()
    assert offers(transport) == claims(transport) == []


@pytest.mark.parametrize("prior", ["pending", "accepted", "refused", "poisoned"])
def test_disabling_window_mode_cannot_bypass_offer_journal(tmp_path, prior):
    transport, miner = window_miner(tmp_path)
    if prior == "pending":
        transport.lose_offer_ack = True
        with pytest.raises(RecoveryRequired):
            miner.run_once()
    else:
        if prior == "refused":
            transport.offer_refusal_reason = "window_closed"
        assert miner.run_once() is False
        if prior == "poisoned":
            transport.advertise_offer_idempotency = False
            with pytest.raises(RecoveryRequired):
                miner.run_once()
    journal = next(tmp_path.rglob("offers.json"))
    before = journal.read_bytes()
    transport.calls.clear()
    restarted = MinerSkeleton(miner.client, replace(miner.config, offer_window=False), miner.solve_fn)
    with pytest.raises(RecoveryRequired, match="offer-window mode"):
        restarted.run_once()
    assert journal.read_bytes() == before
    assert transport.calls == []


@pytest.mark.parametrize("interrupted", [False, True])
def test_new_window_fence_survives_restart_and_queue_omission(tmp_path, interrupted):
    transport, miner = window_miner(tmp_path)
    assert miner.run_once() is False
    transport.jobs[0]["offer_window_id"] = "d" * 32
    transport.queue_response = _FakeResponse(200, {
        "jobs": deepcopy(transport.jobs),
        "offer_idempotency": {
            "protocol": "ormas-offer-request-v1",
            "authority_epoch": transport.offer_authority_epoch,
        },
    })

    def decline(_entry):
        if interrupted:
            raise RuntimeError("synthetic pricing failure")
        return None

    miner.config.offer_fn = decline
    if interrupted:
        with pytest.raises(RuntimeError, match="synthetic pricing failure"):
            miner.run_once()
    else:
        # Do not withdraw the old bid: it was awarded before this page arrived.
        transport.bids["bid_1"].update(status="awarded", award_id="awd_1")
        assert miner.run_once() is False

    transport.set_award("bid_1")
    transport.queue_response = _FakeResponse(200, {
        "jobs": [],
        "offer_idempotency": {
            "protocol": "ormas-offer-request-v1",
            "authority_epoch": transport.offer_authority_epoch,
        },
    })
    restarted = MinerSkeleton(miner.client, replace(miner.config), miner.solve_fn)
    with pytest.raises(RecoveryRequired, match="superseded offer generation"):
        restarted.run_once()
    assert len(offers(transport)) == 1
    assert transport.source.completed is None


def test_queue_cannot_return_to_a_previously_observed_window(tmp_path):
    transport, miner = window_miner(tmp_path)
    assert miner.run_once() is False
    old_window = transport.jobs[0]["offer_window_id"]
    transport.jobs[0]["offer_window_id"] = "d" * 32
    miner.config.offer_fn = lambda _: None
    assert miner.run_once() is False
    transport.jobs[0]["offer_window_id"] = old_window
    transport.calls.clear()
    restarted = MinerSkeleton(miner.client, replace(miner.config), miner.solve_fn)
    with pytest.raises(RecoveryRequired, match="offer window rollback"):
        restarted.run_once()
    assert offers(transport) == claims(transport) == []


def test_old_pending_ack_cannot_be_promoted_after_new_window_pricing_failure(tmp_path):
    transport, miner = window_miner(tmp_path)
    transport.lose_offer_ack = True
    with pytest.raises(RecoveryRequired):
        miner.run_once()
    transport.jobs[0]["offer_window_id"] = "d" * 32

    def fail_pricing(_entry):
        raise RuntimeError("synthetic pricing failure")

    miner.config.offer_fn = fail_pricing
    with pytest.raises(RuntimeError, match="synthetic pricing failure"):
        miner.run_once()
    transport.set_award("bid_1")
    transport.jobs = []
    restarted = MinerSkeleton(miner.client, replace(miner.config), miner.solve_fn)
    with pytest.raises(RecoveryRequired, match="superseded offer generation|unknown own award"):
        restarted.run_once()
    state = json.loads(next(tmp_path.rglob("offers.json")).read_text())
    assert state["current"] == {}
    assert "bid_1" in state["bids"]
    assert transport.source.completed is None


def test_window_transition_must_be_durable_before_pricing_or_claim(tmp_path, monkeypatch):
    transport, miner = window_miner(tmp_path)
    assert miner.run_once() is False
    journal = next(tmp_path.rglob("offers.json"))
    before = journal.read_bytes()
    transport.jobs[0]["offer_window_id"] = "d" * 32
    transport.calls.clear()
    miner.config.offer_fn = lambda _: pytest.fail("pricing preceded durable window history")

    def fail_save(_state):
        raise OSError("synthetic journal failure")

    monkeypatch.setattr(miner, "_save_offers", fail_save)
    with pytest.raises(OSError, match="synthetic journal failure"):
        miner.run_once()
    assert journal.read_bytes() == before
    assert offers(transport) == claims(transport) == []


@pytest.mark.parametrize("capability", [
    {"protocol": "other", "authority_epoch": "a" * 32},
    {"protocol": "ormas-offer-request-v1", "authority_epoch": "A" * 32},
    {"protocol": "ormas-offer-request-v1", "authority_epoch": "a" * 31},
])
def test_malformed_idempotency_capability_prevents_offer_post(tmp_path, capability):
    transport, miner = window_miner(tmp_path)
    transport.queue_response = _FakeResponse(200, {
        "jobs": deepcopy(transport.jobs), "offer_idempotency": capability,
    })
    with pytest.raises(RecoveryRequired, match="no usable offer idempotency capability"):
        miner.run_once()
    assert offers(transport) == []
    assert transport.bids == {} and claims(transport) == []


def test_mixed_gateway_unknown_field_keeps_same_request_for_manual_reconcile(tmp_path):
    transport, miner = window_miner(tmp_path)
    transport.supports_offer_request_id = False
    with pytest.raises(RecoveryRequired, match="offer POST outcome unknown"):
        miner.run_once()
    before = json.loads(next(Path(tmp_path).rglob("offers.json")).read_text())["pending"]
    restarted = MinerSkeleton(miner.client, replace(miner.config), miner.solve_fn)
    with pytest.raises(RecoveryRequired, match="offer POST outcome unknown"):
        restarted.run_once()
    after = json.loads(next(Path(tmp_path).rglob("offers.json")).read_text())["pending"]
    assert after == before
    assert len(offers(transport)) == 2 and offers(transport)[0][2] == offers(transport)[1][2]
    assert transport.bids == {} and claims(transport) == []


def test_authority_epoch_change_cannot_mutate_or_clear_pending_request(tmp_path):
    transport, miner = window_miner(tmp_path)
    transport.lose_offer_before_commit = True
    with pytest.raises(RecoveryRequired, match="offer POST outcome unknown"):
        miner.run_once()
    journal = next(Path(tmp_path).rglob("offers.json"))
    before = json.loads(journal.read_text())["pending"]
    assert before["offer_authority_epoch"] == "a" * 32

    transport.offer_authority_epoch = "b" * 32
    restarted = MinerSkeleton(miner.client, replace(miner.config), miner.solve_fn)
    with pytest.raises(RecoveryRequired, match="authority epoch changed"):
        restarted.run_once()

    assert json.loads(journal.read_text())["pending"] == before
    assert transport.bids == {} and transport.offer_requests == {}
    posted = offers(transport)
    assert len(posted) == 1
    assert claims(transport) == []


@pytest.mark.parametrize("prior", ["accepted", "window_closed", "not_offering"])
def test_authority_epoch_is_pinned_after_every_terminal_protocol_state(tmp_path, prior):
    transport, miner = window_miner(tmp_path)
    if prior != "accepted":
        transport.offer_refusal_reason = prior
        if prior == "not_offering":
            transport.legacy.add("task_1")
            transport.source = None
    assert miner.run_once() is False
    journal = next(Path(tmp_path).rglob("offers.json"))
    before = journal.read_bytes()
    state = json.loads(before)
    assert state["offer_authority_epoch"] == "a" * 32
    assert state["pending"] == "ormas-offer-request-v1"

    transport.offer_authority_epoch = "c" * 32
    transport.calls.clear()
    restarted = MinerSkeleton(miner.client, replace(miner.config), miner.solve_fn)
    with pytest.raises(RecoveryRequired, match="authority epoch changed"):
        restarted.run_once()

    assert journal.read_bytes() == before
    assert offers(transport) == claims(transport) == []


def test_protocol_binding_is_durable_before_pricing_or_network_mutation(tmp_path):
    def fail_pricing(_entry):
        raise RuntimeError("synthetic pricing failure")

    transport, miner = window_miner(tmp_path, fail_pricing)
    with pytest.raises(RuntimeError, match="synthetic pricing failure"):
        miner.run_once()

    state = json.loads(next(Path(tmp_path).rglob("offers.json")).read_text())
    assert state["offer_protocol_floor"] == "ormas-offer-request-v1"
    assert state["offer_authority_epoch"] == transport.offer_authority_epoch
    assert state["pending"] == "ormas-offer-request-v1"
    assert offers(transport) == claims(transport) == []


def test_refusal_only_prerelease_journal_migrates_before_queue_failure(tmp_path):
    transport, miner = window_miner(tmp_path)
    transport.offer_refusal_reason = "window_closed"
    assert miner.run_once() is False
    journal = next(Path(tmp_path).rglob("offers.json"))
    state = json.loads(journal.read_text())
    state.pop("offer_authority_epoch")
    state["offer_protocol_floor"] = None
    state["pending"] = None
    journal.write_text(json.dumps(state))

    transport.calls.clear()
    transport.queue_response = _FakeResponse(503, {"error": {
        "type": "service_unavailable", "message": "synthetic queue outage",
    }})
    restarted = MinerSkeleton(miner.client, replace(miner.config), miner.solve_fn)
    with pytest.raises(OrmasGatewayError):
        restarted.run_once()

    migrated = json.loads(journal.read_text())
    assert migrated["offer_protocol_floor"] == "ormas-offer-request-v1"
    assert migrated["offer_authority_epoch"] == "a" * 32
    assert migrated["pending"] == "ormas-offer-request-v1"
    assert offers(transport) == claims(transport) == []


def test_pending_success_replays_after_queue_advances_to_new_window(tmp_path):
    transport, miner = window_miner(tmp_path)
    transport.lose_offer_ack = True
    with pytest.raises(RecoveryRequired, match="offer POST outcome unknown"):
        miner.run_once()
    original = offers(transport)[0][2]
    transport.offer_window_id = "c" * 32
    transport.jobs[0]["offer_window_id"] = transport.offer_window_id
    restarted = MinerSkeleton(miner.client, replace(miner.config), miner.solve_fn)
    assert restarted.run_once() is False
    posted = offers(transport)
    assert len(posted) == 3 and posted[1][2] == original
    assert posted[2][2]["offer_window_id"] == "c" * 32
    assert posted[2][2]["offer_request_id"] != original["offer_request_id"]
    state = json.loads(next(Path(tmp_path).rglob("offers.json")).read_text())
    assert state["pending"] == "ormas-offer-request-v1"
    assert state["bids"]["bid_1"]["offer_window_id"] == "b" * 32
    assert state["bids"][state["current"]["task_1"]]["offer_window_id"] == "c" * 32


def test_pending_uncommitted_request_gets_bound_close_after_window_advances(tmp_path):
    transport, miner = window_miner(tmp_path)
    transport.lose_offer_before_commit = True
    with pytest.raises(RecoveryRequired, match="offer POST outcome unknown"):
        miner.run_once()
    original = offers(transport)[0][2]
    transport.offer_window_id = "c" * 32
    transport.jobs[0]["offer_window_id"] = transport.offer_window_id
    restarted = MinerSkeleton(miner.client, replace(miner.config), miner.solve_fn)
    assert restarted.run_once() is False
    posted = offers(transport)
    assert len(posted) == 3 and posted[1][2] == original
    assert posted[2][2]["offer_window_id"] == "c" * 32
    state = json.loads(next(Path(tmp_path).rglob("offers.json")).read_text())
    assert state["pending"] == "ormas-offer-request-v1"
    assert next(iter(state["request_outcomes"].values()))["message"] == "window_closed"


def test_replayed_old_window_success_cannot_authorize_old_window_lease(tmp_path):
    transport, miner = window_miner(tmp_path)
    transport.lose_offer_ack = True
    with pytest.raises(RecoveryRequired, match="offer POST outcome unknown"):
        miner.run_once()

    transport.offer_window_id = "d" * 32
    transport.jobs[0]["offer_window_id"] = transport.offer_window_id
    transport.bids["bid_1"].update(status="awarded", award_id="awd_old")
    transport.award = "bid_1"
    transport.queue_response = _FakeResponse(200, {
        "jobs": [{
            "job_id": "task_1", "created_at": "2026-10-08T00:01:00Z",
            "envelope": {}, "offer_window_id": transport.offer_window_id,
        }],
        "offer_idempotency": {
            "protocol": "ormas-offer-request-v1",
            "authority_epoch": transport.offer_authority_epoch,
        },
    })

    restarted = MinerSkeleton(miner.client, replace(miner.config), miner.solve_fn)
    with pytest.raises(RecoveryRequired, match="superseded offer generation"):
        restarted.run_once()
    assert transport.source.completed is None


def test_current_old_window_bid_cannot_authorize_lease_when_new_window_declined(tmp_path):
    desired = {"value": {"job_id": "task_1", "kind": "firm", "price_usd": 1.25}}

    def quote(_entry):
        return None if desired["value"] is None else dict(desired["value"])

    transport, miner = window_miner(tmp_path, quote)
    assert miner.run_once() is False
    transport.offer_window_id = "d" * 32
    transport.jobs[0]["offer_window_id"] = transport.offer_window_id
    transport.bids["bid_1"].update(status="awarded", award_id="awd_old")
    transport.award = "bid_1"
    desired["value"] = None
    transport.queue_response = _FakeResponse(200, {
        "jobs": [{
            "job_id": "task_1", "created_at": "2026-10-08T00:01:00Z",
            "envelope": {}, "offer_window_id": transport.offer_window_id,
        }],
        "offer_idempotency": {
            "protocol": "ormas-offer-request-v1",
            "authority_epoch": transport.offer_authority_epoch,
        },
    })

    restarted = MinerSkeleton(miner.client, replace(miner.config), miner.solve_fn)
    with pytest.raises(RecoveryRequired, match="superseded offer generation"):
        restarted.run_once()
    assert transport.source.completed is None


@pytest.mark.parametrize("terminal", [
    "withdrawn", "replaced", "no_capacity", "award_lapsed",
])
def test_current_terminal_bid_cannot_reappear_as_awarded(tmp_path, terminal):
    desired = {"value": {"job_id": "task_1", "kind": "firm", "price_usd": 1.25}}

    def quote(_entry):
        return None if desired["value"] is None else dict(desired["value"])

    transport, miner = window_miner(tmp_path, quote)
    assert miner.run_once() is False
    transport.bids["bid_1"]["status"] = terminal
    desired["value"] = None
    assert MinerSkeleton(miner.client, replace(miner.config), miner.solve_fn).run_once() is False

    transport.bids["bid_1"].update(status="awarded", award_id="awd_stale")
    transport.award = "bid_1"
    transport.queue_response = _FakeResponse(200, {
        "jobs": [dict(transport.jobs[0])],
        "offer_idempotency": {
            "protocol": "ormas-offer-request-v1",
            "authority_epoch": transport.offer_authority_epoch,
        },
    })
    restarted = MinerSkeleton(miner.client, replace(miner.config), miner.solve_fn)
    with pytest.raises(RecoveryRequired, match="award|bid|terminal|transition"):
        restarted.run_once()
    assert transport.source.completed is None


def test_missing_window_identity_blocks_fresh_request(tmp_path):
    transport, miner = window_miner(tmp_path)
    del transport.jobs[0]["offer_window_id"]
    with pytest.raises(RecoveryRequired, match="window identity"):
        miner.run_once()
    assert offers(transport) == claims(transport) == []


def test_capability_rollback_sets_sticky_poison(tmp_path):
    transport, miner = window_miner(tmp_path)
    assert miner.run_once() is False
    transport.advertise_offer_idempotency = False
    with pytest.raises(RecoveryRequired, match="protocol rollback"):
        miner.run_once()
    journal = next(Path(tmp_path).rglob("offers.json"))
    state = json.loads(journal.read_text())
    assert state["offer_protocol_floor"] == "ormas-offer-request-v1"
    assert state["offer_protocol_poisoned"] is True

    transport.advertise_offer_idempotency = True
    transport.calls.clear()
    restarted = MinerSkeleton(miner.client, replace(miner.config), miner.solve_fn)
    with pytest.raises(RecoveryRequired, match="protocol rollback"):
        restarted.run_once()
    assert transport.calls == []


def test_capability_rollback_keeps_pending_and_blocks_later_replay(tmp_path):
    transport, miner = window_miner(tmp_path)
    transport.lose_offer_before_commit = True
    with pytest.raises(RecoveryRequired, match="offer POST outcome unknown"):
        miner.run_once()
    journal = next(Path(tmp_path).rglob("offers.json"))
    pending = json.loads(journal.read_text())["pending"]
    transport.advertise_offer_idempotency = False
    restarted = MinerSkeleton(miner.client, replace(miner.config), miner.solve_fn)
    with pytest.raises(RecoveryRequired, match="protocol rollback"):
        restarted.run_once()
    state = json.loads(journal.read_text())
    assert state["pending"] == pending
    assert state["offer_protocol_poisoned"] is True
    assert len(offers(transport)) == 1


def test_ambiguous_replacement_never_withdraws_current_v2_bid(tmp_path):
    desired = [{"job_id": "task_1", "kind": "firm", "price_usd": 1.25}]
    transport, miner = window_miner(tmp_path, lambda _: dict(desired[0]))
    assert miner.run_once() is False
    desired[0]["price_usd"] = 2.5
    transport.lose_offer_before_commit = True
    transport.calls.clear()
    with pytest.raises(RecoveryRequired, match="offer POST outcome unknown"):
        miner.run_once()
    assert transport.bids["bid_1"]["status"] == "open"
    assert not any(call[0] == "DELETE" for call in transport.calls)
    state = json.loads(next(Path(tmp_path).rglob("offers.json")).read_text())
    assert state["current"] == {"task_1": "bid_1"}
    assert state["pending"]["terms"] == desired[0]


def test_legacy_pending_absence_never_clears_even_after_window_close(tmp_path):
    transport, miner = window_miner(tmp_path)
    transport.lose_offer_before_commit = True
    with pytest.raises(RecoveryRequired):
        miner.run_once()
    journal = next(Path(tmp_path).rglob("offers.json"))
    state = json.loads(journal.read_text())
    state["pending"] = state["pending"]["terms"]
    journal.write_text(json.dumps(state))
    transport.window_closed = True
    restarted = MinerSkeleton(miner.client, replace(miner.config), miner.solve_fn)
    with pytest.raises(RecoveryRequired, match="offer POST outcome unknown"):
        restarted.run_once()
    assert json.loads(journal.read_text())["pending"] == state["pending"]
    assert len(offers(transport)) == 1


def test_legacy_pending_never_adopts_same_terms_unknown_own_bid(tmp_path):
    transport, miner = window_miner(tmp_path)
    transport.lose_offer_before_commit = True
    with pytest.raises(RecoveryRequired):
        miner.run_once()
    journal = next(Path(tmp_path).rglob("offers.json"))
    state = json.loads(journal.read_text())
    terms = state["pending"]["terms"]
    state["pending"] = terms
    journal.write_text(json.dumps(state))
    before = journal.read_bytes()
    transport.bids["bid_external"] = {"terms": deepcopy(terms), "status": "open"}
    restarted = MinerSkeleton(miner.client, replace(miner.config), miner.solve_fn)
    with pytest.raises(RecoveryRequired, match="offer POST outcome unknown"):
        restarted.run_once()
    assert journal.read_bytes() == before
    recovered = json.loads(journal.read_text())
    assert recovered["pending"] == terms
    assert recovered["current"] == {}
    assert len(offers(transport)) == 1
    assert not any(call[0] == "GET" and "/offers?" in call[1] for call in transport.calls)


@pytest.mark.parametrize("status", ["open", "awarded"])
def test_legacy_pending_never_adopts_known_prior_bid_at_any_status(tmp_path, status):
    desired = [{"job_id": "task_1", "kind": "firm", "price_usd": 1.25}]
    transport, miner = window_miner(tmp_path, lambda _: desired[0])
    assert miner.run_once() is False
    desired[0] = {"job_id": "task_1", "kind": "firm", "price_usd": 2.5}
    transport.lose_offer_before_commit = True
    with pytest.raises(RecoveryRequired):
        miner.run_once()
    journal = next(Path(tmp_path).rglob("offers.json"))
    state = json.loads(journal.read_text())
    state["pending"] = state["pending"]["terms"]
    journal.write_text(json.dumps(state))
    if status == "awarded":
        transport.set_award("bid_1")
    transport.window_closed = True
    restarted = MinerSkeleton(miner.client, replace(miner.config), miner.solve_fn)
    with pytest.raises(RecoveryRequired, match="offer POST outcome unknown"):
        restarted.run_once()
    recovered = json.loads(journal.read_text())
    assert recovered["pending"] == desired[0]
    assert recovered["current"] == {"task_1": "bid_1"}


def test_ambiguous_replacement_fence_preserves_prior_open_bid(tmp_path):
    desired = [{"job_id": "task_1", "kind": "firm", "price_usd": 1.25}]
    transport, miner = window_miner(tmp_path, lambda _: desired[0])
    assert miner.run_once() is False
    desired[0] = {"job_id": "task_1", "kind": "firm", "price_usd": 2.5}
    transport.lose_offer_before_commit = True
    with pytest.raises(RecoveryRequired):
        miner.run_once()
    transport.offer_refusal_reason = "window_closed"
    restarted = MinerSkeleton(miner.client, replace(miner.config), miner.solve_fn)
    assert restarted.run_once() is False
    state = json.loads(next(Path(tmp_path).rglob("offers.json")).read_text())
    assert state["pending"] == "ormas-offer-request-v1"
    assert state["current"] == {"task_1": "bid_1"}
    assert state["bids"]["bid_1"]["status"] == "open"
    posted = offers(transport)
    assert len(posted) == 3 and posted[1][2] == posted[2][2]


def test_ambiguous_replacement_preserves_prior_award_after_window_close(tmp_path):
    desired = [{"job_id": "task_1", "kind": "limit",
                "estimate_usd": 0.9, "limit_usd": 1.25}]
    transport, miner = window_miner(tmp_path, lambda _: desired[0])
    assert miner.run_once() is False
    desired[0] = {"job_id": "task_1", "kind": "limit",
                  "estimate_usd": 1.8, "limit_usd": 2.5}
    transport.lose_offer_before_commit = True
    with pytest.raises(RecoveryRequired):
        miner.run_once()

    transport.set_award("bid_1")
    restarted = MinerSkeleton(miner.client, replace(miner.config), miner.solve_fn)
    assert restarted.run_once() is True
    assert transport.source.completed["terminal"]["settled_price_usd"] == 1.25
    state = json.loads(next(Path(tmp_path).rglob("offers.json")).read_text())
    assert state["pending"] == "ormas-offer-request-v1"
    assert state["bids"]["bid_1"]["status"] == "awarded"
    assert state["bids"]["bid_1"]["award_id"] == "awd_1"
    posted = offers(transport)
    assert len(posted) == 3 and posted[1][2] == posted[2][2]


def test_public_recovery_precedes_all_offer_calls(tmp_path, monkeypatch):
    transport, miner = window_miner(tmp_path)
    from ormas_subnet._recovery import PublicRecovery
    monkeypatch.setattr(PublicRecovery, "pending", lambda _: {"stage": "completion"})
    monkeypatch.setattr(miner, "_recover_public", lambda _: True)
    assert miner.run_once() is True
    assert transport.calls == []


def test_offer_request_is_durable_before_post_and_bid_before_claim(tmp_path, monkeypatch):
    transport, miner = window_miner(tmp_path)
    original = transport.post

    def checked_post(path, json=None, headers=None):
        if path.endswith("/offers"):
            journals = list(miner.config.workdir_root.rglob("offers.json"))
            assert len(journals) == 1, "journal the offer request before issuing its POST"
            pending = json_module.loads(journals[0].read_text())["pending"]
            assert pending["offer_request_id"] == json["offer_request_id"]
            assert pending["offer_authority_epoch"] == json["offer_authority_epoch"]
            assert pending["offer_window_id"] == json["offer_window_id"]
            assert pending["offer_terms_sha256"] == _offer_terms_sha256(pending["terms"])
            assert pending["terms"] == {
                key: value for key, value in json.items()
                if key not in {
                    "runner_id", "offer_request_id", "offer_authority_epoch", "offer_window_id",
                }
            }
        elif path.endswith("/leases"):
            saved = [p for p in miner.config.workdir_root.rglob("*.json") if "bid_1" in p.read_text()]
            assert saved, "save the returned bid identity before issuing a claim"
        return original(path, json=json, headers=headers)

    json_module = json
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

