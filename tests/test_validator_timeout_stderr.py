"""A verifier timeout's partial stderr stays out of validator warnings.

``ValidatorDaemon._decide`` sends a base-phase verifier timeout (the client's own
code) to ``_review_error``. The ``TimeoutExpired`` it receives can carry partial
stderr from that code; the warning keeps the timeout diagnostic and drops the
stderr. Other failures keep their stderr diagnostics and credential redaction.
Client-frozen acceptance test: the deliverer changes ``ormas_subnet/validator.py`` only.
"""
from __future__ import annotations

import logging
import subprocess

import pytest

from ormas_subnet import validator as module
from ormas_subnet.skeleton import GitError

CANARY = "CLIENT-SOURCE-CANARY-7f3a"
TOKEN = "ghs_fixturetoken0123456789"


def _daemon(tmp_path):
    return module.ValidatorDaemon(None, module.ValidatorConfig(tmp_path), lambda digest: digest)


def _assignment():
    return {"assignment_id": "assignment-1",
            "repo_credential": {"kind": "github_token", "token": TOKEN}}


def _string_timeout():
    return subprocess.TimeoutExpired("verify", 1, stderr=f"partial output {CANARY}\nsecond line")


def _bytes_timeout():
    return subprocess.TimeoutExpired("verify", 1, stderr=f"partial output {CANARY}".encode())


def _chained_timeout():
    try:
        try:
            raise _string_timeout()
        except subprocess.TimeoutExpired as exc:
            raise GitError("verification failed") from exc
    except GitError as outer:
        return outer


@pytest.mark.parametrize("make_error", [_string_timeout, _bytes_timeout, _chained_timeout],
                         ids=["str-stderr", "bytes-stderr", "chained-cause"])
def test_timeout_stderr_is_not_logged(tmp_path, caplog, make_error):
    with caplog.at_level(logging.WARNING, logger=module.__name__):
        assert _daemon(tmp_path)._review_error(_assignment(), make_error()) == "error"
    text = " ".join(record.getMessage() for record in caplog.records)
    assert "TimeoutExpired" in text
    assert "timed out after 1 seconds" in text, "timeout diagnostic was dropped"
    assert CANARY not in text, "timeout stderr is copied into validator warning"


def test_non_timeout_stderr_and_redaction_unchanged(tmp_path, caplog):
    error = subprocess.CalledProcessError(
        128, ["git", "fetch"], stderr=f"fatal: could not read from {TOKEN}@remote DIAGNOSTIC-KEEP",
    )
    with caplog.at_level(logging.WARNING, logger=module.__name__):
        assert _daemon(tmp_path)._review_error(_assignment(), error) == "error"
    text = " ".join(record.getMessage() for record in caplog.records)
    assert "CalledProcessError" in text
    assert "DIAGNOSTIC-KEEP" in text, "non-timeout git diagnostics were dropped"
    assert TOKEN not in text and "[redacted]" in text
