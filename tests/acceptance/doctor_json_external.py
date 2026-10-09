"""Ormas external acceptance for card f3780825.

Runs the frozen `ormas-miner doctor --json` check and the existing miner CLI
file inside the candidate, one call per file so each stays under the 60 s
bridge limit. On the base the --json checks fail on plain asserts.
"""

from ormas_acceptance import run

FILES = (
    "tests/test_miner_cli_doctor_json.py",
    "tests/test_miner_cli.py",
)


def _pytest(path: str) -> dict:
    return run(["python3", "-m", "pytest", "-q", "-p", "no:cacheprovider", path],
               timeout_s=60)


def test_doctor_json_passes_in_candidate() -> None:
    result = _pytest(FILES[0])
    assert result["returncode"] == 0, result["output"][-3000:]


def test_miner_cli_stays_green_in_candidate() -> None:
    result = _pytest(FILES[1])
    assert result["returncode"] == 0, result["output"][-3000:]
