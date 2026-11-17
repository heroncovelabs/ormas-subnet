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
import stat
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from importlib import resources
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlsplit

from ._trusted_tools import TrustedExecutableUnavailable, trusted_executable
from .verifier_runtime import (
    RuntimeRefusal,
    dependency_spec,
    structured_test_argv,
    validate_service,
    validate_repository_gitlinks,
)

PUBLIC_FIELDS = frozenset({'execution_environment', 'execution_requirements'})
GRADED_PUBLIC_PRIVATE_FIELDS = frozenset({
    'repo_credential', 'repository_credential', 'private_repository_access',
    'repository_access', 'sharing',
})

# Historical catalogs remain available for interpreting frozen evidence, but a
# compiler that lost execution approval must never become executable merely by
# selecting its old catalog digest.  Updating this allowlist requires review of
# the exact embedded verifier source and coordinated gateway/SDK rollout.
_ACTIVE_VERIFIER_COMPILERS = frozenset({
    # card 31: explicit umask 022 in candidate and verify commands
    '4ae28f0ab47f662b52db84a131fb6f69ca12e4efabea9cbcdb3cf19df3a0eb92',
    '5bc3da22728e0e7cd0c7b447194b36f77e273310f1c2ce822e4d1ce07348b4ac',
    # M5: opt-in verifier package cache; pre-cache qualified compilers stay active.
    '3b5db9e86f9fec5b94639648a812f29c5f05d04a79be33027d84710b67c87010',
    'be421a7acea27181fd8ba8e55419c22cce0d73c0999502fa7ea2ef958af95bee',
    # 7cdead20: Verified Git support and identity-only Graded baseline sync.
    '437943d01eab6012e560aeba1c00b003dc7a61d0367ece699917f709be472133',
    '622c7f9820213a010a66ff9974e91f362e2b60ca69a184beb3235c72e6de6349',
    # DRAFT 7cdead20: opt-in git pytest image; frozen e28c11a6 packets stay active.
    '00affaf06968d2f27c7ba7802da6ee4641913a9bb009b58d2d51bd4a08bbd1f1',
    # DRAFT e28c11a6: local Graded-only compiler; not served, issuance off.
    'd8d5af502989ee906978511b4d4435228de786fa56189aecc74c32f6160f495b',
    # 2026-10-14.2 security release. Still executable: 2026-10-14.3 is a reliability
    # change, not a revocation, so packets frozen on .2 keep running during rollout.
    'd609ed993b3bda292cc93e5e995699da1175ca995d4226d17cb8794a8ab89151',
    # 2026-10-14.3: dependency downloads reuse one connection per host and retry, on the
    # ea3806ce7 trusted-tool hardening. Archived by the 2026-10-18 combined release (dev and
    # Tuxedo packets frozen on it keep running); it returns as the served catalog with the
    # checker-image cut, whose checkers and workers can resolve it locally.
    'eacf5e445082d2d4738c5b4e117a04a043452dc334cf5ded1cdc4de81aece313',
    # 2026-10-14.4 (ff7c1e51…): surgical Protected release cut from gateway 88363d4ef =
    # the 2026-10-04.1 verifier plus the download-resilience fix. SERVED by the 2026-10-18
    # combined release because production qualifications bind this digest and the installed
    # checker image and un-rolled workers resolve only it; the ea3806ce7/86cbeb9ad verifier
    # hardening is deferred to the checker-image cut (release notes).
    '214eadfb183270c187f0dba07ad16123ea402a90dcd2e13d883b906d2060db1c',
})


def digest(value):
    return 'sha256:' + hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def load_support_catalog(catalog_digest=None, *, graded=False):
    assets = resources.files(__package__).joinpath('client_assets')
    name = 'outcomes_support_graded_draft.v1.json' if graded else 'outcomes_support.v1.json'
    current = json.loads(assets.joinpath(name).read_text())
    if catalog_digest is None or digest(current) == catalog_digest:
        return current
    history = assets.joinpath('outcomes_support_history')
    if history.is_dir():
        for path in history.iterdir():
            if path.name.endswith('.json'):
                old = json.loads(path.read_text())
                if digest(old) == catalog_digest:
                    return old
    draft = json.loads(assets.joinpath('outcomes_support_graded_draft.v1.json').read_text())
    if digest(draft) == catalog_digest:
        return draft
    raise ValueError('unsupported_catalog_version')


def _runtime_source():
    return Path(__file__).with_name('verifier_runtime.py').read_text()


def _active_compiler(catalog):
    compiler = catalog.get('verifier_compiler_sha256') if isinstance(catalog, dict) else None
    if compiler not in _ACTIVE_VERIFIER_COMPILERS:
        raise ValueError('verifier_compiler_revoked')
    return compiler


def _frozen_compiler(catalog):
    """The compiler a frozen catalog names, whether or not it still holds approval."""
    compiler = catalog.get('verifier_compiler_sha256') if isinstance(catalog, dict) else None
    if not isinstance(compiler, str) or re.fullmatch('[0-9a-f]{64}', compiler) is None:
        raise ValueError('verifier_compiler_mismatch')
    return compiler


def _profile(profile_id, catalog_digest=None, *, graded=False):
    catalog = load_support_catalog(catalog_digest, graded=True) if graded else load_support_catalog(catalog_digest)
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


# These two historical acceptance cards delegated the verdict to pytest in the
# candidate container.  A candidate import could call os._exit(0) before the
# required assertions ran, and each card treated that status as acceptance.
# PR #37 changed their docstrings only, so both byte identities for each card
# remain revoked as immutable audit evidence.  The public SDK keeps this list in
# verifier_runtime.py; here it guards preparation and packet validation only,
# because the served embedded verifier is frozen by its catalog compiler hash
# (its copy arrives with the checker-image cut).
REVOKED_ACCEPTANCE_DRIVER_SHA256 = frozenset({
    # claim_refusal_backoff_external.py: original, then PR #37 docstring variant
    '545411aeba3d5ac8db574a733ed7d2cf21bec012b19a73a8de5c6a6cbbe6f442',
    'e032021de2cda990e4a8656376932efd2c9e48c565aab8a008688a9acb2f2b88',
    # validator_list_only_external.py: original, then PR #37 docstring variant
    'af06a6db0a155f703848d916f2fef53c22fec1ac2b3147fd5f1bf438744d404a',
    '36e853b3f76fbc41550404d731564091a7659d1a98f2d6871a30fc46a043b361',
})


def _validate_acceptance_files(files):
    if not isinstance(files, dict) or not files or len(files) > 1000 or any(
            not isinstance(name, str) or name.startswith(('/', '~')) or '\\' in name
            or any(p in ('', '.', '..', '.git', '.venv', 'node_modules')
                   for p in name.split('/'))
            or not isinstance(sha, str) or not re.fullmatch('[0-9a-f]{64}', sha)
            for name, sha in files.items()):
        raise ValueError('invalid_acceptance_files')
    if REVOKED_ACCEPTANCE_DRIVER_SHA256.intersection(files.values()):
        raise ValueError('acceptance_driver_revoked')
    return files


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


def graded_runtime_config(environment, *, command, timeout_s, toolchain=None):
    """Use the catalog-bound Graded mode; preserve legacy/Verified config bytes."""
    config = runtime_config(environment, command=command, timeout_s=timeout_s, toolchain=toolchain)
    catalog = load_support_catalog(environment['catalog_digest'])
    graded = catalog.get('acceptance_level') == 'graded'
    if not graded:
        return config
    if config['profile_id'] not in {'linux-python-pytest-v1', 'linux-python-pytest-git-v1'}:
        raise ValueError('graded_plain_command_requires_pytest')
    from .graded_verifier_runtime import RuntimeRefusal, structured_test_argv
    try:
        argv, _ = structured_test_argv(config, None, '/tmp/ormas-pytest-report.xml')
    except RuntimeRefusal as exc:
        raise ValueError(str(exc)) from None
    if len(argv) > 128:
        raise ValueError('invalid_candidate_argv')
    config['candidate_argv'] = list(config['argv'])
    return config


def _service(value):
    try:
        return validate_service(value)
    except RuntimeRefusal as exc:
        raise ValueError(str(exc)) from None


def effective_command(config, *, catalog_digest=None):
    graded = 'candidate_argv' in config
    source = Path(__file__).with_name('graded_verifier_runtime.py').read_text() if graded else _runtime_source()
    compiler = _active_compiler(load_support_catalog(catalog_digest, graded=True) if graded else load_support_catalog(catalog_digest))
    if hashlib.sha256(source.encode()).hexdigest() != compiler:
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
            **({'publication': dict(profile['publication'])} if 'publication' in profile else {}),
            **({'graded_custody': environment['graded_custody']} if 'graded_custody' in environment else {})}


def prepare_environment(selection, *, cwd, command, timeout_s, toolchain=None, graded=False):
    """Freeze a catalog profile and exact lock bytes; no installation or inference."""
    required = {'profile_id', 'lock_paths', 'languages'}
    if not isinstance(selection, dict) or not required <= selection.keys() or selection.keys() - required - {'acceptance_paths', 'service', 'repository_access', 'graded_custody'}:
        raise ValueError('invalid_execution_environment')
    if 'graded_custody' in selection:
        custody = selection['graded_custody']
        if not isinstance(custody, str) or custody != 'operator_services':
            raise ValueError('invalid_execution_environment')
        if not graded or 'repository_access' not in selection:
            raise ValueError('graded_custody_scope_invalid')
    catalog, profile = _profile(selection['profile_id'], graded=True) if graded else _profile(selection['profile_id'])
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
    if 'graded_custody' in selection:
        environment['graded_custody'] = selection['graded_custody']
    if 'service' in selection:
        environment['service'] = _service(selection['service'])
    build_config = graded_runtime_config if graded else runtime_config
    config = build_config(environment, command=command, timeout_s=timeout_s, toolchain=toolchain)
    # The draft's sibling profiles use only the Graded runtime's closed handlers.
    # Never expand the served runtime or alter Verified preparation.
    if graded:
        from .graded_verifier_runtime import (
            dependency_spec as parse_dependencies, structured_test_argv as test_arguments,
            RuntimeRefusal as runtime_refusal,
        )
    else:
        parse_dependencies, test_arguments, runtime_refusal = dependency_spec, structured_test_argv, RuntimeRefusal
    try:
        spec, _ = parse_dependencies(profile['profile_id'], locks)
        if toolchain is not None:
            pins = {p['name'].lower().replace('_', '-') + '==' + p['version'] for p in spec}
            if any(pin.lower().replace('_', '-') not in pins for pin in toolchain['pip_install']):
                raise ValueError('toolchain_profile_mismatch')
        if profile['browser']:
            lock = json.loads(locks['package-lock.json'])
            if lock['packages'].get('node_modules/@playwright/test', {}).get('version') != profile['browser']['driver_version']:
                raise ValueError('browser_driver_lock_mismatch')
        test_argv, kind = test_arguments(config, root, '/tmp/ormas-preflight.xml')
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
        environment['acceptance_files'] = _validate_acceptance_files({
            name: hashlib.sha256(_locked_file(root, name)).hexdigest()
            for name in sorted(names)
        })
        config['acceptance_files'] = environment['acceptance_files']
    except runtime_refusal as exc:
        raise ValueError(str(exc)) from None
    # Reject unsafe frozen driver bytes before resolving or invoking a host Git
    # executable. Only accepted material proceeds to repository inspection.
    # Freeze only pointers in this repository's committed tree. No submodule
    # URL is consulted, and no recursively owned repository is fetched.
    # A symlinks-False profile refuses any tracked symlink before anything is frozen.
    features = profile.get('repository_features', {})
    pointers = features.get('submodules') == 'uninitialized-pointers-v1'
    if pointers or features.get('symlinks') is False:
        links = {}
        for entry in public_git(['ls-tree', '-rz', '--full-tree', 'HEAD'], cwd=root).split(b'\0'):
            if entry:
                metadata, path = entry.split(b'\t', 1)
                mode, kind, oid = metadata.decode('ascii').split()
                if mode == '120000' and features.get('symlinks') is False:
                    raise ValueError('tracked_symlink_unsupported')
                if pointers and mode == '160000':
                    if kind != 'commit':
                        raise ValueError('invalid_repository_gitlinks')
                    links[path.decode('utf-8')] = oid
        if links:
            environment['repository_gitlinks'] = validate_repository_gitlinks(links)
            # The frozen pointers are part of the runtime config.
            config = build_config(environment, command=command, timeout_s=timeout_s, toolchain=toolchain)
    # Local AND deferred preparation refuse a base the runtime's
    # snapshot would refuse, by its own cause, without Docker or the base test.
    refusal = source_fit_refusal(config, cwd=str(root))
    if refusal is not None:
        raise ValueError(refusal)
    requirements = execution_requirements(environment, config, languages=languages, command_digest='sha256:' + '0' * 64)
    return environment, config, requirements


# The runtime's installed-artifact roots (``_snapshot``); parity is pinned by
# tests/mcp_server/test_prepare_source_fit.py against both runtime copies.
_RUNTIME_INSTALLED_ROOTS = frozenset({'.venv', 'venv', 'node_modules', '.ormas-install', '.ormas-cache'})


def source_fit_refusal(config, *, cwd):
    """The runtime ``_snapshot`` refusal this clean base would hit, else None.

    Inspects the checkout's Git top-level (the tree the verifier copies), not a
    subdirectory the client named. Mirrors ``_snapshot`` membership and limits
    in its order (exclusion overrides, ``ls-files --cached --others
    --exclude-standard``, frozen gitlinks, file limit, installed artifacts,
    regular files, the profile's ``source_max_bytes``/``max_bytes``, frozen
    locks present and unchanged) with the runtime's cause names. Portable:
    plain ``subprocess.run`` and ``os.lstat``, no pipe selectors, process
    groups or descriptor-relative opens, so native Windows deferred
    preparation works. Copies nothing and starts no container.
    """
    if 'candidate_argv' in config:
        from . import graded_verifier_runtime as runtime
    else:
        from . import verifier_runtime as runtime
    env = {"PATH": os.environ.get("PATH", os.defpath), "HOME": str(Path.home()),
           "LANG": "C.UTF-8", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    if os.name == 'nt':
        env.update({k: os.environ[k] for k in ('SYSTEMROOT', 'PATHEXT', 'USERPROFILE') if k in os.environ})
    deadline = time.monotonic() + config['limits']['timeout_s']
    try:
        code, top = _inventory_git(runtime, ['rev-parse', '--show-toplevel'],
                                   cwd=Path(cwd).resolve(), deadline=deadline, cap=4096, env=env)
        if code or not top.strip():
            return 'source_inventory_unavailable'
        _inspect_snapshot(runtime, Path(os.fsdecode(top.strip())).resolve(), config, deadline, env)
    except runtime.RuntimeRefusal as exc:
        return str(exc)
    except (OSError, ValueError, UnicodeError, subprocess.SubprocessError):
        return 'source_inventory_unavailable'
    return None


def _inventory_git(runtime, args, *, cwd, deadline, cap, env, warnings=None):
    """Portable stand-in for the runtime's ``_bounded`` git reads (stderr merged).

    With ``warnings`` (a list), stderr is kept apart and appended there instead.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise runtime.RuntimeRefusal('verification_timeout')
    try:
        completed = subprocess.run(runtime.safe_git_argv(*args), cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT if warnings is None else subprocess.PIPE,
                                   timeout=remaining, check=False)
    except subprocess.TimeoutExpired:
        raise runtime.RuntimeRefusal('verification_timeout') from None
    if len(completed.stdout) + len(completed.stderr or b'') > cap:
        raise runtime.RuntimeRefusal('verification_output_limit')
    if warnings is not None and completed.stderr:
        warnings.append(completed.stderr)
    return completed.returncode, completed.stdout


def _source_entry(source, name, refuse, readable_dirs):
    """``lstat`` of a listed path, refusing what the runtime's ``_source_file`` refuses.

    FileNotFoundError (absent) propagates; a non-directory parent raises
    NotADirectoryError, and an unreadable parent raises PermissionError, as the
    runtime's readable no-follow directory open would fail. Each directory is
    probed once with ``os.scandir``, which also works on Windows.
    """
    parts = name.split('/')
    if not name or any(p in ('', '.', '..', '.git') for p in parts) or '\\' in name:
        raise refuse('unsupported_source_path')
    path = source
    for part in parts[:-1]:
        path = path / part
        if not stat.S_ISDIR(os.lstat(path).st_mode):
            raise NotADirectoryError(name)
        if path not in readable_dirs:
            with os.scandir(path):
                pass
            readable_dirs.add(path)
    return path / parts[-1], os.lstat(path / parts[-1])


def _inspect_snapshot(runtime, source, cfg, deadline, env):
    refuse = runtime.RuntimeRefusal
    code, local_excludes = _inventory_git(
        runtime, ['config', '--local', '--includes', '--get', 'core.excludesFile'],
        cwd=source, deadline=deadline, cap=4096, env=env)
    if code == 0 and local_excludes.strip():
        raise refuse('repository_exclusion_override_unsupported')
    code, exclude_path = _inventory_git(runtime, ['rev-parse', '--git-path', 'info/exclude'],
                                        cwd=source, deadline=deadline, cap=4096, env=env)
    if code:
        raise refuse('source_inventory_unavailable')
    path = Path(os.fsdecode(exclude_path.strip()))
    if not path.is_absolute():
        path = source / path
    if path.exists():
        if path.is_symlink() or path.stat().st_size > 65536:
            raise refuse('repository_exclusion_override_unsupported')
        if any(line.strip() and not line.lstrip().startswith('#') for line in path.read_text().splitlines()):
            raise refuse('repository_exclusion_override_unsupported')
    listing_warnings = []
    code, raw = _inventory_git(runtime, ['ls-files', '-z', '--cached', '--others', '--exclude-standard'],
                               cwd=source, deadline=deadline, cap=4 * 1024 * 1024, env=env,
                               warnings=listing_warnings)
    if code:
        raise refuse('source_inventory_unavailable')
    if listing_warnings:
        # Git could not read part of the tree (for example an unreadable
        # directory). The runtime merges this text into its file list and then
        # fails or silently drops a file; neither is a faithful copy.
        raise refuse('unsupported_source_file')
    names = sorted(set(os.fsdecode(n) for n in raw.split(b'\0') if n))
    gitlinks = runtime.validate_repository_gitlinks(cfg.get('repository_gitlinks', {}))
    if _indexed_gitlinks(runtime, source, deadline, env) != gitlinks:
        raise refuse('repository_gitlink_changed')
    if len(names) > cfg['limits']['max_files']:
        raise refuse('source_file_limit')
    byte_limit = cfg['limits'].get('source_max_bytes', cfg['limits']['max_bytes'])
    locks = cfg.get('lock_files', {})
    fingerprints = {}
    readable_dirs = {source}
    total = 0
    for name in names:
        if name.split('/')[0] in _RUNTIME_INSTALLED_ROOTS:
            raise refuse('source_installed_artifacts_refused')
        if time.monotonic() >= deadline:
            raise refuse('verification_timeout')
        if name in gitlinks:
            _empty_gitlink(source, name, refuse, readable_dirs)
            fingerprints[name] = 'gitlink:' + gitlinks[name]
            continue
        try:
            path, info = _source_entry(source, name, refuse, readable_dirs)
        except FileNotFoundError:
            continue  # A deleted file is absent in the runtime's copy too.
        except OSError:
            raise refuse('unsupported_source_file') from None
        if not stat.S_ISREG(info.st_mode):
            raise refuse('unsupported_source_file')
        try:
            with open(path, 'rb'):
                pass  # The runtime opens every file for reading before copying.
        except OSError:
            raise refuse('unsupported_source_file') from None
        total += info.st_size
        if total > byte_limit:
            raise refuse('source_byte_limit')
        if name in locks:
            fingerprints[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    for name in gitlinks:
        _empty_gitlink(source, name, refuse, readable_dirs)
    # The runtime fingerprints every copied file; a frozen lock that is
    # ignored, untracked, absent or changed is not in its copy.
    for name, expected in locks.items():
        if fingerprints.get(name) != expected:
            raise refuse('dependency_lock_changed')


def _indexed_gitlinks(runtime, source, deadline, env):
    code, raw = _inventory_git(runtime, ['ls-files', '--stage', '-z'],
                               cwd=source, deadline=deadline, cap=4 * 1024 * 1024, env=env)
    if code:
        raise runtime.RuntimeRefusal('source_inventory_unavailable')
    links = {}
    for entry in raw.split(b'\0'):
        if not entry:
            continue
        metadata, path = entry.split(b'\t', 1)
        mode, oid, stage = metadata.split()
        if mode == b'160000':
            if stage != b'0':
                raise runtime.RuntimeRefusal('repository_gitlink_changed')
            links[os.fsdecode(path)] = oid.decode('ascii')
    return runtime.validate_repository_gitlinks(links)


def _empty_gitlink(source, name, refuse, readable_dirs):
    try:
        path, info = _source_entry(source, name, refuse, readable_dirs)
    except FileNotFoundError:
        return
    except OSError:
        raise refuse('repository_gitlink_materialized') from None
    if not stat.S_ISDIR(info.st_mode):
        raise refuse('repository_gitlink_materialized')
    try:
        with os.scandir(path) as entries:
            if next(entries, None) is not None:
                raise refuse('repository_gitlink_materialized')
    except OSError:
        raise refuse('repository_gitlink_materialized') from None


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


def validate_public_execution_packet(packet, *, allow_revoked_compiler=False):
    """Recompile, don't trust client-supplied executable or preflight identities.

    ``allow_revoked_compiler`` binds the packet to the compiler its frozen catalog
    names whether or not that compiler still holds execution approval; every other
    check runs unchanged. Only settled-job accounting reads use it (the reward
    scorer judging a paid receipt against its frozen acceptance); anything that
    claims, leases, executes or repairs keeps the default and stays refused.
    """
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
    if isinstance(environment, dict) and 'graded_custody' in environment:
        fields.add('graded_custody')
    if not isinstance(environment, dict) or set(environment) != fields:
        raise ValueError('invalid_execution_environment')
    if environment['schema_version'] != 'outcomes.execution-environment.v1' or not isinstance(environment['profile'], dict):
        raise ValueError('invalid_execution_environment')
    if 'graded_custody' in environment:
        custody = environment['graded_custody']
        if not isinstance(custody, str) or custody != 'operator_services':
            raise ValueError('invalid_execution_environment')
        if 'repository_access' not in environment or not (
                packet.get('acceptance_level') == 'graded' or 'graded_contract' in packet):
            raise ValueError('graded_custody_scope_invalid')
    catalog, profile = _profile(environment['profile'].get('profile_id'), environment['catalog_digest'])
    if environment['catalog_digest'] != digest(catalog) or environment['profile'] != profile:
        raise ValueError('execution_catalog_changed')
    compiler = _frozen_compiler(catalog) if allow_revoked_compiler else _active_compiler(catalog)
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
    files = _validate_acceptance_files(environment['acceptance_files'])
    vp, req, policy = packet.get('verifier_profile'), packet.get('execution_requirements'), packet.get('execution_policy')
    if not isinstance(vp, dict) or not isinstance(req, dict) or not isinstance(policy, dict):
        raise ValueError('public_execution_contract_required')
    validate_gitlink_scope(environment, policy.get('allowed_paths'))
    limits = req.get('limits')
    if not isinstance(limits, dict) or type(limits.get('timeout_s')) is not int:
        raise ValueError('invalid_runtime_limits')
    graded = packet.get('acceptance_level') == 'graded' or 'graded_contract' in packet
    if catalog.get('acceptance_level') == 'graded' and not graded:
        raise ValueError('graded_compiler_requires_graded_packet')
    build_config = graded_runtime_config if catalog.get('acceptance_level') == 'graded' else runtime_config
    config = build_config(environment, command=vp.get('original_command'), timeout_s=limits['timeout_s'], toolchain=packet.get('toolchain'))
    expected_command = packet.get('verification_command')
    if not isinstance(expected_command, str):
        raise ValueError('verifier_compiler_mismatch')
    encoded = base64.b64encode(json.dumps(config, sort_keys=True, separators=(',', ':')).encode()).decode()
    try:
        source = (Path(__file__).with_name('graded_verifier_runtime.py').read_text()
                  if 'candidate_argv' in config else _runtime_source())
    except (OSError, UnicodeDecodeError):
        source = None
    candidate = (shlex.join(['python3', '-I', '-S', '-c', source, encoded])
                 if source is not None and hashlib.sha256(source.encode()).hexdigest() == compiler else None)
    if candidate != expected_command:
        arguments = shlex.split(expected_command)
        if (len(arguments) != 6 or arguments[:4] != ['python3', '-I', '-S', '-c']
                or hashlib.sha256(arguments[4].encode()).hexdigest() != compiler
                or arguments[5] != encoded or shlex.join(arguments) != expected_command):
            raise ValueError('verifier_compiler_mismatch')
    sha = hashlib.sha256(expected_command.encode()).hexdigest()
    # int == float in Python, so packets written as 600.0 (pre-2026-10-09 clients) and 600 both pass.
    if vp != {'schema_version': 'outcomes.verifier-profile.v1', 'profile_id': 'external-driver-v1',
              'original_command': vp['original_command'], 'generated_command_sha256': sha,
              'timeout_s': config['limits']['timeout_s'], 'max_files': config['limits']['max_files'],
              'max_bytes': config['limits']['max_bytes'], 'execution_mode': 'oci-v1', 'assertion_failure_exit_code': 86}:
        raise ValueError('verifier_profile_mismatch')
    expected = execution_requirements(environment, config, languages=req.get('languages'), command_digest='sha256:' + sha)
    if req != expected or not isinstance(packet.get('task_features'), dict) or packet['task_features'].get('languages') != req['languages']:
        raise ValueError('execution_requirements_mismatch')
    if (graded and expected['repository_visibility'] == 'public'
            and GRADED_PUBLIC_PRIVATE_FIELDS & packet.keys()):
        raise ValueError('graded_public_form_private_binding')
    immutable = policy.get('immutable_paths')
    if not isinstance(immutable, list) or any(not isinstance(p, str) for p in immutable):
        raise ValueError('invalid_immutable_paths')
    if any(not any(path == rule or path.startswith(rule.rstrip('/') + '/') for rule in immutable) for path in locks):
        raise ValueError('dependency_locks_must_be_immutable')
    if any(not any(path == rule or path.startswith(rule.rstrip('/') + '/') for rule in immutable) for path in files):
        raise ValueError('acceptance_files_must_be_immutable')
    # v2 (proven locally by the preparer) is byte-compatible with the
    # original closed shape; v3's remote/deferred
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
    if graded:
        # Graded freezes regression observations, not proof of an acceptance
        # assertion. Keep the Verified v2/v3 preimages and compiler unchanged.
        contract = packet.get('graded_contract')
        if (packet.get('acceptance_level') != 'graded' or not isinstance(contract, dict)
                or not isinstance(contract.get('digest'), str)
                or re.fullmatch('[0-9a-f]{64}', contract['digest']) is None
                or contract.get('base_sha') != policy.get('repo_base_sha')
                or contract.get('immutable_paths') != immutable
                or contract.get('regression_commands') != [vp['original_command']]):
            raise ValueError('graded_base_preflight_required')
        statuses = contract.get('regression_base_status')
        if (not isinstance(statuses, dict) or set(statuses) != {vp['original_command']}
                or any(status not in ('green', 'red', 'unknown') for status in statuses.values())):
            raise ValueError('graded_base_preflight_required')
        deferred = isinstance(verify_base, dict) and verify_base.get('source') == 'deferred'
        if any(status not in (('unknown',) if deferred else ('green', 'red')) for status in statuses.values()):
            raise ValueError('graded_base_preflight_required')
        # Commit the whole contract mapping, including its canonical contract
        # digest. The gateway independently validates GradedContract semantics;
        # this stdlib wire validator must also detect body/digest substitution.
        evidence = {
            'schema_version': 'outcomes.graded-base-preflight.v1',
            'source': 'deferred' if deferred else 'packet',
            'outcome': 'not_run' if deferred else 'observed',
            **({'reason': 'client_preflight_deferred'} if deferred else {}),
            'verifier_digest': expected['verifier_digest'],
            'environment_digest': expected['environment_digest'],
            'base_sha': policy.get('repo_base_sha'), 'contract_digest': digest(contract),
            'regression_base_status': statuses,
        }
        if verify_base != evidence:
            raise ValueError('graded_base_preflight_required')
    elif verify_base != v2 and verify_base != v3:
        raise ValueError('public_base_preflight_required')
    return expected


def _trusted_tool(name):
    try:
        return trusted_executable(name)
    except TrustedExecutableUnavailable as exc:
        raise ValueError(f'trusted {name} executable unavailable') from exc


def _git_environment():
    git = _trusted_tool('git')
    environment = {'PATH': str(git.parent), 'HOME': '/nonexistent',
                   'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_SYSTEM': os.devnull,
                   'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_TERMINAL_PROMPT': '0',
                   'GIT_NO_REPLACE_OBJECTS': '1'}
    gate = os.environ.get('ORMAS_NITRO_GATE_URL')
    if gate is not None:
        environment['https_proxy'] = gate
    return environment


def _isolated_git(args, *, cwd, env):
    git = _trusted_tool('git')
    proc = subprocess.run([str(git), '--literal-pathspecs', '-c', 'credential.helper=', '-c', 'core.hooksPath=' + os.devnull,
        '-c', 'core.fsmonitor=false', '-c', 'protocol.ext.allow=never',
        '-c', 'protocol.file.allow=never', *args], cwd=str(cwd), env=env,
        capture_output=True, check=False, timeout=120)
    if proc.returncode != 0:
        raise ValueError('public Git operation failed') from subprocess.CalledProcessError(
            proc.returncode, 'git', stderr=proc.stderr)
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
    nitro = 'ORMAS_NITRO_GATE_URL' in os.environ
    repository_path = parts.path.lstrip('/')
    binding = (parts.scheme, parts.netloc, repository_path.rstrip('/') if nitro else repository_path)
    env = _git_environment()
    with tempfile.TemporaryDirectory(prefix='ormas-read-') as directory:
        helper, endpoint = Path(directory) / 'helper.py', str(Path(directory) / 'channel')
        fd = os.open(helper, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(_APP_READ_HELPER)
        flags = _app_read_flags(repo_url, '!' + shlex.join([sys.executable, '-I', str(helper), endpoint]))
        if nitro:
            try:
                url_exact = _app_read_url_is_exact(flags, cwd=cwd, env=env, repo_url=repo_url)
            except (OSError, ValueError, subprocess.SubprocessError):
                url_exact = None
            if url_exact is None:
                raise ValueError('private_repository_http_config')
        else:
            url_exact = _app_read_url_is_exact(flags, cwd=cwd, env=env, repo_url=repo_url)
        if not url_exact:
            raise ValueError('private_repository_url_rewritten')
        def http_exact():
            if not _app_read_http_is_exact(flags, cwd=cwd, env=env, repo_url=repo_url):
                return False
            transport_root = repo_url.rstrip('/') + '/'
            return not nitro or _app_read_http_is_exact(
                flags, cwd=cwd, env=env, repo_url=transport_root)

        if nitro:
            try:
                output = _isolated_git([*flags, 'config', '--null', '--list'], cwd=cwd, env=env)
                entries = output.split(b'\0')
            except (OSError, ValueError, subprocess.SubprocessError):
                entries = None
            if entries is None:
                raise ValueError('private_repository_http_config')
            for entry in entries:
                key, _, _ = entry.partition(b'\n')
                if key.startswith(b'remote.'):
                    subsection, separator, _ = key.removeprefix(b'remote.').rpartition(b'.')
                    if separator and subsection == repo_url.encode():
                        raise ValueError('private_repository_url_rewritten')
            try:
                exact = http_exact()
            except (OSError, ValueError, subprocess.SubprocessError):
                exact = False
            if not exact:
                raise ValueError('private_repository_http_config')
        if nitro and args[0] == 'fetch':
            args = ['fetch', '--no-recurse-submodules', *args[1:]]
        wake, woken = os.pipe()
        try:
            with socket.socket(socket.AF_UNIX) as listener:
                listener.bind(endpoint)
                listener.listen()
                release = threading.Thread(target=_release_app_read, daemon=True, args=(
                    listener, wake, binding, token, expiry, http_exact))
                release.start()
                try:
                    return _isolated_git([*flags, *args], cwd=cwd, env=env)
                finally:
                    os.write(woken, b'\0')
                    release.join()
        finally:
            os.close(wake)
            os.close(woken)


def protected_repository_git(args, *, cwd, repo_url, token, expires_at):
    """Read one frozen repository with an attested token, never a wire credential."""
    validate_repository_credential('public', repo_url, None)  # canonical URL only
    if not isinstance(token, str) or not re.fullmatch(r'[\x21-\x7e]+', token):
        raise ValueError('private_repository_read_access_required')
    try:
        if not isinstance(expires_at, str):
            raise ValueError()
        expiry = datetime.fromisoformat(expires_at.removesuffix('Z') +
                                       ('+00:00' if expires_at.endswith('Z') else ''))
    except (TypeError, ValueError):
        raise ValueError('private_repository_read_access_required') from None
    if expiry.tzinfo is None or expiry.utcoffset() is None:
        raise ValueError('private_repository_read_access_required')
    if expiry <= datetime.now(timezone.utc):
        raise ValueError('private_repository_read_access_expired')
    if (not isinstance(args, (list, tuple)) or not args or not isinstance(args[0], str)
            or args[0] not in {'clone', 'fetch'} or args.count(repo_url) != 1):
        raise ValueError('private_repository_read_operation_required')
    return _app_read_git(args, cwd=cwd, repo_url=repo_url, token=token, expiry=expiry)


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
        env['GIT_SSH_COMMAND'] = shlex.join([str(_trusted_tool('ssh')), '-F', os.devnull, '-i', str(key),
            '-o', 'IdentityAgent=none', '-o', 'IdentitiesOnly=yes',
            '-o', 'StrictHostKeyChecking=yes', '-o', 'UserKnownHostsFile=' + str(hosts),
            '-o', 'GlobalKnownHostsFile=' + os.devnull, '-o', 'HostKeyAlgorithms=ssh-ed25519',
            '-o', 'BatchMode=yes'])
        return _isolated_git(args, cwd=cwd, env=env)



def public_result_scope(packet, workdir, result_commit):
    """Return the authoritative public result scope decision and changed paths.

    The decision uses the same frozen allowed/immutable rules the artifact
    serializer and the gateway apply (``_validate_raw_file_paths``).
    """
    validate_public_execution_packet(packet)
    if 'publication' not in packet['execution_requirements'] or not re.fullmatch('[0-9a-f]{40}', result_commit):
        raise ValueError('public artifact contract required')
    policy = packet['execution_policy']
    allowed, immutable = policy.get('allowed_paths'), policy.get('immutable_paths')
    if not isinstance(allowed, list) or any(not isinstance(path, str) for path in allowed):
        raise ValueError('invalid_allowed_paths')
    if not isinstance(immutable, list) or any(not isinstance(path, str) for path in immutable):
        raise ValueError('invalid_immutable_paths')
    raw = public_git(['diff', '--name-only', '--no-renames', '-z', policy['repo_base_sha'],
        result_commit, '--'], cwd=workdir)
    if raw and not raw.endswith(b'\0'):
        raise ValueError('public artifact path list is not NUL terminated')
    try:
        changed = tuple(sorted(raw[:-1].decode('utf-8').split('\0'))) if raw else ()
    except UnicodeDecodeError:
        raise ValueError('public artifact path is not valid UTF-8') from None
    try:
        _validate_raw_file_paths(changed, policy)
    except ValueError:
        return False, changed
    return True, changed


def build_public_artifact(packet, workdir, result_commit, target):
    """Serialize committed regular files as bounded raw bytes, never a Git pack.

    Scope refusals (``public_artifact_out_of_scope``, ``public_artifact_immutable``,
    secret paths) are raised by ``_validate_raw_file_paths`` before any blob is read.
    """
    validate_public_execution_packet(packet)
    if 'publication' not in packet['execution_requirements'] or not re.fullmatch('[0-9a-f]{40}', result_commit):
        raise ValueError('public artifact contract required')
    return build_raw_file_artifact(packet['execution_policy'], workdir, result_commit, target,
        max_bytes=packet['execution_requirements']['limits']['max_bytes'],
        max_files=packet['execution_requirements']['publication']['max_changed_files'])


def graded_delivery_limits(value):
    """Explicit packet bounds; never borrow scratch or public-execution defaults."""
    if (not isinstance(value, Mapping) or set(value) != {'max_bytes', 'max_files'}
            or any(type(value[key]) is not int or value[key] <= 0 for key in value)):
        raise ValueError('graded_delivery_limits_required')
    return dict(value)


def build_graded_artifact(packet, workdir, result_commit, target):
    """Graded custody uses the raw-file wire, not public-execution authority."""
    if packet.get('acceptance_level') != 'graded' or not isinstance(packet.get('graded_contract'), Mapping):
        raise ValueError('graded_contract_required')
    limits = graded_delivery_limits(packet.get('delivery_limits'))
    return build_raw_file_artifact(packet['execution_policy'], workdir, result_commit, target,
                                  **limits, enforce_allowed_paths=False)


def _validate_raw_file_paths(paths, policy, *, enforce_allowed_paths=True):
    import unicodedata
    from ._raw_file_paths import _root_covering

    root_scope = any(_root_covering(rule) for rule in policy.get('allowed_paths') or ())
    if root_scope:
        import fnmatch
        from ._raw_file_paths import (
            PatchIntakeError, SECRET_PATH_PATTERNS, _filesystem_key, _path, _path_is_allowed,
            _patterns,
        )

        immutable = list(policy.get('immutable_paths', []))
        # Preserve publication's literal-subtree bounds as well as S8's glob
        # and filesystem-alias refusals, just as the root material selector does.
        immutable += [rule.rstrip('/') + '/**' for rule in immutable
                      if not any(character in rule for character in '*?[')]
        immutable_keys = _patterns([_filesystem_key(rule) for rule in immutable], 'immutable_path')

    def covers(path, permission):
        return path == permission.rstrip('/') or path.startswith(permission.rstrip('/') + '/')

    seen = set()
    for path in paths:
        if (len(path.encode('utf-8')) > 4096 or path != unicodedata.normalize('NFC', path)
                or '\\' in path or any(ord(ch) < 32 or ord(ch) == 127 for ch in path)
                or any(part in ('', '.', '..') or part.casefold() == '.git'
                       for part in path.split('/'))):
            raise ValueError('public_artifact_path_invalid')
        if path.casefold() in seen:
            raise ValueError('public_artifact_path_collision')
        seen.add(path.casefold())
        if root_scope:
            _path(path)  # S8's repository-control and unsafe-path refusals.
            key = _filesystem_key(path)
            if _path_is_allowed(key, immutable_keys):
                raise ValueError('public_artifact_immutable')
            if any(fnmatch.fnmatchcase(component, pattern) for component in key.split('/')
                   for pattern in SECRET_PATH_PATTERNS):
                raise PatchIntakeError('secret_path')
        if enforce_allowed_paths and not root_scope and not any(covers(path, rule) for rule in policy['allowed_paths']):
            raise ValueError('public_artifact_out_of_scope')
        if any(covers(path, rule) or covers(rule, path) for rule in policy.get('immutable_paths', [])):
            raise ValueError('public_artifact_immutable')


def build_raw_file_artifact(policy, workdir, result_commit, target, *, max_bytes, max_files,
                            enforce_allowed_paths=True):
    """Serialize the same raw-file bundle without requiring v2 execution authority."""
    if any(type(value) is not int or value <= 0 for value in (max_bytes, max_files)):
        raise ValueError('graded_delivery_limits_required')
    if not isinstance(result_commit, str) or not re.fullmatch('[0-9a-f]{40}', result_commit):
        raise ValueError('public artifact contract required')
    changed = public_git(['diff', '--name-only', '--no-renames', '-z', policy['repo_base_sha'],
        result_commit, '--'], cwd=workdir).decode('utf-8').split('\0')
    changed = sorted(p for p in changed if p)
    if not 0 < len(changed) <= max_files:
        raise ValueError('public artifact file limit')
    _validate_raw_file_paths(changed, policy, enforce_allowed_paths=enforce_allowed_paths)
    tree = {}
    # The immutable tree supplies blob sizes too; avoid a Git process per size.
    # Limit size lookups to changed paths, using public_git's literal pathspecs.
    listing = ['ls-tree', '-rlz', '--full-tree', result_commit]
    try:
        records = public_git([*listing, '--', *changed], cwd=workdir)
    except OSError as exc:
        import errno

        if exc.errno != errno.E2BIG:
            raise
        # Preserve large, explicitly bounded deliveries beyond the OS argv limit.
        records = public_git(listing, cwd=workdir)
    for record in records.split(b'\0'):
        if record:
            metadata, path = record.split(b'\t', 1)
            mode, kind, sha, length = metadata.decode('ascii').split()
            tree[path.decode('utf-8')] = (mode, kind, sha, length)
    tree_sha = public_git(['rev-parse', result_commit + '^{tree}'], cwd=workdir).decode().strip()
    entries = []
    size = 0
    with tempfile.TemporaryFile() as bodies:
        for path in changed:
            item = tree.get(path)
            if item is None:
                entries.append({'path': path, 'op': 'delete', 'mode': None, 'byte_len': 0, 'sha256': None})
                continue
            mode, kind, sha, raw_length = item
            if mode not in ('100644', '100755') or kind != 'blob':
                raise ValueError('public artifact file mode unsupported')
            if re.fullmatch('[0-9]+', raw_length) is None:
                raise ValueError('public artifact blob mismatch')
            length = int(raw_length)
            size += length
            if size > max_bytes:
                raise ValueError('public artifact byte limit')
            raw = public_git(['cat-file', 'blob', sha], cwd=workdir)
            if len(raw) != length:
                raise ValueError('public artifact blob mismatch')
            bodies.write(raw)
            entries.append({'path': path, 'op': 'upsert', 'mode': mode,
                'byte_len': length, 'sha256': hashlib.sha256(raw).hexdigest()})
        header = json.dumps({'schema_version': 'ormas.public-artifact.v1',
            'base_commit': policy['repo_base_sha'], 'tree_sha': tree_sha, 'files': entries},
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
