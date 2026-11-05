"""protected-env seals SayGM frontier and Engy ZDR relay policies and keys for the shell."""
from __future__ import annotations

import json
import os
import stat

import pytest

from ormas_subnet import miner_cli as cli
from ormas_subnet import protected_env

TOKEN = "ormr_relay_token_canary"
SAYGM = "saygm_key_canary_0123456789"
ENGY = "engy_key_canary_0123456789"
ENGY_MODELS = [
    "deepseek-v4.1-flash", "deepseek-v4-flash-0731", "qwen3.8-27b",
    "glm-5.3-flash", "glm-5.2", "glm-5.3", "kimi-k3",
]
DIGEST = "sha256:" + "a" * 64


@pytest.fixture(autouse=True)
def home(monkeypatch, tmp_path, request):
    previous_umask = os.umask(0o077)
    request.addfinalizer(lambda: os.umask(previous_umask))
    monkeypatch.setenv("HOME", str(tmp_path))
    path = tmp_path / ".ormas-miner" / "credentials.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"gateway": cli.DEFAULT_GATEWAY, "token": TOKEN}))
    path.chmod(0o600)


@pytest.fixture
def args(tmp_path):
    worker = tmp_path / "worker.json"
    worker.write_text(json.dumps({"GROKBUILD_WORKER_MODEL": "example-worker-cell"}))
    key = tmp_path / "saygm.key"
    key.write_text(SAYGM + "\n")
    return [
        "protected-env", "--api-url", cli.DEFAULT_GATEWAY, "--miner-id", "aegis-atalanta",
        "--runtime", "production", "--cell-bounds", "c=1.00", "--approved-by", "op",
        "--task-cells", "task:code", "--bind-project-id", "proj_test",
        "--worker-image", "example/worker@" + DIGEST, "--worker-env-file", str(worker),
        "--out", str(tmp_path / "miner.env"),
    ]


def _fields(tmp_path):
    return dict(line.split("=", 1) for line in (tmp_path / "miner.env").read_text().splitlines())


def test_relay_frontier_seals_policy_and_key_for_the_shell_not_the_worker(args, tmp_path, capsys):
    models = ["--relay-model", "model-alpha-1", "--relay-model", "model-beta-2", "--relay-model", "model-gamma-3"]
    assert cli.main([*args, "--relay", "saygm-frontier", *models, "--provider-key-file",
                     str(tmp_path / "saygm.key")]) == 0
    fields = _fields(tmp_path)
    policy = json.loads(fields["ORMAS_PROTECTED_RELAY_POLICY_JSON"])
    assert policy["provider"] == "saygm-frontier" and policy["privacy_class"] == "frontier_gateway"
    assert sorted(policy["models"]) == ["model-alpha-1", "model-beta-2", "model-gamma-3"]
    assert fields["SAYGM_API_KEY"] == SAYGM
    assert "SAYGM" not in fields["MINER_WORKER_ENV_JSON"] and SAYGM not in fields["MINER_WORKER_ENV_JSON"]
    digest = (tmp_path / "relay-policy.digest").read_text().strip()
    out = capsys.readouterr().out
    assert f"relay policy digest {digest}" in out and SAYGM not in out
    assert "model-alpha-1" not in out  # model ids stay private
    for name in ("relay-policy.json", "relay-policy.digest", "miner.env"):
        assert stat.S_IMODE((tmp_path / name).stat().st_mode) == 0o600
    # The sealed policy re-canonicalizes to itself, so the approved digest is stable.
    sealed = fields["ORMAS_PROTECTED_RELAY_POLICY_JSON"]
    assert json.loads(sealed) == policy and protected_env.read_relay_policy(sealed) == (sealed, digest)
    # Parity with the monorepo relay's own digest lives in tests/privacy/test_protected_env_relay_parity.py.


def test_reusing_the_saved_policy_keeps_the_approved_digest(args, tmp_path):
    key = ["--provider-key-file", str(tmp_path / "saygm.key")]
    assert cli.main([*args, "--relay", "saygm-frontier", "--relay-model", "model-beta-2", *key]) == 0
    first = (tmp_path / "relay-policy.digest").read_text()
    saved = tmp_path / "keep.json"
    saved.write_text((tmp_path / "relay-policy.json").read_text())
    assert cli.main([*args, "--relay-policy-file", str(saved), *key, "--force"]) == 0
    assert (tmp_path / "relay-policy.digest").read_text() == first


def test_relay_refuses_worker_credentials_and_missing_key(args, tmp_path, capsys):
    assert cli.main([*args, "--relay", "saygm-frontier", "--relay-model", "model-beta-2"]) == 1
    assert "SAYGM_API_KEY" in capsys.readouterr().err
    (tmp_path / "worker.json").write_text(json.dumps({"SAYGM_API_KEY": SAYGM}))
    assert cli.main([*args, "--relay", "saygm-frontier", "--provider-key-file",
                     str(tmp_path / "saygm.key")]) == 1
    err = capsys.readouterr().err
    assert "MINER_WORKER_ENV_JSON" in err and SAYGM not in err
    assert not (tmp_path / "miner.env").exists()


def test_without_relay_both_relay_inputs_are_empty(args, tmp_path):
    assert cli.main(args) == 0
    fields = _fields(tmp_path)
    assert fields["ORMAS_PROTECTED_RELAY_POLICY_JSON"] == "" and fields["SAYGM_API_KEY"] == ""
    assert not (tmp_path / "relay-policy.json").exists()


@pytest.mark.parametrize("models", [["model-alpha-1", "model-beta-2", "model-gamma-3"], ["model-beta-2"]])
def test_public_policy_digest_is_stable_for_the_same_inputs(models):
    """Same inputs => same canonical bytes and digest; parity with the monorepo relay's own
    policy digest is asserted in tests/privacy/test_protected_env_relay_parity.py."""
    text, digest = protected_env.relay_policy("saygm-frontier", models, "cd" * 32)
    assert protected_env.read_relay_policy(text) == (text, digest)
    assert protected_env.relay_policy("saygm-frontier", list(reversed(models)), "cd" * 32) == (text, digest)
    assert sorted(json.loads(text)["models"]) == sorted(models)
    assert len(digest) == 64 and all(character in "0123456789abcdef" for character in digest)


def test_relay_without_models_or_policy_file_is_refused_and_ships_no_default_ladder(args, tmp_path, capsys):
    # Our miner's model ladder is its own IP; the public CLI must not carry it as a default.
    assert cli.main([*args, "--relay", "saygm-frontier", "--provider-key-file", str(tmp_path / "saygm.key")]) != 0
    assert "--relay-model" in capsys.readouterr().err
    assert not (tmp_path / "miner.env").exists()
    assert not hasattr(protected_env, "FRONTIER_V1_MODELS")


@pytest.fixture
def engy_key(tmp_path):
    key = tmp_path / "engy.key"
    key.write_text(ENGY + "\n")
    return ["--provider-key-file", str(key)]


def test_engy_zdr_seals_policy_and_key_for_the_shell(args, engy_key, tmp_path, capsys):
    models = ["--relay-model", "kimi-k3", "--relay-model", "deepseek-v4.1-flash"]
    assert cli.main([*args, "--relay", "engy-zdr", *models, *engy_key]) == 0
    fields = _fields(tmp_path)
    sealed = fields["ORMAS_PROTECTED_RELAY_POLICY_JSON"]
    policy = json.loads(sealed)
    assert policy["provider"] == "engy" and policy["privacy_class"] == "zero_retention"
    assert policy["models"] == ["deepseek-v4.1-flash", "kimi-k3"]
    assert fields["ENGY_API_KEY"] == ENGY and fields["SAYGM_API_KEY"] == ""
    assert "ENGY" not in fields["MINER_WORKER_ENV_JSON"]
    assert ENGY not in fields["MINER_WORKER_ENV_JSON"] and ENGY not in sealed
    digest = (tmp_path / "relay-policy.digest").read_text().strip()
    assert protected_env.read_relay_policy(sealed) == (sealed, digest)
    assert (tmp_path / "relay-policy.json").read_text().strip() == sealed
    output = capsys.readouterr()
    assert f"relay policy digest {digest}" in output.out
    assert ENGY not in output.out + output.err
    assert all(model not in output.out + output.err for model in policy["models"])
    for name in ("miner.env", "relay-policy.json", "relay-policy.digest"):
        assert stat.S_IMODE((tmp_path / name).stat().st_mode) == 0o600


def test_engy_zdr_saved_policy_keeps_digest(args, engy_key, tmp_path):
    assert cli.main([*args, "--relay", "engy-zdr", "--relay-model", "kimi-k3",
                     *engy_key]) == 0
    first = (tmp_path / "relay-policy.digest").read_text()
    saved = tmp_path / "keep.json"
    saved.write_text((tmp_path / "relay-policy.json").read_text())
    assert cli.main([*args, "--relay", "engy-zdr", "--relay-policy-file", str(saved),
                     *engy_key, "--force"]) == 0
    assert (tmp_path / "relay-policy.digest").read_text() == first
    assert _fields(tmp_path)["ENGY_API_KEY"] == ENGY


def test_engy_policy_digest_is_deterministic():
    text, digest = protected_env.relay_policy("engy", ENGY_MODELS, "cd" * 32)
    assert protected_env.read_relay_policy(text) == (text, digest)
    assert protected_env.relay_policy("engy", list(reversed(ENGY_MODELS)), "cd" * 32) == (
        text, digest,
    )
    assert json.loads(text)["models"] == sorted(ENGY_MODELS)
    assert protected_env.relay_policy("engy", ENGY_MODELS, "ab" * 32)[1] != digest


@pytest.mark.parametrize("models", [
    [], ["qwen3.6-35b-a3b"], ["minimax-h3"], ["model-alpha-1"],
    ["engy/kimi-k3"], ["kimi-k3[1m]"], ["kimi-k3", "minimax-h3"],
    ["kimi-k3", "kimi-k3"],
])
def test_engy_zdr_refuses_missing_or_unqualified_models(args, engy_key, tmp_path,
                                                        capsys, models):
    flags = [value for model in models for value in ("--relay-model", model)]
    assert cli.main([*args, "--relay", "engy-zdr", *flags, *engy_key]) == 1
    output = capsys.readouterr()
    assert ("model ids" if models else "--relay-model") in output.err
    assert ENGY not in output.out + output.err
    assert not (tmp_path / "miner.env").exists()
    assert not (tmp_path / "relay-policy.json").exists()


def test_engy_zdr_refuses_missing_key_and_worker_credentials(args, engy_key, tmp_path,
                                                            capsys):
    flags = ["--relay", "engy-zdr", "--relay-model", "kimi-k3"]
    assert cli.main([*args, *flags]) == 1
    assert "ENGY_API_KEY" in capsys.readouterr().err
    (tmp_path / "worker.json").write_text(json.dumps({"ENGY_API_KEY": ENGY}))
    assert cli.main([*args, *flags, *engy_key]) == 1
    output = capsys.readouterr()
    assert "MINER_WORKER_ENV_JSON" in output.err and ENGY not in output.out + output.err
    assert not (tmp_path / "miner.env").exists()
