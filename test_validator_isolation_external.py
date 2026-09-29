"""Caller-owned external observation of reference validator phase isolation."""

import json
from pathlib import Path

import ormas_acceptance
import pytest


OBSERVER = Path(__file__).with_name("observe_validator.py").read_text()


@pytest.mark.parametrize("scenario,expected", [
    ("untracked", "accept"),
    ("ignored", "accept"),
    ("tracked", "accept"),
    ("false_accept", "reject"),
    ("toolchain", "accept"),
    ("clean", "accept"),
    ("already_green", "reject"),
    ("immutable", "reject"),
])
def test_independent_validator_decision(scenario, expected):
    result = ormas_acceptance.run(
        ["/work/.venv/bin/python", "-c", OBSERVER, "/work", scenario],
        timeout_s=30,
    )
    assert result["returncode"] == 0, result["output"]
    observation = json.loads(result["output"])
    assert observation["processed"] and observation["idle"]
    assert observation["count"] == 1 and observation["signature_valid"]
    assert observation["source_clean"] and observation["source_head_unchanged"]
    assert observation["decision"] == expected, observation
