"""Independent validators enforce the compiled public contract before execution."""
from __future__ import annotations

import copy
import hashlib
import json

import pytest

from ormas_subnet import outcomes_support as support
from ormas_subnet import validator as module


@pytest.fixture
def assignment(tmp_path):
    package = {'name': 'acceptance-fixture', 'version': '1.0.0', 'private': True}
    (tmp_path / 'package.json').write_text(json.dumps(package))
    (tmp_path / 'package-lock.json').write_text(json.dumps({
        'name': package['name'], 'version': '1.0.0', 'lockfileVersion': 3,
        'packages': {'': {'name': package['name'], 'version': '1.0.0'}},
    }))
    (tmp_path / 'value.test.cjs').write_text("require('node:test')('value',()=>{});\n")
    original = 'node --test value.test.cjs'
    environment, config, _ = support.prepare_environment(
        {'profile_id': 'linux-node-test-v1', 'lock_paths': ['package-lock.json', 'package.json'],
         'languages': ['javascript']}, cwd=str(tmp_path), command=original, timeout_s=30,
    )
    command = support.effective_command(config)
    command_sha = hashlib.sha256(command.encode()).hexdigest()
    requirements = support.execution_requirements(environment, config,
        languages=['javascript'], command_digest='sha256:' + command_sha)
    contract = {
        'schema_version': 'outcomes.validation-contract.v2', 'work_packet_sha256': 'f' * 64,
        'execution_environment': environment, 'execution_requirements': requirements,
        'verifier_profile': {'schema_version': 'outcomes.verifier-profile.v1',
            'profile_id': 'external-driver-v1', 'original_command': original,
            'generated_command_sha256': command_sha, 'timeout_s': 30.0,
            'max_files': config['limits']['max_files'], 'max_bytes': config['limits']['max_bytes'],
            'execution_mode': 'oci-v1', 'assertion_failure_exit_code': 86},
        'verify_base': {'schema_version': 'outcomes.base-preflight.v2', 'source': 'packet',
            'outcome': 'assertion_failed', 'verifier_digest': requirements['verifier_digest'],
            'environment_digest': requirements['environment_digest'], 'base_sha': 'a' * 40},
    }
    fields = module.canonical_evidence_fields(job_id='job_public', miner_id='miner:public',
        base_commit='a' * 40, result_commit='b' * 40, repo_url='https://github.com/example/public.git',
        verify_command=command, allowed_paths=['cli.cjs'],
        immutable_paths=['package.json', 'package-lock.json', 'value.test.cjs'],
        execution_contract=contract)
    return {'assignment_id': 'asgn_public', **fields,
            'evidence_digest_sha256': module.evidence_digest_hex(fields)}


def daemon(tmp_path):
    return module.ValidatorDaemon(None, module.ValidatorConfig(tmp_path / 'checkouts'), lambda digest: digest)


@pytest.mark.parametrize('base_exit,result_exit,expected', [
    (86, 0, 'accept'), (86, 86, 'reject'),
    (98, 0, 'error'), (124, 0, 'error'), (127, 0, 'error'), (1, 0, 'error'),
    (0, 0, 'error'), (86, 98, 'error'), (86, 124, 'error'), (86, 127, 'error'),
])
def test_public_decision_distinguishes_assertion_from_environment_failure(
        tmp_path, monkeypatch, assignment, base_exit, result_exit, expected):
    exits = iter([base_exit, result_exit])
    calls = []
    monkeypatch.setattr(module, '_run_git', lambda argv, **kw: 'cli.cjs\n' if argv[0] == 'diff' else '')
    def verify(command, **kwargs):
        assert command == assignment['verify_command']
        assert kwargs.get('path_prefix') is None
        calls.append(command)
        return next(exits)
    monkeypatch.setattr(module, '_run_verify_command', verify)
    assert daemon(tmp_path)._decide(assignment, tmp_path) == expected
    assert len(calls) == (2 if base_exit == 86 else 1)


def test_public_contract_never_provisions_an_ambient_host_toolchain(tmp_path, monkeypatch, assignment):
    def forbidden(*args, **kwargs):
        pytest.fail('public OCI assignment attempted legacy host provisioning')
    monkeypatch.setattr(module, 'provision_toolchain', forbidden)
    monkeypatch.setattr(module, '_run_git', lambda argv, **kw: 'cli.cjs\n' if argv[0] == 'diff' else '')
    exits = iter([86, 0])
    monkeypatch.setattr(module, '_run_verify_command', lambda *args, **kw: next(exits))
    assert daemon(tmp_path)._decide(assignment, tmp_path) == 'accept'


@pytest.mark.parametrize('field', ['verify_command', 'environment', 'base', 'compiler', 'contract_shape'])
def test_invalid_public_contract_never_executes(tmp_path, monkeypatch, assignment, field):
    value = copy.deepcopy(assignment)
    if field == 'verify_command':
        value['verify_command'] = 'python3 -c "raise SystemExit(0)"'
    elif field == 'environment':
        value['execution_contract']['execution_environment']['profile']['limits']['cpus'] = 99
    elif field == 'base':
        value['base_commit'] = 'c' * 40
    elif field == 'compiler':
        value['execution_contract']['verifier_profile']['generated_command_sha256'] = '0' * 64
    else:
        value['execution_contract']['unbound_extra'] = 'forbidden'
    def forbidden(*args, **kwargs):
        pytest.fail('invalid contract reached host provisioning, Git or executable')
    for symbol in ('provision_toolchain', '_run_git', '_run_verify_command'):
        monkeypatch.setattr(module, symbol, forbidden)
    assert daemon(tmp_path)._decide(value, tmp_path) == 'error'


@pytest.mark.parametrize('changed', ['value.test.cjs', 'outside.cjs', 'package-lock.json'])
def test_public_scope_violation_refuses_before_verification(tmp_path, monkeypatch, assignment, changed):
    monkeypatch.setattr(module, '_run_git', lambda argv, **kw: changed + '\n' if argv[0] == 'diff' else '')
    def forbidden(*args, **kwargs):
        pytest.fail('out-of-scope candidate was executed')
    monkeypatch.setattr(module, '_run_verify_command', forbidden)
    assert daemon(tmp_path)._decide(assignment, tmp_path) == 'reject'


def test_run_once_checks_contract_and_evidence_before_cloning(tmp_path, monkeypatch, assignment):
    class Client:
        def list_assignments(self): return [assignment]
        def post_decision(self, assignment_id, **kwargs): self.result = kwargs
    client = Client()
    instance = module.ValidatorDaemon(client, module.ValidatorConfig(tmp_path), lambda digest: digest)
    assignment['verify_command'] = 'python3 -c "raise SystemExit(0)"'
    def forbidden(*args, **kwargs):
        pytest.fail('unvalidated public assignment reached repository credential/clone')
    monkeypatch.setattr(instance, '_clone', forbidden)
    assert instance.run_once()
    assert client.result['decision'] == 'error'
