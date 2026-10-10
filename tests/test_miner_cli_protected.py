"""PW-5: split sealed environment and gateway-owned Protected diagnostics."""
from __future__ import annotations

import importlib.util
import json
import stat
from pathlib import Path

import httpx
import pytest

from ormas_subnet import miner_cli as cli

TOKEN = "ormr_pw5_token_canary"
REGISTRY = "pw5_registry_canary"
DIGEST = "sha256:" + "a" * 64
OTHER_DIGEST = "sha256:" + "b" * 64
STAMP = "2026-10-10T01:00:00+00:00"


def _redacted(text):
    return all(secret not in text for secret in (TOKEN, REGISTRY))


@pytest.fixture(autouse=True)
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("ORMAS_MINER_TOKEN", raising=False)
    path = tmp_path / ".ormas-miner" / "credentials.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"gateway": cli.DEFAULT_GATEWAY, "token": TOKEN}))
    path.chmod(0o600)


@pytest.fixture
def env_args(tmp_path):
    worker = tmp_path / "worker.json"
    worker.write_text(json.dumps({"PROVIDER_KEY": "worker-canary"}))
    registry = tmp_path / "registry.txt"
    registry.write_text(REGISTRY)
    return [
        "protected-env", "--api-url", cli.DEFAULT_GATEWAY,
        "--miner-id", "my-miner", "--runtime", "production",
        "--cell-bounds", "my-cell=1.00", "--approved-by", "miner-operator",
        "--task-cells", "task:code", "--task-cells", "task:service/protected",
        "--bind-project-id", "proj_test", "--worker-image", "example/worker@" + DIGEST,
        "--worker-env-file", str(worker), "--registry-auth-file", str(registry),
        "--provider", "api.example.com", "--pricing-template",
        "--out", str(tmp_path / "miner.env"),
    ]


def _call(args):
    # Base has no commands yet: turn parser refusal into a behavioral assertion,
    # not collection/import failure. Suppress argv/errors that could carry values.
    try:
        return cli.main(args)
    except SystemExit as exc:
        return exc.code


def test_env_complete_measured_order_private_redacted(env_args, tmp_path, capsys):
    assert _call(env_args) == 0
    path = tmp_path / "miner.env"
    spec = importlib.util.spec_from_file_location(
        "public_compose_hash", Path(__file__).resolve().parents[1] / "protected/miner/compose_hash.py",
    )
    hashed = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hashed)
    rows = path.read_text().splitlines()
    assert [line.split("=", 1)[0] for line in rows] == hashed.SPLIT_ALLOWED_ENVS
    assert path.read_bytes().endswith(b"\n")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    fields = dict(line.split("=", 1) for line in rows)
    assert bool(fields["ORMAS_RUNNER_TOKEN"] == TOKEN)
    assert bool(fields["MINER_WORKER_REGISTRY_AUTH"] == REGISTRY)
    assert json.loads(fields["MINER_WORKER_ENV_JSON"]).keys() == {"PROVIDER_KEY"}
    assert fields["ORMAS_AUTHORIZATION_JSON"] == ""
    assert fields["ORMAS_TASK_CELLS"] == "task:code task:service/protected"
    assert fields["MINER_WORKER_IMAGE"] == "example/worker@" + DIGEST
    assert (tmp_path / "declared-providers.list").read_text() == "api.example.com\n"
    template = json.loads((tmp_path / "pricing.json").read_text())
    assert set(template) == {"estimate_usd", "limit_usd"}
    assert not any(isinstance(v, (int, float)) for v in template.values())
    output = capsys.readouterr()
    assert output.out == f"{path}\n{len(rows)} keys\n"
    assert output.err == ""
    assert _redacted(output.out + output.err)


@pytest.mark.parametrize("bad", ["#", "'", '"', "\n", "\r", "\x00"], ids=[
    "hash", "single-quote", "double-quote", "newline", "carriage-return", "nul",
])
@pytest.mark.parametrize("flag,key", [
    ("--miner-id", "ORMAS_MINER_ID"), ("--approved-by", "ORMAS_APPROVED_BY"),
    ("--cell-bounds", "ORMAS_CELL_BOUNDS"), ("--task-cells", "ORMAS_TASK_CELLS"),
    ("--bind-project-id", "ORMAS_BIND_PROJECT_ID"),
])
def test_free_text_refusal_names_key_only(env_args, tmp_path, capsys, flag, key, bad):
    env_args[env_args.index(flag) + 1] = REGISTRY + bad
    assert _call(env_args) == 1
    output = capsys.readouterr()
    assert key in output.err
    assert _redacted(output.out + output.err)
    assert not (tmp_path / "miner.env").exists()


@pytest.mark.parametrize("source,key", [
    ("worker.json", "MINER_WORKER_ENV_JSON"),
    ("registry.txt", "MINER_WORKER_REGISTRY_AUTH"),
    ("token.txt", "ORMAS_RUNNER_TOKEN"),
    ("authorization.json", "ORMAS_AUTHORIZATION_JSON"),
])
@pytest.mark.parametrize("bad", ["#", "'", '"', "\n"], ids=["hash", "single", "double", "newline"])
def test_file_values_refuse_unsafe_content(env_args, tmp_path, capsys, source, key, bad):
    value = REGISTRY + bad
    if source == "worker.json":
        text = json.dumps({"PROVIDER_KEY": value})
    elif source == "authorization.json":
        text = json.dumps({"approved_by": value})
        env_args.extend(["--authorization-file", str(tmp_path / source)])
        for flag in ("--cell-bounds", "--approved-by"):
            index = env_args.index(flag)
            del env_args[index:index + 2]
    elif source == "token.txt":
        text = TOKEN + bad + "tail"
        env_args.extend(["--token-file", str(tmp_path / source)])
    else:
        # A single terminal newline is a file delimiter; embedded newlines are unsafe.
        text = value + "tail" if bad == "\n" else value
    path = tmp_path / source
    path.write_text(text)
    path.chmod(0o600)
    assert _call(env_args) == 1
    output = capsys.readouterr()
    assert key in output.err
    assert _redacted(output.out + output.err)
    assert not (tmp_path / "miner.env").exists()


def test_authorization_compact_json_alternative(env_args, tmp_path, capsys):
    path = tmp_path / "authorization.json"
    path.write_text(json.dumps({"approved_by": "miner-operator", "bounds": {"cell": 1.0}}, indent=2))
    for flag in ("--cell-bounds", "--approved-by"):
        index = env_args.index(flag)
        del env_args[index:index + 2]
    env_args.extend(["--authorization-file", str(path)])
    assert _call(env_args) == 0
    fields = dict(line.split("=", 1) for line in (tmp_path / "miner.env").read_text().splitlines())
    assert fields["ORMAS_CELL_BOUNDS"] == fields["ORMAS_APPROVED_BY"] == ""
    assert json.loads(fields["ORMAS_AUTHORIZATION_JSON"])["approved_by"] == "miner-operator"
    assert _redacted(str(capsys.readouterr()))


def test_no_overwrite_and_force(env_args, tmp_path, capsys):
    assert _call(env_args) == 0
    path = tmp_path / "miner.env"
    path.write_text("retained\n")
    assert _call(env_args) == 1
    assert path.read_text() == "retained\n"
    path.chmod(0o644)
    assert _call(env_args + ["--force"]) == 0
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert _redacted(str(capsys.readouterr()))


@pytest.mark.parametrize("image", ["worker:latest", "worker@sha256:abc", "@" + DIGEST])
def test_image_must_be_digest_pinned(env_args, tmp_path, capsys, image):
    env_args[env_args.index("--worker-image") + 1] = image
    assert _call(env_args) == 1
    assert "MINER_WORKER_IMAGE" in capsys.readouterr().err
    assert not (tmp_path / "miner.env").exists()


def test_help_no_values(env_args, capsys):
    assert _call(["protected-env", "--help"]) == 0
    assert _redacted(str(capsys.readouterr()))


@pytest.fixture
def gateway(monkeypatch):
    requests = []
    protected = {
        "slot_approved": True, "slot_bound": True, "declared_worker_digests": [DIGEST],
        "approved_worker_policy": True, "qualification_job_status": "done",
        "audit_status": "ok", "last_refusal": None,
        "last_release": {"worker_image_digest": DIGEST, "at": STAMP},
    }
    response = {"runner_id": "runr_mine", "cells": [], "protected": protected}
    state = {"status": 200, "body": response}
    client_class = httpx.Client

    def handler(request):
        requests.append(request)
        assert request.url.path == "/api/runner/v1/runners/me"
        return httpx.Response(state["status"], json=state["body"])

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return client_class(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", client)
    return protected, state, requests


def test_doctor_protected_gateway_only(gateway, capsys):
    assert _call(["doctor", "--protected", "--runner-id", "runr_mine", "--device-nonce", "device"]) == 0
    output = capsys.readouterr()
    assert "ok registration: yes" in output.out
    assert "ok slot: approved" in output.out
    assert "ok declared-workers: " + DIGEST in output.out
    assert "ok worker-policy: yes" in output.out
    assert "ok qualification: done" in output.out
    assert "ok key-release: no refusal recorded" in output.out
    assert "ok worker-digest: match " + DIGEST in output.out
    assert all(line.startswith(("ok ", "warn ", "fail ")) for line in output.out.splitlines())
    assert _redacted(output.out + output.err)
    request = gateway[2][0]
    assert request.url.params["runner_id"] == "runr_mine"
    assert bool(request.headers["Authorization"] == "Bearer " + TOKEN)
    assert request.headers["X-Ormas-Runner-Device"] == "device"


@pytest.mark.parametrize("changes,expected,code", [
    ({"slot_approved": False}, "fail slot: not approved", 1),
    ({"approved_worker_policy": False}, "warn worker-policy: no policy approved yet", 0),
    ({"qualification_job_status": "queued"}, "warn qualification: queued", 0),
    ({"qualification_job_status": "offering"}, "warn qualification: offering", 0),
    ({"qualification_job_status": "running"}, "warn qualification: running", 0),
    ({"qualification_job_status": "failed"}, "fail qualification: failed", 1),
    ({"qualification_job_status": "cancelled"}, "fail qualification: cancelled", 1),
    ({"qualification_job_status": "outcome_unknown"}, "fail qualification: outcome_unknown", 1),
    ({"qualification_job_status": None}, "warn qualification: none", 0),
    ({"declared_worker_digests": []}, "warn worker-digest: none declared", 0),
    ({"last_release": None}, "warn worker-digest: no release recorded", 0),
    ({"last_release": {"worker_image_digest": OTHER_DIGEST, "at": STAMP}}, "fail worker-digest: mismatch", 1),
    ({"last_refusal": {"reason": "worker_digest_mismatch", "at": STAMP}}, "fail key-release: worker_digest_mismatch " + STAMP, 1),
    ({"last_refusal": {"reason": "worker_policy_required", "at": STAMP}}, "fail key-release: worker_policy_required " + STAMP, 1),
    ({"audit_status": "unavailable"}, "fail key-release: audit unavailable", 1),
])
def test_doctor_states(gateway, capsys, changes, expected, code):
    gateway[0].update(changes)
    assert _call(["doctor", "--protected"]) == code
    output = capsys.readouterr()
    assert expected in output.out
    assert _redacted(output.out + output.err)


@pytest.mark.parametrize("status", [401, 404, 500])
def test_doctor_lookup_refusals_redacted(gateway, capsys, status):
    gateway[1].update(status=status, body={"error": {"type": "not_found_error", "message": TOKEN}})
    assert _call(["doctor", "--protected"]) == 1
    output = capsys.readouterr()
    assert "fail registration:" in output.out
    assert _redacted(output.out + output.err)


def test_doctor_untrusted_fields_never_echoed(gateway, capsys):
    gateway[0].update({
        "declared_worker_digests": [REGISTRY], "qualification_job_status": TOKEN,
        "last_refusal": {"reason": TOKEN, "at": REGISTRY},
        "last_release": {"worker_image_digest": TOKEN, "at": STAMP},
    })
    assert _call(["doctor", "--protected"]) == 1
    output = capsys.readouterr()
    assert _redacted(output.out + output.err)


def test_argparse_does_not_echo_bad_runtime(env_args, capsys):
    env_args[env_args.index("--runtime") + 1] = REGISTRY
    assert _call(env_args) != 0
    output = capsys.readouterr()
    assert _redacted(output.out + output.err)


def test_no_providers_is_an_explicit_empty_declaration(env_args, tmp_path, capsys):
    index = env_args.index("--provider")
    del env_args[index:index + 2]
    assert _call(env_args) == 0
    assert (tmp_path / "declared-providers.list").read_text() == ""
    assert _redacted(str(capsys.readouterr()))


def test_force_replaces_symlink_not_target(env_args, tmp_path, capsys):
    target = tmp_path / "untouched"
    target.write_text("retain\n")
    path = tmp_path / "miner.env"
    path.symlink_to(target)
    assert _call(env_args) == 1
    assert _call(env_args + ["--force"]) == 0
    assert not path.is_symlink()
    assert target.read_text() == "retain\n"
    assert _redacted(str(capsys.readouterr()))


def test_companion_no_overwrite_before_env_written(env_args, tmp_path, capsys):
    companion = tmp_path / "pricing.json"
    companion.write_text("retain\n")
    assert _call(env_args) == 1
    assert companion.read_text() == "retain\n"
    assert not (tmp_path / "miner.env").exists()
    assert _redacted(str(capsys.readouterr()))


def test_doctor_missing_protected_block_fails(gateway, capsys):
    del gateway[1]["body"]["protected"]
    assert _call(["doctor", "--protected"]) == 1
    assert "fail protected: gateway diagnostics unavailable" in capsys.readouterr().out


def test_doctor_historical_refusal_resolved_by_later_release(gateway, capsys):
    gateway[0]["last_refusal"] = {"reason": "worker_policy_required", "at": "2026-10-09T01:00:00+00:00"}
    assert _call(["doctor", "--protected"]) == 0
    assert "warn key-release: worker_policy_required" in capsys.readouterr().out


def test_protected_doctor_parser_refusal_redacted(capsys):
    assert _call(["doctor", "--protected", "--unknown", REGISTRY]) != 0
    output = capsys.readouterr()
    assert _redacted(output.out + output.err)


def test_pw5_doctor_unbound_slot_warns(gateway, capsys):
    gateway[0].update(slot_bound=False, declared_worker_digests=[], approved_worker_policy=False)
    assert _call(["doctor", "--protected"]) == 0
    output = capsys.readouterr()
    assert "warn slot: approved, not yet bound" in output.out
    assert "warn worker-policy: no policy approved yet" in output.out
    assert _redacted(output.out + output.err)


def test_pw5_doctor_pending_qualification(gateway, capsys):
    gateway[0]["qualification_job_status"] = "pending"
    assert _call(["doctor", "--protected"]) == 0
    assert "warn qualification: pending" in capsys.readouterr().out


@pytest.mark.parametrize("runtime,url", [
    ("production", "https://gateway.invalid"),
    ("production", "https://api.ormas.ai/"),
    ("production", "https://api.ormas.ai:443"),
    ("production", "http://127.0.0.1"),
    ("development", "http://localhost"),
    ("development", "http://127.0.0.1"),
], ids=["other-production", "trailing-slash", "explicit-port", "production-http", "dev-local-http", "dev-ip-http"])
def test_pw5_runtime_url_refused(env_args, tmp_path, capsys, runtime, url):
    env_args[env_args.index("--runtime") + 1] = runtime
    env_args[env_args.index("--api-url") + 1] = url
    key = tmp_path / "runtime.key"
    key.write_text(TOKEN)
    key.chmod(0o600)
    env_args.extend(["--token-file", str(key)])
    assert _call(env_args) == 1
    output = capsys.readouterr()
    assert "ORMAS_API_URL" in output.err
    assert _redacted(output.out + output.err)
    assert not (tmp_path / "miner.env").exists()


def test_pw5_development_https_allowed(env_args, tmp_path, capsys):
    env_args[env_args.index("--runtime") + 1] = "development"
    env_args[env_args.index("--api-url") + 1] = "https://gateway.invalid"
    key = tmp_path / "runtime.key"
    key.write_text(TOKEN)
    key.chmod(0o600)
    env_args.extend(["--token-file", str(key)])
    assert _call(env_args) == 0
    assert _redacted(str(capsys.readouterr()))


@pytest.mark.parametrize("miner", ["ab", "a" * 41, "-miner", "Miner", "my_miner", "a.b", ""],
                         ids=["short", "long", "leading-hyphen", "upper", "underscore", "dot", "empty"])
def test_pw5_miner_id_registration_domain(env_args, tmp_path, capsys, miner):
    index = env_args.index("--miner-id")
    env_args[index:index + 2] = ["--miner-id=" + miner]
    assert _call(env_args) == 1
    output = capsys.readouterr()
    assert "ORMAS_MINER_ID" in output.err
    assert _redacted(output.out + output.err)
    assert not (tmp_path / "miner.env").exists()


def test_pw5_registry_file_strips_one_terminal_newline(env_args, tmp_path, capsys):
    (tmp_path / "registry.txt").write_text(REGISTRY + "\n")
    assert _call(env_args) == 0
    fields = dict(line.split("=", 1) for line in (tmp_path / "miner.env").read_text().splitlines())
    assert bool(fields["MINER_WORKER_REGISTRY_AUTH"] == REGISTRY)
    assert _redacted(str(capsys.readouterr()))


def test_pw5_registry_file_two_newlines_still_refused(env_args, tmp_path, capsys):
    (tmp_path / "registry.txt").write_text(REGISTRY + "\n\n")
    assert _call(env_args) == 1
    output = capsys.readouterr()
    assert "MINER_WORKER_REGISTRY_AUTH" in output.err
    assert _redacted(output.out + output.err)


def test_pw5_dollar_interpolation_refused_free_text(env_args, tmp_path, capsys):
    env_args[env_args.index("--approved-by") + 1] = REGISTRY + "$VARIABLE"
    assert _call(env_args) == 1
    output = capsys.readouterr()
    assert "ORMAS_APPROVED_BY" in output.err
    assert _redacted(output.out + output.err)
    assert not (tmp_path / "miner.env").exists()


def test_pw5_dollar_interpolation_refused_json(env_args, tmp_path, capsys):
    (tmp_path / "worker.json").write_text(json.dumps({"PROVIDER_KEY": REGISTRY + "$VARIABLE"}))
    assert _call(env_args) == 1
    output = capsys.readouterr()
    assert "MINER_WORKER_ENV_JSON" in output.err
    assert _redacted(output.out + output.err)
    assert not (tmp_path / "miner.env").exists()
