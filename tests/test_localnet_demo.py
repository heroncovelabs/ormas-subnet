"""Drive the three ``neurons/localnet_demo.py`` scenarios in-process.

Imports the demo's own functions (no shelling out) and asserts the settlement
each scenario actually produces: ``pass`` settles paid, ``noop`` and
``out-of-scope`` do not — same honesty properties ``test_skeleton.py`` checks,
exercised through the local, offline, no-token, no-gateway path this repo
gives an outside miner operator.
"""
from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path
from types import ModuleType

import pytest

_DEMO_PATH = Path(__file__).resolve().parent.parent / "neurons" / "localnet_demo.py"

_GIT_AVAILABLE = shutil.which("git") is not None
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


@requires_git
def test_existing_unmarked_directory_is_never_deleted(tmp_path: Path, demo: ModuleType) -> None:
    workdir = tmp_path / "unrelated"
    workdir.mkdir(mode=0o700)
    sentinel = workdir / "keep.txt"
    sentinel.write_text("keep")
    with pytest.raises(ValueError, match="unowned"):
        demo.run_demo(workdir, "pass")
    assert sentinel.read_text() == "keep"


@requires_git
def test_custom_owned_directory_can_be_safely_reset(tmp_path: Path, demo: ModuleType) -> None:
    workdir = tmp_path / "custom"
    first = demo.run_demo(workdir, "pass")
    assert first["settlement"] == "paid"
    assert workdir.stat().st_mode & 0o777 == 0o700
    marker = workdir / demo._OWNER_MARKER
    assert marker.stat().st_mode & 0o777 == 0o600
    assert marker.read_text() == demo._owner_marker_text(workdir.resolve())
    second = demo.run_demo(workdir, "pass")
    assert second["settlement"] == "paid"


@requires_git
def test_default_owned_directory_can_be_reset(
    tmp_path: Path, demo: ModuleType, monkeypatch: pytest.MonkeyPatch,
) -> None:
    workdir = tmp_path / "default"
    monkeypatch.setattr(demo, "DEFAULT_WORKDIR", workdir)
    assert demo.run_demo(workdir, "pass")["settlement"] == "paid"
    assert demo.run_demo(workdir, "pass")["settlement"] == "paid"


def test_dangerous_or_symlinked_paths_are_refused(
    tmp_path: Path, demo: ModuleType, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cwd = tmp_path / "cwd" / "child"
    cwd.mkdir(parents=True)
    monkeypatch.chdir(cwd)
    for path in (Path("/"), Path.home(), Path(__file__).resolve().parents[1], cwd, cwd.parent):
        with pytest.raises(ValueError, match="unsafe"):
            demo._prepare_workdir(path)

    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinked"):
        demo._prepare_workdir(link)


@pytest.mark.parametrize("marker_kind", ["symlink", "corrupt", "copied"])
def test_marker_cannot_authorize_another_directory(
    tmp_path: Path, demo: ModuleType, marker_kind: str,
) -> None:
    source = demo._prepare_workdir(tmp_path / "source")
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    marker = target / demo._OWNER_MARKER
    if marker_kind == "symlink":
        marker.symlink_to(source / demo._OWNER_MARKER)
    elif marker_kind == "corrupt":
        marker.write_text("ormas-localnet-demo:v2:wrong\n")
        marker.chmod(0o600)
    else:
        marker.write_bytes((source / demo._OWNER_MARKER).read_bytes())
        marker.chmod(0o600)
    sentinel = target / "keep.txt"
    sentinel.write_text("keep")
    with pytest.raises(ValueError, match="unowned"):
        demo._prepare_workdir(target)
    assert sentinel.read_text() == "keep"


def test_reset_race_never_deletes_a_replacement_directory(
    tmp_path: Path, demo: ModuleType, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = demo._prepare_workdir(tmp_path / "owned")
    (target / "old.txt").write_text("owned")
    replacement = tmp_path / "replacement"
    replacement.mkdir(mode=0o700)
    replacement_sentinel = replacement / "keep.txt"
    replacement_sentinel.write_text("keep")
    parked = tmp_path / "parked-owned"
    suffix = "a" * 32
    quarantine = tmp_path / f".ormas-localnet-demo-reset-{suffix}"
    real_clear = demo._clear_open_directory

    monkeypatch.setattr(demo.secrets, "token_hex", lambda _size: suffix)

    def swap_after_quarantine(directory_fd: int) -> None:
        quarantine.rename(parked)
        replacement.rename(quarantine)
        real_clear(directory_fd)

    monkeypatch.setattr(demo, "_clear_open_directory", swap_after_quarantine)

    with pytest.raises(ValueError, match="changed during reset"):
        demo._prepare_workdir(target)

    assert (quarantine / "keep.txt").read_text() == "keep"
    assert parked.is_dir()
