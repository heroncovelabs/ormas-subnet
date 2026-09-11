"""Test-session git identity.

The skeleton clones into fresh workdirs, and a fresh clone has no user.name /
user.email; CI runners have no global identity either. A real miner supplies
its own identity in its environment — the tests do the same here.
"""
from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _git_identity_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in {
        "GIT_AUTHOR_NAME": "ormas-subnet tests",
        "GIT_AUTHOR_EMAIL": "tests@ormas.invalid",
        "GIT_COMMITTER_NAME": "ormas-subnet tests",
        "GIT_COMMITTER_EMAIL": "tests@ormas.invalid",
    }.items():
        if not os.environ.get(key):
            monkeypatch.setenv(key, value)
