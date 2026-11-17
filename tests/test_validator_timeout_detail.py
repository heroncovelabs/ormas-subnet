"""Review warnings preserve timeout diagnostics and keep client stderr private."""
from __future__ import annotations

import logging
import subprocess
from types import SimpleNamespace

import pytest

from ormas_subnet.validator import ValidatorConfig, ValidatorDaemon


@pytest.fixture
def daemon(tmp_path):
    return ValidatorDaemon(
        None, ValidatorConfig(tmp_path / "checkouts"), lambda digest: digest,
    )


@pytest.mark.parametrize("stderr", [
    "client_source_fragment\nmore_client_source",
    b"client_source_fragment\nmore_client_source",
], ids=["str-stderr", "bytes-stderr"])
@pytest.mark.parametrize("cause_depth", [0, 1, 2], ids=["direct", "cause", "nested-cause"])
def test_timeout_warning_omits_stderr(daemon, caplog, stderr, cause_depth):
    timeout = subprocess.TimeoutExpired("verify", 1, stderr=stderr)
    error = timeout
    for _ in range(cause_depth):
        wrapper = RuntimeError("review wrapper")
        wrapper.__cause__ = error
        error = wrapper

    with caplog.at_level(logging.WARNING, logger="ormas_subnet.validator"):
        result = daemon._review_error({"assignment_id": "asgn_timeout"}, error)

    assert result == "error"
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.levelno == logging.WARNING
    warning = record.getMessage()
    assert "TimeoutExpired" in warning
    assert str(timeout) in warning
    assert "timed out after 1 seconds" in warning
    assert "client_source_fragment" not in warning
    assert "more_client_source" not in warning


@pytest.mark.parametrize("as_bytes", [False, True], ids=["str-stderr", "bytes-stderr"])
def test_non_timeout_warning_keeps_stderr_and_redacts_credentials(daemon, caplog, as_bytes):
    assignment = {
        "assignment_id": "asgn_error",
        "repo_credential": {
            "token": "repo-token-secret",
            "private_key": "private-key-secret",
        },
    }
    daemon._protected_tokens["asgn_error"] = SimpleNamespace(token="protected-token-secret")
    stderr = (
        "diagnostic café\nrepo-token-secret\nprivate-key-secret\nprotected-token-secret"
    )
    if as_bytes:
        stderr = stderr.encode("utf-8")
    error = subprocess.CalledProcessError(1, "verify", stderr=stderr)

    with caplog.at_level(logging.WARNING, logger="ormas_subnet.validator"):
        result = daemon._review_error(assignment, error)

    assert result == "error"
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.levelno == logging.WARNING
    warning = record.getMessage()
    assert warning == (
        f"review failed for assignment asgn_error: CalledProcessError: {error}: "
        "diagnostic café [redacted] [redacted] [redacted]"
    )
    assert "\n" not in warning
    for secret in ("repo-token-secret", "private-key-secret", "protected-token-secret"):
        assert secret not in warning
