"""The deployed catalog is executable only with its exact draining compiler pair."""
from __future__ import annotations

import copy
import hashlib
from importlib import resources

import pytest

from ormas_subnet import outcomes_support as support
from tests.test_acceptance_driver_revocation import _packet_for_digest


DEPLOYED_DIGEST = 'sha256:ff7c1e51b36c54f8aa8b63d2b9b6ea91c4d1587ee39bc31575b7763ffbf40367'
DEPLOYED_COMPILER = '214eadfb183270c187f0dba07ad16123ea402a90dcd2e13d883b906d2060db1c'
PUBLIC_DIGEST = 'sha256:398f6c9fca4744e9414171eccc39401701d76a5f22b31e7ebcd6d10fd169889e'
PUBLIC_COMPILER = '2ba990540ac8653f130efe5ed132a0dfdca6048db18386352719d3a317ae2c00'
REVOKED_DIGEST = 'sha256:e4511421b6b2d459810e31cf50338c12bd91b02cee054a98d69d602cd9b0717f'


def test_deployed_catalog_bytes_and_distinct_public_default():
    raw = resources.files('ormas_subnet').joinpath(
        'client_assets/outcomes_support_history/2026-10-14.4-ff7c1e51b36c.json',
    ).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == (
        'bdb55f9d9d1149ef9e5227168478d3a4b7639a4d122c33bcf75319af1fa200cd'
    )
    deployed = support.load_support_catalog(DEPLOYED_DIGEST)
    current = support.load_support_catalog()
    assert support.digest(deployed) == DEPLOYED_DIGEST
    assert support.digest(current) == PUBLIC_DIGEST
    assert deployed['revision'] == current['revision'] == '2026-10-14.4'
    assert deployed != current


@pytest.mark.parametrize(('catalog_digest', 'compiler'), [
    (DEPLOYED_DIGEST, DEPLOYED_COMPILER),
    (PUBLIC_DIGEST, PUBLIC_COMPILER),
])
def test_current_and_draining_catalog_compilers_are_executable(catalog_digest, compiler):
    assert support._active_compiler(support.load_support_catalog(catalog_digest)) == compiler


def test_draining_compiler_is_not_globally_active():
    assert DEPLOYED_COMPILER not in support._ACTIVE_VERIFIER_COMPILERS


@pytest.mark.parametrize('change', ['revision', 'profile', 'compiler'])
def test_draining_allowance_is_bound_to_the_entire_catalog(change):
    catalog = copy.deepcopy(support.load_support_catalog(DEPLOYED_DIGEST))
    if change == 'revision':
        catalog['revision'] = '2026-10-14.5'
    elif change == 'profile':
        catalog['profiles'][0]['limits']['cpus'] = 99
    else:
        catalog['verifier_compiler_sha256'] = support.load_support_catalog(
            REVOKED_DIGEST,
        )['verifier_compiler_sha256']
    with pytest.raises(ValueError, match='^verifier_compiler_revoked$'):
        support._active_compiler(catalog)


def test_draining_compiler_cannot_be_used_with_another_historical_catalog():
    catalog = copy.deepcopy(support.load_support_catalog(REVOKED_DIGEST))
    catalog['verifier_compiler_sha256'] = DEPLOYED_COMPILER
    with pytest.raises(ValueError, match='^verifier_compiler_revoked$'):
        support._active_compiler(catalog)


def test_draining_packet_still_requires_its_exact_embedded_verifier():
    # This synthetic packet deliberately embeds the current (not draining) runtime.
    packet, _ = _packet_for_digest('1' * 64, catalog_digest=DEPLOYED_DIGEST)
    with pytest.raises(ValueError, match='^verifier_compiler_mismatch$'):
        support.validate_public_execution_packet(packet)


def test_public_default_packet_still_validates():
    packet, _ = _packet_for_digest('1' * 64)
    assert support.validate_public_execution_packet(packet) == packet['execution_requirements']
