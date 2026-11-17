"""Public result-phase verifier failures and hangs are counted rejects."""
from __future__ import annotations

import pathlib
import subprocess

from ormas_subnet import skeleton
from ormas_subnet import validator as module

from tests.test_validator_public_execution import assignment


def test_public_result_exit_and_verify_timeout_return_reject(tmp_path, monkeypatch, assignment):
    instance = module.ValidatorDaemon(
        None, module.ValidatorConfig(tmp_path / 'checkouts'), lambda digest: digest,
    )
    monkeypatch.setattr(
        module, '_run_git',
        lambda argv, **kw: 'cli.cjs\0' if argv[0] == 'diff' else '',
    )
    monkeypatch.setattr(
        skeleton, 'trusted_executable',
        lambda name: pathlib.Path('/usr/bin/python3'),
    )
    recorded = []
    exits = iter([86, 0])

    def run(*args, **kwargs):
        recorded.append(kwargs)
        return subprocess.CompletedProcess(args, next(exits))

    monkeypatch.setattr(skeleton.subprocess, 'run', run)
    assert instance._decide(assignment, tmp_path) == 'accept'
    assert [mapping.get('timeout') for mapping in recorded] == [65.0, 65.0]

    result_exits = iter([86, 98])
    monkeypatch.setattr(module, '_run_verify_command', lambda command, **kwargs: next(result_exits))
    assert instance._decide(assignment, tmp_path) == 'reject'

    hang_calls = {'n': 0}

    def verify_hang(command, **kwargs):
        hang_calls['n'] += 1
        if hang_calls['n'] == 1:
            return 86
        raise subprocess.TimeoutExpired('verify', 1)

    monkeypatch.setattr(module, '_run_verify_command', verify_hang)
    assert instance._decide(assignment, tmp_path) == 'reject'

    # Controls that already hold: a reproduced base plus a clean or failing result.
    for result_exit, expected in ((0, 'accept'), (86, 'reject')):
        pair = iter([86, result_exit])
        monkeypatch.setattr(module, '_run_verify_command', lambda command, **kwargs: next(pair))
        assert instance._decide(assignment, tmp_path) == expected

    base_calls = []

    def verify_base_error(command, **kwargs):
        base_calls.append(command)
        return 1

    monkeypatch.setattr(module, '_run_verify_command', verify_base_error)
    assert instance._decide(assignment, tmp_path) == 'error'
    assert len(base_calls) == 1

    base_timeout_calls = []

    def verify_base_timeout(command, **kwargs):
        base_timeout_calls.append(command)
        raise subprocess.TimeoutExpired('verify', 1)

    monkeypatch.setattr(module, '_run_verify_command', verify_base_timeout)
    assert instance._decide(assignment, tmp_path) == 'error'
    assert len(base_timeout_calls) == 1
