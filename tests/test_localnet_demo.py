"""Drive the three ``neurons/localnet_demo.py`` scenarios in-process.

Imports the demo's own functions (no shelling out) and asserts the settlement
each scenario actually produces: ``pass`` settles paid, ``noop`` and
``out-of-scope`` do not — same honesty properties ``test_skeleton.py`` checks,
exercised through the local, offline, no-token, no-gateway path this repo
gives an outside miner operator.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

_DEMO_PATH = Path(__file__).resolve().parent.parent / "neurons" / "localnet_demo.py"

_GIT_AVAILABLE = subprocess.run(["git", "--version"], capture_output=True).returncode == 0
requires_git = pytest.mark.skipif(not _GIT_AVAILABLE, reason="git binary not available")


def _load_demo_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ormas_localnet_demo", _DEMO_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def demo() -> ModuleType:
    return _load_demo_module()


@requires_git
def test_pass_scenario_settles_paid(tmp_path: Path, demo: ModuleType) -> None:
    receipt = demo.run_demo(tmp_path / "pass", "pass")

    assert receipt["verification_state"] == "verified"
    assert receipt["scope_ok"] is True
    assert receipt["settlement"] == "paid"
    assert receipt["failure_class"] is None
    assert receipt["customer_billed_usd"] > 0.0
    assert len(receipt["result_commit"]) == 40


@requires_git
def test_noop_scenario_is_no_delivery(tmp_path: Path, demo: ModuleType) -> None:
    receipt = demo.run_demo(tmp_path / "noop", "noop")

    assert receipt["verification_state"] == "failed"
    assert receipt["settlement"] == "no_delivery"
    assert receipt["settlement"] != "paid"
    assert receipt["failure_class"] is not None
    assert receipt["customer_billed_usd"] == 0.0


@requires_git
def test_out_of_scope_scenario_is_no_delivery(tmp_path: Path, demo: ModuleType) -> None:
    receipt = demo.run_demo(tmp_path / "out-of-scope", "out-of-scope")

    assert receipt["scope_ok"] is False
    assert receipt["settlement"] == "no_delivery"
    assert receipt["settlement"] != "paid"
    assert receipt["failure_class"] is not None
    assert receipt["customer_billed_usd"] == 0.0
    # The verify command itself would have passed (out.txt was fixed) — only
    # the out-of-scope edit makes this a non-payable delivery.
    assert "other.txt" in receipt["changed_paths"]


@requires_git
def test_main_exit_code_reflects_expected_settlement(tmp_path: Path, demo: ModuleType) -> None:
    assert demo.main(["--workdir", str(tmp_path / "cli-pass"), "--scenario", "pass"]) == 0
    assert demo.main(["--workdir", str(tmp_path / "cli-noop"), "--scenario", "noop"]) == 0
    assert demo.main(["--workdir", str(tmp_path / "cli-oos"), "--scenario", "out-of-scope"]) == 0
