"""The repo-root requirements.lock must stay valid for the public pytest profile.

Outcomes jobs on this repo verify inside `linux-python-pytest-v1`, which installs
only what this lock names (offline, hash-checked). A lock the verifier refuses turns
every job on the repo into a setup failure, so parse it the way the verifier does.
"""
from __future__ import annotations

import re
from pathlib import Path

from ormas_subnet.verifier_runtime import dependency_spec

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    tomllib = None

REPO = Path(__file__).resolve().parents[1]
LOCK = REPO / "requirements.lock"


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _pins() -> list[dict]:
    pins, _ = dependency_spec("linux-python-pytest-v1", {"requirements.lock": LOCK.read_bytes()})
    return pins


def test_lock_parses_with_the_verifier_and_names_pytest():
    names = [_norm(p["name"]) for p in _pins()]
    assert "pytest" in names
    assert len(names) == len(set(names)), "duplicate pins"
    assert all(len(p["hashes"]) == 1 for p in _pins()), "one wheel hash per pin"


def _direct_dependencies() -> set[str]:
    text = (REPO / "pyproject.toml").read_text()
    if tomllib is not None:
        deps = tomllib.loads(text)["project"]["dependencies"]
    else:
        block = re.search(r"^dependencies = \[(.*?)^\]", text, re.M | re.S).group(1)
        deps = re.findall(r'^\s*"([^"]+)"', block, re.M)
    return {_norm(re.split(r"[<>=!~\[; ]", d, maxsplit=1)[0]) for d in deps}


def test_lock_covers_every_direct_dependency():
    locked = {_norm(p["name"]) for p in _pins()}
    missing = sorted(_direct_dependencies() - locked)
    assert not missing, f"direct dependencies absent from requirements.lock: {missing}"
