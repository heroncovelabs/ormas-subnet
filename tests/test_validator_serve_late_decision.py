"""Single-slot serve keeps polling after a gateway decision post or list fails.

A 409 on ``post_decision`` is a late decision after settlement (validator_timeout).
The daemon logs it, drops that assignment's local read token, and still posts the
next assignment. The same pass does not post the settled assignment again.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import httpx

from ormas_subnet.validator import ValidatorConfig, ValidatorDaemon


_DIGEST = "ab" * 32
_TOKEN = "ormv_do_not_log_this_token"
_BODY = "SECRET_ASSIGNMENT_BODY_do_not_log"


def _row(assignment_id: str) -> dict:
    return {
        "assignment_id": assignment_id,
        "evidence_digest_sha256": _DIGEST,
        "repo_credential": {"token": _TOKEN},
        "verify_command": _BODY,
    }


def _conflict(assignment_id: str) -> httpx.HTTPStatusError:
    request = httpx.Request(
        "POST",
        f"https://gateway.example/api/validator/v1/assignments/{assignment_id}/decisions",
    )
    response = httpx.Response(
        409,
        json={"error": {"type": "conflict_error", "message": "validator_timeout"}},
        request=request,
    )
    return httpx.HTTPStatusError(
        "decision post was refused after settlement",
        request=request,
        response=response,
    )


def test_slots_one_serve_survives_409_and_posts_the_next_assignment(tmp_path, caplog):
    posted: list[tuple[str, str, str]] = []

    class Client:
        def list_assignments(self):
            if any(row[0] == "asgn_next" for row in posted):
                return []
            return [_row("asgn_late"), _row("asgn_next")]

        def post_decision(self, assignment_id, *, decision, signature_hex):
            posted.append((assignment_id, decision, signature_hex))
            if assignment_id == "asgn_late":
                raise _conflict(assignment_id)
            return {"decision": decision}

    signs: list[str] = []

    def sign_fn(digest: str) -> str:
        signs.append(digest)
        return "cd" * 64

    daemon = ValidatorDaemon(Client(), ValidatorConfig(workdir_root=tmp_path / "work"), sign_fn)
    daemon._protected_tokens["asgn_late"] = SimpleNamespace(
        token=_TOKEN, expires_at="2099-01-01T00:00:00Z",
    )
    daemon._protected_tokens["asgn_other"] = SimpleNamespace(
        token="ormv_other_token_keep", expires_at="2099-01-01T00:00:00Z",
    )
    polls = {"n": 0}

    def until() -> bool:
        polls["n"] += 1
        if any(row[0] == "asgn_next" for row in posted):
            return True
        return polls["n"] > 8

    with caplog.at_level(logging.WARNING, logger="ormas_subnet.validator"):
        daemon.serve(1, 0, until=until)

    late = [row for row in posted if row[0] == "asgn_late"]
    nxt = [row for row in posted if row[0] == "asgn_next"]
    assert late == [("asgn_late", "error", "cd" * 64)]
    assert nxt == [("asgn_next", "error", "cd" * 64)]
    assert signs == [_DIGEST, _DIGEST]
    assert "asgn_late" not in daemon._protected_tokens
    assert daemon._protected_tokens["asgn_other"].token == "ormv_other_token_keep"
    text = caplog.text
    assert "post_decision failed" in text
    assert "HTTPStatusError" in text
    assert "409" in text
    assert "validator_timeout" not in text
    assert _TOKEN not in text
    assert _BODY not in text
    assert "ormv_other_token_keep" not in text


def test_slots_one_serve_survives_list_transport_error_and_posts_later(tmp_path, caplog):
    posted: list[str] = []
    lists = {"n": 0}

    class Client:
        def list_assignments(self):
            lists["n"] += 1
            if lists["n"] == 1:
                raise httpx.ConnectError("temporary gateway transport failure")
            if posted:
                return []
            return [_row("asgn_next")]

        def post_decision(self, assignment_id, *, decision, signature_hex):
            del decision, signature_hex
            posted.append(assignment_id)
            return {"decision": "error"}

    daemon = ValidatorDaemon(
        Client(), ValidatorConfig(workdir_root=tmp_path / "work"), lambda _digest: "cd" * 64,
    )
    daemon._protected_tokens["asgn_keep"] = SimpleNamespace(
        token=_TOKEN, expires_at="2099-01-01T00:00:00Z",
    )
    polls = {"n": 0}

    def until() -> bool:
        polls["n"] += 1
        if posted:
            return True
        return polls["n"] > 8

    with caplog.at_level(logging.WARNING, logger="ormas_subnet.validator"):
        daemon.serve(1, 0, until=until)

    assert posted == ["asgn_next"]
    assert daemon._protected_tokens["asgn_keep"].token == _TOKEN
    text = caplog.text
    assert "list_assignments failed" in text
    assert "ConnectError" in text
    assert _TOKEN not in text
    assert _BODY not in text
