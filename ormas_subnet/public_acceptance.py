"""Closed public acceptance policy and claim-plan validation."""

from __future__ import annotations

import copy
import re
from typing import Any

ACCEPTANCE_CONTRACT_SCHEMA = "ormas.public-acceptance-contract.v1"
ACCEPTANCE_POLICY_SCHEMA = "ormas.public-acceptance-policy.v1"

_CATALOG_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ED25519_PUBLIC_KEY_RE = re.compile(r"^[0-9a-f]{64}$")
_CONTRACT_KEYS = frozenset({"schema_version", "policy", "miner", "validators", "claim_nonce"})
_POLICY_KEYS = frozenset(
    {
        "schema_version",
        "required_validators",
        "catalog_digest",
        "profile_id",
        "protocol",
        "liveness_s",
        "timeout_s",
        "max_concurrent_assignments",
    }
)
_MINER_KEYS = frozenset(
    {"subject_id", "operator_id", "qualification_id", "credential_id", "miner_id"}
)
_VALIDATOR_KEYS = frozenset({"subject_id", "operator_id", "qualification_id", "credential_id"})


def _require_exact_dict(value: Any, keys: frozenset[str], name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a dict")  # noqa: TRY004 - malformed wire values share one error contract
    if set(value) != keys:
        raise ValueError(f"{name} has an unexpected key set")
    return value


def _require_nonblank(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonblank string")
    return value


def _require_positive_int(value: Any, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def validate_acceptance_policy(value: Any) -> dict[str, Any]:
    """Validate and independently snapshot the server-owned public policy."""
    policy = _require_exact_dict(value, _POLICY_KEYS, "acceptance policy")
    if policy["schema_version"] != ACCEPTANCE_POLICY_SCHEMA:
        raise ValueError("acceptance policy schema_version mismatch")
    _require_positive_int(policy["required_validators"], "required_validators")
    digest = policy["catalog_digest"]
    if not isinstance(digest, str) or _CATALOG_DIGEST_RE.fullmatch(digest) is None:
        raise ValueError("catalog_digest must be sha256: plus 64 lowercase hex chars")
    _require_nonblank(policy["profile_id"], "profile_id")
    if policy["protocol"] != ACCEPTANCE_CONTRACT_SCHEMA:
        raise ValueError("acceptance policy protocol mismatch")
    _require_positive_int(policy["liveness_s"], "liveness_s")
    _require_positive_int(policy["timeout_s"], "timeout_s")
    if (
        type(policy["max_concurrent_assignments"]) is not int
        or policy["max_concurrent_assignments"] != 1
    ):
        raise ValueError("max_concurrent_assignments must be 1")
    return copy.deepcopy(policy)


def _validate_subject(value: Any, *, miner: bool) -> dict[str, Any]:
    name = "miner" if miner else "validator"
    keys = _MINER_KEYS if miner else _VALIDATOR_KEYS
    subject = _require_exact_dict(value, keys, name)
    for key in ("subject_id", "operator_id", "qualification_id", "credential_id"):
        _require_nonblank(subject[key], f"{name}.{key}")
    if miner:
        _require_nonblank(subject["miner_id"], "miner.miner_id")
    elif _ED25519_PUBLIC_KEY_RE.fullmatch(subject["credential_id"]) is None:
        raise ValueError("validator.credential_id must be a 64 lowercase hex Ed25519 public key")
    return subject


def validate_acceptance_contract(value: Any) -> dict[str, Any]:
    """Validate and independently snapshot a frozen public claim plan."""
    contract = _require_exact_dict(value, _CONTRACT_KEYS, "acceptance contract")
    if contract["schema_version"] != ACCEPTANCE_CONTRACT_SCHEMA:
        raise ValueError("acceptance contract schema_version mismatch")
    policy = validate_acceptance_policy(contract["policy"])
    miner = _validate_subject(contract["miner"], miner=True)
    validators = contract["validators"]
    if not isinstance(validators, list):
        raise ValueError("validators must be a list")  # noqa: TRY004 - malformed wire values share one error contract
    if len(validators) != policy["required_validators"]:
        raise ValueError("validators length must equal required_validators")
    checked = [_validate_subject(item, miner=False) for item in validators]
    for field in ("subject_id", "credential_id", "operator_id"):
        values = [item[field] for item in checked]
        if len(set(values)) != len(values):
            raise ValueError(f"validator {field}s must be distinct")
    if any(item["operator_id"] == miner["operator_id"] for item in checked):
        raise ValueError("validator operators must differ from the miner operator")
    _require_nonblank(contract["claim_nonce"], "claim_nonce")
    return copy.deepcopy(contract)


__all__ = [
    "ACCEPTANCE_CONTRACT_SCHEMA",
    "ACCEPTANCE_POLICY_SCHEMA",
    "validate_acceptance_contract",
    "validate_acceptance_policy",
]
