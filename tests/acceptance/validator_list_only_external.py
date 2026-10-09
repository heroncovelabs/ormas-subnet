"""Ormas external acceptance for card 5a960b1f.

Runs the frozen validator --list-only check inside the candidate in one call,
which stays under the 60 s bridge limit. On the base the --list-only checks
fail on plain asserts.
"""

from ormas_acceptance import run

FILE = "tests/test_validator_list_only.py"


def test_validator_list_only_passes_in_candidate() -> None:
    result = run(["python3", "-m", "pytest", "-q", "-p", "no:cacheprovider", FILE],
                 timeout_s=60)
    assert result["returncode"] == 0, result["output"][-3000:]
