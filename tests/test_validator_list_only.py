"""`ormas-validator --list-only` prints current assignments and exits without reviewing."""
from __future__ import annotations

import pytest

from ormas_subnet import validator

SECRET = "ghs_synthetic_read_token_never_printed"


def _run(monkeypatch, tmp_path, capsys, assignments):
    calls = {"list": 0, "register": 0}

    class Client:
        def __init__(self, **_kwargs):
            pass

        def register(self, **_kwargs):
            calls["register"] += 1
            return {"validator_id": "val_cli"}

        def list_assignments(self):
            calls["list"] += 1
            return [dict(a) for a in assignments]

        def read_repository_credential(self, *_args, **_kwargs):
            raise AssertionError("list-only read a repository credential")

        def post_decision(self, *_args, **_kwargs):
            raise AssertionError("list-only posted a decision")

        def close(self):
            pass

    def no_review(*_args, **_kwargs):
        raise AssertionError("list-only reviewed an assignment")

    monkeypatch.setattr(validator, "OrmasValidatorClient", Client)
    monkeypatch.setattr(validator, "make_ed25519_signer",
                        lambda _key: (lambda _digest: "ab" * 64, "cd" * 32))
    monkeypatch.setattr(validator, "review_assignment", no_review)
    monkeypatch.setattr(validator.ValidatorDaemon, "run_once", no_review)
    monkeypatch.setattr(validator.ValidatorDaemon, "serve", no_review)
    monkeypatch.delenv("ORMAS_PROTECTED_EVIDENCE_SOURCE", raising=False)
    monkeypatch.setenv("VALIDATOR_TOKEN_TEST", "ormv_synthetic_cli_test")
    monkeypatch.setenv("VALIDATOR_KEY_TEST", "13" * 32)
    workdir = tmp_path / "work"
    try:
        status = validator.main([
            "--gateway", "http://127.0.0.1:9",
            "--token-env", "VALIDATOR_TOKEN_TEST",
            "--private-key-env", "VALIDATOR_KEY_TEST",
            "--workdir-root", str(workdir),
            "--list-only",
        ])
    except SystemExit as exc:
        raise AssertionError(f"validator main rejected --list-only (exit {exc.code})") from None
    out = capsys.readouterr().out
    assert SECRET not in out
    assert not workdir.exists(), "list-only created a review workdir"
    assert calls["list"] == 1
    return status, out.splitlines()


def test_list_only_prints_each_assignment(monkeypatch, tmp_path, capsys):
    status, lines = _run(monkeypatch, tmp_path, capsys, [
        {"assignment_id": "asgn_1", "job_id": "job_aaaaaaaaaaaa",
         "acceptance_contract": {"policy": {"timeout_s": 600}},
         "repo_credential": {"token": SECRET}},
        {"assignment_id": "asgn_2", "job_id": "job_bbbbbbbbbbbb",
         "acceptance_contract": {"policy": {"timeout_s": 45}}},
    ])
    assert status == 0
    assert lines == [
        "asgn_1 job=job_aaaaaaaaaaaa timeout_s=600",
        "asgn_2 job=job_bbbbbbbbbbbb timeout_s=45",
    ]


def test_list_only_marks_missing_fields(monkeypatch, tmp_path, capsys):
    status, lines = _run(monkeypatch, tmp_path, capsys, [{"assignment_id": "asgn_3"}])
    assert status == 0
    assert lines == ["asgn_3 job=- timeout_s=-"]


def test_list_only_when_idle(monkeypatch, tmp_path, capsys):
    status, lines = _run(monkeypatch, tmp_path, capsys, [])
    assert status == 0
    assert lines == ["no assignments"]


def test_help_mentions_list_only(capsys):
    with pytest.raises(SystemExit):
        validator.main(["--help"])
    assert "--list-only" in capsys.readouterr().out
