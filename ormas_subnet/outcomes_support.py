"""Canonical public execution catalog, compiler and contract validation.

This standard-library module and verifier_runtime are mirrored in the public SDK.
No queue, model routing, available-capacity or settlement authority lives here.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from importlib import resources
from pathlib import Path

from .verifier_runtime import (
    RuntimeRefusal,
    dependency_spec,
    structured_test_argv,
    validate_service,
)

PUBLIC_FIELDS = frozenset({'execution_environment', 'execution_requirements'})


def digest(value):
    return 'sha256:' + hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def load_support_catalog(catalog_digest=None):
    assets = resources.files(__package__).joinpath('client_assets')
    current = json.loads(assets.joinpath('outcomes_support.v1.json').read_text())
    if catalog_digest is None or digest(current) == catalog_digest:
        return current
    history = assets.joinpath('outcomes_support_history')
    if history.is_dir():
        for path in history.iterdir():
            if path.name.endswith('.json'):
                old = json.loads(path.read_text())
                if digest(old) == catalog_digest:
                    return old
    raise ValueError('unsupported_catalog_version')


def _runtime_source():
    return Path(__file__).with_name('verifier_runtime.py').read_text()


def _profile(profile_id, catalog_digest=None):
    catalog = load_support_catalog(catalog_digest)
    profile = next((p for p in catalog['profiles'] if p['profile_id'] == profile_id), None)
    if profile is None or profile['status'] == 'planned':
        raise ValueError('unsupported_execution_profile')
    return catalog, profile


def _lock_paths(profile):
    if profile['lock_format'] == 'pip-hashes-v1':
        return ['requirements.lock']
    if profile['lock_format'] == 'npm-lock-v3':
        return ['package-lock.json', 'package.json']
    raise ValueError('unsupported_dependency_lock_layout')


def _locked_file(root, name):
    if not isinstance(name, str) or not name or Path(name).is_absolute() or any(p in ('', '.', '..', '.git') for p in name.split('/')) or '\\' in name:
        raise ValueError('invalid_dependency_lock_path')
    path = root
    for part in name.split('/'):
        path = path / part
        if path.is_symlink():
            raise ValueError('dependency_lock_symlink')
    if not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError('dependency_lock_unavailable')
    return path.read_bytes()


def _languages(value, profile):
    if not isinstance(value, list) or not value or any(not isinstance(v, str) for v in value):
        raise ValueError('invalid_execution_languages')
    if value != sorted(set(value)) or not set(value) <= ({'python'} if profile['runtime']['kind'] == 'python' else {'javascript', 'typescript'}):
        raise ValueError('invalid_execution_languages')
    return list(value)


def _command(command, profile, toolchain):
    if not isinstance(command, str) or not command or len(command) > 16384:
        raise ValueError('invalid_verify_command')
    argv = shlex.split(command)
    env = {}
    while argv and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*=.*', argv[0], re.DOTALL):
        name, value = argv.pop(0).split('=', 1)
        if name not in {'LANG', 'LC_ALL', 'TZ', 'PYTHONDONTWRITEBYTECODE'} or name in env:
            raise ValueError('unsupported_verifier_environment')
        env[name] = value
    if not argv:
        raise ValueError('verification_required')
    if '/' in argv[0] and not argv[0].startswith('/') and toolchain is None:
        raise ValueError('toolchain_required_for_relative_interpreter')
    if profile['runtime']['kind'] == 'python':
        if argv[0] not in {'python', 'python3', 'python3.12', '.venv/bin/python', '.venv/bin/pytest', 'pytest'}:
            raise ValueError('unsupported_profile_command')
        if toolchain is not None and (not isinstance(toolchain, dict) or toolchain.get('kind') != 'python' or toolchain.get('python') != '3.12'):
            raise ValueError('toolchain_profile_mismatch')
        if argv[0] in {'pytest', '.venv/bin/pytest'}:
            argv = ['/work/.venv/bin/python', '-m', 'pytest'] + argv[1:]
        elif argv[1:3] == ['-m', 'pytest']:
            argv[0] = '/work/.venv/bin/python'
        else:
            raise ValueError('unsupported_profile_command')
    elif argv[0] not in {'node', 'npm', 'npx'} or toolchain is not None:
        raise ValueError('unsupported_profile_command')
    if any(token.startswith(('/', '~')) or '..' in token.split('/') for token in argv[1:]):
        raise ValueError('verifier_external_source')
    return argv, env


def runtime_config(environment, *, command, timeout_s, toolchain=None):
    profile = environment['profile']
    limits = dict(profile['limits'])
    if type(timeout_s) not in (int, float) or not 1 <= timeout_s <= limits['timeout_s'] or int(timeout_s) != timeout_s:
        raise ValueError('unsupported_verification_timeout')
    limits['timeout_s'] = int(timeout_s)
    argv, env = _command(command, profile, toolchain)
    return {'schema_version': 'ormas.oci-verifier.v2', 'image': profile['image'], 'platform': profile['platform'],
            'profile_id': profile['profile_id'], 'argv': argv, 'env': env, 'lock_files': environment['lock_files'],
            'acceptance_files': environment['acceptance_files'],
            'limits': limits, 'install_argv': list(profile['installer_argv']),
            **({'service': _service(environment.get('service'))}
               if profile['profile_id'] == 'linux-node-browser-http-v1' else {})}


def _service(value):
    try:
        return validate_service(value)
    except RuntimeRefusal as exc:
        raise ValueError(str(exc)) from None


def effective_command(config):
    source = _runtime_source()
    if hashlib.sha256(source.encode()).hexdigest() != load_support_catalog().get('verifier_compiler_sha256'):
        raise ValueError('compiler_catalog_stale')
    encoded = base64.b64encode(json.dumps(config, sort_keys=True, separators=(',', ':')).encode()).decode()
    return shlex.join(['python3', '-I', '-S', '-c', source, encoded])


def execution_requirements(environment, config, *, languages, command_digest):
    profile = environment['profile']
    return {'schema_version': ('outcomes.execution-requirements.v2' if 'publication' in profile else 'outcomes.execution-requirements.v1'), 'catalog_digest': environment['catalog_digest'],
            'profile_id': profile['profile_id'], 'environment_digest': digest(environment),
            'verifier_profile_id': 'external-driver-v1', 'verifier_digest': command_digest,
            'protocol_versions': {'client': 2, 'miner': 2, 'validator': 2}, 'repository_visibility': 'public',
            'languages': _languages(languages, profile), **{key: profile[key] for key in
            ('os', 'arch', 'runtime', 'package_manager', 'browser', 'system_packages', 'services', 'network', 'workspace_mode')},
            'limits': config['limits'],
            **({'publication': dict(profile['publication'])} if 'publication' in profile else {})}


def prepare_environment(selection, *, cwd, command, timeout_s, toolchain=None):
    """Freeze a catalog profile and exact lock bytes; no installation or inference."""
    required = {'profile_id', 'lock_paths', 'languages'}
    if not isinstance(selection, dict) or not required <= selection.keys() or selection.keys() - required - {'acceptance_paths', 'service'}:
        raise ValueError('invalid_execution_environment')
    catalog, profile = _profile(selection['profile_id'])
    if ('service' in selection) != (profile['profile_id'] == 'linux-node-browser-http-v1'):
        raise ValueError('invalid_candidate_service')
    if not isinstance(cwd, str) or not cwd:
        raise ValueError('public_base_preflight_required')
    languages = _languages(selection['languages'], profile)
    if selection['lock_paths'] != _lock_paths(profile):
        raise ValueError('unsupported_dependency_lock_layout')
    root = Path(cwd).resolve()
    locks = {name: _locked_file(root, name) for name in _lock_paths(profile)}
    environment = {'schema_version': 'outcomes.execution-environment.v1', 'catalog_digest': digest(catalog),
                   'profile': profile, 'lock_files': {name: hashlib.sha256(raw).hexdigest() for name, raw in locks.items()},
                   'acceptance_files': {}}
    if 'service' in selection:
        environment['service'] = _service(selection['service'])
    config = runtime_config(environment, command=command, timeout_s=timeout_s, toolchain=toolchain)
    try:
        spec, _ = dependency_spec(profile['profile_id'], locks)
        if toolchain is not None:
            pins = {p['name'].lower().replace('_', '-') + '==' + p['version'] for p in spec}
            if any(pin.lower().replace('_', '-') not in pins for pin in toolchain['pip_install']):
                raise ValueError('toolchain_profile_mismatch')
        if profile['browser']:
            lock = json.loads(locks['package-lock.json'])
            if lock['packages'].get('node_modules/@playwright/test', {}).get('version') != profile['browser']['driver_version']:
                raise ValueError('browser_driver_lock_mismatch')
        test_argv, kind = structured_test_argv(config, root, '/tmp/ormas-preflight.xml')
        # The ordinary case needs no new client ceremony: the named test paths
        # become the frozen driver. Support/config files can be named explicitly.
        offset = {'junit-file': 9, 'node-junit': 3, 'playwright-json': 5}[kind]
        if profile['profile_id'] == 'linux-node-browser-http-v1':
            offset += 1
        selected = [p.split('::')[0] for p in test_argv[offset:]]
        paths = selection.get('acceptance_paths', selected)
        if not isinstance(paths, list) or not paths or any(not isinstance(p, str) or not p for p in paths):
            raise ValueError('acceptance_paths_required')
        names = set()
        for name in paths:
            if name.startswith(('/', '~')) or any(p in ('', '.', '..', '.git', '.venv', 'node_modules') for p in name.split('/')) or '\\' in name:
                raise ValueError('invalid_acceptance_path')
            target = root / name
            if target.is_symlink():
                raise ValueError('acceptance_symlink')
            if target.is_dir():
                for child in target.rglob('*'):
                    if child.is_symlink():
                        raise ValueError('acceptance_symlink')
                    if child.is_file():
                        names.add(child.relative_to(root).as_posix())
            else:
                names.add(name)
        if not names or len(names) > 1000 or not all(any(n == p or n.startswith(p.rstrip('/') + '/') for n in names) for p in selected):
            raise ValueError('named_tests_must_be_frozen')
        environment['acceptance_files'] = {name: hashlib.sha256(_locked_file(root, name)).hexdigest() for name in sorted(names)}
        config['acceptance_files'] = environment['acceptance_files']
    except RuntimeRefusal as exc:
        raise ValueError(str(exc)) from None
    requirements = execution_requirements(environment, config, languages=languages, command_digest='sha256:' + '0' * 64)
    return environment, config, requirements


def has_public_execution(packet):
    return isinstance(packet, dict) and (bool(PUBLIC_FIELDS & packet.keys()) or
        isinstance(packet.get('verifier_profile'), dict) and packet['verifier_profile'].get('execution_mode') == 'oci-v1')


def validate_public_execution_packet(packet):
    """Recompile, don't trust client-supplied executable or preflight identities."""
    if not isinstance(packet, dict) or not PUBLIC_FIELDS <= packet.keys():
        raise ValueError('public_execution_contract_required')
    environment = packet['execution_environment']
    fields = {'schema_version', 'catalog_digest', 'profile', 'lock_files', 'acceptance_files'}
    if isinstance(environment, dict) and isinstance(environment.get('profile'), dict) and environment['profile'].get('profile_id') == 'linux-node-browser-http-v1':
        fields.add('service')
    if not isinstance(environment, dict) or set(environment) != fields:
        raise ValueError('invalid_execution_environment')
    if environment['schema_version'] != 'outcomes.execution-environment.v1' or not isinstance(environment['profile'], dict):
        raise ValueError('invalid_execution_environment')
    catalog, profile = _profile(environment['profile'].get('profile_id'), environment['catalog_digest'])
    if environment['catalog_digest'] != digest(catalog) or environment['profile'] != profile:
        raise ValueError('execution_catalog_changed')
    locks = environment['lock_files']
    if not isinstance(locks, dict) or set(locks) != set(_lock_paths(profile)) or any(not isinstance(v, str) or not re.fullmatch('[0-9a-f]{64}', v) for v in locks.values()):
        raise ValueError('invalid_dependency_locks')
    files = environment['acceptance_files']
    if not isinstance(files, dict) or not files or len(files) > 1000 or any(
            not isinstance(name, str) or name.startswith(('/', '~')) or '\\' in name
            or any(p in ('', '.', '..', '.git', '.venv', 'node_modules') for p in name.split('/'))
            or not isinstance(sha, str) or not re.fullmatch('[0-9a-f]{64}', sha) for name, sha in files.items()):
        raise ValueError('invalid_acceptance_files')
    vp, req, policy = packet.get('verifier_profile'), packet.get('execution_requirements'), packet.get('execution_policy')
    if not isinstance(vp, dict) or not isinstance(req, dict) or not isinstance(policy, dict):
        raise ValueError('public_execution_contract_required')
    limits = req.get('limits')
    if not isinstance(limits, dict) or type(limits.get('timeout_s')) is not int:
        raise ValueError('invalid_runtime_limits')
    config = runtime_config(environment, command=vp.get('original_command'), timeout_s=limits['timeout_s'], toolchain=packet.get('toolchain'))
    expected_command = packet.get('verification_command')
    if not isinstance(expected_command, str):
        raise ValueError('verifier_compiler_mismatch')
    arguments = shlex.split(expected_command)
    encoded = base64.b64encode(json.dumps(config, sort_keys=True, separators=(',', ':')).encode()).decode()
    if (len(arguments) != 6 or arguments[:4] != ['python3', '-I', '-S', '-c']
            or hashlib.sha256(arguments[4].encode()).hexdigest() != catalog.get('verifier_compiler_sha256')
            or arguments[5] != encoded or shlex.join(arguments) != expected_command):
        raise ValueError('verifier_compiler_mismatch')
    sha = hashlib.sha256(expected_command.encode()).hexdigest()
    if vp != {'schema_version': 'outcomes.verifier-profile.v1', 'profile_id': 'external-driver-v1',
              'original_command': vp['original_command'], 'generated_command_sha256': sha,
              'timeout_s': float(config['limits']['timeout_s']), 'max_files': config['limits']['max_files'],
              'max_bytes': config['limits']['max_bytes'], 'execution_mode': 'oci-v1', 'assertion_failure_exit_code': 86}:
        raise ValueError('verifier_profile_mismatch')
    expected = execution_requirements(environment, config, languages=req.get('languages'), command_digest='sha256:' + sha)
    if req != expected or not isinstance(packet.get('task_features'), dict) or packet['task_features'].get('languages') != req['languages']:
        raise ValueError('execution_requirements_mismatch')
    immutable = policy.get('immutable_paths')
    if not isinstance(immutable, list) or any(not isinstance(p, str) for p in immutable):
        raise ValueError('invalid_immutable_paths')
    if any(not any(path == rule or path.startswith(rule.rstrip('/') + '/') for rule in immutable) for path in locks):
        raise ValueError('dependency_locks_must_be_immutable')
    if any(not any(path == rule or path.startswith(rule.rstrip('/') + '/') for rule in immutable) for path in files):
        raise ValueError('acceptance_files_must_be_immutable')
    if packet.get('verify_base') != {'schema_version': 'outcomes.base-preflight.v2', 'source': 'packet',
            'outcome': 'assertion_failed', 'verifier_digest': expected['verifier_digest'],
            'environment_digest': expected['environment_digest'], 'base_sha': policy.get('repo_base_sha')}:
        raise ValueError('public_base_preflight_required')
    return expected


def public_git(args, *, cwd, index_file=None):
    """Public Git never borrows host credential helpers, hooks or URL rewrites."""
    env = {'PATH': os.environ.get('PATH', ''), 'HOME': '/nonexistent',
           'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_SYSTEM': os.devnull,
           'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_TERMINAL_PROMPT': '0'}
    if index_file is not None:
        env['GIT_INDEX_FILE'] = str(index_file)
    proc = subprocess.run(['git', '--literal-pathspecs', '-c', 'credential.helper=', '-c', 'core.hooksPath=' + os.devnull,
        '-c', 'core.fsmonitor=false', '-c', 'protocol.ext.allow=never',
        '-c', 'protocol.file.allow=never', *args], cwd=str(cwd), env=env,
        capture_output=True, check=False, timeout=120)
    if proc.returncode != 0:
        raise ValueError('public Git operation failed')
    return proc.stdout



def build_public_artifact(packet, workdir, result_commit, target):
    """Serialize committed regular files as bounded raw bytes, never a Git pack."""
    validate_public_execution_packet(packet)
    if 'publication' not in packet['execution_requirements'] or not re.fullmatch('[0-9a-f]{40}', result_commit):
        raise ValueError('public artifact contract required')
    limits = packet['execution_requirements']['limits']
    changed = public_git(['diff', '--name-only', '--no-renames', '-z', packet['execution_policy']['repo_base_sha'],
        result_commit, '--'], cwd=workdir).decode('utf-8').split('\0')
    changed = sorted(p for p in changed if p)
    if not 0 < len(changed) <= packet['execution_requirements']['publication']['max_changed_files']:
        raise ValueError('public artifact file limit')
    tree = {}
    for record in public_git(['ls-tree', '-rz', '--full-tree', result_commit], cwd=workdir).split(b'\0'):
        if record:
            metadata, path = record.split(b'\t', 1)
            mode, kind, sha = metadata.decode('ascii').split(' ')
            tree[path.decode('utf-8')] = (mode, kind, sha)
    tree_sha = public_git(['rev-parse', result_commit + '^{tree}'], cwd=workdir).decode().strip()
    entries = []
    size = 0
    with tempfile.TemporaryFile() as bodies:
        for path in changed:
            item = tree.get(path)
            if item is None:
                entries.append({'path': path, 'op': 'delete', 'mode': None, 'byte_len': 0, 'sha256': None})
                continue
            mode, kind, sha = item
            if mode not in ('100644', '100755') or kind != 'blob':
                raise ValueError('public artifact file mode unsupported')
            length = int(public_git(['cat-file', '-s', sha], cwd=workdir).decode())
            size += length
            if size > limits['max_bytes']:
                raise ValueError('public artifact byte limit')
            raw = public_git(['cat-file', 'blob', sha], cwd=workdir)
            if len(raw) != length:
                raise ValueError('public artifact blob mismatch')
            bodies.write(raw)
            entries.append({'path': path, 'op': 'upsert', 'mode': mode,
                'byte_len': length, 'sha256': hashlib.sha256(raw).hexdigest()})
        header = json.dumps({'schema_version': 'ormas.public-artifact.v1',
            'base_commit': packet['execution_policy']['repo_base_sha'], 'tree_sha': tree_sha, 'files': entries},
            sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()
        if len(header) > 1_048_576:
            raise ValueError('public artifact header limit')
        target.write(len(header).to_bytes(8, 'big'))
        target.write(header)
        bodies.seek(0)
        shutil.copyfileobj(bodies, target, 65536)
    length = target.tell()
    target.seek(0)
    digest = hashlib.sha256()
    for chunk in iter(lambda: target.read(65536), b''):
        digest.update(chunk)
    target.seek(0)
    return length, digest.hexdigest(), tree_sha
