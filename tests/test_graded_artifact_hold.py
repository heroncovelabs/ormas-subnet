"""Graded delivery enters custody, never the client's repository."""
from __future__ import annotations

import hashlib
import io
import json
import shutil
import subprocess
from contextlib import contextmanager

import httpx
import pytest

from ormas_subnet import outcomes_support as support
from ormas_subnet import skeleton as miner_module
from ormas_subnet import validator
from ormas_subnet._recovery import PublicRecovery
from ormas_subnet.client import OrmasMinerClient
from ormas_subnet.protocol import TaskDraft, TaskLease
from ormas_subnet.skeleton import MinerConfig, MinerSkeleton, SolveResult


def _git(root, *args):
    return subprocess.check_output([
        'git', '-c', 'user.name=Ormas tests', '-c', 'user.email=tests@ormas.invalid', *args,
    ], cwd=root).decode().strip()


@pytest.fixture
def run(tmp_path, monkeypatch):
    repo = tmp_path / 'source'
    repo.mkdir()
    _git(repo, 'init', '--quiet')
    (repo / 'value.txt').write_text('base\n')
    (repo / 'notes.md').write_text('base document\n')
    _git(repo, 'add', '.')
    _git(repo, 'commit', '--quiet', '-m', 'base')
    base = _git(repo, 'rev-parse', 'HEAD')
    packet = {
        'acceptance_level': 'graded',
        'graded_contract': {'base_sha': base, 'immutable_paths': ['tests/']},
        'execution_policy': {'repo_base_sha': base, 'allowed_paths': ['value.txt', 'notes.md'],
                             'immutable_paths': ['tests/']},
        'delivery_limits': {'max_bytes': 4096, 'max_files': 2},
    }
    lease = TaskLease(
        lease_id='lease-held', task_id='task-held', expires_at='2099-01-01T00:00:00Z',
        selected_cell='task:code', provider_pin='unset', fallback_policy='unset',
        hold_ref='unset', now='2026-10-28T00:00:00Z', outcome_price_usd=1.0,
    )
    draft = TaskDraft(
        task_id=lease.task_id, runner_id='miner-held', repo_id='repo-held', base_commit=base,
        brief='Update code and document', verify_command='test -f value.txt',
        allowed_paths=['value.txt', 'notes.md'], budget_usd=1.0, work_packet=packet,
        work_packet_sha256='a' * 64, repo_url='https://github.com/acme/demo.git',
        attempt=1, parent_job_id='', repair_findings=[],
    )
    state = {'requests': [], 'uploads': [], 'completions': [], 'git': [],
             'credentials': [], 'solves': 0, 'fault': None, 'ack': 'held',
             'refusal': None, 'heartbeat_refusal': None, 'completion_refusal': None, 'idle': False}

    def gateway(request):
        state['requests'].append(request)
        path = request.url.path
        if path.endswith('/leases'):
            if state['idle']:
                return httpx.Response(204)
            return httpx.Response(200, json={'lease': lease.to_wire(), 'draft': draft.to_wire()})
        if path.endswith('/heartbeat'):
            if state.pop('heartbeat_fault', None) == 'server':
                return httpx.Response(503, json={'error': {'type': 'service_unavailable',
                                                         'message': 'store_unavailable'}})
            if state['heartbeat_refusal'] and json.loads(request.read())['renew']:
                return httpx.Response(409, json={'error': {'type': 'lease_lost',
                                                         'message': 'lease_lost'}})
            return httpx.Response(200, json={'ok': True})
        if path.endswith('/graded-artifact'):
            raw = request.read()
            state['uploads'].append(raw)
            fault, state['fault'] = state['fault'], None
            if fault == 'transport':
                raise httpx.ReadError('lost custody acknowledgement', request=request)
            if fault == 'restart':
                raise KeyboardInterrupt('process lost after upload')
            if fault == 'server':
                return httpx.Response(503, json={'error': {'type': 'service_unavailable',
                                                         'message': 'store_unavailable'}})
            if state['refusal']:
                status, code = state['refusal']
                return httpx.Response(status, json={'error': {'type': code, 'message': code}})
            digest = hashlib.sha256(raw).hexdigest()
            return httpx.Response(200, json={
                'status': 'published' if state['ack'] == 'status' else 'held',
                'artifact_digest': '0' * 64 if state['ack'] == 'digest' else digest,
            })
        if path.endswith('/complete'):
            state['completions'].append(json.loads(request.read()))
            if state['completion_refusal'] == 'restart':
                raise KeyboardInterrupt('process lost after failed completion')
            if state['completion_refusal']:
                return httpx.Response(400, json={'error': {'type': 'invalid_request_error',
                                                         'message': 'completion_refused'}})
            state['idle'] = True
            return httpx.Response(200, json={'status': 'done', 'receipt': {
                'receipt_id': 'receipt-held', 'settlement': 'pending_acceptance',
            }})
        return httpx.Response(400, json={'error': {'type': 'invalid_request_error',
                                                 'message': 'unexpected route'}})

    client = OrmasMinerClient('https://gateway.invalid', 'ormr_fixture', device_nonce='device-held',
                             http_client=httpx.Client(transport=httpx.MockTransport(gateway),
                                                     base_url='https://gateway.invalid',
                                                     headers={'Authorization': 'Bearer ormr_fixture'}))
    config = MinerConfig('miner-held', 'test', 'linux', 1, ('task:code',), tmp_path / 'work',
                         'repo-held', str(repo), push_remote=None)

    def solve(_draft, workdir):
        state['solves'] += 1
        (workdir / 'value.txt').write_bytes(b'candidate\x00code\n')
        (workdir / 'notes.md').write_text('candidate document\n')
        _git(workdir, 'add', '.')
        _git(workdir, 'commit', '--quiet', '-m', 'candidate')
        return SolveResult(_git(workdir, 'rev-parse', 'HEAD'), ('notes.md', 'value.txt'))

    def public_git(args, *, cwd):
        state['git'].append(list(args))
        assert args[0] not in {'fetch', 'push'}, 'custody must never fetch or push a result'
        args = [str(repo) if arg == draft.repo_url else arg for arg in args]
        return subprocess.check_output(['git', *args], cwd=cwd)

    original_git = miner_module._run_git

    def tracked_git(args, **kwargs):
        state['git'].append(list(args))
        assert args[0] not in {'fetch', 'push'}
        return original_git(args, **kwargs)

    original_credentials = miner_module._credential_git_env

    @contextmanager
    def credentials(value):
        if value is not None:
            state['credentials'].append(value)
        with original_credentials(value) as env:
            yield env

    monkeypatch.setattr(miner_module, '_public_git', public_git)
    monkeypatch.setattr(miner_module, '_run_git', tracked_git)
    monkeypatch.setattr(miner_module, '_credential_git_env', credentials)
    monkeypatch.setattr(miner_module.time, 'sleep', lambda _delay: None)
    return MinerSkeleton(client, config, solve), state, draft


def _assert_held(state):
    assert state['uploads'], 'Graded delivery must upload to private custody'
    assert len(state['completions']) == 1
    body = state['completions'][0]
    assert body['terminal']['verification_state'] == 'verified'
    assert body['terminal']['result_ref'] is None
    assert body['terminal']['result_commit'] == ''
    assert 'pr_url' not in body and 'pr_error' not in body
    assert state['credentials'] == []
    assert not any(args[0] in {'fetch', 'push'} for args in state['git'])
    assert not any(request.url.path.endswith(('/publication', '/repo-credential'))
                   for request in state['requests'])


def test_held_acknowledgement(run):
    miner, state, _ = run
    assert miner.run_once() is True
    _assert_held(state)
    assert len(state['uploads']) == 1
    request = next(r for r in state['requests'] if r.url.path.endswith('/graded-artifact'))
    assert request.url.path == '/api/runner/v1/leases/task-held/graded-artifact'
    assert request.headers['Authorization'] == 'Bearer ormr_fixture'
    assert request.headers['X-Ormas-Runner-Device'] == 'device-held'
    assert request.headers['X-Ormas-Runner-Id'] == 'miner-held'
    assert request.headers['X-Ormas-Lease-Token'] == 'lease-held'
    assert request.headers['Content-Type'] == 'application/vnd.ormas.public-artifact.v1'
    assert int(request.headers['Content-Length']) == len(state['uploads'][0])
    assert b'candidate document' in state['uploads'][0]


@pytest.mark.parametrize('ack', ['digest', 'status'])
def test_mismatched_acknowledgement_refuses(run, ack):
    miner, state, _ = run
    state['ack'] = ack
    miner.run_once()
    assert state['completions'] == [], 'a mismatched hold must never complete a delivery'
    assert len(state['uploads']) == 1
    store = PublicRecovery(miner.config.workdir_root, miner.client.base_url, miner.config.runner_id)
    assert store.pending()['stage'] == 'artifact'


@pytest.mark.parametrize('fault', ['transport', 'server', 'restart'])
def test_lost_acknowledgement_replays_identical_bytes(run, fault):
    miner, state, _ = run
    state['fault'] = fault
    if fault == 'restart':
        interrupted = False
        try:
            miner.run_once()
        except KeyboardInterrupt:
            interrupted = True
        assert interrupted, 'upload must precede completion and survive process loss'
        shutil.rmtree(miner.config.workdir_root / 'task-held')
        miner = MinerSkeleton(miner.client, miner.config, miner.solve_fn)
    else:
        miner.run_once()
        if state['completions']:
            _assert_held(state)
            assert len(state['uploads']) == 2
            assert state['uploads'][0] == state['uploads'][1]
            assert state['solves'] == 1
            return
    miner.run_once()
    _assert_held(state)
    assert len(state['uploads']) == 2
    assert state['uploads'][0] == state['uploads'][1]
    assert state['solves'] == 1
    assert sum(r.url.path.endswith('/leases') for r in state['requests']) == 1


def test_verified_lease_calls_unchanged(run):
    miner, state, draft = run
    object.__setattr__(draft, 'work_packet', {})
    assert miner.run_once() is True
    assert state['uploads'] == []
    assert [r.url.path for r in state['requests']] == [
        '/api/runner/v1/leases', '/api/runner/v1/leases/task-held/heartbeat',
        '/api/runner/v1/leases/task-held/complete',
    ]
    terminal = state['completions'][0]['terminal']
    assert terminal['verification_state'] == 'verified'
    assert terminal['result_ref'] == 'local:ormas/job/task-held'
    assert len(terminal['result_commit']) == 40


def test_graded_clone_never_reads_credentials(run):
    miner, state, draft = run
    object.__setattr__(draft, 'repo_credential', {'kind': 'ssh_deploy_key', 'private_key': 'fixture'})
    miner.run_once()
    assert state['credentials'] == [], 'Graded must not enter the credential/push path'
    _assert_held(state)


@pytest.mark.parametrize('signed_contract', [True, False])
@pytest.mark.parametrize('graded_key', ['graded_contract', 'graded_artifact_digest',
                                       'graded_tree_digest', 'graded_delivery_limits'])
def test_validator_graded_assignment_never_cloned_or_signed(
    tmp_path, monkeypatch, signed_contract, graded_key,
):
    fields = validator.canonical_evidence_fields(
        job_id='job-held', miner_id='miner-held', base_commit='a' * 40, result_commit='b' * 40,
        repo_url='https://github.com/acme/demo.git', verify_command='test -f value.txt',
        allowed_paths=['value.txt'], immutable_paths=['tests/'],
    )
    legacy_digest = validator.evidence_digest_hex(fields)
    # Actual gateway projection: top-level graded_contract, no acceptance_level.
    fields[graded_key] = ({'base_sha': 'a' * 40} if graded_key == 'graded_contract' else
                          {'max_bytes': 4096, 'max_files': 2} if graded_key == 'graded_delivery_limits'
                          else 'c' * 64)
    assignment = {**fields, 'assignment_id': 'asgn_held', 'evidence_digest_sha256': (
        validator.evidence_digest_hex(fields) if signed_contract else legacy_digest)}
    effects = []

    class Client:
        def list_assignments(self):
            return [assignment]

        def post_decision(self, *args, **kwargs):
            effects.append('post')

    def git(*args, **kwargs):
        effects.append('git')
        return ''

    monkeypatch.setattr(validator, '_run_git', git)
    daemon = validator.ValidatorDaemon(Client(), validator.ValidatorConfig(tmp_path),
                                       lambda _digest: effects.append('sign') or 'fixture')
    assert daemon.run_once() is False, 'Graded is not work for the public acceptance seat'
    assert effects == [], 'not even an error decision may be signed or posted'
    assert daemon._preface(assignment) == ('skip', None)
    assert validator.review_assignment(str(tmp_path), assignment) is None
    assert effects == []


@pytest.mark.parametrize('limits', [None, {}, {'max_bytes': True, 'max_files': 2},
                                    {'max_bytes': 1, 'max_files': 2},
                                    {'max_bytes': 4096, 'max_files': 1}])
def test_graded_builder_requires_packet_limits(run, limits):
    miner, _, draft = run
    builder = getattr(support, 'build_graded_artifact', None)
    assert callable(builder), 'public SDK needs a Graded raw-file builder'
    workdir = miner._clone_and_checkout(draft)
    result = miner.solve_fn(draft, workdir)
    packet = {**draft.work_packet, 'delivery_limits': limits}
    with pytest.raises(ValueError):
        builder(packet, workdir, result.result_commit, io.BytesIO())


@pytest.mark.parametrize('allowed_paths', [None, ['value.txt']], ids=['absent', 'restrictive'])
def test_graded_builder_uses_contract_scope(run, allowed_paths):
    miner, _, draft = run
    workdir = miner._clone_and_checkout(draft)
    result = miner.solve_fn(draft, workdir)
    policy = dict(draft.work_packet['execution_policy'])
    policy.pop('allowed_paths')
    if allowed_paths is not None:
        policy['allowed_paths'] = allowed_paths
    target = io.BytesIO()
    length, digest, _ = support.build_graded_artifact(
        {**draft.work_packet, 'execution_policy': policy}, workdir, result.result_commit, target)
    raw = target.getvalue()
    assert length == len(raw) and digest == hashlib.sha256(raw).hexdigest()
    assert b'candidate document' in raw


def test_raw_builder_keeps_allowed_scope(run):
    miner, _, draft = run
    workdir = miner._clone_and_checkout(draft)
    result = miner.solve_fn(draft, workdir)
    policy = {**draft.work_packet['execution_policy'], 'allowed_paths': ['value.txt']}
    with pytest.raises(ValueError, match='scope'):
        support.build_raw_file_artifact(policy, workdir, result.result_commit, io.BytesIO(),
                                        **draft.work_packet['delivery_limits'])


@pytest.mark.parametrize(('paths', 'error'), [
    (['cafe\u0301.txt'], 'path_invalid'), (['../value.txt'], 'path_invalid'),
    (['.GIT/config'], 'path_invalid'), (['dir\\value.txt'], 'path_invalid'),
    (['Value.txt', 'value.txt'], 'path_collision'),
    (['tests/frozen.txt'], 'immutable'), (['tests'], 'immutable'),
])
def test_graded_builder_keeps_path_safety(run, monkeypatch, paths, error):
    _, _, draft = run
    # Supply names Git cannot represent as well as real-tree hazards. Validation
    # must refuse before any blob/tree read, even with allowed scope bypassed.
    def git(args, *, cwd):
        assert args[0] == 'diff', 'unsafe paths must be refused before reading file bodies'
        return ('\0'.join(paths) + '\0').encode()

    monkeypatch.setattr(support, 'public_git', git)
    policy = {**draft.work_packet['execution_policy'], 'allowed_paths': paths}
    with pytest.raises(ValueError, match=error):
        support.build_graded_artifact({**draft.work_packet, 'execution_policy': policy},
                                     '.', 'a' * 40, io.BytesIO())


def test_graded_pre_journal_failure_has_no_result_identity(run):
    miner, state, draft = run
    packet = dict(draft.work_packet)
    packet['graded_contract'] = {**packet['graded_contract'], 'base_sha': '0' * 40}
    object.__setattr__(draft, 'work_packet', packet)
    miner.run_once()
    assert len(state['completions']) == 1
    terminal = state['completions'][0]['terminal']
    assert terminal['result_ref'] is None
    assert terminal['result_commit'] == '', 'even a Graded failure cannot supply a client commit'
    assert terminal['verification_state'] != 'verified'
    assert state['solves'] == 0 and state['uploads'] == []
    assert state['git'] == [] and state['credentials'] == []



def _store(miner):
    return PublicRecovery(miner.config.workdir_root, miner.client.base_url, miner.config.runner_id)


def _assert_no_publication(state):
    assert state['credentials'] == []
    assert not any(args[0] in {'fetch', 'push'} for args in state['git'])
    assert not any(r.url.path.endswith(('/publication', '/repo-credential'))
                   for r in state['requests'])
    assert all(body['terminal']['result_ref'] is None
               and body['terminal']['result_commit'] == '' for body in state['completions'])


def _restart_at_artifact(miner, state):
    state['fault'] = 'restart'
    with pytest.raises(KeyboardInterrupt, match='process lost'):
        miner.run_once()
    assert _store(miner).pending()['stage'] == 'artifact'
    return MinerSkeleton(miner.client, miner.config, miner.solve_fn)


@pytest.mark.parametrize('recovered', [False, True], ids=['initial', 'restart'])
@pytest.mark.parametrize(('status', 'code'), [
    (400, 'public_artifact_header_invalid'), (409, 'graded_artifact_binding_mismatch'),
])
def test_graded_upload_refusal_releases_record(run, recovered, status, code):
    miner, state, _ = run
    if recovered:
        miner = _restart_at_artifact(miner, state)
    state['refusal'] = (status, code)
    assert miner.run_once() is True, 'permanent upload refusal must resolve, not wedge the journal'
    assert _store(miner).pending() is None
    assert len(state['completions']) == 1
    body = state['completions'][0]
    assert body['terminal']['verification_state'] == 'publish_failed'
    assert body['capture']['failure_class'] == 'publish_failed'
    _assert_no_publication(state)
    assert miner.run_once() is False  # Can poll for new work; gateway is idle.
    assert sum(r.url.path.endswith('/leases') for r in state['requests']) == 2
    assert state['solves'] == 1


@pytest.mark.parametrize('recovered', [False, True], ids=['initial', 'restart'])
def test_graded_upload_failure_report_refused_releases_record(run, recovered):
    miner, state, _ = run
    state['refusal'] = (409, 'graded_artifact_binding_mismatch')
    if recovered:
        state['completion_refusal'] = 'restart'
        with pytest.raises(KeyboardInterrupt, match='after failed completion'):
            miner.run_once()
        assert _store(miner).pending()['stage'] == 'completion'
        miner = MinerSkeleton(miner.client, miner.config, miner.solve_fn)
    state['completion_refusal'] = True
    assert miner.run_once() is True
    assert _store(miner).pending() is None
    assert len(state['completions']) == (2 if recovered else 1)
    assert state['completions'][0]['terminal']['verification_state'] == 'publish_failed'
    if recovered:
        assert state['completions'][0] == state['completions'][1]
    _assert_no_publication(state)


@pytest.mark.parametrize('recovered', [False, True], ids=['initial', 'restart'])
@pytest.mark.parametrize('where', ['upload', 'heartbeat', 'heartbeat_during_upload'])
def test_graded_lease_lost_acknowledged(run, monkeypatch, recovered, where):
    miner, state, _ = run
    if recovered:
        miner = _restart_at_artifact(miner, state)
    if where == 'upload':
        state['refusal'] = (409, 'lease_lost')
    elif where == 'heartbeat':
        state['heartbeat_refusal'] = True
    else:
        upload = miner.client.upload_graded_artifact

        def upload_with_lost_heartbeat(*args, **kwargs):
            state['heartbeat_refusal'] = True

            class Tick:
                def wait(self, _delay):
                    return False

            # Drive the actual heartbeat loop while upload is in flight, without
            # racing wall-clock sleeps against the real background thread.
            miner._heartbeat_loop(args[0], TaskLease(lease_id=args[2], task_id=args[0],
                expires_at='', selected_cell='', provider_pin='', fallback_policy='',
                hold_ref='', now='', outcome_price_usd=1.0), Tick(), {})
            return upload(*args, **kwargs)

        monkeypatch.setattr(miner.client, 'upload_graded_artifact', upload_with_lost_heartbeat)
    assert miner.run_once() is True, 'a revoked Graded lease must be acknowledged immediately'
    assert _store(miner).pending() is None
    assert miner.last_run['lease_lost'] is True
    assert state['completions'] == [], 'never complete against a revoked lease'
    _assert_no_publication(state)
    state['idle'] = True
    assert miner.run_once() is False
    assert state['solves'] == 1


@pytest.mark.parametrize(('assignment', 'expected'), [
    ({'graded_contract_digest': None}, False),
    ({'graded_contract': None, 'graded_artifact_digest': None, 'acceptance_level': 'verified'}, False),
    ({'graded_contract_digest': 'a' * 64}, True),
    ({'graded_contract_digest': None, 'acceptance_level': 'graded'}, True),
    ({'graded_contract': {}}, True),
])
def test_validator_graded_detection_requires_non_none_value(assignment, expected):
    assert validator._is_graded_assignment(assignment) is expected


@pytest.mark.parametrize('recovered', [False, True], ids=['initial', 'restart'])
def test_heartbeat_503_during_graded_upload_retries_and_holds(run, monkeypatch, recovered):
    miner, state, _ = run
    if recovered:
        miner = _restart_at_artifact(miner, state)
    upload = miner.client.upload_graded_artifact
    waits, renewals = [], {}

    def upload_with_transient_heartbeat(*args, **kwargs):
        state['heartbeat_fault'] = 'server'
        ticks = iter((False, False, True))
        class Tick:
            def wait(self, delay):
                waits.append(delay)
                # One failed renewal, its immediate retry, then end the simulated run.
                return next(ticks)
        miner._heartbeat_loop(args[0], TaskLease(lease_id=args[2], task_id=args[0],
            expires_at='', selected_cell='', provider_pin='', fallback_policy='',
            hold_ref='', now='', outcome_price_usd=1.0), Tick(), renewals)
        assert renewals == {'renewals': 1}
        assert not miner.cancelled.is_set()
        assert state['completions'] == [], 'transient heartbeat must not close the lease'
        assert _store(miner).pending()['stage'] == 'artifact'
        return upload(*args, **kwargs)

    monkeypatch.setattr(miner.client, 'upload_graded_artifact', upload_with_transient_heartbeat)
    assert miner.run_once() is True
    interval = max(1.0, miner.config.heartbeat_interval_s)
    assert waits == [interval, 0.0, interval]
    _assert_held(state)
    assert not miner.cancelled.is_set() and not miner.last_run.get('lease_lost')
    assert _store(miner).pending() is None
    assert state['solves'] == 1
