"""Parallel serve drops a 409 decision post as a benign late decision.

``_retry_pending_posts`` (the ``_serve_parallel`` path) posts a settled
assignment once, then removes it from ``pending``, records it in ``posted``,
and releases its Protected read token. A later retry pass does not post it
again. Any other ``post_decision`` error stays pending and is retried.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import httpx

from ormas_subnet.validator import (
    ValidatorConfig,
    ValidatorDaemon,
    _PendingPost,
    _retry_pending_posts,
)


_DIGEST = "ab" * 32
_LATE_TOKEN = "ormv_late_do_not_log_this_token"
_OK_TOKEN = "ormv_ok_do_not_log_this_token"
_BODY = "SECRET_ASSIGNMENT_BODY_do_not_log"
_LATE_SIGNATURE = "cd" * 64
_OK_SIGNATURE = "ef" * 64


def _assignment(assignment_id: str, token: str) -> dict:
    return {
        "assignment_id": assignment_id,
        "evidence_digest_sha256": _DIGEST,
        "repo_credential": {"token": token},
        "verify_command": _BODY,
    }


def _http_error(assignment_id: str, status: int) -> httpx.HTTPStatusError:
    request = httpx.Request(
        "POST",
        f"https://gateway.example/api/validator/v1/assignments/{assignment_id}/decisions",
    )
    response = httpx.Response(
        status,
        json={"error": {"type": "conflict_error", "message": "validator_timeout"}},
        request=request,
    )
    return httpx.HTTPStatusError(
        "decision post was refused after settlement",
        request=request,
        response=response,
    )


def _pending(assignment_id: str, token: str, *, decision: str, signature_hex: str) -> _PendingPost:
    return _PendingPost(
        assignment=_assignment(assignment_id, token),
        digest=_DIGEST,
        decision=decision,
        signature_hex=signature_hex,
    )


def _daemon(tmp_path, client) -> ValidatorDaemon:
    def sign_fn(_digest: str) -> str:
        raise AssertionError("a stored signature must not be signed again")

    return ValidatorDaemon(client, ValidatorConfig(workdir_root=tmp_path / "work"), sign_fn)


def test_retry_pending_posts_drops_409_once_and_posts_the_other(tmp_path, caplog):
    calls: list[tuple[str, str, str]] = []

    class Client:
        def post_decision(self, assignment_id, *, decision, signature_hex):
            calls.append((assignment_id, decision, signature_hex))
            if assignment_id == "asgn_late":
                raise _http_error(assignment_id, 409)
            return {"decision": decision}

    daemon = _daemon(tmp_path, Client())
    daemon._protected_tokens["asgn_late"] = SimpleNamespace(
        token=_LATE_TOKEN, expires_at="2099-01-01T00:00:00Z",
    )
    daemon._protected_tokens["asgn_ok"] = SimpleNamespace(
        token=_OK_TOKEN, expires_at="2099-01-01T00:00:00Z",
    )
    daemon._protected_tokens["asgn_other"] = SimpleNamespace(
        token="ormv_other_token_keep", expires_at="2099-01-01T00:00:00Z",
    )
    pending = {
        "asgn_late": _pending(
            "asgn_late", _LATE_TOKEN, decision="reject", signature_hex=_LATE_SIGNATURE,
        ),
        "asgn_ok": _pending(
            "asgn_ok", _OK_TOKEN, decision="accept", signature_hex=_OK_SIGNATURE,
        ),
    }
    posted: set[str] = set()

    with caplog.at_level(logging.WARNING, logger="ormas_subnet.validator"):
        first = _retry_pending_posts(daemon, pending, posted)
        after_first = list(calls)
        second = _retry_pending_posts(daemon, pending, posted)

    assert first is True
    assert second is False
    late = [row for row in calls if row[0] == "asgn_late"]
    ok = [row for row in calls if row[0] == "asgn_ok"]
    assert late == [("asgn_late", "reject", _LATE_SIGNATURE)]
    assert late == [row for row in after_first if row[0] == "asgn_late"]
    assert ok == [("asgn_ok", "accept", _OK_SIGNATURE)]
    assert "asgn_late" not in pending
    assert "asgn_late" in posted
    assert "asgn_ok" not in pending
    assert "asgn_ok" in posted
    assert "asgn_late" not in daemon._protected_tokens
    assert "asgn_ok" not in daemon._protected_tokens
    assert daemon._protected_tokens["asgn_other"].token == "ormv_other_token_keep"
    text = caplog.text
    assert "post_decision failed" in text
    assert "HTTPStatusError" in text
    assert "409" in text
    assert "validator_timeout" not in text
    assert _LATE_TOKEN not in text
    assert _OK_TOKEN not in text
    assert _BODY not in text
    assert "ormv_other_token_keep" not in text


def test_retry_pending_posts_keeps_a_non_409_pending_and_retries(tmp_path):
    calls: list[str] = []

    class Client:
        def post_decision(self, assignment_id, *, decision, signature_hex):
            del decision, signature_hex
            calls.append(assignment_id)
            raise _http_error(assignment_id, 500)

    daemon = _daemon(tmp_path, Client())
    daemon._protected_tokens["asgn_busy"] = SimpleNamespace(
        token=_LATE_TOKEN, expires_at="2099-01-01T00:00:00Z",
    )
    pending = {
        "asgn_busy": _pending(
            "asgn_busy", _LATE_TOKEN, decision="accept", signature_hex=_OK_SIGNATURE,
        ),
    }
    posted: set[str] = set()

    assert _retry_pending_posts(daemon, pending, posted) is False
    assert _retry_pending_posts(daemon, pending, posted) is False

    assert calls == ["asgn_busy", "asgn_busy"]
    assert "asgn_busy" in pending
    assert "asgn_busy" not in posted
    assert daemon._protected_tokens["asgn_busy"].token == _LATE_TOKEN
