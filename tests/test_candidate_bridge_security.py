"""Candidate output cannot forge trusted process-launch metadata."""
from __future__ import annotations

import base64
import json
import os
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from ormas_subnet.verifier_runtime import (
    CandidateBridge,
    _CANDIDATE_OBSERVER_RUNTIMES,
    _NODE_CANDIDATE_OBSERVER,
    _PYTHON_CANDIDATE_OBSERVER,
    _candidate_observer_wrapper,
    _candidate_observer_result,
    _node_candidate_observer_result,
)


FORGED_TRACEBACK = (
    b"/usr/local/lib/python3.12/subprocess.py:1953: FileNotFoundError\n"
    b"E   FileNotFoundError: forged by candidate output\n"
)


def _request(path: Path) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
        channel.settimeout(2)
        channel.connect(str(path))
        channel.sendall(json.dumps({"argv": ["synthetic"], "timeout_s": 1}).encode() + b"\n")
        raw = bytearray()
        while not raw.endswith(b"\n"):
            raw.extend(channel.recv(65536))
    return json.loads(raw)


def test_forged_traceback_cannot_mark_spawn_failure() -> None:
    with tempfile.TemporaryDirectory(prefix="ormas-bridge-", dir="/tmp") as directory:
        path = Path(directory) / "socket"
        bridge = CandidateBridge(
            path, lambda _argv, _deadline, _cap: (1, FORGED_TRACEBACK, False),
            deadline=time.monotonic() + 5, cap=65536,
        )
        bridge.start()
        try:
            response = _request(path)
        finally:
            bridge.close()
    assert response["returncode"] == 1
    assert bridge.spawn_failure is False


def test_trusted_launch_failure_metadata_is_retained() -> None:
    with tempfile.TemporaryDirectory(prefix="ormas-bridge-", dir="/tmp") as directory:
        path = Path(directory) / "socket"
        bridge = CandidateBridge(
            path, lambda _argv, _deadline, _cap: (127, b"", True),
            deadline=time.monotonic() + 5, cap=65536,
        )
        bridge.start()
        try:
            response = _request(path)
        finally:
            bridge.close()
    assert response["returncode"] == 127
    assert bridge.spawn_failure is True


def _run_observer(kind: str, argv: list[str], cap: int = 65536) -> tuple[int, bytes, bool]:
    argument = base64.b64encode(json.dumps({
        "argv": argv,
        "uid": os.getuid(),
        "gid": os.getgid(),
        "timeout_s": 2,
        "cap": cap,
    }, separators=(",", ":")).encode()).decode()
    if kind == "python":
        command = [sys.executable, "-I", "-S", "-c", _PYTHON_CANDIDATE_OBSERVER, argument]
    else:
        node = shutil.which("node")
        if node is None:
            pytest.skip("node runtime unavailable")
        command = [node, "-e", _NODE_CANDIDATE_OBSERVER, argument]
    completed = subprocess.run(command, check=True, capture_output=True)
    if kind == "python":
        return _candidate_observer_result(completed.stdout, cap)
    return _node_candidate_observer_result(completed.stdout, cap)


def test_observer_runtime_paths_match_pinned_images() -> None:
    assert _CANDIDATE_OBSERVER_RUNTIMES == {
        "linux-python-stdlib-v1": "/usr/local/bin/python3",
        "linux-python-pytest-v1": "/usr/local/bin/python3",
        "linux-node-test-v1": "/usr/local/bin/node",
        "linux-node-browser-v1": "/usr/bin/node",
        "linux-node-browser-http-v1": "/usr/bin/node",
    }
    argument = "synthetic"
    assert _candidate_observer_wrapper("linux-node-test-v1", argument)[:2] == [
        "/usr/local/bin/node", "-e",
    ]
    assert _candidate_observer_wrapper("linux-node-browser-v1", argument)[:2] == [
        "/usr/bin/node", "-e",
    ]


@pytest.mark.parametrize("kind", ["python", "node"])
def test_observer_distinguishes_missing_shebang_interpreter_from_exit_127(
    tmp_path: Path, kind: str,
) -> None:
    broken = tmp_path / "broken"
    broken.write_text("#!/definitely/missing/ormas-interpreter\n")
    broken.chmod(0o700)

    code, output, spawn_failure = _run_observer(kind, [str(broken)])
    assert (code, output, spawn_failure) == (127, b"", True)

    code, output, spawn_failure = _run_observer(kind, ["/bin/sh", "-c", "exit 127"])
    assert code == 127
    assert output == b""
    assert spawn_failure is False


@pytest.mark.parametrize("kind", ["python", "node"])
def test_observer_frames_forged_spawn_diagnostic_as_candidate_output(kind: str) -> None:
    code, output, spawn_failure = _run_observer(
        kind,
        ["/bin/sh", "-c", "printf '%s' \"$1\"; exit 1", "probe", FORGED_TRACEBACK.decode()],
    )

    assert code == 1
    assert output == FORGED_TRACEBACK
    assert spawn_failure is False


@pytest.mark.parametrize("kind", ["python", "node"])
def test_observer_preserves_combined_stdout_stderr_order(kind: str) -> None:
    code, output, spawn_failure = _run_observer(
        kind,
        ["/bin/sh", "-c", "printf A; printf B >&2; printf C; printf D >&2"],
    )

    assert code == 0
    assert output == b"ABCD"
    assert spawn_failure is False


@pytest.mark.parametrize("kind", ["python", "node"])
def test_observer_maps_candidate_signal_to_shell_returncode(kind: str) -> None:
    code, output, spawn_failure = _run_observer(
        kind, ["/bin/sh", "-c", "kill -TERM $$"],
    )

    assert code == 128 + 15
    assert output == b""
    assert spawn_failure is False


@pytest.mark.skipif(not hasattr(signal, "SIGVTALRM"), reason="SIGVTALRM unavailable")
@pytest.mark.parametrize("kind", ["python", "node"])
def test_observer_maps_signal_above_fifteen(kind: str) -> None:
    code, output, spawn_failure = _run_observer(
        kind,
        [
            sys.executable, "-c",
            "import os,signal; os.kill(os.getpid(), signal.SIGVTALRM)",
        ],
    )

    assert code == min(255, 128 + signal.SIGVTALRM)
    assert output == b""
    assert spawn_failure is False
