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
from typing import Any, Callable, Mapping

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

# Mirrors the gateway's ``runner_api.CLAIM_PAGE_LIMIT``: the most offers one claim may carry.
CLAIM_PAGE_LIMIT = 20


def _positive_usd(value: Any) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value)) and value > 0
    except OverflowError:
        return False


EFFORT_FIELDS: tuple[str, ...] = (
    "attempts", "model_turns", "models_used", "output_tokens", "total_tokens",
)


def _validate_effort(effort: Any) -> dict[str, int]:
    """Return the wire effort block or raise ``ValueError``.

    Mirrors ``runner_api._parse_effort``: exactly the five count fields, each a
    non-negative ``int`` (bools rejected). Counts only — no model identity.
    """
    if not isinstance(effort, Mapping):
        raise ValueError("effort must be a mapping of the five count fields")
    if set(effort) != set(EFFORT_FIELDS):
        raise ValueError(f"effort has exactly the fields {', '.join(EFFORT_FIELDS)}")
    for field in EFFORT_FIELDS:
        value = effort[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"effort.{field} must be a non-negative integer")
    return {field: int(effort[field]) for field in EFFORT_FIELDS}


def _validate_offers(offers: Any) -> None:
    """Raise ``ValueError`` unless ``offers`` is a wire body the gateway accepts.

    Mirrors ``runner_api._parse_offers``: a firm offer is exactly
    ``{job_id, kind, price_usd}``; a limit offer is exactly
    ``{job_id, kind, estimate_usd, limit_usd}`` with ``0 < estimate_usd <= limit_usd``.
    """
    if not isinstance(offers, list) or len(offers) > CLAIM_PAGE_LIMIT:
        raise ValueError(f"offers must be a list of at most {CLAIM_PAGE_LIMIT} entries")
    seen: set[str] = set()
    for offer in offers:
        if not isinstance(offer, dict):
            raise ValueError("each offer must be a dict")
        job_id, kind = offer.get("job_id"), offer.get("kind")
        if not isinstance(job_id, str) or not job_id or job_id in seen:
            raise ValueError("each offer needs a unique non-empty string job_id")
        seen.add(job_id)
        if kind == "firm":
            fields: tuple[str, ...] = ("price_usd",)
        elif kind == "limit":
            fields = ("estimate_usd", "limit_usd")
        else:
            raise ValueError("offer kind must be 'firm' or 'limit'")
        if set(offer) != {"job_id", "kind", *fields}:
            raise ValueError(f"a {kind} offer has exactly job_id, kind and {' and '.join(fields)}")
        for field in fields:
            if not _positive_usd(offer[field]):
                raise ValueError(f"{field} must be a finite, positive number")
        # The gateway compares float-normalized amounts.
        if kind == "limit" and float(offer["estimate_usd"]) > float(offer["limit_usd"]):
            raise ValueError("estimate_usd must be at most limit_usd")


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

    def get_registration(self, runner_id: str | None = None) -> dict[str, Any]:
        """GET /api/runner/v1/runners/me — this token's active registration."""
        from urllib.parse import quote

        path = "/api/runner/v1/runners/me"
        if runner_id is not None:
            path += f"?runner_id={quote(str(runner_id), safe='')}"
        headers = self._headers()
        resp = self._client.get(path) if headers is None else self._client.get(path, headers=headers)
        self._raise_for_status(resp)
        payload = resp.json()
        if not isinstance(payload, dict):
            raise ValueError("invalid registration payload")
        return payload

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

    def list_queue(self, runner_id: str) -> dict[str, Any]:
        """GET /api/runner/v1/queue — privacy-safe entries with job_id and envelope.

        Non-2xx responses raise ``OrmasGatewayError``. Only a 404 without
        ``error.type`` signals an older gateway without this route; a typed 404
        such as ``not_found_error`` (unknown runner, or no priced binding) is an
        ordinary error.
        """
        from urllib.parse import quote

        path = f"/api/runner/v1/queue?runner_id={quote(str(runner_id), safe='')}"
        headers = self._headers()
        resp = self._client.get(path) if headers is None else self._client.get(path, headers=headers)
        self._raise_for_status(resp)
        payload = resp.json()
        if not isinstance(payload, dict):
            raise ValueError("invalid queue payload")
        return payload

    def claim_task(
        self, runner_id: str, *, ask_usd: float | None = None,
        offers: list[dict[str, Any]] | None = None,
        claim_request_id: str | None = None,
    ) -> tuple[TaskLease, TaskDraft] | None:
        """POST /api/runner/v1/leases — typed lease+draft, or None when idle (HTTP 204).

        ``ask_usd`` is an optional legacy firm bid. Omitting it keeps the
        byte-identical claim body. Non-numeric, bool, non-finite or negative
        asks raise ``ValueError`` before any request.

        ``offers`` contains per-job ``{job_id, kind: "firm", price_usd}`` or
        ``{job_id, kind: "limit", estimate_usd, limit_usd}`` dicts
        (``0 < estimate_usd <= limit_usd``) and cannot be combined with
        ``ask_usd``. Malformed
        offers, more than ``CLAIM_PAGE_LIMIT`` entries or a duplicate ``job_id``
        raise ``ValueError`` before any request. An empty list declines new jobs
        but can resume a live lease.

        ``claim_request_id`` is an optional opaque id for this claim request.
        It is sent only when provided. The returned lease exposes the gateway's
        echo when present.
        """
        if offers is not None and ask_usd is not None:
            raise ValueError("offers cannot be combined with ask_usd")
        if ask_usd is not None and (
            not isinstance(ask_usd, (int, float))
            or isinstance(ask_usd, bool)
            or not math.isfinite(float(ask_usd))
            or ask_usd < 0
        ):
            raise ValueError("ask_usd must be a finite, non-negative number")
        if offers is not None:
            _validate_offers(offers)
        body: dict[str, Any] = {"schema_version": RUNNER_PROTOCOL_V1, "runner_id": runner_id}
        if offers is not None:
            body["offers"] = [dict(offer) for offer in offers]
        elif ask_usd is not None:
            body["ask_usd"] = float(ask_usd)
        if claim_request_id is not None:
            body["claim_request_id"] = claim_request_id
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

    def read_repository_credential(
        self, task_id: str, runner_id: str, lease_token: str,
    ) -> dict[str, Any]:
        """POST /api/runner/v1/leases/{task_id}/repo-credential — ``{repo_credential}``
        for the live lease. It does not renew the lease."""
        task_id = require_task_id(task_id)
        body: dict[str, Any] = {
            "schema_version": RUNNER_PROTOCOL_V1,
            "runner_id": runner_id,
            "lease_token": lease_token,
        }
        resp = self._post(f"/api/runner/v1/leases/{task_id}/repo-credential", body)
        self._raise_for_status(resp)
        return resp.json()

    def publish_result(self, task_id: str, runner_id: str, lease_token: str, *, source, length: int) -> dict[str, Any]:
        """Upload the bounded public artifact automatically, without repository keys."""
        task_id = require_task_id(task_id)
        headers = dict(self._headers() or {})
        headers.update({'X-Ormas-Runner-Id': runner_id, 'X-Ormas-Lease-Token': lease_token,
            'Content-Type': 'application/vnd.ormas.public-artifact.v1', 'Content-Length': str(length)})
        source.seek(0)
        response = self._client.post(f'/api/runner/v1/leases/{task_id}/publication',
            content=iter(lambda: source.read(65536), b''), headers=headers, timeout=180.0)
        self._raise_for_status(response)
        return response.json()

    def complete_task(
        self,
        task_id: str,
        runner_id: str,
        lease_token: str,
        *,
        receipt: TaskReceipt,
        terminal: TaskTerminal,
        capture: Mapping[str, Any] | None = None,
        effort: Mapping[str, int] | None = None,
        failure_evidence: Mapping[str, Any] | None = None,
        pr_url: str | None = None,
        pr_error: str | None = None,
    ) -> dict[str, Any]:
        """POST /api/runner/v1/leases/{task_id}/complete — 200 done, 202 settling, 410 replay.

        ``effort`` is the optional counts-only disclosure (``attempts``,
        ``model_turns``, ``models_used``, ``output_tokens``, ``total_tokens``);
        it is validated locally and sent only when supplied.
        """
        task_id = require_task_id(task_id)
        wire_effort = _validate_effort(effort) if effort is not None else None
        body: dict[str, Any] = {
            "schema_version": RUNNER_PROTOCOL_V1,
            "runner_id": runner_id,
            "lease_token": lease_token,
            "receipt": receipt.to_wire(),
            "terminal": terminal.to_wire(),
        }
        if capture is not None:
            body["capture"] = snapshot_evidence(capture)
        if wire_effort is not None:
            body["effort"] = wire_effort
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

    def hotkey_challenge(self, runner_id: str, *, hotkey_ss58: str) -> dict[str, Any]:
        """POST /api/runner/v1/hotkey/challenge — mint a one-time registration challenge.

        The gateway binds the challenge to (runner, miner identity, hotkey); it
        is valid for 300 s and can be consumed exactly once. Response keys:
        ``challenge``, ``expires_at``, ``now``, ``miner_identity``. Raises
        ``ValueError`` if the payload is not a mapping or lacks a str
        ``challenge``.

        This package never holds, derives, or loads a hotkey — the challenge is
        an opaque string here; signing it is the miner's own tooling's job (see
        :meth:`register_hotkey`).
        """
        body: dict[str, Any] = {
            "schema_version": RUNNER_PROTOCOL_V1,
            "runner_id": runner_id,
            "hotkey_ss58": hotkey_ss58,
        }
        resp = self._post("/api/runner/v1/hotkey/challenge", body)
        self._raise_for_status(resp)
        payload = resp.json()
        if not isinstance(payload, Mapping) or not isinstance(payload.get("challenge"), str):
            raise ValueError("invalid hotkey challenge payload")
        return dict(payload)

    def register_hotkey(
        self,
        runner_id: str,
        *,
        hotkey_ss58: str,
        sign_fn: Callable[[bytes], str],
    ) -> dict[str, Any]:
        """Drive challenge → sign → register for the miner's chain hotkey.

        Mints a challenge via :meth:`hotkey_challenge`, then calls the injected
        signer as ``sign_fn(challenge.encode("utf-8"))`` — the signature covers
        EXACTLY the UTF-8 bytes of the challenge string, nothing prepended. The
        signer is injected because this package never holds, derives, or loads a
        hotkey; the miner signs with its own tooling (e.g. a Bittensor wallet
        ``Keypair.sign``) and hands back the hex signature.

        The signature is validated locally BEFORE the register request is sent:
        it must be a non-empty ``str`` of even length consisting only of hex
        digits — anything else raises ``ValueError`` and no request is made.

        Then POSTs ``/api/runner/v1/hotkey`` with ``{schema_version, runner_id,
        hotkey_ss58, challenge, signature_hex}``. A refusal (400/409/503, e.g.
        ``hotkey_claimed``) raises :class:`OrmasGatewayError` carrying the
        server's message — never a returned result — and ``verified`` is never
        set or inferred locally. Returns the response JSON dict
        (``miner_identity``, ``hotkey_ss58``, ``verified``,
        ``signature_scheme``).
        """
        challenge = self.hotkey_challenge(runner_id, hotkey_ss58=hotkey_ss58)["challenge"]
        signature_hex = sign_fn(challenge.encode("utf-8"))
        if (
            not isinstance(signature_hex, str)
            or not signature_hex
            or len(signature_hex) % 2 != 0
            or any(c not in "0123456789abcdefABCDEF" for c in signature_hex)
        ):
            raise ValueError(
                "sign_fn must return a non-empty, even-length hex signature string"
            )
        body: dict[str, Any] = {
            "schema_version": RUNNER_PROTOCOL_V1,
            "runner_id": runner_id,
            "hotkey_ss58": hotkey_ss58,
            "challenge": challenge,
            "signature_hex": signature_hex,
        }
        resp = self._post("/api/runner/v1/hotkey", body)
        self._raise_for_status(resp)
        return resp.json()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "OrmasMinerClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
