"""Card 60: the reference miner's Graded inbox under the gateway's real rules.

The fake gateway below encodes the gateway behaviour the miner depends on. Each
rule cites the gateway code it mirrors (``tensorbox_spec/customer_api``).
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from ormas_subnet import outcomes_support, skeleton
from ormas_subnet._recovery import PublicRecovery, RecoveryRequired
from ormas_subnet.client import OrmasMinerClient
from ormas_subnet.protocol import TaskDraft, TaskLease
from ormas_subnet.skeleton import MinerConfig, MinerSkeleton, SolveResult

BASE = "a" * 40
ARTIFACT = b"frozen round artifact"
FUTURE = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
PARENT_TERMS = {"job_id": "parent-job", "kind": "firm", "price_usd": 2.0}


def revision(**overrides):
    return {
        "job_id": "parent-job", "request_id": "revision-1", "kind": "revision",
        "status": "pending", "contract_digest": "original-digest",
        "amended_contract_digest": "amended-digest", "acceptance_window_s": 86400,
        # outcomes_validators.py revision_request: revision_index = count + 1.
        "revision_index": 1, "revision_allowance": 1,
        "revision_delivery_deadline": FUTURE, "window_closes_at": FUTURE,
        **overrides,
    }


def change_order(**overrides):
    return revision(request_id="change-1", kind="change_order", **overrides)


def lease_for(job_id, *, bid_id=None, terms=None):
    terms = terms or {"kind": "firm", "price_usd": 1.0}
    limit = terms["kind"] == "limit"
    return TaskLease(
        lease_id="lease-" + job_id, task_id=job_id, expires_at="2099-01-01T00:00:00Z",
        selected_cell="task:code", provider_pin="unset", fallback_policy="unset",
        hold_ref="unset", now="2026-11-14T00:00:00Z",
        outcome_price_usd=float(terms["limit_usd"] if limit else terms["price_usd"]),
        bid_id=bid_id, offer_kind=terms["kind"],
        estimate_usd=float(terms["estimate_usd"]) if limit else None,
        limit_usd=float(terms["limit_usd"]) if limit else None,
    )


def draft_for(job_id, round_kind=None, *, origin="parent-job", digest=True):
    # runner_api.py:_draft_and_lease serves the stored packet. A C1 child's
    # contract is the amended one (graded_round_exchange.py:submit_quote) and
    # only a revision draft names round lineage, the original awarded job.
    contract = {"base_sha": BASE, "immutable_paths": []}
    if round_kind == "change_order" and digest:
        contract["digest"] = "amended-digest"
    packet = {} if round_kind is None else {
        "acceptance_level": "graded",
        "graded_contract": contract,
        "execution_policy": {"repo_base_sha": BASE, "immutable_paths": []},
        "delivery_limits": {"max_bytes": 4096, "max_files": 2},
    }
    return TaskDraft(
        task_id=job_id, runner_id="miner-1", repo_id="repo-1", base_commit=BASE,
        brief="work", verify_command="unused", allowed_paths=["value.txt"], budget_usd=1.0,
        work_packet=packet, work_packet_sha256="c" * 64, attempt=1, parent_job_id="",
        repair_findings=[], graded_round_kind="revision" if round_kind == "revision" else None,
        graded_parent_job_id=origin if round_kind == "revision" else None,
    )


class LostReply(httpx.ReadError):
    pass


class Gateway:
    """In-memory gateway for one Graded parent job awarded to ``miner-1``."""

    def __init__(self, *, requests=(), ordinary=(), parent_awarded=True, parent_terms=None):
        self.requests = [dict(r) for r in requests]
        self.ordinary = list(ordinary)  # queued ordinary jobs, oldest first
        self.parent_awarded = parent_awarded
        self.parent_terms = dict(parent_terms or PARENT_TERMS)
        self.children = []              # queued revision children, oldest first
        self.change = None              # change-order child
        self.leased = set()
        self.held = set()               # jobs with a held Graded artifact
        self.inbox_error = None
        self.lose = set()               # operations whose committed reply is lost
        self.calls = []
        self.answers = []
        self.quotes = []
        self.claims = []
        self.completions = []           # (job_id, verification_state)
        self.completed = []             # verified Graded deliveries with custody
        self.lease_terms = {}           # override served terms, by job id
        self.awards = {"bid-parent": "award-parent"} if parent_awarded else {}
        # Requests are keyed by (job_id, request_id): outcomes_store.py
        # append_graded_round_event. A child keeps its original job's bid.
        self.parent_bids = {"parent-job": "bid-parent"}
        self.origin = {}                # child job -> original awarded job
        self.targeted_error = None      # a claim by job id fails this way
        self.quote_error = None         # a quote refused before any commit
        self.draft_digest = True        # a C1 draft carries the amended digest

    def add_parent(self, job_id, bid_id):
        self.parent_bids[job_id] = bid_id
        if self.parent_awarded:
            self.awards[bid_id] = "award-" + job_id

    def accept_quote(self):
        # graded_round_exchange.py:accept_quote awards the miner's own C1 bid;
        # the child stays 'offering' with award_bid_id set until claimed.
        self.change["awarded"] = True

    def committed(self, operation, response):
        if operation in self.lose:
            self.lose.discard(operation)
            raise LostReply("reply lost after the gateway committed")
        return response

    def __call__(self, request):
        path, method = request.url.path, request.method
        self.calls.append((method, path))
        body = json.loads(request.read()) if method == "POST" and not path.endswith("/graded-artifact") else None
        if path == "/api/runner/v1/graded-rounds" and method == "GET":
            if self.inbox_error == "transport":
                raise httpx.ConnectError("inbox down")
            if self.inbox_error is not None:
                return httpx.Response(self.inbox_error, json={"error": {"type": "refused", "message": "no"}})
            return httpx.Response(200, json={"requests": self.requests})
        if path.startswith("/api/runner/v1/graded-rounds/"):
            return self.committed("answer", self.answer(path.rsplit("/", 1)[1], body))
        if path == "/api/runner/v1/offers" and method == "POST":
            if "client_request_id" in body:
                if self.quote_error is not None:
                    return httpx.Response(self.quote_error, json={"error": {"type": "internal", "message": "no"}})
                return self.committed("quote", self.quote(body))
            # runner_api.py:post_offer: a queued v1 job without a window is
            # 409 not_offering; an awarded one is 409 window_closed.
            assert body["job_id"] in self.children, body
            return httpx.Response(409, json={"error": {"type": "conflict_error", "message": (
                "window_closed" if self.parent_awarded else "not_offering")}})
        if path == "/api/runner/v1/offers" and method == "GET":
            return self.offer_for_job(request.url.params["job_id"])
        if path.startswith("/api/runner/v1/offers/") and method == "GET":
            return self.offer(path.rsplit("/", 1)[1])
        if path == "/api/runner/v1/queue":
            return httpx.Response(200, json={"schema_version": "ormas-runner-queue-v1", "jobs": self.queue()})
        if path == "/api/runner/v1/leases":
            self.claims.append(body)
            return self.claim(body)
        if path.endswith("/heartbeat"):
            return httpx.Response(200, json={"ok": True})
        job_id = path.split("/")[-2]
        if path.endswith("/graded-artifact"):
            assert request.read() == ARTIFACT
            self.held.add(job_id)
            return httpx.Response(200, json={"status": "held", "artifact_digest": hashlib.sha256(ARTIFACT).hexdigest()})
        assert path.endswith("/complete"), path
        state = body["terminal"]["verification_state"]
        self.completions.append((job_id, state))
        # runner_api.py:_runner_capture: a verified Graded completion needs the
        # held artifact (_held_graded_artifact) and awaits acceptance; any
        # other state settles no_delivery.
        if state == "verified" and job_id not in self.held:
            return httpx.Response(409, json={"error": {"type": "conflict_error", "message": "graded_artifact_required"}})
        settlement = "pending_acceptance" if state == "verified" else "no_delivery"
        if state == "verified":
            self.completed.append(job_id)
        return httpx.Response(200, json={"status": "done", "receipt": {
            "receipt_id": "receipt-" + job_id, "settlement": settlement}})

    def entry(self, request_id, job_id):
        return next(r for r in self.requests if (r["job_id"], r["request_id"]) == (job_id, request_id))

    def answer(self, request_id, body):
        entry = self.entry(request_id, body["job_id"])
        self.answers.append(body["decision"])
        # graded_round_exchange.py:inbox reports the stored reply as the
        # status, and nothing more: no child job or bid identity.
        entry["status"] = body["decision"]
        if body["decision"] != "revision_agree":
            return httpx.Response(200, json={"request_id": request_id, "decision": body["decision"]})
        # graded_round_exchange.py:answer activates the child at once;
        # outcomes_jobs.py:_activate_graded_revision enqueues it 'queued' and it
        # inherits the parent's bid_id, award_bid_id, award_id and price terms.
        child = "revision-%d" % (len(self.children) + 1)
        self.children.append(child)
        self.origin[child] = self.origin.get(entry["job_id"], entry["job_id"])
        return httpx.Response(200, json={"request_id": request_id, "decision": "revision_agree",
                                         "child_job_id": child})

    def quote(self, body):
        entry = self.entry(body["client_request_id"], body["job_id"])
        terms = {k: body[k] for k in ("kind", "price_usd", "estimate_usd", "limit_usd") if k in body}
        self.quotes.append(body)
        entry["status"] = "change_order_quote"
        # graded_round_exchange.py:submit_quote opens a separate 'offering' C1
        # child and records the miner's bid on it; runner_api.py:_record_offer
        # returns that child's job and bid identities.
        self.change = {"job_id": "change-job", "bid_id": "bid-change", "terms": terms, "awarded": False}
        return httpx.Response(200, json={"bid_id": "bid-change", "job_id": "change-job",
                                         "ack_by_at": FUTURE, "window_closes_at": FUTURE})

    def offer(self, bid_id):
        # runner_api.py:get_offer. The parent bid stays 'awarded' after
        # delivery; a legacy claim's bid keeps the column default 'legacy'
        # (outcomes_store.py outcomes_bids status).
        if bid_id in self.awards:
            return httpx.Response(200, json={"bid_id": bid_id, "status": "awarded", "award_id": self.awards[bid_id]})
        if bid_id in self.parent_bids.values():
            return httpx.Response(200, json={"bid_id": bid_id, "status": "legacy"})
        if self.change is not None and bid_id == self.change["bid_id"]:
            if self.change["awarded"]:
                return httpx.Response(200, json={"bid_id": bid_id, "status": "awarded", "award_id": "award-change"})
            return httpx.Response(200, json={"bid_id": bid_id, "status": "open"})
        return httpx.Response(404, json={"error": {"type": "not_found_error", "message": "no"}})

    def offer_for_job(self, job_id):
        # runner_api.py:get_offer_for_job: this runner's own offer on a C1 child.
        change = self.change
        if change is None or job_id != change["job_id"]:
            return httpx.Response(404, json={"error": {"type": "not_found_error", "message": "no"}})
        bid = {"bid_id": change["bid_id"], "status": "awarded" if change["awarded"] else "open",
               "terms": {"job_id": job_id, **change["terms"]}, "ack_by_at": FUTURE, "window_closes_at": FUTURE}
        if change["awarded"]:
            bid.update(award_id="award-change", job_status="offering")
        return httpx.Response(200, json={"job_id": job_id, "window_closed": change["awarded"], "bid": bid})

    def queue(self):
        # runner_api.py:list_queue. _claim_candidates skips C1 children; a
        # revision child is listed to its own runner, stamped with the
        # inherited parent award when the parent had one.
        jobs = []
        for child in self.children:
            if child in self.leased:
                continue
            entry = {"job_id": child, "created_at": "2026-11-14T00:00:00Z", "envelope": {}}
            if self.parent_awarded:
                bid = self.parent_bids[self.origin.get(child, "parent-job")]
                entry.update(awarded_to_you=True, bid_id=bid, award_id=self.awards[bid])
            jobs.append(entry)
        return jobs

    def lease(self, job_id, bid_id, terms, round_kind):
        self.leased.add(job_id)
        terms = self.lease_terms.get(job_id, terms)
        lease = lease_for(job_id, bid_id=bid_id, terms=terms).to_wire()
        if bid_id is None:
            # runner_api.py:_draft_and_lease drops offer terms from a lease
            # that neither came from an offers claim nor carries an award.
            for key in ("bid_id", "offer_kind", "estimate_usd", "limit_usd"):
                lease.pop(key, None)
        draft = draft_for(job_id, round_kind, origin=self.origin.get(job_id, "parent-job"),
                          digest=self.draft_digest)
        return httpx.Response(200, json={"lease": lease, "draft": draft.to_wire()})

    def claim(self, body):
        offers = body.get("offers")
        wanted = body.get("job_id")  # runner_api.py:_claim_lease_impl requested_job_id
        if wanted is not None and self.targeted_error is not None:
            if self.targeted_error == "transport":
                raise httpx.ReadError("claim reply lost")
            return httpx.Response(self.targeted_error, json={"error": {"type": "conflict_error", "message": "no"}})
        change = self.change
        awarded = (change is not None and change["awarded"] and change["job_id"] not in self.leased
                   and wanted in (None, change["job_id"]))
        if offers == [] and not awarded:
            # runner_api.py:_claim_lease_impl: "Empty offers: re-serve only,
            # never lease a new job" unless an own award is in 'offering'.
            return httpx.Response(204)
        if awarded:
            # Own 'offering' awards come first and replace the candidate scan.
            return self.lease(change["job_id"], change["bid_id"], change["terms"], "change_order")
        offered = {offer["job_id"] for offer in offers or ()}
        for job_id in self.ordinary:
            if (job_id not in self.leased and wanted in (None, job_id)
                    and (offers is None or job_id in offered)):
                return self.lease(job_id, None, None, None)
        for child in self.children:
            if child not in self.leased and wanted in (None, child):
                # Claim loop: the revision branch precedes the offers lookup, so
                # any claim that reaches the scan leases the child at the
                # parent's terms, under the parent's bid_id.
                terms = {k: v for k, v in self.parent_terms.items() if k != "job_id"}
                carries = self.parent_awarded or offers is not None
                bid = self.parent_bids[self.origin.get(child, "parent-job")]
                return self.lease(child, bid if carries else None, terms, "revision")
        return httpx.Response(204)


def make_miner(tmp_path, gateway, monkeypatch, **config):
    client = OrmasMinerClient(
        "https://gateway.invalid", "ormr_test", device_nonce="device-test",
        http_client=httpx.Client(base_url="https://gateway.invalid",
                                 headers={"Authorization": "Bearer ormr_test"},
                                 transport=httpx.MockTransport(gateway)),
    )
    miner = MinerSkeleton(client, MinerConfig(
        runner_id="miner-1", runner_version="test", platform="linux", capacity=1,
        cells=("task:code",), workdir_root=tmp_path, repo_id="", repo_url="", **config,
    ), lambda received, workdir: SolveResult("b" * 40, ("value.txt",)))
    # Ordinary work is recorded, not solved. Graded rounds take the real
    # publication and completion path with solving and git stubbed.
    ordinary = []
    real_run = miner._run_leased_task

    def run(lease, draft, start):
        if not draft.work_packet:
            ordinary.append(lease.task_id)
            return None
        return real_run(lease, draft, start)

    def build(packet, workdir, commit, target):
        target.write(ARTIFACT)
        return len(ARTIFACT), hashlib.sha256(ARTIFACT).hexdigest(), "d" * 64

    monkeypatch.setattr(miner, "_run_leased_task", run)
    monkeypatch.setattr(miner, "_clone_and_checkout", lambda _: tmp_path)
    monkeypatch.setattr(miner, "_heartbeat_loop", lambda *_: None)
    monkeypatch.setattr(miner, "_material_diff_sha256", lambda *_: None)
    monkeypatch.setattr(miner, "_compute_scope", lambda *_: (True, ("value.txt",)))
    monkeypatch.setattr(skeleton, "_run_verify_command", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(outcomes_support, "build_graded_artifact", build)
    miner.ordinary = ordinary
    return miner


def journal(miner, state=None):
    """Read, or write, the miner's offer journal under its recovery lock."""
    store = PublicRecovery(miner.config.workdir_root, miner.client.base_url, miner.config.runner_id)
    with store.lock():
        miner._public_recovery = store
        try:
            if state is not None:
                miner._save_offers(state)
            return miner._offer_state(allow_pending=True)
        finally:
            miner._public_recovery = None


def window_miner(tmp_path, gateway, monkeypatch, **config):
    miner = make_miner(tmp_path, gateway, monkeypatch, offer_window=True, ask_usd=1.0, **config)
    journal(miner, {
        "gateway": "https://gateway.invalid", "runner_id": "miner-1", "pending": None,
        "bids": {"bid-parent": {"terms": dict(PARENT_TERMS), "status": "awarded",
                                "window_closes_at": FUTURE, "award_id": "award-parent"}},
        "current": {"parent-job": "bid-parent"},
    })
    return miner


# A1 ------------------------------------------------------------------------

@pytest.mark.parametrize("allowance,index,decision", [
    (1, 1, "revision_agree"),
    (1, 2, "revision_decline"),
    (2, 1, "revision_agree"),
    (2, 2, "revision_agree"),
    (2, 3, "revision_decline"),
    (1, 0, "revision_decline"),
    (2, -1, "revision_decline"),
    (1, True, "revision_decline"),
    (1, "1", "revision_decline"),
    ("1", 1, "revision_decline"),
    (None, 1, "revision_decline"),
])
def test_revision_consent_uses_gateway_numbering(tmp_path, monkeypatch, allowance, index, decision):
    gateway = Gateway(requests=[revision(revision_index=index, revision_allowance=allowance)])
    miner = make_miner(tmp_path, gateway, monkeypatch)
    miner.run_once()
    assert gateway.answers == [decision]


@pytest.mark.parametrize("deadline", ["2000-01-01T00:00:00Z", "2099-01-01T00:00:00", "soon", None])
def test_revision_consent_needs_future_delivery_deadline(tmp_path, monkeypatch, deadline):
    gateway = Gateway(requests=[revision(revision_delivery_deadline=deadline)])
    miner = make_miner(tmp_path, gateway, monkeypatch)
    miner.run_once()
    assert gateway.answers == ["revision_decline"]


# A2 ------------------------------------------------------------------------

def busy_after_idle(tmp_path, monkeypatch, requests, ordinary, **config):
    """A miner that read its (empty) inbox once, then meets requests and work."""
    gateway = Gateway()
    miner = make_miner(tmp_path, gateway, monkeypatch, **config)
    assert miner.run_once() is False
    gateway.requests = [dict(r) for r in requests]
    gateway.ordinary = list(ordinary)
    gateway.calls.clear()
    gateway.claims.clear()
    return gateway, miner


def test_busy_pass_answers_revision_and_claims_child_next_pass(tmp_path, monkeypatch):
    """The pass that holds ordinary work answers after it; the next pass claims the child."""
    gateway, miner = busy_after_idle(tmp_path, monkeypatch, [revision(revision_allowance=2)], ["job-1", "job-2"])
    assert miner.run_once() is True
    assert miner.ordinary == ["job-1"]
    assert gateway.answers == ["revision_agree"]
    # The ordinary claim is still the first call of the pass.
    assert gateway.calls[:2] == [("POST", "/api/runner/v1/leases"), ("GET", "/api/runner/v1/graded-rounds")]
    assert miner.run_once() is True
    assert gateway.completed == ["revision-1"] and miner.ordinary == ["job-1"]
    assert miner.run_once() is True
    assert miner.ordinary == ["job-1", "job-2"]
    assert miner.run_once() is False
    assert gateway.answers == ["revision_agree"]


def test_fresh_miner_busy_pass_sends_only_ordinary_requests(tmp_path, monkeypatch):
    gateway = Gateway(requests=[revision()], ordinary=["job-1", "job-2"])
    miner = make_miner(tmp_path, gateway, monkeypatch)
    assert miner.run_once() is True
    assert miner.run_once() is True
    assert gateway.calls == [("POST", "/api/runner/v1/leases")] * 2
    # The first idle pass reads the inbox as before, agrees and claims the child.
    assert miner.run_once() is True
    assert gateway.answers == ["revision_agree"]
    assert gateway.completed == ["revision-1"]


@pytest.mark.parametrize("terms", [None, {"kind": "firm", "price_usd": 2.5}])
def test_busy_pass_answers_change_order(tmp_path, monkeypatch, terms):
    config = {} if terms is None else {"change_order_terms": terms}
    gateway, miner = busy_after_idle(tmp_path, monkeypatch, [change_order()], ["job-1", "job-2"], **config)
    assert miner.run_once() is True
    if terms is None:
        assert gateway.answers == ["change_order_decline"] and gateway.quotes == []
    else:
        assert gateway.answers == [] and len(gateway.quotes) == 1
        assert {k: gateway.quotes[0][k] for k in terms} == terms
    assert miner.run_once() is True
    assert miner.ordinary == ["job-1", "job-2"]
    assert len(gateway.answers) + len(gateway.quotes) == 1


@pytest.mark.parametrize("failure", [500, 409, 401, 403, "transport"])
def test_busy_inbox_failure_never_changes_ordinary_work(tmp_path, monkeypatch, failure):
    gateway, miner = busy_after_idle(tmp_path, monkeypatch, [revision()], ["job-1", "job-2"])
    gateway.inbox_error = failure
    assert miner.run_once() is True
    assert miner.run_once() is True
    assert miner.ordinary == ["job-1", "job-2"]
    assert gateway.claims == [{"schema_version": "ormas-runner-v1", "runner_id": "miner-1"}] * 2
    assert gateway.answers == []


@pytest.mark.parametrize("status", ["revision_agree", "revision_decline", "expired"])
def test_busy_pass_never_answers_twice(tmp_path, monkeypatch, status):
    gateway, miner = busy_after_idle(
        tmp_path, monkeypatch, [revision(status=status), change_order(status="change_order_decline")],
        ["job-1"], change_order_terms={"kind": "firm", "price_usd": 2.0})
    assert miner.run_once() is True
    assert miner.run_once() is False
    assert ("GET", "/api/runner/v1/graded-rounds") in gateway.calls
    assert gateway.answers == [] and gateway.quotes == []


# A3 ------------------------------------------------------------------------

def test_offer_window_miner_claims_and_delivers_agreed_revision(tmp_path, monkeypatch):
    gateway = Gateway(requests=[revision(revision_allowance=2)])
    miner = window_miner(tmp_path, gateway, monkeypatch)
    for _ in range(2):
        miner.run_once()
    assert gateway.answers == ["revision_agree"]
    assert gateway.completed == ["revision-1"] and gateway.held == {"revision-1"}
    assert gateway.completions == [("revision-1", "verified")]
    targeted = [c for c in gateway.claims if c.get("job_id")]
    assert targeted == [{"schema_version": "ormas-runner-v1", "runner_id": "miner-1",
                         "offers": [{**PARENT_TERMS, "job_id": "revision-1"}], "job_id": "revision-1"}]


def test_offer_window_revision_agreed_while_busy_is_claimed_next_pass(tmp_path, monkeypatch):
    gateway = Gateway(requests=[revision(revision_allowance=2)])
    miner = window_miner(tmp_path, gateway, monkeypatch)
    # The busy pass's own lease: a C1 award already held, so the inbox is read
    # after that work finishes.
    gateway.change = {"job_id": "change-job", "bid_id": "bid-change",
                      "terms": {"kind": "firm", "price_usd": 3.0}, "awarded": True}
    state = journal(miner)
    state["bids"]["bid-change"] = {"terms": {"job_id": "change-job", "kind": "firm", "price_usd": 3.0},
                                   "status": "open", "window_closes_at": FUTURE}
    state["current"]["change-job"] = "bid-change"
    journal(miner, state)
    assert miner.run_once() is True
    assert gateway.completed == ["change-job"]
    assert gateway.answers == ["revision_agree"]
    assert miner.run_once() is True
    assert gateway.completed == ["change-job", "revision-1"]


def test_offer_window_parent_without_award_journals_and_claims_revision(tmp_path, monkeypatch):
    """A parent leased by a legacy (non-window) claim: the child has no award stamp."""
    gateway = Gateway(requests=[revision(revision_allowance=2)], parent_awarded=False)
    miner = make_miner(tmp_path, gateway, monkeypatch, offer_window=True, ask_usd=1.0)
    journal(miner, {
        "gateway": "https://gateway.invalid", "runner_id": "miner-1", "pending": None,
        "bids": {"bid-parent": {"terms": dict(PARENT_TERMS), "status": "awarded", "legacy": True}},
        "current": {"parent-job": "bid-parent"},
    })
    for _ in range(2):
        miner.run_once()
    assert gateway.completed == ["revision-1"]


def test_offer_window_unjournalled_legacy_parent_agrees_and_delivers(tmp_path, monkeypatch):
    """Review P1-b: no offer journal, legacy parent, ask unrelated to the parent price."""
    gateway = Gateway(requests=[revision(revision_allowance=2)], parent_awarded=False)
    miner = make_miner(tmp_path, gateway, monkeypatch, offer_window=True, ask_usd=0.5)
    for _ in range(2):
        miner.run_once()
    assert gateway.answers == ["revision_agree"]
    assert gateway.completed == ["revision-1"] and gateway.completions == [("revision-1", "verified")]


def test_offer_window_unknown_window_award_is_a_claim_time_recovery(tmp_path, monkeypatch):
    """The parent's frozen window award is not in the journal: consent stands, the claim refuses."""
    gateway = Gateway(requests=[revision(revision_allowance=2)])
    miner = make_miner(tmp_path, gateway, monkeypatch, offer_window=True, ask_usd=1.0)
    with pytest.raises(RecoveryRequired, match="unknown own award bid"):
        for _ in range(2):
            miner.run_once()
    assert gateway.answers == ["revision_agree"]
    assert gateway.completed == []


def test_offer_window_unagreed_child_with_parent_award_is_left_unclaimed(tmp_path, monkeypatch):
    gateway = Gateway()
    gateway.children.append("revision-1")  # not agreed through this miner's journal
    miner = window_miner(tmp_path, gateway, monkeypatch)
    assert [miner.run_once() for _ in range(2)] == [False, False]
    assert gateway.completed == [] and all("job_id" not in c for c in gateway.claims)
    assert ("POST", "/api/runner/v1/offers") not in gateway.calls  # never priced


def test_offer_window_queue_award_under_another_award_id_still_refuses(tmp_path, monkeypatch):
    gateway = Gateway()
    gateway.children.append("revision-1")
    gateway.awards["bid-parent"] = "award-other"
    miner = window_miner(tmp_path, gateway, monkeypatch)
    with pytest.raises(RecoveryRequired, match="invalid own bid status or award identity"):
        miner.run_once()


@pytest.mark.parametrize("served", [
    {"kind": "firm", "price_usd": 2.5},
    {"kind": "limit", "estimate_usd": 1.0, "limit_usd": 2.0},
])
def test_offer_window_agreed_child_keeps_frozen_terms_check(tmp_path, monkeypatch, served):
    gateway = Gateway(requests=[revision(revision_allowance=2)])
    gateway.lease_terms["revision-1"] = served
    miner = window_miner(tmp_path, gateway, monkeypatch)
    with pytest.raises(RecoveryRequired, match="lease award bid identity or frozen terms mismatch"):
        for _ in range(2):
            miner.run_once()
    assert gateway.completed == []


def test_offer_window_limit_parent_child_keeps_estimate_check(tmp_path, monkeypatch):
    parent = {"job_id": "parent-job", "kind": "limit", "estimate_usd": 1.0, "limit_usd": 2.0}
    gateway = Gateway(requests=[revision(revision_allowance=2)], parent_terms=parent)
    gateway.lease_terms["revision-1"] = {"kind": "limit", "estimate_usd": 1.5, "limit_usd": 2.0}
    miner = window_miner(tmp_path, gateway, monkeypatch)
    state = journal(miner)
    state["bids"]["bid-parent"]["terms"] = dict(parent)
    journal(miner, state)
    with pytest.raises(RecoveryRequired, match="lease award bid identity or frozen terms mismatch"):
        for _ in range(2):
            miner.run_once()
    assert gateway.completed == []


def test_offer_window_limit_parent_child_with_frozen_terms_delivers(tmp_path, monkeypatch):
    parent = {"job_id": "parent-job", "kind": "limit", "estimate_usd": 1.0, "limit_usd": 2.0}
    gateway = Gateway(requests=[revision(revision_allowance=2)], parent_terms=parent)
    miner = window_miner(tmp_path, gateway, monkeypatch)
    state = journal(miner)
    state["bids"]["bid-parent"]["terms"] = dict(parent)
    journal(miner, state)
    for _ in range(2):
        miner.run_once()
    assert gateway.completed == ["revision-1"]


def test_offer_window_agreed_child_under_another_bid_refuses(tmp_path, monkeypatch):
    gateway = Gateway(requests=[revision(revision_allowance=2)])
    miner = window_miner(tmp_path, gateway, monkeypatch)
    state = journal(miner)
    state["bids"]["bid-other"] = {"terms": {**PARENT_TERMS, "job_id": "other-job"}, "status": "awarded",
                                  "window_closes_at": FUTURE, "award_id": "award-other"}
    state["current"]["other-job"] = "bid-other"
    journal(miner, state)
    gateway.awards["bid-other"] = "award-other"
    real = gateway.lease

    def wrong_bid(job_id, bid_id, terms, round_kind):
        return real(job_id, "bid-other" if round_kind == "revision" else bid_id, terms, round_kind)

    gateway.lease = wrong_bid
    with pytest.raises(RecoveryRequired):
        for _ in range(2):
            miner.run_once()
    assert gateway.completed == []


# A4 ------------------------------------------------------------------------

@pytest.mark.parametrize("terms", [{"kind": "firm", "price_usd": 2.5},
                                   {"kind": "limit", "estimate_usd": 2.0, "limit_usd": 3.0}])
def test_offer_window_accepted_quote_is_journalled_claimed_and_delivered(tmp_path, monkeypatch, terms):
    gateway = Gateway(requests=[change_order()])
    miner = window_miner(tmp_path, gateway, monkeypatch, change_order_terms=terms)
    assert miner.run_once() is False
    assert len(gateway.quotes) == 1
    state = journal(miner)
    assert state["bids"]["bid-change"] == {"terms": {"job_id": "change-job", **terms},
                                           "status": "open", "window_closes_at": FUTURE}
    assert state["current"]["change-job"] == "bid-change"
    gateway.accept_quote()
    assert miner.run_once() is True
    assert gateway.completed == ["change-job"] and gateway.held == {"change-job"}
    assert gateway.completions == [("change-job", "verified")]
    assert journal(miner)["bids"]["bid-change"]["status"] == "awarded"


def test_quote_without_offer_window_writes_no_journal(tmp_path, monkeypatch):
    gateway = Gateway(requests=[change_order()])
    miner = make_miner(tmp_path, gateway, monkeypatch, change_order_terms={"kind": "firm", "price_usd": 2.5})
    assert miner.run_once() is False
    assert len(gateway.quotes) == 1
    assert list(tmp_path.rglob("offers.json")) == []
    gateway.accept_quote()
    assert miner.run_once() is True
    assert gateway.completed == ["change-job"]


# Review P2: an aborted solve is not a delivery ------------------------------

def test_aborted_revision_solve_is_not_counted_as_delivered(tmp_path, monkeypatch):
    gateway = Gateway(requests=[revision(revision_allowance=2)])
    miner = window_miner(tmp_path, gateway, monkeypatch)

    def abort(received, workdir):
        raise RuntimeError("solver crashed")

    miner.solve_fn = abort
    for _ in range(2):
        miner.run_once()
    assert gateway.held == set() and gateway.completed == []
    assert [job for job, state in gateway.completions] == ["revision-1"]
    assert gateway.completions[0][1] != "verified"


# Review P1-a: ordinary pricing never strands an agreed child ---------------

@pytest.mark.parametrize("window", [False, True])
def test_declining_offer_hook_still_claims_agreed_revision(tmp_path, monkeypatch, window):
    gateway = Gateway(requests=[revision(revision_allowance=2)], ordinary=["job-1"])
    if window:
        miner = window_miner(tmp_path, gateway, monkeypatch, offer_fn=lambda _entry: None)
    else:
        miner = make_miner(tmp_path, gateway, monkeypatch, offer_fn=lambda _entry: None)
    assert miner.run_once() is True
    assert gateway.answers == ["revision_agree"]
    assert gateway.completed == ["revision-1"]
    assert miner.ordinary == []  # the hook declined ordinary work throughout


def test_agreed_child_is_claimed_before_new_ordinary_work(tmp_path, monkeypatch):
    gateway, miner = busy_after_idle(tmp_path, monkeypatch, [revision(revision_allowance=2)],
                                     ["job-1", "job-2", "job-3"])
    assert miner.run_once() is True
    assert gateway.answers == ["revision_agree"] and miner.ordinary == ["job-1"]
    assert miner.run_once() is True
    assert gateway.completed == ["revision-1"] and miner.ordinary == ["job-1"]


# K8: a lost agree reply or failed journal write is not recovered -----------

def test_lost_agree_reply_default_mode_child_is_claimed_by_ordinary_claim(tmp_path, monkeypatch):
    gateway = Gateway(requests=[revision(revision_allowance=2)])
    gateway.lose.add("answer")
    miner = make_miner(tmp_path, gateway, monkeypatch)
    for _ in range(3):
        miner.run_once()
    assert gateway.answers == ["revision_agree"]
    assert gateway.completed == ["revision-1"]
    assert all("job_id" not in c for c in gateway.claims)


def test_lost_agree_reply_without_any_claim_path_fails_narrowly(tmp_path, monkeypatch):
    """No gateway read names the child; a declining hook leaves it, and only it, unclaimed."""
    gateway = Gateway(requests=[revision(revision_allowance=2)], parent_awarded=False)
    gateway.lose.add("answer")
    miner = make_miner(tmp_path, gateway, monkeypatch, offer_fn=lambda _entry: None)
    assert [miner.run_once() for _ in range(3)] == [False, False, False]
    assert gateway.answers == ["revision_agree"] and gateway.completed == []


def fail_first_journal_write_after(monkeypatch, sent):
    """The first journal write after the gateway committed a reply fails."""
    replace = os.replace
    failed = []

    def flaky(source, target, *args, **kwargs):
        if not failed and sent() and ".ormas-recovery" in str(target):
            failed.append(target)
            raise OSError("disk write failed")
        return replace(source, target, *args, **kwargs)

    monkeypatch.setattr(os, "replace", flaky)
    return failed


@pytest.mark.parametrize("fault", ["lost_reply", "journal_write"])
def test_unrecorded_window_child_is_left_unclaimed_and_never_resent(tmp_path, monkeypatch, fault):
    gateway = Gateway(requests=[revision(revision_allowance=2)])
    miner = window_miner(tmp_path, gateway, monkeypatch)
    if fault == "lost_reply":
        gateway.lose.add("answer")
    else:
        assert fail_first_journal_write_after(monkeypatch, lambda: gateway.answers) is not None
    assert [miner.run_once() for _ in range(3)] == [False, False, False]
    assert gateway.answers == ["revision_agree"] and gateway.completed == []
    assert all("job_id" not in c for c in gateway.claims)


def test_lost_agree_reply_window_legacy_parent_is_refused_as_on_main(tmp_path, monkeypatch):
    """Stated limit: a legacy parent's child has no award stamp, so it is priced like
    ordinary work, and its unrecorded revision lease is refused as on origin/main."""
    gateway = Gateway(requests=[revision(revision_allowance=2)], parent_awarded=False)
    gateway.lose.add("answer")
    miner = make_miner(tmp_path, gateway, monkeypatch, offer_window=True, ask_usd=1.0)
    journal(miner, {
        "gateway": "https://gateway.invalid", "runner_id": "miner-1", "pending": None,
        "bids": {"bid-parent": {"terms": dict(PARENT_TERMS), "status": "awarded", "legacy": True}},
        "current": {"parent-job": "bid-parent"},
    })
    assert miner.run_once() is False
    with pytest.raises(RecoveryRequired, match="lease award bid identity or frozen terms mismatch"):
        miner.run_once()
    assert gateway.answers == ["revision_agree"] and gateway.completed == []


# Round 3: second review P1s, after the scope cut ---------------------------

def window_journal_add_parent(miner, gateway, job_id, bid_id):
    gateway.add_parent(job_id, bid_id)
    state = journal(miner)
    state["bids"][bid_id] = {"terms": {**PARENT_TERMS, "job_id": job_id}, "status": "awarded",
                             "window_closes_at": FUTURE, "award_id": gateway.awards[bid_id]}
    state["current"][job_id] = bid_id
    journal(miner, state)


@pytest.mark.parametrize("window", [False, True])
def test_two_parents_with_the_same_request_id_both_deliver(tmp_path, monkeypatch, window):
    """Review 2 P1-1: request ids are scoped to a job (append_graded_round_event)."""
    gateway = Gateway(requests=[revision(revision_allowance=2),
                                revision(job_id="parent-2", revision_allowance=2)])
    # A declining hook: only the claim by job id reaches either child.
    if window:
        miner = window_miner(tmp_path, gateway, monkeypatch, offer_fn=lambda _entry: None)
        window_journal_add_parent(miner, gateway, "parent-2", "bid-parent-2")
    else:
        miner = make_miner(tmp_path, gateway, monkeypatch, offer_fn=lambda _entry: None)
        gateway.add_parent("parent-2", "bid-parent-2")
    for _ in range(4):
        miner.run_once()
    assert gateway.answers == ["revision_agree", "revision_agree"]
    assert gateway.origin == {"revision-1": "parent-job", "revision-2": "parent-2"}
    assert sorted(gateway.completed) == ["revision-1", "revision-2"]
    assert all(state == "verified" for _job, state in gateway.completions)


class Clock(datetime):
    at = None

    @classmethod
    def now(cls, tz=None):
        return cls.at


def test_second_revision_after_first_deadline_keeps_window_lineage(tmp_path, monkeypatch):
    """Review 2 P1-2: an expired record is kept; only it stops being a claim target."""
    start = datetime.now(timezone.utc)
    Clock.at = start
    monkeypatch.setattr(skeleton, "datetime", Clock)
    first_deadline = (start + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    gateway = Gateway(requests=[revision(revision_allowance=2, revision_delivery_deadline=first_deadline)])
    miner = window_miner(tmp_path, gateway, monkeypatch)
    assert miner.run_once() is True
    assert gateway.completed == ["revision-1"]
    Clock.at = start + timedelta(hours=2)
    # graded_round_exchange.py: the next revision is requested on the child.
    gateway.requests.append(revision(job_id="revision-1", request_id="revision-2", revision_index=2,
                                     revision_allowance=2))
    assert miner.run_once() is True
    assert gateway.answers == ["revision_agree", "revision_agree"]
    assert gateway.completed == ["revision-1", "revision-2"]
    targeted = [c for c in gateway.claims if c.get("job_id") == "revision-2"]
    assert targeted == [{"schema_version": "ormas-runner-v1", "runner_id": "miner-1",
                         "offers": [{**PARENT_TERMS, "job_id": "revision-2"}], "job_id": "revision-2"}]
    assert miner.run_once() is False


@pytest.mark.parametrize("digest", [True, False])
def test_wrong_parent_quote_award_is_refused_as_on_main(tmp_path, monkeypatch, digest):
    """Review 2 P1-3(i): equal terms and digest are not an identity."""
    terms = {"kind": "firm", "price_usd": 2.5}
    gateway = Gateway(requests=[change_order(job_id="parent-a")])
    gateway.quote_error = 503  # parent A's quote outcome is unknown; nothing is re-sent
    gateway.draft_digest = digest
    miner = window_miner(tmp_path, gateway, monkeypatch, change_order_terms=terms)
    assert miner.run_once() is False
    # Parent B's C1 child, quoted outside this journal, equal terms and digest.
    gateway.change = {"job_id": "change-b", "bid_id": "bid-b", "terms": dict(terms), "awarded": True}
    with pytest.raises(RecoveryRequired, match="lease has unknown own award bid"):
        miner.run_once()
    assert "bid-b" not in journal(miner)["bids"] and gateway.completed == []


@pytest.mark.parametrize("failure", [404, 409, 500, "transport"])
def test_failed_targeted_claim_leaves_the_ordinary_claim_unchanged(tmp_path, monkeypatch, failure):
    """Review 2 P1-4: a targeted claim refusal or transport error stays inside the pass."""
    gateway = Gateway(requests=[revision(revision_allowance=2)])
    gateway.targeted_error = failure
    miner = make_miner(tmp_path, gateway, monkeypatch)
    assert miner.run_once() is False
    assert gateway.answers == ["revision_agree"]
    gateway.ordinary = ["job-1"]
    gateway.claims.clear()
    assert miner.run_once() is True
    assert miner.ordinary == ["job-1"]
    assert gateway.claims == [
        {"schema_version": "ormas-runner-v1", "runner_id": "miner-1", "job_id": "revision-1"},
        {"schema_version": "ormas-runner-v1", "runner_id": "miner-1"},
    ]
    # Once the gateway serves it, the child is still claimed and delivered.
    gateway.targeted_error = None
    assert miner.run_once() is True
    assert gateway.completed == ["revision-1"]


@pytest.mark.parametrize("failure", [401, 403])
def test_targeted_claim_credential_refusal_stops_the_pass(tmp_path, monkeypatch, failure):
    gateway = Gateway(requests=[revision(revision_allowance=2)])
    miner = make_miner(tmp_path, gateway, monkeypatch, offer_fn=lambda _entry: None)
    gateway.targeted_error = failure
    with pytest.raises(skeleton.OrmasGatewayError):
        miner.run_once()


def test_failed_targeted_claim_in_window_mode_still_claims_the_award(tmp_path, monkeypatch):
    gateway = Gateway(requests=[revision(revision_allowance=2)])
    gateway.targeted_error = "transport"
    miner = window_miner(tmp_path, gateway, monkeypatch)
    assert miner.run_once() is False
    # An awarded C1 child already in this journal is the ordinary work.
    gateway.change = {"job_id": "change-job", "bid_id": "bid-change",
                      "terms": {"kind": "firm", "price_usd": 3.0}, "awarded": True}
    state = journal(miner)
    state["bids"]["bid-change"] = {"terms": {"job_id": "change-job", "kind": "firm", "price_usd": 3.0},
                                   "status": "open", "window_closes_at": FUTURE}
    state["current"]["change-job"] = "bid-change"
    journal(miner, state)
    assert miner.run_once() is True
    assert gateway.completed == ["change-job"]


def _round_records(node):
    if isinstance(node, dict):
        if "child_job_id" in node:
            yield node
        for value in node.values():
            yield from _round_records(value)
    elif isinstance(node, list):
        for value in node:
            yield from _round_records(value)


def _corrupt(raw, form):
    if form == "truncated":
        return raw[: len(raw) // 2]
    state = json.loads(raw)
    [record] = list(_round_records(state))
    if form == "unknown_field":
        record["note"] = "x"
    elif form == "claimed_text":
        record["claimed"] = "false"
    else:
        record["kind"] = "revision_v2"
    return json.dumps(state).encode()


@pytest.mark.parametrize("form", ["truncated", "unknown_field", "claimed_text", "newer_kind"])
def test_bad_round_journal_never_fences_ordinary_work(tmp_path, monkeypatch, caplog, form):
    """Review 2 P1-4: an unreadable journal disables round claiming, never ordinary claiming."""
    gateway = Gateway(requests=[revision(revision_allowance=2)])
    miner = make_miner(tmp_path, gateway, monkeypatch)
    assert miner.run_once() is True
    assert gateway.completed == ["revision-1"]
    [path] = tmp_path.rglob("rounds.json")
    path.write_bytes(_corrupt(path.read_bytes(), form))
    os.chmod(path, 0o600)
    evidence = path.read_bytes()
    gateway.ordinary = ["job-1", "job-2"]
    # A new request while the journal is unusable is answered, and declined.
    gateway.requests.append(revision(job_id="parent-2", revision_allowance=2))
    gateway.claims.clear()
    caplog.set_level("WARNING", logger=skeleton.__name__)
    assert [miner.run_once() for _ in range(3)] == [True, True, False]
    assert miner.ordinary == ["job-1", "job-2"]
    assert gateway.answers == ["revision_agree", "revision_decline"]
    assert all("job_id" not in claim for claim in gateway.claims)
    assert path.read_bytes() == evidence
    assert sum("round journal" in r.getMessage() for r in caplog.records) == 1


@pytest.mark.parametrize("fault", ["lost_reply", "journal_write"])
def test_lost_quote_award_refusal_repeats_and_fences_the_pass(tmp_path, monkeypatch, fault):
    """K8: an award this miner cannot identify is refused as on origin/main, every pass."""
    gateway = Gateway(requests=[change_order()])
    miner = window_miner(tmp_path, gateway, monkeypatch, change_order_terms={"kind": "firm", "price_usd": 2.5})
    if fault == "lost_reply":
        gateway.lose.add("quote")
    else:
        assert fail_first_journal_write_after(monkeypatch, lambda: gateway.quotes) is not None
    assert miner.run_once() is False
    assert len(gateway.quotes) == 1
    gateway.accept_quote()
    # runner_api.py:_claim_lease_impl re-serves the runner's live lease to an
    # empty-offer claim, so every later pass meets the same award first.
    gateway.leased.discard("change-job")
    real_lease = gateway.lease

    def reserve(job_id, *args):
        response = real_lease(job_id, *args)
        gateway.leased.discard(job_id)
        return response

    gateway.lease = reserve
    for _ in range(3):
        with pytest.raises(RecoveryRequired, match="lease has unknown own award bid"):
            miner.run_once()
    assert len(gateway.quotes) == 1 and gateway.completed == []
    assert gateway.claims[-3:] == [{"schema_version": "ormas-runner-v1", "runner_id": "miner-1", "offers": []}] * 3


def mismatched_award_miner(tmp_path, monkeypatch, responses):
    miner = make_miner(tmp_path, Gateway(), monkeypatch, offer_window=True, ask_usd=1.0)
    terms = {job: {"job_id": job, "kind": "firm", "price_usd": float(index)}
             for index, job in enumerate(("job-a", "job-b", "job-c"), 1)}
    journal(miner, {
        "gateway": "https://gateway.invalid", "runner_id": "miner-1", "pending": None,
        "bids": {f"bid-{job[-1]}": {"terms": terms[job], "status": "awarded",
                                    "window_closes_at": FUTURE, "award_id": f"award-{job[-1]}"}
                 for job in terms},
        "current": {job: f"bid-{job[-1]}" for job in terms},
    })
    entries = [
        {"job_id": "job-a", "awarded_to_you": True, "bid_id": "bid-a", "award_id": "award-a"},
        {"job_id": "job-b", "awarded_to_you": True, "bid_id": "bid-a", "award_id": "award-a"},
        {"job_id": "job-c", "awarded_to_you": True, "bid_id": "bid-c", "award_id": "award-c"},
    ]
    leased = set()
    monkeypatch.setattr(miner.client, "list_queue", lambda _runner_id: {
        "jobs": [entry for entry in entries if entry["job_id"] not in leased],
    })
    monkeypatch.setattr(miner.client, "get_offer", lambda bid_id: {
        "bid_id": bid_id, "status": "awarded", "award_id": "award-" + bid_id[-1],
    })
    claims = []

    def claim_task(_runner_id, **body):
        assert body == {"offers": []}
        job_id = responses[min(len(claims), len(responses) - 1)]
        claims.append(job_id)
        leased.add(job_id)
        return (lease_for(job_id, bid_id="bid-" + job_id[-1], terms=terms[job_id]),
                draft_for(job_id))

    monkeypatch.setattr(miner.client, "claim_task", claim_task)
    return miner, claims


def test_mismatched_queue_award_refuses_lease_without_fencing_other_jobs(tmp_path, monkeypatch):
    """Refused leases survive replay and bad writes; malformed identities stay off disk."""
    miner, claims = mismatched_award_miner(
        tmp_path, monkeypatch, ["job-b", "job-b", "job-c", "job-b"])
    miner.run_forever(max_iterations=3, idle_sleep=lambda _: None)
    assert miner.ordinary == ["job-c"]
    assert claims == ["job-b", "job-b", "job-c"]

    # B is now absent from the queue. A restart must still refuse its live lease.
    restarted = make_miner(tmp_path, Gateway(), monkeypatch, offer_window=True, ask_usd=1.0)
    for name in ("list_queue", "get_offer", "claim_task"):
        monkeypatch.setattr(restarted.client, name, getattr(miner.client, name))
    restarted.run_forever(max_iterations=1, idle_sleep=lambda _: None)
    assert restarted.ordinary == []
    assert claims == ["job-b", "job-b", "job-c", "job-b"]

    # A failed durable write must leave a process-local refusal, then retry it.
    write_miner, write_claims = mismatched_award_miner(
        tmp_path / "write-failure", monkeypatch, ["job-b", "job-b", "job-c"])
    real_replace, failed = os.replace, []

    def fail_once(source, target, *args, **kwargs):
        if (Path(target).name == "offers.json" and not failed
                and json.loads(Path(source).read_text()).get("refused_leases")):
            failed.append(str(target))
            raise OSError("transient refusal journal replace failure")
        return real_replace(source, target, *args, **kwargs)

    monkeypatch.setattr(os, "replace", fail_once)
    write_miner.run_forever(max_iterations=3, idle_sleep=lambda _: None)
    assert failed and write_miner.ordinary == ["job-c"]
    assert write_claims == ["job-b", "job-b", "job-c"]
    assert len(journal(write_miner)["refused_leases"]) == 1
    monkeypatch.setattr(os, "replace", real_replace)

    for bad_lease_id in ("", None, 123):
        case = tmp_path / f"malformed-{bad_lease_id}"
        malformed, malformed_claims = mismatched_award_miner(
            case, monkeypatch, ["job-b", "job-c"])
        real_claim = malformed.client.claim_task

        def malformed_claim(*args, _claim=real_claim, _bad=bad_lease_id, **kwargs):
            lease, draft = _claim(*args, **kwargs)
            if lease.task_id == "job-b":
                lease = replace(lease, lease_id=_bad)
            return lease, draft

        monkeypatch.setattr(malformed.client, "claim_task", malformed_claim)
        with pytest.raises(RecoveryRequired, match="queue award bid identity mismatch"):
            malformed.run_once()
        [path] = case.rglob("offers.json")
        assert not json.loads(path.read_text()).get("refused_leases")
        assert malformed.run_once() is True
        assert malformed.ordinary == ["job-c"]
        assert malformed_claims == ["job-b", "job-c"]
