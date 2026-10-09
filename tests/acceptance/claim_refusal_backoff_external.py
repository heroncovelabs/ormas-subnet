"""Ormas external acceptance for card f18d030c.

Runs the frozen claim-refusal backoff check and the existing run_forever
resilience file inside the candidate, one call per file so each stays under
the 60 s bridge limit. On the base the backoff checks fail on plain asserts.
"""

from ormas_acceptance import run

FILES = (
    "tests/test_claim_refusal_backoff.py",
    "tests/test_run_forever_resilience.py",
)


def _pytest(path: str) -> dict:
    return run(["python3", "-m", "pytest", "-q", "-p", "no:cacheprovider", path],
               timeout_s=60)


def test_claim_refusal_backoff_passes_in_candidate() -> None:
    result = _pytest(FILES[0])
    assert result["returncode"] == 0, result["output"][-3000:]


def test_run_forever_resilience_stays_green_in_candidate() -> None:
    result = _pytest(FILES[1])
    assert result["returncode"] == 0, result["output"][-3000:]
