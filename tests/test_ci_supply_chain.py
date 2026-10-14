"""CI runs reviewed actions and a hash-pinned dependency set."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_ci_actions_are_commit_pinned_and_credentials_are_not_persisted() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    actions = re.findall(r"uses:\s*([^\s#]+)", workflow)
    assert actions
    assert all(re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", action) for action in actions)
    assert "permissions:\n  contents: read" in workflow
    assert "persist-credentials: false" in workflow


def test_ci_installs_only_the_hash_pinned_dependency_lock() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "--require-hashes -r requirements-ci.lock" in workflow
    assert "--no-deps --no-build-isolation -e ." in workflow
    assert "pip install --upgrade" not in workflow
    lock = (ROOT / "requirements-ci.lock").read_text()
    for package in ("cryptography", "httpx", "pytest", "ruff", "setuptools"):
        assert re.search(rf"^{package}==[^\s]+", lock, re.MULTILINE)


def test_ci_input_tracks_runtime_and_build_dependencies() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text()
    project = pyproject.split("[project]", 1)[1].split("[project.scripts]", 1)[0]
    runtime = set(re.findall(r'^\s*"([^\"]+)",?$', project, re.MULTILINE))
    build = re.search(r'^requires = \["([^\"]+)"\]$', pyproject, re.MULTILINE)
    assert build is not None
    expected = runtime | {build.group(1), "pytest", "ruff"}
    actual = {
        line.strip()
        for line in (ROOT / "requirements-ci.in").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert actual == expected
