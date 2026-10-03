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
import select
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timezone
from importlib import resources
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlsplit

from .verifier_runtime import (
    RuntimeRefusal,
    dependency_spec,
    structured_test_argv,
    validate_service,
    validate_repository_gitlinks,
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
            **({'repository_gitlinks': validate_repository_gitlinks(environment['repository_gitlinks'])}
               if 'repository_gitlinks' in environment else {}),
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


def _private_repository_access(access, catalog):
    if (not isinstance(access, dict)
            or access != {'visibility': 'private', 'sharing': 'shared-code-v1'}
            or 'private' not in catalog.get('repository_visibility', [])):
        raise ValueError('private_repository_sharing_required')


def execution_requirements(environment, config, *, languages, command_digest):
    profile = environment['profile']
    return {'schema_version': ('outcomes.execution-requirements.v2' if 'publication' in profile else 'outcomes.execution-requirements.v1'), 'catalog_digest': environment['catalog_digest'],
            'profile_id': profile['profile_id'], 'environment_digest': digest(environment),
            'verifier_profile_id': 'external-driver-v1', 'verifier_digest': command_digest,
            'protocol_versions': {'client': 2, 'miner': 2, 'validator': 2}, 'repository_visibility': environment.get('repository_access', {}).get('visibility', 'public'),
            'languages': _languages(languages, profile), **{key: profile[key] for key in
            ('os', 'arch', 'runtime', 'package_manager', 'browser', 'system_packages', 'services', 'network', 'workspace_mode')},
            'limits': config['limits'],
            **({'publication': dict(profile['publication'])} if 'publication' in profile else {})}


def prepare_environment(selection, *, cwd, command, timeout_s, toolchain=None):
    """Freeze a catalog profile and exact lock bytes; no installation or inference."""
    required = {'profile_id', 'lock_paths', 'languages'}
    if not isinstance(selection, dict) or not required <= selection.keys() or selection.keys() - required - {'acceptance_paths', 'service', 'repository_access'}:
        raise ValueError('invalid_execution_environment')
    catalog, profile = _profile(selection['profile_id'])
    if 'repository_access' in selection:
        _private_repository_access(selection['repository_access'], catalog)
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
    if 'repository_access' in selection:
        environment['repository_access'] = dict(selection['repository_access'])
    # Freeze only pointers in this repository's committed tree. No submodule
    # URL is consulted, and no recursively owned repository is fetched.
    if profile.get('repository_features', {}).get('submodules') == 'uninitialized-pointers-v1':
        links = {}
        for entry in public_git(['ls-tree', '-rz', '--full-tree', 'HEAD'], cwd=root).split(b'\0'):
            if entry:
                metadata, path = entry.split(b'\t', 1)
                mode, kind, oid = metadata.decode('ascii').split()
                if mode == '160000':
                    if kind != 'commit':
                        raise ValueError('invalid_repository_gitlinks')
                    links[path.decode('utf-8')] = oid
        if links:
            environment['repository_gitlinks'] = validate_repository_gitlinks(links)
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


def validate_gitlink_scope(environment, allowed_paths):
    """Opaque repository pointers cannot be delivered, read as locks, or tested.

    A glob's fixed directory prefix bounds its possible matches. Broad patterns
    are refused when that prefix overlaps a pointer; no repository traversal is
    needed to decide whether a task could address unavailable source.
    """
    links = environment.get('repository_gitlinks', {})
    if not links:
        return
    if not isinstance(allowed_paths, list) or any(not isinstance(p, str) for p in allowed_paths):
        raise ValueError('invalid_allowed_paths')
    paths = [*allowed_paths, *environment['lock_files'], *environment['acceptance_files']]
    for path in paths:
        components = []
        for part in path.split('/'):
            if any(char in part for char in '*?['):
                break
            if part not in ('', '.'):
                components.append(part)
        prefix = '/'.join(components)
        if any(not prefix or prefix == link or prefix.startswith(link + '/')
               or link.startswith(prefix + '/') for link in links):
            raise ValueError('repository_gitlink_scope_unsupported')


def validate_public_execution_packet(packet):
    """Recompile, don't trust client-supplied executable or preflight identities."""
    if not isinstance(packet, dict) or not PUBLIC_FIELDS <= packet.keys():
        raise ValueError('public_execution_contract_required')
    environment = packet['execution_environment']
    fields = {'schema_version', 'catalog_digest', 'profile', 'lock_files', 'acceptance_files'}
    if isinstance(environment, dict) and isinstance(environment.get('profile'), dict) and environment['profile'].get('profile_id') == 'linux-node-browser-http-v1':
        fields.add('service')
    if isinstance(environment, dict) and 'repository_access' in environment:
        fields.add('repository_access')
    if isinstance(environment, dict) and 'repository_gitlinks' in environment:
        fields.add('repository_gitlinks')
    if not isinstance(environment, dict) or set(environment) != fields:
        raise ValueError('invalid_execution_environment')
    if environment['schema_version'] != 'outcomes.execution-environment.v1' or not isinstance(environment['profile'], dict):
        raise ValueError('invalid_execution_environment')
    catalog, profile = _profile(environment['profile'].get('profile_id'), environment['catalog_digest'])
    if environment['catalog_digest'] != digest(catalog) or environment['profile'] != profile:
        raise ValueError('execution_catalog_changed')
    if 'repository_access' in environment:
        _private_repository_access(environment['repository_access'], catalog)
    if 'repository_gitlinks' in environment:
        if profile.get('repository_features', {}).get('submodules') != 'uninitialized-pointers-v1':
            raise ValueError('repository_gitlinks_unsupported')
        try:
            validate_repository_gitlinks(environment['repository_gitlinks'])
        except RuntimeRefusal as exc:
            raise ValueError(str(exc)) from None
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
    validate_gitlink_scope(environment, policy.get('allowed_paths'))
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
    # Card 70cd3006: v2 (proven locally by the preparer) is byte-compatible
    # with the original closed shape; v3 (card 70cd3006's remote/deferred
    # preflight) is a SEPARATE closed shape asserting only that the miner has
    # not yet run the base — never treated as red/failing evidence, and never
    # accepted with any extra, missing, or mismatched key.
    verify_base = packet.get('verify_base')
    v2 = {'schema_version': 'outcomes.base-preflight.v2', 'source': 'packet',
          'outcome': 'assertion_failed', 'verifier_digest': expected['verifier_digest'],
          'environment_digest': expected['environment_digest'], 'base_sha': policy.get('repo_base_sha')}
    v3 = {'schema_version': 'outcomes.base-preflight.v3', 'source': 'deferred',
          'outcome': 'not_run', 'reason': 'client_preflight_deferred',
          'verifier_digest': expected['verifier_digest'],
          'environment_digest': expected['environment_digest'], 'base_sha': policy.get('repo_base_sha')}
    if verify_base != v2 and verify_base != v3:
        raise ValueError('public_base_preflight_required')
    return expected


def _git_environment():
    return {'PATH': os.environ.get('PATH', ''), 'HOME': '/nonexistent',
            'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_SYSTEM': os.devnull,
            'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_TERMINAL_PROMPT': '0'}


def _isolated_git(args, *, cwd, env):
    proc = subprocess.run(['git', '--literal-pathspecs', '-c', 'credential.helper=', '-c', 'core.hooksPath=' + os.devnull,
        '-c', 'core.fsmonitor=false', '-c', 'protocol.ext.allow=never',
        '-c', 'protocol.file.allow=never', *args], cwd=str(cwd), env=env,
        capture_output=True, check=False, timeout=120)
    if proc.returncode != 0:
        raise ValueError('public Git operation failed')
    return proc.stdout


def public_git(args, *, cwd, index_file=None):
    """Credential-free Git never borrows host config, hooks or URL rewrites."""
    env = _git_environment()
    if index_file is not None:
        env['GIT_INDEX_FILE'] = str(index_file)
    return _isolated_git(args, cwd=cwd, env=env)


# api.github.com/meta, 2026-09-25. Rotation requires a reviewed runtime update.
_GITHUB_HOST_KEY = 'github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl\n'


_APP_READ_FIELDS = frozenset({'kind', 'scope', 'repository_url', 'repository_id', 'token', 'expires_at'})


def _app_read_expiry(credential):
    try:
        expiry = datetime.fromisoformat(credential['expires_at'])
    except (TypeError, ValueError):
        expiry = None
    if expiry is None or expiry.tzinfo is None:
        raise ValueError('private_repository_read_access_required')
    return expiry


def _validate_app_read(repo_url, credential):
    """A GitHub App read token binds one frozen repository URL and id until its expiry."""
    repository_id, token = credential.get('repository_id'), credential.get('token')
    if (set(credential) != _APP_READ_FIELDS or credential.get('scope') != 'read'
            or credential.get('repository_url') != repo_url
            or type(repository_id) is not int or repository_id <= 0
            or not isinstance(token, str) or not re.fullmatch(r'[\x21-\x7e]+', token)):
        raise ValueError('private_repository_read_access_required')
    if _app_read_expiry(credential) <= datetime.now(timezone.utc):
        # Refresh belongs to the authenticated lease; never fall back to an anonymous read.
        raise ValueError('private_repository_read_access_expired')


def validate_repository_credential(visibility, repo_url, credential):
    if (not isinstance(repo_url, str) or not re.fullmatch(
            r'https://github\.com/[a-z0-9-]+/[a-z0-9_.-]+\.git', repo_url)):
        raise ValueError('repository_url_invalid')
    if visibility == 'public':
        if credential is not None:
            raise ValueError('public repository credential forbidden')
    elif visibility == 'private':
        if isinstance(credential, Mapping) and credential.get('kind') == 'github_app_read_token':
            _validate_app_read(repo_url, credential)
        elif (not isinstance(credential, Mapping)
                or set(credential) != {'kind', 'scope', 'private_key', 'fingerprint', 'repository_url'}
                or credential.get('kind') != 'ssh_deploy_key' or credential.get('scope') != 'read'
                or credential.get('repository_url') != repo_url
                or not isinstance(credential.get('private_key'), str)
                or not credential['private_key'].startswith('-----BEGIN OPENSSH PRIVATE KEY-----')
                or len(credential['private_key']) > 32768
                or not isinstance(credential.get('fingerprint'), str)
                or not credential['fingerprint'].startswith('SHA256:')):
            raise ValueError('private_repository_read_access_required')
    else:
        raise ValueError('repository_visibility_invalid')


# Forwards one Git credential request to the parent's private channel. It never
# holds the token between calls and ignores store/erase.
_APP_READ_HELPER = """import socket, sys
if sys.argv[2:] == ['get']:
    with socket.socket(socket.AF_UNIX) as channel:
        channel.connect(sys.argv[1])
        channel.sendall(sys.stdin.buffer.read())
        channel.shutdown(socket.SHUT_WR)
        sys.stdout.buffer.write(channel.makefile('rb').read())
"""
# The only HTTP settings allowed to reach the authenticated repository URL.
_APP_READ_HTTP = {('http.extraheader', ''), ('http.followredirects', 'false')}


def _app_read_flags(repo_url, helper):
    # URL-specific keys outrank general ones, so reset extraHeader and redirects at
    # the exact repository URL too. The empty helper in _isolated_git runs first.
    flags = ['core.askPass=', 'credential.helper=' + helper, 'credential.useHttpPath=true']
    for prefix in ('http.', 'http.' + repo_url + '.'):
        flags += [prefix + 'followRedirects=false', prefix + 'extraHeader=']
    return [part for flag in flags for part in ('-c', flag)]


def _app_read_http_is_exact(flags, *, cwd, env, repo_url):
    """Local config may not add a proxy, TLS or other HTTP setting for the authenticated URL."""
    try:
        output = _isolated_git([*flags, 'config', '--get-urlmatch', 'http', repo_url], cwd=cwd, env=env)
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
    return {line.partition(' ')[::2] for line in output.decode().splitlines()} == _APP_READ_HTTP


def _app_read_url_is_exact(flags, *, cwd, env, repo_url):
    """No insteadOf rule may rewrite the frozen URL; Git applies any rule whose value prefixes it."""
    # credential.useHttpPath is always set, so Git exits 0 even when no rule exists.
    output = _isolated_git([*flags, 'config', '--null', '--get-regexp',
                            r'^(url\..*\.insteadof|credential\.usehttppath)$'], cwd=cwd, env=env)
    for entry in output.decode('utf-8', 'surrogateescape').split('\0'):
        key, _, prefix = entry.partition('\n')
        if key.startswith('url.') and repo_url.startswith(prefix):
            return False
    return True


def _release_app_read(listener, wake, binding, token, expiry, exact):
    """Answer helper requests; release only for the exact protocol, host and path."""
    while True:
        ready, _, _ = select.select([listener, wake], [], [])
        if wake in ready:
            return
        channel, _ = listener.accept()
        # A helper that exits early costs only its own request.
        with channel, suppress(OSError):
            request = channel.makefile('rb').read().decode('utf-8', 'replace')
            fields = dict(line.partition('=')[::2] for line in request.splitlines())
            if ((fields.get('protocol'), fields.get('host'), fields.get('path')) == binding
                    and datetime.now(timezone.utc) < expiry and exact()):
                channel.sendall(f'username=x-access-token\npassword={token}\n'.encode())


def _app_read_git(args, *, cwd, repo_url, token, expiry):
    """Run Git with a per-call credential helper backed by an in-process channel."""
    parts = urlsplit(repo_url)
    binding = (parts.scheme, parts.netloc, parts.path.lstrip('/'))
    env = _git_environment()
    with tempfile.TemporaryDirectory(prefix='ormas-read-') as directory:
        helper, endpoint = Path(directory) / 'helper.py', str(Path(directory) / 'channel')
        fd = os.open(helper, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(_APP_READ_HELPER)
        flags = _app_read_flags(repo_url, '!' + shlex.join([sys.executable, '-I', str(helper), endpoint]))
        if not _app_read_url_is_exact(flags, cwd=cwd, env=env, repo_url=repo_url):
            raise ValueError('private_repository_url_rewritten')
        wake, woken = os.pipe()
        try:
            with socket.socket(socket.AF_UNIX) as listener:
                listener.bind(endpoint)
                listener.listen()
                release = threading.Thread(target=_release_app_read, daemon=True, args=(
                    listener, wake, binding, token, expiry,
                    lambda: _app_read_http_is_exact(flags, cwd=cwd, env=env, repo_url=repo_url)))
                release.start()
                try:
                    return _isolated_git([*flags, *args], cwd=cwd, env=env)
                finally:
                    os.write(woken, b'\0')
                    release.join()
        finally:
            os.close(wake)
            os.close(woken)


def repository_git(args, *, cwd, repo_url, credential):
    """A private network read with one temporary credential and no ambient authority."""
    validate_repository_credential('private', repo_url, credential)
    # Callers supply the canonical repository explicitly, never a saved remote.
    if not args or args[0] not in {'clone', 'fetch'} or args.count(repo_url) != 1:
        raise ValueError('private_repository_read_operation_required')
    if credential['kind'] == 'github_app_read_token':
        return _app_read_git(args, cwd=cwd, repo_url=repo_url, token=credential['token'],
                             expiry=_app_read_expiry(credential))
    ssh_url = 'git@github.com:' + repo_url.removeprefix('https://github.com/')
    args = [ssh_url if arg == repo_url else arg for arg in args]
    with tempfile.TemporaryDirectory(prefix='ormas-read-key-') as directory:
        key = Path(directory) / 'id_read'
        hosts = Path(directory) / 'known_hosts'
        for path, body in ((key, credential['private_key']), (hosts, _GITHUB_HOST_KEY)):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as stream:
                stream.write(body if body.endswith('\n') else body + '\n')
        env = _git_environment()
        env['GIT_SSH_COMMAND'] = shlex.join(['ssh', '-F', os.devnull, '-i', str(key),
            '-o', 'IdentityAgent=none', '-o', 'IdentitiesOnly=yes',
            '-o', 'StrictHostKeyChecking=yes', '-o', 'UserKnownHostsFile=' + str(hosts),
            '-o', 'GlobalKnownHostsFile=' + os.devnull, '-o', 'HostKeyAlgorithms=ssh-ed25519',
            '-o', 'BatchMode=yes'])
        return _isolated_git(args, cwd=cwd, env=env)



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
