"""Thin HTTP client for the Ormas gateway ``/api/runner/v1`` miner control plane.

Extracted from the private ``tensorbox_spec.mcp_server.ormas_http_client``'s
``OrmAsGatewayClient`` (the runner-facing subset only — the customer task API,
Outcomes project/job admin, and review endpoints stay private; a miner never
calls them). See ``public_subnet/docs/protocol.md`` for the route contract.

The client never reads a hardcoded token path. Pass either ``token`` directly, or
``token_env`` (an environment variable name) / ``token_path`` (a file path) and
call :func:`load_token` yourself — the caller owns where the token lives.
"""
from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any, Mapping

from .protocol import (
    RUNNER_DEVICE_HEADER,
    RUNNER_PROTOCOL_V1,
    RepoRegistration,
    RunnerRegistration,
    TaskDraft,
    TaskEvent,
    TaskLease,
    TaskReceipt,
    TaskTerminal,
    require_task_id,
    snapshot_evidence,
)

__all__ = ["OrmasMinerClient", "OrmasGatewayError", "load_token"]


class OrmasGatewayError(RuntimeError):
    """An HTTP error from the gateway, carrying its structured error body.

    The gateway answers failures with ``{"error": {"type": ..., "message": ...}}``
    (``docs/protocol.md``); a bare ``resp.raise_for_status()`` drops that body and
    leaves the operator with only a status code. This error instead carries the
    parsed ``status_code`` / ``error_type`` / ``message``, and its string names
    them (falling back to the raw response text when the body is not the
    structured shape).
    """

    def __init__(
        self,
        *,
        status_code: int | None,
        error_type: str | None,
        message: str,
    ) -> None:
        self.status_code = status_code
        self.error_type = error_type
        self.message = message
        label = f"HTTP {status_code}" if status_code is not None else "HTTP error"
        type_part = f" [{error_type}]" if error_type else ""
        super().__init__(f"gateway {label}{type_part}: {message}")


def load_token(*, token_env: str | None = None, token_path: str | os.PathLike[str] | None = None) -> str:
    """Resolve a runner token from an env var name or a file path.

    Exactly one of ``token_env`` / ``token_path`` must be given. Never hardcodes
    a default location — the caller decides where its token lives.
    """
    if bool(token_env) == bool(token_path):
        raise ValueError("pass exactly one of token_env or token_path")
    if token_env:
        token = os.environ.get(token_env)
        if not token:
            raise ValueError(f"environment variable {token_env!r} is not set")
        return token
    text = Path(token_path).read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"token file {token_path!r} is empty")
    return text


class OrmasMinerClient:
    """Miner-facing subset of the Ormas gateway HTTP API (``/api/runner/v1``).

    Args:
        base_url: Gateway base URL, e.g. ``https://api.ormas.ai``.
        token: Runner bearer token (``ormr_...``). Sent as ``Authorization: Bearer``.
        http_client: Optional injectable transport (any object with ``.post``/``.get``
            returning a response with ``.raise_for_status()`` + ``.json()`` +
            ``.status_code``). When ``None``, an ``httpx.Client`` is created.
        device_nonce: Optional per-device binding, sent as ``X-Ormas-Runner-Device``
            once the gateway has assigned one at registration.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        http_client: Any | None = None,
        device_nonce: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.device_nonce = device_nonce
        if http_client is not None:
            self._client = http_client
            self._owns_client = False
        else:
            import httpx  # optional dep — only needed on the real HTTP path

            self._client = httpx.Client(
                base_url=self.base_url,
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=60.0,
            )
            self._owns_client = True

    def _headers(self) -> dict[str, str] | None:
        if not self.device_nonce:
            return None
        return {RUNNER_DEVICE_HEADER: str(self.device_nonce)}

    def _post(self, path: str, body: dict[str, Any]) -> Any:
        headers = self._headers()
        if headers is None:
            return self._client.post(path, json=body)
        return self._client.post(path, json=body, headers=headers)

    @staticmethod
    def _raise_for_status(resp: Any) -> None:
        """``resp.raise_for_status()`` that surfaces the gateway's error body.

        On an HTTP error the raised :class:`OrmasGatewayError` carries the
        structured ``error.type`` / ``error.message`` from ``resp.json()`` when
        present (falling back to ``resp.text``), chained from the original
        transport error.
        """
        try:
            resp.raise_for_status()
        except Exception as exc:
            error_type: str | None = None
            detail: str | None = None
            try:
                payload = resp.json()
            except Exception:  # noqa: BLE001 - the body may not be JSON at all
                payload = None
            if isinstance(payload, Mapping):
                error = payload.get("error")
                if isinstance(error, Mapping):
                    raw_type = error.get("type")
                    raw_message = error.get("message")
                    error_type = None if raw_type is None else str(raw_type)
                    detail = None if raw_message is None else str(raw_message)
            if detail is None:
                text = getattr(resp, "text", None)
                detail = str(text) if text else str(exc)
            raise OrmasGatewayError(
                status_code=getattr(resp, "status_code", None),
                error_type=error_type,
                message=detail,
            ) from exc

    # ------------------------------------------------------------------
    # /api/runner/v1 routes
    # ------------------------------------------------------------------

    def register_runner(self, registration: RunnerRegistration) -> dict[str, Any]:
        """POST /api/runner/v1/registrations.

        Response carries the gateway-authoritative ``poll_interval_s``,
        ``lease_ttl_s``, ``heartbeat_s`` — honor those over any local default.
        """
        resp = self._post("/api/runner/v1/registrations", registration.to_wire())
        self._raise_for_status(resp)
        return resp.json()

    def register_repository(
        self,
        runner_id: str,
        repository: RepoRegistration,
        *,
        project_id: str,
    ) -> dict[str, Any]:
        """POST /api/runner/v1/repositories — bind a repo to a project + runner."""
        body: dict[str, Any] = {
            **repository.to_wire(),
            "runner_id": runner_id,
            "project_id": project_id,
        }
        resp = self._post("/api/runner/v1/repositories", body)
        self._raise_for_status(resp)
        return resp.json()

    def claim_task(
        self, runner_id: str, *, ask_usd: float | None = None,
    ) -> tuple[TaskLease, TaskDraft] | None:
        """POST /api/runner/v1/leases — typed lease+draft, or None when idle (HTTP 204).

        ``ask_usd`` is the miner's optional firm bid for any job leased on this
        call (mirrors the private ``OrmAsGatewayClient.claim_task``); omitted keeps
        the server's own derived-ask pricing and a byte-identical wire body.
        An ask at or under the client's reserve is leased and becomes the price;
        an ask above it is recorded and the job is skipped (see
        ``docs/protocol.md``). A bad value (non-numeric, a bool, non-finite, or
        negative) raises ``ValueError`` here before any request is sent — the
        server rejects the same values (``runner_api.claim_lease``).
        """
        if ask_usd is not None and (
            not isinstance(ask_usd, (int, float))
            or isinstance(ask_usd, bool)
            or not math.isfinite(float(ask_usd))
            or ask_usd < 0
        ):
            raise ValueError("ask_usd must be a finite, non-negative number")
        body: dict[str, Any] = {"schema_version": RUNNER_PROTOCOL_V1, "runner_id": runner_id}
        if ask_usd is not None:
            body["ask_usd"] = float(ask_usd)
        resp = self._post("/api/runner/v1/leases", body)
        self._raise_for_status(resp)
        if getattr(resp, "status_code", None) == 204:
            return None
        payload = resp.json()
        if not isinstance(payload, Mapping):
            raise ValueError("invalid claim payload")
        lease_raw = payload.get("lease")
        draft_raw = payload.get("draft")
        if not isinstance(lease_raw, Mapping) or not isinstance(draft_raw, Mapping):
            raise ValueError("invalid claim payload")
        return TaskLease.from_wire(lease_raw), TaskDraft.from_wire(draft_raw)

    def heartbeat_task(
        self,
        task_id: str,
        runner_id: str,
        lease_token: str,
        *,
        renew: bool = True,
        event: TaskEvent | None = None,
    ) -> dict[str, Any]:
        """POST /api/runner/v1/leases/{task_id}/heartbeat — extend the lease or emit progress."""
        task_id = require_task_id(task_id)
        body: dict[str, Any] = {
            "schema_version": RUNNER_PROTOCOL_V1,
            "runner_id": runner_id,
            "lease_token": lease_token,
            "renew": bool(renew),
            "event": None if event is None else event.to_wire(),
        }
        resp = self._post(f"/api/runner/v1/leases/{task_id}/heartbeat", body)
        self._raise_for_status(resp)
        return resp.json()

    def complete_task(
        self,
        task_id: str,
        runner_id: str,
        lease_token: str,
        *,
        receipt: TaskReceipt,
        terminal: TaskTerminal,
        capture: Mapping[str, Any] | None = None,
        failure_evidence: Mapping[str, Any] | None = None,
        pr_url: str | None = None,
        pr_error: str | None = None,
    ) -> dict[str, Any]:
        """POST /api/runner/v1/leases/{task_id}/complete — 200 done, 202 settling, 410 replay."""
        task_id = require_task_id(task_id)
        body: dict[str, Any] = {
            "schema_version": RUNNER_PROTOCOL_V1,
            "runner_id": runner_id,
            "lease_token": lease_token,
            "receipt": receipt.to_wire(),
            "terminal": terminal.to_wire(),
        }
        if capture is not None:
            body["capture"] = snapshot_evidence(capture)
        if failure_evidence is not None:
            body["failure_evidence"] = snapshot_evidence(failure_evidence)
        if pr_url is not None:
            body["pr_url"] = pr_url
        if pr_error is not None:
            body["pr_error"] = pr_error
        resp = self._post(f"/api/runner/v1/leases/{task_id}/complete", body)
        status = getattr(resp, "status_code", None)
        if status in (202, 410):
            return resp.json()
        self._raise_for_status(resp)
        return resp.json()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "OrmasMinerClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
