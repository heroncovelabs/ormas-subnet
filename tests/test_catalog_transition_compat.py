"""Stage-one compiler compatibility without changing the emitted catalog."""
from __future__ import annotations

import base64
import gzip
import hashlib
import json
import shlex
from pathlib import Path

import pytest

from ormas_subnet import outcomes_support as support


PUBLIC_V4_CATALOG = (
    "sha256:398f6c9fca4744e9414171eccc39401701d76a5f22b31e7ebcd6d10fd169889e"
)
PUBLIC_V4_COMPILER = (
    "2ba990540ac8653f130efe5ed132a0dfdca6048db18386352719d3a317ae2c00"
)
PUBLIC_V6_CATALOG = (
    "sha256:060d5d8b80cc47d050b55f169644d68f33ede3a0cda5cc588a008b805dfc2551"
)
PUBLIC_V6_COMPILER = (
    "bffacd5331642f9515f21ac8fd0ac634c24ea7f1f4ac1e50c655617c1c63124f"
)
PRIVATE_V3_CATALOG = (
    "sha256:303d916bed2312ced5649a23df326996c91999487b5bea122641797a96e92f30"
)
PRIVATE_V2_CATALOG = (
    "sha256:9251253a5dc1c1a4fd5947f0b1190a27393bf499f30c325d6345a0db98b946a9"
)


def _packet(tmp_path: Path) -> tuple[dict, dict]:
    package = {"name": "catalog-transition", "version": "1.0.0", "private": True}
    (tmp_path / "package.json").write_text(json.dumps(package))
    (tmp_path / "package-lock.json").write_text(json.dumps({
        "name": package["name"],
        "version": package["version"],
        "lockfileVersion": 3,
        "packages": {"": package},
    }))
    (tmp_path / "value.test.cjs").write_text(
        "require('node:test')('catalog transition', () => {});\n"
    )
    original = "node --test value.test.cjs"
    environment, config, _ = support.prepare_environment(
        {
            "profile_id": "linux-node-test-v1",
            "lock_paths": ["package-lock.json", "package.json"],
            "languages": ["javascript"],
        },
        cwd=str(tmp_path),
        command=original,
        timeout_s=30,
    )
    command = support.effective_command(config)
    command_sha = hashlib.sha256(command.encode()).hexdigest()
    requirements = support.execution_requirements(
        environment,
        config,
        languages=["javascript"],
        command_digest="sha256:" + command_sha,
    )
    packet = {
        "execution_environment": environment,
        "execution_requirements": requirements,
        "execution_policy": {
            "allowed_paths": ["result.cjs"],
            "immutable_paths": ["package.json", "package-lock.json", "value.test.cjs"],
            "repo_base_sha": "a" * 40,
        },
        "task_features": {"languages": ["javascript"]},
        "verification_command": command,
        "verifier_profile": {
            "schema_version": "outcomes.verifier-profile.v1",
            "profile_id": "external-driver-v1",
            "original_command": original,
            "generated_command_sha256": command_sha,
            "timeout_s": 30,
            "max_files": config["limits"]["max_files"],
            "max_bytes": config["limits"]["max_bytes"],
            "execution_mode": "oci-v1",
            "assertion_failure_exit_code": 86,
        },
        "verify_base": {
            "schema_version": "outcomes.base-preflight.v2",
            "source": "packet",
            "outcome": "assertion_failed",
            "verifier_digest": requirements["verifier_digest"],
            "environment_digest": requirements["environment_digest"],
            "base_sha": "a" * 40,
        },
    }
    return packet, config


def _retarget_to_v6(packet: dict) -> dict:
    catalog = support.load_support_catalog(PUBLIC_V6_CATALOG)
    environment = packet["execution_environment"]
    profile_id = environment["profile"]["profile_id"]
    environment["catalog_digest"] = PUBLIC_V6_CATALOG
    environment["profile"] = next(
        profile for profile in catalog["profiles"] if profile["profile_id"] == profile_id
    )
    source = gzip.decompress(
        Path(__file__).with_name("fixtures").joinpath(
            "verifier_runtime_2026_10_14_6.py.gz"
        ).read_bytes()
    ).decode()
    assert hashlib.sha256(source.encode()).hexdigest() == PUBLIC_V6_COMPILER
    original = packet["verifier_profile"]["original_command"]
    timeout_s = packet["execution_requirements"]["limits"]["timeout_s"]
    config = support.runtime_config(environment, command=original, timeout_s=timeout_s)
    encoded = base64.b64encode(
        json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
    ).decode()
    command = shlex.join(["python3", "-I", "-S", "-c", source, encoded])
    command_sha = hashlib.sha256(command.encode()).hexdigest()
    requirements = support.execution_requirements(
        environment,
        config,
        languages=["javascript"],
        command_digest="sha256:" + command_sha,
    )
    packet["verification_command"] = command
    packet["execution_requirements"] = requirements
    packet["verifier_profile"].update(
        generated_command_sha256=command_sha,
        timeout_s=config["limits"]["timeout_s"],
        max_files=config["limits"]["max_files"],
        max_bytes=config["limits"]["max_bytes"],
    )
    packet["verify_base"].update(
        verifier_digest=requirements["verifier_digest"],
        environment_digest=requirements["environment_digest"],
    )
    return packet


def test_bridge_keeps_current_public_v4_as_the_emitted_catalog(tmp_path: Path) -> None:
    current = support.load_support_catalog()
    source = Path(support.__file__).with_name("verifier_runtime.py").read_text()
    packet, _ = _packet(tmp_path)

    assert current["revision"] == "2026-10-14.4"
    assert support.digest(current) == PUBLIC_V4_CATALOG
    assert hashlib.sha256(source.encode()).hexdigest() == PUBLIC_V4_COMPILER
    assert packet["execution_environment"]["catalog_digest"] == PUBLIC_V4_CATALOG
    assert support.validate_public_execution_packet(packet) == (
        packet["execution_requirements"]
    )


def test_bridge_accepts_frozen_v6_packet_without_emitting_v6(tmp_path: Path) -> None:
    packet, _ = _packet(tmp_path)
    packet = _retarget_to_v6(packet)

    assert support._ACTIVE_VERIFIER_COMPILERS == {
        PUBLIC_V4_COMPILER,
        PUBLIC_V6_COMPILER,
    }
    assert support.validate_public_execution_packet(packet) == (
        packet["execution_requirements"]
    )
    assert support.digest(support.load_support_catalog()) == PUBLIC_V4_CATALOG


def test_public_bridge_does_not_import_private_gateway_catalog() -> None:
    with pytest.raises(ValueError, match="unsupported_catalog_version"):
        support.load_support_catalog(PRIVATE_V3_CATALOG)

    private_v2 = support.load_support_catalog(PRIVATE_V2_CATALOG)
    with pytest.raises(ValueError, match="verifier_compiler_revoked"):
        support._active_compiler(private_v2)

    project = (Path(__file__).parents[1] / "pyproject.toml").read_text()
    project_section = project.split("[project]", 1)[1].split("[", 1)[0]
    assert '\nname = "ormas-subnet"\n' in project_section
