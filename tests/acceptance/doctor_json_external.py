"""Ormas external acceptance for card f3780825.

Runs the frozen `ormas-miner doctor --json` check inside the candidate. The
existing miner CLI file is not run here: it needs a `git` binary, which the
verifier candidate does not have. On the base the --json checks fail on plain
asserts.
"""

from ormas_acceptance import run

FILES = ("tests/test_miner_cli_doctor_json.py",)


def _pytest(path: str) -> dict:
    return run(["python3", "-m", "pytest", "-q", "-p", "no:cacheprovider", path],
               timeout_s=60)


def test_doctor_json_passes_in_candidate() -> None:
    result = _pytest(FILES[0])
    assert result["returncode"] == 0, result["output"][-3000:]
