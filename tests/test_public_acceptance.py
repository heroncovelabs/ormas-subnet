from __future__ import annotations

import copy
from pathlib import Path

import pytest

from ormas_subnet import protocol as public_protocol
from ormas_subnet import public_acceptance as public_acceptance
from ormas_subnet.skeleton import MinerConfig, MinerSkeleton
from ormas_subnet.validator import (
    ValidatorConfig,
    ValidatorDaemon,
    canonical_evidence_fields,
    evidence_digest_hex,
)

ROOT = Path(__file__).resolve().parents[2]


def _private_acceptance():
    return pytest.importorskip("tensorbox_spec.client_assets.public_acceptance")


def _private_protocol():
    return pytest.importorskip("tensorbox_spec.mcp_server.ormas_http_client")


def _policy(*, required: int = 2) -> dict:
    return {
        "schema_version": "ormas.public-acceptance-policy.v1",
        "required_validators": required,
        "catalog_digest": "sha256:" + "a" * 64,
        "profile_id": "public-python-small-v1",
        "protocol": "ormas.public-acceptance-contract.v1",
        "liveness_s": 60,
        "timeout_s": 120,
        "max_concurrent_assignments": 1,
    }


def _contract() -> dict:
    return {
        "schema_version": "ormas.public-acceptance-contract.v1",
        "policy": _policy(),
        "miner": {
            "subject_id": "runr_jake",
            "operator_id": "operator:jake",
            "qualification_id": "qual_miner_jake_v1",
            "credential_id": "rtok_jake",
            "miner_id": "miner:jake",
        },
        "validators": [
            {
                "subject_id": "val_1",
                "operator_id": "operator:validator-1",
                "qualification_id": "qual_validator_1_v1",
                "credential_id": "1" * 64,
            },
            {
                "subject_id": "val_2",
                "operator_id": "operator:validator-2",
                "qualification_id": "qual_validator_2_v1",
                "credential_id": "2" * 64,
            },
        ],
        "claim_nonce": "claim_123",
    }


def _draft_kwargs() -> dict:
    return {
        "task_id": "job_public",
        "runner_id": "runr_jake",
        "repo_id": "",
        "base_commit": "b" * 40,
        "brief": "repair the public check",
        "verify_command": "pytest -q",
        "allowed_paths": ["src/feature.py"],
        "budget_usd": None,
        "work_packet": {"task": "repair the public check"},
        "work_packet_sha256": "c" * 64,
        "attempt": 1,
        "parent_job_id": "",
        "repair_findings": [],
    }


def _evidence(contract: dict) -> dict:
    return canonical_evidence_fields(
        job_id="job_public",
        miner_id="miner:jake",
        base_commit="b" * 40,
        result_commit="c" * 40,
        repo_url="https://github.com/example/public.git",
        verify_command="pytest -q",
        allowed_paths=["src/feature.py"],
        immutable_paths=["tests/test_feature.py"],
        acceptance_contract=contract,
    )


def test_private_and_public_acceptance_helpers_are_exact_mirrors() -> None:
    private_path = ROOT / "tensorbox_spec/client_assets/public_acceptance.py"
    public_path = ROOT / "public_subnet/ormas_subnet/public_acceptance.py"
    if not private_path.exists():
        pytest.skip("private monorepo mirror is not installed")
    assert private_path.read_bytes() == public_path.read_bytes()


def test_policy_and_contract_validation_return_independent_snapshots() -> None:
    private_acceptance = _private_acceptance()
    original = _contract()
    private_policy = private_acceptance.validate_acceptance_policy(original["policy"])
    private_contract = private_acceptance.validate_acceptance_contract(original)
    public_contract = public_acceptance.validate_acceptance_contract(original)
    assert private_contract == public_contract == original

    original["policy"]["profile_id"] = "changed"
    original["validators"][0]["subject_id"] = "changed"
    assert private_policy["profile_id"] == "public-python-small-v1"
    assert private_contract["policy"]["profile_id"] == "public-python-small-v1"
    assert private_contract["validators"][0]["subject_id"] == "val_1"


def _operator_run_contract() -> dict:
    contract = _contract()
    contract["schema_version"] = "ormas.public-acceptance-contract.v2"
    contract["policy"].update(
        schema_version="ormas.public-acceptance-policy.v2",
        protocol=contract["schema_version"],
        verification_mode="operator-run",
        validator_operator_id="operator:ormas",
        required_validators=1,
    )
    contract["miner"]["operator_id"] = "operator:ormas"
    contract["validators"] = contract["validators"][:1]
    contract["validators"][0]["operator_id"] = "operator:ormas"
    return contract


def test_operator_run_alpha_round_trips_and_binds_signed_owner_policy() -> None:
    contract = _operator_run_contract()
    assert public_acceptance.validate_acceptance_contract(contract) == contract
    wire = public_protocol.TaskDraft(**_draft_kwargs(), acceptance_contract=contract).to_wire()
    assert public_protocol.TaskDraft.from_wire(wire).acceptance_contract == contract
    before = evidence_digest_hex(_evidence(contract))
    other = copy.deepcopy(contract)
    other["policy"]["validator_operator_id"] = "operator:other"
    other["validators"][0]["operator_id"] = "operator:other"
    assert evidence_digest_hex(_evidence(other)) != before
    assert public_acceptance.acceptance_task_cell(contract["policy"]) == "task:acceptance/operator-run-v2"


@pytest.mark.parametrize("mutate", [
    lambda value: value["policy"].pop("verification_mode"),
    lambda value: value["policy"].pop("validator_operator_id"),
    lambda value: value["policy"].update(validator_operator_id=" "),
    lambda value: value["policy"].update(verification_mode="independent"),
    lambda value: value["policy"].update(required_validators=2),
    lambda value: value["policy"].update(extra=True),
    lambda value: value["validators"][0].update(operator_id="operator:unapproved"),
    lambda value: value.update(schema_version="ormas.public-acceptance-contract.v1"),
    lambda value: value["policy"].update(protocol="ormas.public-acceptance-contract.v1"),
])
def test_operator_run_alpha_refuses_missing_or_changed_terms(mutate) -> None:
    contract = _operator_run_contract()
    mutate(contract)
    with pytest.raises(ValueError):
        public_acceptance.validate_acceptance_contract(contract)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(extra=True),
        lambda value: value["policy"].update(catalog_digest="a" * 64),
        lambda value: value["policy"].update(required_validators=True),
        lambda value: value["policy"].update(max_concurrent_assignments=2),
        lambda value: value.update(validators=value["validators"][:1]),
        lambda value: value["validators"][1].update(subject_id="val_1"),
        lambda value: value["validators"][1].update(credential_id="1" * 64),
        lambda value: value["validators"][1].update(operator_id="operator:validator-1"),
        lambda value: value["validators"][0].update(operator_id="operator:jake"),
        lambda value: value.update(claim_nonce=" "),
    ],
)
def test_malformed_subset_duplicate_and_self_plans_are_refused(mutate) -> None:
    private_acceptance = _private_acceptance()
    value = _contract()
    mutate(value)
    with pytest.raises(ValueError):
        private_acceptance.validate_acceptance_contract(value)
    with pytest.raises(ValueError):
        public_acceptance.validate_acceptance_contract(value)


def test_task_draft_legacy_wire_is_unchanged_and_contract_round_trips() -> None:
    private_protocol = _private_protocol()
    legacy_private = private_protocol.TaskDraft(**_draft_kwargs()).to_wire()
    legacy_public = public_protocol.TaskDraft(**_draft_kwargs()).to_wire()
    assert legacy_private == legacy_public
    assert "acceptance_contract" not in legacy_private

    contract = _contract()
    private_wire = private_protocol.TaskDraft(
        **_draft_kwargs(), acceptance_contract=contract
    ).to_wire()
    public_wire = public_protocol.TaskDraft(
        **_draft_kwargs(), acceptance_contract=contract
    ).to_wire()
    assert private_wire == public_wire
    assert private_protocol.TaskDraft.from_wire(private_wire).to_wire() == private_wire
    assert public_protocol.TaskDraft.from_wire(public_wire).to_wire() == public_wire

    contract["claim_nonce"] = "mutated-after-construction"
    assert private_wire["acceptance_contract"]["claim_nonce"] == "claim_123"


class _RegistrationClient:
    def __init__(self) -> None:
        self.registration = None

    def register_runner(self, registration):
        self.registration = registration
        return {"runner_id": "runr_public"}


def test_public_sdk_advertises_acceptance_protocol_with_publication(tmp_path) -> None:
    client = _RegistrationClient()
    config = MinerConfig(
        runner_id="runr_public",
        runner_version="test",
        platform="linux",
        capacity=1,
        cells=("task:code", "task:lang/python"),
        workdir_root=tmp_path,
        repo_id="repo_public",
        repo_url="https://github.com/example/public.git",
    )
    MinerSkeleton(client, config, lambda *_args: None).register()
    assert client.registration.health["cells"] == [
        "task:code",
        "task:lang/python",
        "task:publication/github-artifact-v1",
        "task:acceptance/independent-v1",
        "task:acceptance/independent-v3",
        "task:preflight/deferred-v1",
    ]


def test_signed_digest_changes_with_policy_key_and_nonce() -> None:
    original = _contract()
    original_digest = evidence_digest_hex(_evidence(original))

    changed_policy = copy.deepcopy(original)
    changed_policy["policy"]["timeout_s"] += 1
    changed_key = copy.deepcopy(original)
    changed_key["validators"][0]["credential_id"] = "3" * 64
    changed_nonce = copy.deepcopy(original)
    changed_nonce["claim_nonce"] = "claim_456"

    assert evidence_digest_hex(_evidence(changed_policy)) != original_digest
    assert evidence_digest_hex(_evidence(changed_key)) != original_digest
    assert evidence_digest_hex(_evidence(changed_nonce)) != original_digest


def test_gateway_and_public_validator_canonical_evidence_match() -> None:
    gateway = pytest.importorskip("tensorbox_spec.customer_api.outcomes_validators")
    contract = _contract()
    public_fields = _evidence(contract)
    gateway_fields = gateway.canonical_evidence_fields(
        job_id="job_public",
        miner_id="miner:jake",
        base_commit="b" * 40,
        result_commit="c" * 40,
        repo_url="https://github.com/example/public.git",
        verify_command="pytest -q",
        allowed_paths=["src/feature.py"],
        immutable_paths=["tests/test_feature.py"],
        acceptance_contract=contract,
    )
    assert public_fields == gateway_fields
    assert evidence_digest_hex(public_fields) == gateway.evidence_digest_hex(gateway_fields)


class _FakeValidatorClient:
    def __init__(self, assignment: dict) -> None:
        self.assignment = assignment
        self.posted: list[tuple[str, str, str]] = []

    def list_assignments(self) -> list[dict]:
        return [self.assignment]

    def post_decision(self, assignment_id: str, *, decision: str, signature_hex: str) -> dict:
        self.posted.append((assignment_id, decision, signature_hex))
        return {"decision": decision}


def test_validator_run_binds_assignment_acceptance_contract(tmp_path, monkeypatch) -> None:
    contract = _contract()
    evidence = _evidence(contract)
    assignment = {
        "assignment_id": "asgn_public",
        **evidence,
        "evidence_digest_sha256": evidence_digest_hex(evidence),
    }
    client = _FakeValidatorClient(assignment)
    daemon = ValidatorDaemon(client, ValidatorConfig(tmp_path), lambda digest: digest)
    monkeypatch.setattr(daemon, "_clone", lambda _assignment: tmp_path)
    monkeypatch.setattr(daemon, "_decide", lambda _assignment, _workdir: "accept")

    assert daemon.run_once() is True
    assert client.posted == [("asgn_public", "accept", assignment["evidence_digest_sha256"])]


def test_validator_canonical_evidence_refuses_malformed_contract() -> None:
    malformed = _contract()
    malformed["validators"] = malformed["validators"][:1]
    with pytest.raises(ValueError):
        _evidence(malformed)
