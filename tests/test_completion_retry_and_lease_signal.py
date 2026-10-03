"""Card 63982bd8 (miner side): the day Jake did the work and was not paid.

Evidence, job_c978f3fe82be (2026-09-16): claimed 12:58:33Z, result branch pushed
and recorded, completion POST at 12:59:23Z failed on his side, the wrapper
discarded the HTTP status and body into a bare ``completion_request_failed``, the
crash-report completion failed seconds later too, **0 lease renewals**, lease
expired 13:03:33Z. Reading ``skeleton.py`` shows why the renewal count was zero
and why it would have been zero even with a working network:

  * ``_heartbeat_loop`` catches every exception and ``return``s — the thread dies
    silently on the FIRST failure, with nothing logged.
  * ``stop.set()`` runs in the ``finally`` of ``solve_fn``, so the heartbeat is
    already shut down before publish, verify and complete — exactly the phase
    Jake's job died in. Nothing renews the lease there at all.
  * ``complete_task`` is called once. A transient refusal loses the job.

What this file pins:
  1. A transient completion refusal is RETRIED with backoff while the lease is live.
  2. The lease keeps being RENEWED across those retries.
  3. A terminal 4xx is NOT retried (the gateway has decided).
  4. A heartbeat failure is logged with its status/body and the loop CONTINUES.
  5. A 409 ``lease_lost`` cancels the run: the solve is signalled, no completion
     is attempted on a dead lease, and the run summary says so.
  6. ``complete_task`` errors carry status + body (already true — kept green).

Bounds come from the GATEWAY, never invented here: retries continue while the
lease is live (``lease_ttl_s`` from registration) and back off from the
registered heartbeat cadence. There is no magic attempt count.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path


from ormas_subnet import MinerConfig, MinerSkeleton, OrmasMinerClient
from ormas_subnet.localnet import LocalGateway, LocalResponse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from neurons.localnet_demo import (  # noqa: E402
    ALLOWED_PATHS,
    BRIEF,
    TASK_ID,
    VERIFY_COMMAND,
    _make_client_bare_repo,
    _solve_pass,
)

FAST_CADENCE = {"poll_interval_s": 1, "heartbeat_s": 1, "lease_ttl_s": 30}


class _FastGateway(LocalGateway):
    """Advertises a 1 s heartbeat so a test can watch renewals without waiting."""

    def _handle_registration(self, body):
        resp = super()._handle_registration(body)
        payload = resp.json()
        payload.update(FAST_CADENCE)
        return LocalResponse(200, payload)


class _FlakyCompleteGateway(_FastGateway):
    """Refuses the first ``fail_n`` completions with ``status``, then accepts."""

    fail_n = 2
    status = 503

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.complete_attempts = 0
        self.heartbeats_at_attempt = []

    def _handle_complete(self, body):
        self.complete_attempts += 1
        self.heartbeats_at_attempt.append(len(self.heartbeats))
        if self.complete_attempts <= self.fail_n:
            return LocalResponse(
                self.status,
                {"error": {"type": "upstream_unavailable", "message": "try again"}},
            )
        return super()._handle_complete(body)


class _TerminalRefusalGateway(_FlakyCompleteGateway):
    fail_n = 99
    status = 400


class _LeaseLostGateway(_FastGateway):
    """Every renewing heartbeat answers 409 lease_lost."""

    def _handle_heartbeat(self, body):
        self.heartbeats.append(body)
        if body.get("renew"):
            return LocalResponse(
                409, {"error": {"type": "lease_lost", "message": "lease reassigned"}}
            )
        return super()._handle_heartbeat(body)


class _HeartbeatBlipGateway(_FastGateway):
    """The first renewing heartbeat fails transiently; later ones succeed."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.renew_calls = 0

    def _handle_heartbeat(self, body):
        if body.get("renew"):
            self.renew_calls += 1
            if self.renew_calls == 1:
                self.heartbeats.append(body)
                return LocalResponse(
                    503, {"error": {"type": "unavailable", "message": "blip"}}
                )
        return super()._handle_heartbeat(body)


def _wire(tmp_path, solve_fn, gateway_cls=_FastGateway):
    bare_repo, base_commit = _make_client_bare_repo(tmp_path)
    gateway = gateway_cls(
        task_id=TASK_ID, base_commit=base_commit, verify_command=VERIFY_COMMAND,
        repo_url=str(bare_repo), brief=BRIEF, allowed_paths=ALLOWED_PATHS,
    )
    client = OrmasMinerClient(base_url="local://test", token="no-token", http_client=gateway)
    config = MinerConfig(
        runner_id="retry-miner", runner_version="0.0.1", platform="local", capacity=1,
        cells=("code-edit-small",), workdir_root=tmp_path / "work", repo_id="repo1",
        repo_url=str(bare_repo), push_remote=None,
    )
    skeleton = MinerSkeleton(client, config, solve_fn)
    skeleton.register()
    skeleton.bind(project_id="p", base_commit=base_commit)
    return gateway, skeleton


# --------------------------------------------------------------------------- #
# 1-3. completion retry
# --------------------------------------------------------------------------- #

def test_a_transient_completion_refusal_is_retried(tmp_path):
    gateway, skeleton = _wire(tmp_path, _solve_pass, _FlakyCompleteGateway)
    assert skeleton.run_once() is True
    assert gateway.complete_attempts == 3, "two refusals then the accepted completion"
    assert gateway.completed is not None
    summary = skeleton.last_run
    assert summary["completion_attempts"] == 3
    assert summary["lease_lost"] is False


def test_the_lease_is_renewed_across_completion_retries(tmp_path):
    """Jake's zero renewals. The heartbeat must still be alive at complete time."""
    gateway, skeleton = _wire(tmp_path, _solve_pass, _FlakyCompleteGateway)
    skeleton.run_once()
    counts = gateway.heartbeats_at_attempt
    assert len(counts) >= 2
    assert counts[-1] > counts[0], (
        "no heartbeat reached the gateway between the first refused completion and "
        f"the retry (counts={counts}) — the lease was unrenewed exactly where "
        "job_c978f3fe82be died"
    )
    assert skeleton.last_run["renewals"] > 0


def test_a_terminal_refusal_is_not_retried(tmp_path):
    gateway, skeleton = _wire(tmp_path, _solve_pass, _TerminalRefusalGateway)
    skeleton.run_once()
    # One completion attempt, and one more only for the crash report the skeleton
    # already sends on a contained failure. Never a retry ladder: the gateway has
    # decided, so retrying a 400 is noise that burns the remaining lease.
    assert gateway.complete_attempts <= 2, (
        f"a terminal 400 was retried ({gateway.complete_attempts} attempts)"
    )
    assert skeleton.last_run["completion_attempts"] == 1


def test_a_completion_error_carries_status_and_body(tmp_path):
    """The client library already surfaces both; pinned so the skeleton's own
    summary cannot collapse them back into a bare completion_request_failed."""
    gateway, skeleton = _wire(tmp_path, _solve_pass, _TerminalRefusalGateway)
    skeleton.run_once()
    error = skeleton.last_run["completion_error"]
    assert error["status_code"] == 400
    assert error["error_type"] == "upstream_unavailable"
    assert "try again" in error["message"]


# --------------------------------------------------------------------------- #
# 4. a heartbeat failure is logged and survivable
# --------------------------------------------------------------------------- #

def test_a_transient_heartbeat_failure_is_logged_and_the_loop_continues(tmp_path, caplog):
    slow = _slow_solve(1.5)
    gateway, skeleton = _wire(tmp_path, slow, _HeartbeatBlipGateway)
    with caplog.at_level("WARNING"):
        skeleton.run_once()
    assert gateway.renew_calls >= 2, (
        "the heartbeat thread returned on its first failure — the current "
        "`except Exception: return` is why Jake's renewal count was 0"
    )
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("503" in m for m in warnings), f"status not logged: {warnings}"
    assert any("blip" in m for m in warnings), f"body not logged: {warnings}"


# --------------------------------------------------------------------------- #
# 5. lease_lost cancels the run
# --------------------------------------------------------------------------- #

def _slow_solve(seconds: float):
    """A solve that just takes time, so the heartbeat thread gets to run."""

    def solve(draft, workdir):
        time.sleep(seconds)
        return _solve_pass(draft, workdir)

    return solve


def test_lease_lost_signals_the_solve_and_skips_the_completion(tmp_path):
    cancelled_seen = threading.Event()

    def solve(draft, workdir):
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if skeleton.cancelled.is_set():
                cancelled_seen.set()
                break
            time.sleep(0.05)
        return _solve_pass(draft, workdir)

    gateway, skeleton = _wire(tmp_path, solve, _LeaseLostGateway)
    skeleton.run_once()
    assert cancelled_seen.is_set(), "a lost lease must reach the running solve"
    assert gateway.completed is None, "never complete a lease we no longer hold"
    summary = skeleton.last_run
    assert summary["lease_lost"] is True
    assert summary["renewals"] == 0
