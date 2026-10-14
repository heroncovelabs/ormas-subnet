"""Known status-only acceptance drivers fail closed at every execution boundary."""
from __future__ import annotations

import hashlib
import os
import runpy
import subprocess
import sys
import types
from pathlib import Path

import pytest

from ormas_subnet import outcomes_support as support
from ormas_subnet import verifier_runtime as runtime


ROOT = Path(__file__).resolve().parents[1]
DRIVER_VARIANTS = {
    "tests/acceptance/claim_refusal_backoff_external.py": {
        "545411aeba3d5ac8db574a733ed7d2cf21bec012b19a73a8de5c6a6cbbe6f442",
        "e032021de2cda990e4a8656376932efd2c9e48c565aab8a008688a9acb2f2b88",
    },
    "tests/acceptance/validator_list_only_external.py": {
        "af06a6db0a155f703848d916f2fef53c22fec1ac2b3147fd5f1bf438744d404a",
        "36e853b3f76fbc41550404d731564091a7659d1a98f2d6871a30fc46a043b361",
    },
}
HISTORICAL_DRIVER_HEADINGS = {
    "tests/acceptance/claim_refusal_backoff_external.py":
        b'"""Ormas external acceptance for card f18d' b'030c.\n',
    "tests/acceptance/validator_list_only_external.py":
        b'"""Ormas external acceptance for card 5a96' b'0b1f.\n',
}
CURRENT_DRIVER_DIGESTS = {
    path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
    for path in DRIVER_VARIANTS
}
ALL_REVOKED_DIGESTS = sorted({
    digest for variants in DRIVER_VARIANTS.values() for digest in variants
})


def _driver_fixture(path, expected_digest):
    current = (ROOT / path).read_bytes()
    if hashlib.sha256(current).hexdigest() == expected_digest:
        return current
    historical = HISTORICAL_DRIVER_HEADINGS[path] + current.split(b"\n", 1)[1]
    assert hashlib.sha256(historical).hexdigest() == expected_digest
    return historical


def _complete_packet(environment, config, original_command):
    command = support.effective_command(config)
    command_sha = hashlib.sha256(command.encode()).hexdigest()
    requirements = support.execution_requirements(
        environment,
        config,
        languages=["python"],
        command_digest="sha256:" + command_sha,
    )
    base_sha = "a" * 40
    immutable = sorted([*environment["lock_files"], *environment["acceptance_files"]])
    return {
        "execution_environment": environment,
        "execution_requirements": requirements,
        "execution_policy": {
            "repo_base_sha": base_sha,
            "allowed_paths": ["ormas_subnet/**"],
            "immutable_paths": immutable,
        },
        "verifier_profile": {
            "schema_version": "outcomes.verifier-profile.v1",
            "profile_id": "external-driver-v1",
            "original_command": original_command,
            "generated_command_sha256": command_sha,
            "timeout_s": config["limits"]["timeout_s"],
            "max_files": config["limits"]["max_files"],
            "max_bytes": config["limits"]["max_bytes"],
            "execution_mode": "oci-v1",
            "assertion_failure_exit_code": 86,
        },
        "verification_command": command,
        "task_features": {"languages": ["python"]},
        "verify_base": {
            "schema_version": "outcomes.base-preflight.v3",
            "source": "deferred",
            "outcome": "not_run",
            "reason": "client_preflight_deferred",
            "verifier_digest": requirements["verifier_digest"],
            "environment_digest": requirements["environment_digest"],
            "base_sha": base_sha,
        },
    }


def _packet_for_digest(driver_digest):
    catalog = support.load_support_catalog()
    profile = next(
        value for value in catalog["profiles"]
        if value["profile_id"] == "linux-python-pytest-v1"
    )
    path = "tests/acceptance/external.py"
    environment = {
        "schema_version": "outcomes.execution-environment.v1",
        "catalog_digest": support.digest(catalog),
        "profile": profile,
        "lock_files": {"requirements.lock": "0" * 64},
        "acceptance_files": {path: driver_digest},
    }
    original = f"python3 -m pytest -q {path}"
    config = support.runtime_config(
        environment, command=original, timeout_s=60,
    )
    return _complete_packet(environment, config, original), config


def test_revocation_inventory_covers_both_historical_source_variants():
    assert runtime.REVOKED_ACCEPTANCE_DRIVER_SHA256 == frozenset(ALL_REVOKED_DIGESTS)
    for path, digest in CURRENT_DRIVER_DIGESTS.items():
        assert digest in DRIVER_VARIANTS[path]


@pytest.mark.parametrize(
    ("driver_path", "test_functions", "candidate_tests"),
    [
        (
            "tests/acceptance/claim_refusal_backoff_external.py",
            [
                "test_claim_refusal_backoff_passes_in_candidate",
                "test_run_forever_resilience_stays_green_in_candidate",
            ],
            ["tests/test_claim_refusal_backoff.py", "tests/test_run_forever_resilience.py"],
        ),
        (
            "tests/acceptance/validator_list_only_external.py",
            ["test_validator_list_only_passes_in_candidate"],
            ["tests/test_validator_list_only.py"],
        ),
    ],
)
def test_revoked_card_false_passes_when_candidate_exits_zero_during_collection(
    tmp_path, monkeypatch, driver_path, test_functions, candidate_tests,
):
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    for index, path in enumerate(candidate_tests):
        target = candidate / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            "import os\n"
            "os._exit(0)\n"
            "from pathlib import Path\n"
            f"Path('required-assertion-{index}-ran').write_text('yes')\n"
            "def test_required_behavior():\n"
            "    raise AssertionError('the required assertion must run')\n"
        )

    observations = []

    def candidate_run(argv, timeout_s=30):
        process = subprocess.run(
            [sys.executable, *argv[1:]],
            cwd=candidate,
            env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout_s,
            check=False,
        )
        observations.append(process.returncode)
        return {
            "returncode": process.returncode,
            "output": process.stdout.decode("utf-8", "replace"),
        }

    monkeypatch.setitem(
        sys.modules, "ormas_acceptance", types.SimpleNamespace(run=candidate_run),
    )
    card = runpy.run_path(str(ROOT / driver_path))
    for name in test_functions:
        card[name]()

    assert observations == [0] * len(candidate_tests)
    assert not list(candidate.glob("required-assertion-*-ran"))


@pytest.mark.parametrize(
    ("path", "driver_digest"),
    [
        (path, driver_digest)
        for path, variants in sorted(DRIVER_VARIANTS.items())
        for driver_digest in sorted(variants)
    ],
)
def test_unsafe_card_is_refused_before_host_git_lookup(
    tmp_path, monkeypatch, path, driver_digest,
):
    target = tmp_path / path
    target.parent.mkdir(parents=True)
    target.write_bytes(_driver_fixture(path, driver_digest))
    (tmp_path / "requirements.lock").write_bytes((ROOT / "requirements.lock").read_bytes())
    host_git_lookups = []

    def unexpected_host_git(*args, **kwargs):
        host_git_lookups.append((args, kwargs))
        raise AssertionError("revoked acceptance material reached host Git lookup")

    monkeypatch.setattr(support, "public_git", unexpected_host_git)
    with pytest.raises(ValueError, match="acceptance_driver_revoked"):
        support.prepare_environment(
            {
                "profile_id": "linux-python-pytest-v1",
                "lock_paths": ["requirements.lock"],
                "languages": ["python"],
                "acceptance_paths": [path],
            },
            cwd=str(tmp_path),
            command=f"python3 -m pytest -q {path}",
            timeout_s=60,
        )
    assert host_git_lookups == []


@pytest.mark.parametrize("driver_digest", ALL_REVOKED_DIGESTS)
def test_frozen_packet_with_revoked_driver_fails_validation(driver_digest):
    packet, _ = _packet_for_digest(driver_digest)
    with pytest.raises(ValueError, match="acceptance_driver_revoked"):
        support.validate_public_execution_packet(packet)


@pytest.mark.parametrize("driver_digest", ALL_REVOKED_DIGESTS)
def test_runtime_refuses_revoked_driver_before_candidate_execution(
    tmp_path, monkeypatch, driver_digest,
):
    _, config = _packet_for_digest(driver_digest)
    host_tool_lookups = []

    def unexpected_host_tool(name):
        host_tool_lookups.append(name)
        pytest.fail("revoked acceptance material reached runtime setup")

    monkeypatch.setattr(runtime, "_trusted_executable", unexpected_host_tool)
    with pytest.raises(runtime.RuntimeRefusal, match="acceptance_driver_revoked"):
        runtime.run_verifier(config, cwd=tmp_path)
    assert host_tool_lookups == []


def test_unrelated_external_driver_still_prepares_and_validates(tmp_path, monkeypatch):
    path = "tests/acceptance/safe_external.py"
    target = tmp_path / path
    target.parent.mkdir(parents=True)
    target.write_text(
        "from ormas_acceptance import run\n\n"
        "def test_cli_value():\n"
        "    result = run(['python3', 'cli.py'])\n"
        "    assert result['returncode'] == 0\n"
        "    assert result['output'].strip() == 'expected'\n"
    )
    (tmp_path / "requirements.lock").write_bytes((ROOT / "requirements.lock").read_bytes())
    host_git_lookups = []

    def repository_without_gitlinks(args, *, cwd):
        host_git_lookups.append((args, cwd))
        return b""

    monkeypatch.setattr(support, "public_git", repository_without_gitlinks)
    original = f"python3 -m pytest -q {path}"

    environment, config, _ = support.prepare_environment(
        {
            "profile_id": "linux-python-pytest-v1",
            "lock_paths": ["requirements.lock"],
            "languages": ["python"],
            "acceptance_paths": [path],
        },
        cwd=str(tmp_path),
        command=original,
        timeout_s=60,
    )
    assert not runtime.REVOKED_ACCEPTANCE_DRIVER_SHA256.intersection(
        environment["acceptance_files"].values()
    )
    assert host_git_lookups == [
        (["ls-tree", "-rz", "--full-tree", "HEAD"], tmp_path.resolve())
    ]
    packet = _complete_packet(environment, config, original)
    assert support.validate_public_execution_packet(packet) == packet["execution_requirements"]
    runtime._validate_config(config)
