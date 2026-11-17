"""Standalone public-runtime smoke test for the cross-job wheel cache."""
import hashlib
import importlib.util
import json
import os
import subprocess
import time
from pathlib import Path
from unittest.mock import patch


_RUNTIME_PATH = Path(__file__).resolve().parents[1] / "ormas_subnet" / "verifier_runtime.py"
_SPEC = importlib.util.spec_from_file_location("public_verifier_package_cache_runtime", _RUNTIME_PATH)
runtime = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(runtime)


CACHE_ENV = "ORMAS_VERIFIER_PACKAGE_CACHE"


def test_second_locked_wheel_fetch_skips_metadata_and_download(tmp_path, monkeypatch):
    wheel = b"public locked wheel"
    digest = hashlib.sha256(wheel).hexdigest()
    filename = "pytest-1.0-py3-none-any.whl"
    pin = {"name": "pytest", "version": "1.0", "hashes": [digest]}
    calls = []

    def download(url, target, **kwargs):
        calls.append(url)
        if url.endswith("/json"):
            raw = json.dumps({"urls": [{
                "packagetype": "bdist_wheel", "filename": filename,
                "digests": {"sha256": digest},
                "url": "https://files.pythonhosted.org/packages/" + filename,
            }]}).encode()
        else:
            raw = wheel
        target.write_bytes(raw)
        return len(raw)

    monkeypatch.setenv(CACHE_ENV, str(tmp_path / "host-cache"))
    monkeypatch.setattr(runtime, "_DownloadSession", lambda: object())
    monkeypatch.setattr(runtime, "_download", download)
    cfg = {"profile_id": "linux-python-pytest-v1"}
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    runtime._fetch_dependencies(cfg, [pin], None, {}, first,
                                time.monotonic() + 60, 1024 * 1024, object())
    runtime._fetch_dependencies(cfg, [pin], None, {}, second,
                                time.monotonic() + 60, 1024 * 1024, object())

    assert calls == [
        "https://pypi.org/pypi/pytest/1.0/json",
        "https://files.pythonhosted.org/packages/" + filename,
    ]
    assert (second / filename).read_bytes() == wheel


def test_public_verify_launcher_preserves_warm_cache(tmp_path, monkeypatch):
    try:
        from ormas_subnet import skeleton
    except ModuleNotFoundError as exc:  # combined monorepo test run
        if exc.name != "ormas_subnet":
            raise
        from public_subnet.ormas_subnet import skeleton

    wheel = b"launcher locked wheel"
    digest = hashlib.sha256(wheel).hexdigest()
    filename = "pytest-1.0-py3-none-any.whl"
    pin = {"name": "pytest", "version": "1.0", "hashes": [digest]}
    cache = tmp_path / "host-cache"
    monkeypatch.setenv(CACHE_ENV, str(cache))
    monkeypatch.setenv("ENGY_API_KEY", "must-not-cross")
    calls = []

    def download(url, target, **kwargs):
        calls.append(url)
        if url.endswith("/json"):
            raw = json.dumps({"urls": [{
                "packagetype": "bdist_wheel", "filename": filename,
                "digests": {"sha256": digest},
                "url": "https://files.pythonhosted.org/packages/" + filename,
            }]}).encode()
        else:
            raw = wheel
        target.write_bytes(raw)
        return len(raw)

    monkeypatch.setattr(runtime, "_download", download)
    first = tmp_path / "launcher-first"
    second = tmp_path / "launcher-second"
    first.mkdir()
    second.mkdir()
    seen = {}

    def run(argv, **kwargs):
        seen.update(kwargs["env"])
        with patch.dict(os.environ, kwargs["env"], clear=True):
            runtime._fetch_dependencies({"profile_id": "linux-python-pytest-v1"},
                                        [pin], None, {}, first, time.monotonic() + 60,
                                        1024 * 1024, object())
            runtime._fetch_dependencies({"profile_id": "linux-python-pytest-v1"},
                                        [pin], None, {}, second, time.monotonic() + 60,
                                        1024 * 1024, object())
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(skeleton.subprocess, "run", run)

    assert skeleton._run_verify_command("python3 -V", cwd=tmp_path) == 0
    assert calls == [
        "https://pypi.org/pypi/pytest/1.0/json",
        "https://files.pythonhosted.org/packages/" + filename,
    ]
    assert seen.get(CACHE_ENV) == str(cache)
    assert "ENGY_API_KEY" not in seen
