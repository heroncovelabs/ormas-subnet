"""``LocalGateway`` — a local, offline stand-in for ``/api/runner/v1``.

This is a local stand-in for the trusted-path gateway — it does NOT settle
through validators and prices nothing; a real gateway does. It exists so a
miner operator can run the whole register → bind → claim → solve → verify →
publish → complete loop with no network host, no token, and no gateway at all
(see ``neurons/localnet_demo.py`` and ``docs/QUICKSTART_LOCAL.md``).

Lifted from ``tests/test_skeleton.py``'s ``FakeGateway`` (a protocol-shape
double already used to drive :class:`ormas_subnet.skeleton.MinerSkeleton`
end to end in that test) so the same behaviour is importable and supported,
not copy-pasted. It implements exactly the transport surface
:class:`ormas_subnet.client.OrmasMinerClient` expects (``.post`` / ``.get``
returning an object with ``.status_code`` / ``.raise_for_status()`` /
``.json()``) — no auth, no persistence, one seeded job per instance.

Settlement here mirrors what the docstring in ``README.md`` calls "today's
trusted path": the production gateway derives settlement from the miner's own
reported ``verification_state`` plus a scope/commit check, because today only
our own trusted miner runs against it. ``LocalGateway`` implements exactly
the wire vocabulary the real gateway uses for that derivation
(``docs/protocol.md`` § "Settlement derivation") — not a shortcut around
validator quorum, the same trusted-path rule the real gateway runs today:

- ``verified`` + ``scope_ok`` + a valid (40-hex) ``result_commit`` → settlement
  ``"paid"``.
- ``verified`` but ``scope_ok=False`` → ``"no_delivery"`` with
  ``failure_class="scope_violation"``.
- ``verified`` but an invalid/missing ``result_commit`` → ``"no_delivery"``
  with ``failure_class="publish_failed"``.
- any other ``verification_state`` → ``"no_delivery"`` with a failure class
  derived from that state (or the miner's own ``capture.failure_class``, if
  it named one).
"""
from __future__ import annotations

from typing import Any, Sequence

from .protocol import RUNNER_PROTOCOL_V1
from .skeleton import DEFAULT_HEARTBEAT_S, DEFAULT_LEASE_TTL_S, DEFAULT_POLL_INTERVAL_S

__all__ = ["LocalGateway", "LocalResponse"]


class LocalResponse:
    """Minimal response object: ``.status_code`` / ``.raise_for_status()`` / ``.json()``."""

    def __init__(self, status_code: int, body: Any) -> None:
        self.status_code = status_code
        self._body = body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}: {self._body}")

    def json(self) -> Any:
        return self._body


class LocalGateway:
    """Enough of ``/api/runner/v1`` to drive :class:`~ormas_subnet.skeleton.MinerSkeleton`
    end to end, entirely in-process, with a single seeded job.

    Args:
        task_id: The one job id this gateway will lease.
        base_commit: The commit the job is based on (must already exist on
            ``repo_url``, or on whatever the miner's ``MinerConfig.repo_url``
            resolves to — this gateway does not create the repository itself).
        verify_command: The command the miner (and, in a real deployment, the
            validator) runs to decide whether a delivery is correct.
        repo_url: Informational clone source recorded on the served
            ``TaskDraft`` — typically a local bare git repository path. The
            skeleton itself always clones ``MinerConfig.repo_url``, not this
            field; it rides along here only because a real draft carries it.
        brief: Human-readable task description.
        allowed_paths: Paths the delivery's diff must stay within. Empty means
            unrestricted.
        budget_usd / outcome_price_usd: Advisory pricing fields on the lease
            and completion receipt. This gateway prices nothing for real —
            these are fixed numbers for demo purposes only.
        repo_id: The repo id this job is bound under.
    """

    def __init__(
        self,
        *,
        task_id: str,
        base_commit: str,
        verify_command: str,
        repo_url: str = "",
        brief: str = "local demo task",
        allowed_paths: Sequence[str] | None = None,
        budget_usd: float = 1.0,
        outcome_price_usd: float = 0.05,
        repo_id: str = "repo1",
    ) -> None:
        self.task_id = task_id
        self.base_commit = base_commit
        self.verify_command = verify_command
        self.repo_url = repo_url
        self.brief = brief
        self.allowed_paths = list(allowed_paths) if allowed_paths is not None else ["out.txt"]
        self.budget_usd = budget_usd
        self.outcome_price_usd = outcome_price_usd
        self.repo_id = repo_id

        self.registered: dict[str, Any] | None = None
        self.bound: dict[str, Any] | None = None
        self.claims: list[dict[str, Any]] = []
        self.claimed = False
        self.heartbeats: list[dict[str, Any]] = []
        self.completed: dict[str, Any] | None = None
        self._receipts: list[dict[str, Any]] = []
        self._lease_token = "local-lease-1"

    # ------------------------------------------------------------------
    # Recorded receipts — for the demo to print, and for tests to assert on.
    # ------------------------------------------------------------------

    @property
    def receipts(self) -> tuple[dict[str, Any], ...]:
        """Every completion this gateway has recorded, most recent last."""
        return tuple(self._receipts)

    # ------------------------------------------------------------------
    # Transport surface expected by OrmasMinerClient(http_client=...).
    # ------------------------------------------------------------------

    def post(self, path: str, json: dict[str, Any] | None = None, headers: Any = None) -> LocalResponse:
        body = json or {}
        assert body.get("schema_version") == RUNNER_PROTOCOL_V1
        if path == "/api/runner/v1/registrations":
            return self._handle_registration(body)
        if path == "/api/runner/v1/repositories":
            return self._handle_bind(body)
        if path == "/api/runner/v1/leases":
            return self._handle_claim(body)
        if path.endswith("/heartbeat"):
            return self._handle_heartbeat(body)
        if path.endswith("/complete"):
            return self._handle_complete(body)
        raise AssertionError(f"unhandled path: {path}")

    def get(self, path: str) -> LocalResponse:  # pragma: no cover - unused by the skeleton
        raise AssertionError(f"unexpected GET: {path}")

    # ------------------------------------------------------------------
    # Route handlers
    # ------------------------------------------------------------------

    def _handle_registration(self, body: dict[str, Any]) -> LocalResponse:
        self.registered = body
        return LocalResponse(
            200,
            {
                "runner_id": body["runner_id"],
                "poll_interval_s": DEFAULT_POLL_INTERVAL_S,
                "lease_ttl_s": DEFAULT_LEASE_TTL_S,
                "heartbeat_s": DEFAULT_HEARTBEAT_S,
                "protocol": RUNNER_PROTOCOL_V1,
            },
        )

    def _handle_bind(self, body: dict[str, Any]) -> LocalResponse:
        self.bound = body
        return LocalResponse(200, {"repo_id": body["repo_id"], "project_id": body["project_id"]})

    def _handle_claim(self, body: dict[str, Any]) -> LocalResponse:
        self.claims.append(body)
        if self.claimed:
            return LocalResponse(204, None)
        self.claimed = True
        lease = {
            "schema_version": RUNNER_PROTOCOL_V1,
            "lease_id": self._lease_token,
            "task_id": self.task_id,
            "expires_at": "2026-01-01T00:05:00Z",
            "selected_cell": "code-edit-small",
            "provider_pin": "unset",
            "fallback_policy": "unset",
            "hold_ref": "unset",
            "now": "2026-01-01T00:00:00Z",
            "outcome_price_usd": self.outcome_price_usd,
        }
        draft = {
            "schema_version": RUNNER_PROTOCOL_V1,
            "task_id": self.task_id,
            "runner_id": body["runner_id"],
            "repo_id": self.repo_id,
            "base_commit": self.base_commit,
            "brief": self.brief,
            "verify_command": self.verify_command,
            "allowed_paths": self.allowed_paths,
            "budget_usd": self.budget_usd,
            "work_packet": {"task": self.brief},
            "work_packet_sha256": "b" * 64,
            "attempt": 0,
            "parent_job_id": "",
            "repair_findings": [],
            "repo_url": self.repo_url,
        }
        return LocalResponse(200, {"lease": lease, "draft": draft})

    def _handle_heartbeat(self, body: dict[str, Any]) -> LocalResponse:
        self.heartbeats.append(body)
        return LocalResponse(200, {"expires_at": "2026-01-01T00:05:00Z", "now": "2026-01-01T00:00:30Z"})

    def _derive_settlement(
        self, verification_state: str, scope_ok: Any, result_commit: str | None, capture: dict[str, Any],
    ) -> tuple[str, str | None]:
        """Settlement derivation from ``docs/protocol.md`` § "Settlement derivation".

        Returns ``(settlement, failure_class)``. This is the same rule the
        real gateway applies server-side — the miner never constructs its own
        settlement, it only reports ``verification_state``/``scope_ok``/
        ``result_commit`` honestly and the gateway (here, this double) decides.
        """
        valid_commit = isinstance(result_commit, str) and len(result_commit) == 40
        if verification_state == "verified":
            if not scope_ok:
                return "no_delivery", "scope_violation"
            if not valid_commit:
                return "no_delivery", "publish_failed"
            return "paid", None
        # Any other verification_state: no_delivery, with a failure class
        # derived from the state — or the miner's own capture.failure_class,
        # if it named a recognized one.
        return "no_delivery", capture.get("failure_class") or verification_state

    def _handle_complete(self, body: dict[str, Any]) -> LocalResponse:
        assert body["receipt"]["schema_version"] == RUNNER_PROTOCOL_V1
        terminal = body["terminal"]
        assert terminal["verification_state"] in {"verified", "failed"}
        self.completed = body

        capture = body.get("capture") or {}
        scope_ok = capture.get("scope_ok")
        result_commit = terminal.get("result_commit")
        settlement, failure_class = self._derive_settlement(
            terminal["verification_state"], scope_ok, result_commit, capture,
        )
        paid = settlement == "paid"
        receipt = {
            "receipt_id": f"local_rcpt_{len(self._receipts) + 1}",
            "settlement": settlement,
            "failure_class": failure_class,
            "customer_billed_usd": self.outcome_price_usd if paid else 0.0,
            "debit_status": "delivered" if paid else "none",
            "upstream_cost_usd": body["receipt"].get("upstream_cost_usd"),
        }
        self._receipts.append(
            {
                "task_id": self.task_id,
                "verification_state": terminal["verification_state"],
                "scope_ok": scope_ok,
                "result_commit": result_commit,
                "result_ref": terminal.get("result_ref"),
                "changed_paths": [entry.get("path") for entry in capture.get("changed_paths", [])],
                **receipt,
            }
        )
        return LocalResponse(
            200,
            {
                "status": "done" if paid else "failed",
                "receipt": receipt,
            },
        )
