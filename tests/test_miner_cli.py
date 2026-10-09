"""Installed miner commands and credential handling."""
from __future__ import annotations

import importlib
import json
import os
import stat
from importlib.metadata import EntryPoint
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "ormr_test_private_abcd"
GATEWAY = "https://api.ormas.ai"
STANDARD_CELLS = [
    "task:code", "task:code/small", "task:lang/python",
    "task:publication/github-artifact-v1", "task:acceptance/operator-run-v3",
    "task:preflight/deferred-v1",
]


@pytest.fixture
def cli(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("ORMAS_MINER_TOKEN", raising=False)
    return importlib.import_module("ormas_subnet.miner_cli")


@pytest.fixture
def gateway(monkeypatch):
    requests = []
    responses = {
        "/health": (200, {"status": "ok"}),
        "/api/runner/v1/queue": (200, {"jobs": []}),
        "/api/runner/v1/registrations": (200, {"runner_id": "runr_test"}),
        "/api/runner/v1/runners/me": (200, {
            "runner_id": "runr_test", "cells": list(STANDARD_CELLS), "claim_cap": 1,
            "hotkey": {"bound": True, "verified": True},
        }),
    }
    client_class = httpx.Client

    def handler(request):
        requests.append(request)
        status, payload = responses[request.url.path]
        return httpx.Response(status, json=payload)

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return client_class(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", client)
    return requests, responses


def credentials(token=TOKEN, gateway=GATEWAY):
    path = Path.home() / ".ormas-miner" / "credentials.json"
    path.parent.mkdir(mode=0o700, exist_ok=True)
    path.write_text(json.dumps({"gateway": gateway, "token": token}))
    path.chmod(0o600)
    return path


def test_entry_point_resolves():
    line = 'ormas-miner = "ormas_subnet.miner_cli:main"'
    assert line in (ROOT / "pyproject.toml").read_text()
    entry = EntryPoint("ormas-miner", "ormas_subnet.miner_cli:main", "console_scripts")
    assert callable(entry.load())


def test_login_private_and_redacted(cli, monkeypatch, capsys):
    prompts = []
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: prompts.append(prompt) or TOKEN)
    assert cli.main(["login", "--gateway", GATEWAY]) == 0
    path = Path.home() / ".ormas-miner" / "credentials.json"
    assert json.loads(path.read_text()) == {"gateway": GATEWAY, "token": TOKEN}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert prompts
    output = capsys.readouterr()
    assert TOKEN not in output.out + output.err
    assert GATEWAY in output.out and TOKEN not in output.out and TOKEN[-4:] not in output.out
    path.chmod(0o644)
    assert cli.main(["login"]) == 0
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_login_environment(cli, monkeypatch, capsys):
    monkeypatch.setenv("TEST_MINER_KEY", TOKEN)
    monkeypatch.setattr(cli.getpass, "getpass", lambda _: pytest.fail("unexpected prompt"))
    assert cli.main(["login", "--token-env", "TEST_MINER_KEY"]) == 0
    assert TOKEN not in capsys.readouterr().out


@pytest.mark.parametrize("token", ["", "invalid_secret_abcd"])
def test_login_rejects_bad_key(cli, monkeypatch, capsys, token):
    monkeypatch.setattr(cli.getpass, "getpass", lambda _: token)
    assert cli.main(["login"]) != 0
    assert "ormr_" in capsys.readouterr().err
    assert not (Path.home() / ".ormas-miner" / "credentials.json").exists()


def test_doctor_success(cli, gateway, capsys, monkeypatch):
    credentials()
    monkeypatch.setenv("PATH", str(Path.home() / ".local" / "bin") + os.pathsep + os.defpath)
    assert cli.main(["doctor"]) == 0
    output = capsys.readouterr().out
    assert "FAIL" not in output
    assert "ok token:" in output
    assert TOKEN not in output
    assert "key accepted (account enablement not confirmed)" in output
    requests, _ = gateway
    assert [request.url.path for request in requests] == [
        "/health", "/api/runner/v1/queue", "/api/runner/v1/runners/me",
    ]
    assert requests[1].headers["Authorization"] == f"Bearer {TOKEN}"
    assert requests[1].method == "GET"
    assert requests[2].headers["Authorization"] == f"Bearer {TOKEN}"
    assert requests[2].method == "GET"
    assert "cells: ok (sizes: small)" in output
    assert "hotkey: bound" in output
    assert "qualification: cap 1" in output


def test_doctor_missing_preflight(cli, gateway, capsys):
    credentials()
    registration = gateway[1]["/api/runner/v1/runners/me"][1]
    registration["cells"].remove("task:preflight/deferred-v1")
    assert cli.main(["doctor"]) == 1
    output = capsys.readouterr().out
    assert "cells: MISSING task:preflight/deferred-v1" in output
    assert "sizes: small" in output


@pytest.mark.parametrize("size", ["small", "medium", "large"])
def test_doctor_accepts_any_code_size(cli, gateway, capsys, size):
    credentials()
    registration = gateway[1]["/api/runner/v1/runners/me"][1]
    registration["cells"][1] = "task:code/" + size
    assert cli.main(["doctor"]) == 0
    assert f"cells: ok (sizes: {size})" in capsys.readouterr().out


def test_doctor_requires_code_size(cli, gateway, capsys):
    credentials()
    registration = gateway[1]["/api/runner/v1/runners/me"][1]
    registration["cells"].remove("task:code/small")
    assert cli.main(["doctor"]) == 1
    output = capsys.readouterr().out
    assert "cells: MISSING" in output
    for size in ("small", "medium", "large"):
        assert "task:code/" + size in output


@pytest.mark.parametrize("hotkey,expected,warning", [
    ({"bound": False, "verified": False}, "hotkey: none", True),
    ({"bound": True, "verified": True}, "hotkey: bound\n", False),
    ({"bound": True, "verified": False}, "hotkey: bound (unverified)", False),
])
def test_doctor_hotkey(cli, gateway, capsys, hotkey, expected, warning):
    credentials()
    gateway[1]["/api/runner/v1/runners/me"][1]["hotkey"] = hotkey
    assert cli.main(["doctor"]) == 0
    output = capsys.readouterr().out
    assert expected in output
    assert ("warn hotkey: run ormas-miner register-hotkey" in output) is warning


@pytest.mark.parametrize("qualification", [
    {"status": "queued"}, {"job_status": "queued"}, {"job": {"status": "queued"}},
    {"status": "offering"}, {"job_status": "offering"}, {"job": {"status": "offering"}},
])
def test_doctor_optional_qualification_status(cli, gateway, capsys, qualification):
    credentials()
    gateway[1]["/api/runner/v1/runners/me"][1]["qualification"] = qualification
    assert cli.main(["doctor"]) == 0
    status = qualification.get("status") or qualification.get("job_status") or qualification["job"]["status"]
    assert f"qualification: cap 1 (job: {status})" in capsys.readouterr().out


def test_doctor_unknown_qualification_outcome(cli, gateway, capsys):
    credentials()
    gateway[1]["/api/runner/v1/runners/me"][1]["qualification"] = {"status": "outcome_unknown"}
    assert cli.main(["doctor"]) == 0
    assert "qualification: cap 1 (job: outcome_unknown)" in capsys.readouterr().out


@pytest.mark.parametrize("payload", [None, [], "invalid", {}, {"cells": "task:code"}])
def test_doctor_malformed_registration(cli, gateway, capsys, payload):
    credentials()
    gateway[1]["/api/runner/v1/runners/me"] = 200, payload
    assert cli.main(["doctor"]) == 1
    output = capsys.readouterr()
    assert "Traceback" not in output.out + output.err
    assert TOKEN not in output.out + output.err


def test_doctor_malformed_optional_registration_fields(cli, gateway, capsys):
    credentials()
    gateway[1]["/api/runner/v1/runners/me"][1].update({
        "hotkey": {"bound": "false", "verified": "false"},
        "claim_cap": True, "qualification": [],
    })
    assert cli.main(["doctor"]) == 0
    output = capsys.readouterr().out
    assert "hotkey: none" in output
    assert "qualification: cap unknown" in output


@pytest.mark.parametrize("status", [401, 404, 500])
def test_doctor_registration_refusal_redacted(cli, gateway, capsys, status):
    credentials()
    gateway[1]["/api/runner/v1/runners/me"] = status, {
        "error": {"type": "not_found_error", "message": TOKEN},
    }
    assert cli.main(["doctor"]) == 1
    output = capsys.readouterr().out
    assert f"FAIL registration: gateway rejected lookup (HTTP {status})" in output
    assert TOKEN not in output


def test_doctor_old_gateway_without_runners_me_route_warns(cli, gateway, capsys):
    credentials()
    gateway[1]["/api/runner/v1/runners/me"] = 404, {"detail": "Not Found"}
    assert cli.main(["doctor"]) == 0
    output = capsys.readouterr().out
    assert "WARN registration: gateway does not expose /runners/me" in output
    assert "FAIL registration" not in output


def test_doctor_registration_selection_and_device(cli, gateway):
    credentials()
    assert cli.main(["doctor", "--runner-id", "runr_test", "--device-nonce", "device"]) == 0
    for request in gateway[0][1:]:
        assert request.headers["X-Ormas-Runner-Device"] == "device"
        assert request.url.params["runner_id"] == "runr_test"


def test_doctor_old_python(cli, gateway, monkeypatch, capsys):
    credentials()
    monkeypatch.setattr(cli.sys, "version_info", (3, 9, 0))
    assert cli.main(["doctor"]) != 0
    assert "FAIL python: Python >= 3.10 required" in capsys.readouterr().out


def test_doctor_missing_token(cli, gateway, capsys):
    assert cli.main(["doctor"]) != 0
    assert "FAIL credentials: missing token" in capsys.readouterr().out
    assert len(gateway[0]) == 1


def test_doctor_bad_prefix(cli, gateway, monkeypatch, capsys):
    monkeypatch.setenv("ORMAS_MINER_TOKEN", "bad_secret")
    assert cli.main(["doctor"]) != 0
    output = capsys.readouterr().out
    assert "FAIL credentials:" in output and "ormr_" in output
    assert "bad_secret" not in output


def test_doctor_unreachable(cli, monkeypatch, capsys):
    credentials()

    def client(*_args, **_kwargs):
        raise httpx.ConnectError("unreachable")

    monkeypatch.setattr(httpx, "Client", client)
    assert cli.main(["doctor"]) != 0
    assert "FAIL gateway: unreachable" in capsys.readouterr().out


@pytest.mark.parametrize("status,payload", [(503, {"status": "down"}), (200, "not-json")])
def test_doctor_requires_json_health(cli, gateway, capsys, status, payload):
    credentials()
    gateway[1]["/health"] = status, payload
    assert cli.main(["doctor"]) != 0
    assert "FAIL gateway:" in capsys.readouterr().out


@pytest.mark.parametrize("status,error_type,accepted", [
    (401, "authentication_error", False),
    (403, "permission_error", False),
    (404, "not_found_error", True),
    (404, None, False),
    (500, "internal_error", False),
])
def test_doctor_auth_results(cli, gateway, capsys, status, error_type, accepted):
    credentials()
    gateway[1]["/api/runner/v1/queue"] = status, {
        "error": {"type": error_type, "message": TOKEN},
    }
    assert (cli.main(["doctor"]) == 0) is accepted
    output = capsys.readouterr().out
    assert ("ok token:" if accepted else "FAIL token:") in output
    if accepted:
        assert "key accepted (account enablement not confirmed)" in output
    assert TOKEN not in output


def test_doctor_env_and_gateway_override(cli, gateway, monkeypatch):
    credentials(token="bad_old_key")
    monkeypatch.setenv("ORMAS_MINER_TOKEN", TOKEN)
    assert cli.main(["doctor", "--gateway", "http://127.0.0.1:1234"]) == 0
    assert all(request.url.host == "127.0.0.1" for request in gateway[0])


def test_doctor_missing_git_and_path_warning(cli, gateway, monkeypatch, capsys):
    credentials()
    monkeypatch.setattr(cli.shutil, "which", lambda _: None)
    monkeypatch.setenv("PATH", "/missing")
    assert cli.main(["doctor"]) != 0
    output = capsys.readouterr().out
    assert "FAIL git:" in output
    assert "warn PATH:" in output


def test_register_uses_existing_registration(cli, gateway, capsys):
    credentials()
    assert cli.main(["register", "--cell", "task:code", "--capacity", "2"]) == 0
    request = gateway[0][0]
    assert request.url.path == "/api/runner/v1/registrations"
    payload = json.loads(request.content)
    assert payload["runner_id"] == ""
    assert payload["capacity"] == 2
    assert payload["health"]["cells"] == ["task:code"]
    assert "runr_test" in capsys.readouterr().out
    assert len(gateway[0]) == 1


def test_register_sends_miner_id_and_reports_qualification(cli, gateway, capsys):
    credentials()
    gateway[1]["/api/runner/v1/registrations"] = (200, {
        "runner_id": "runr_test", "miner_id": "acme-miner",
        "qualification": {"status": "enqueued", "job_id": "job_proof1"},
    })
    assert cli.main(["register", "--cell", "task:code", "--miner-id", "acme-miner"]) == 0
    payload = json.loads(gateway[0][0].content)
    assert payload["miner_id"] == "acme-miner"
    captured = capsys.readouterr()
    assert json.loads(captured.out.strip())["qualification"]["job_id"] == "job_proof1"
    assert "qualification: proof job job_proof1 reserved for you" in captured.err


def test_register_without_miner_id_omits_it(cli, gateway, capsys):
    credentials()
    assert cli.main(["register", "--cell", "task:code"]) == 0
    assert "miner_id" not in json.loads(gateway[0][0].content)
    assert "qualification: none" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("qualification", "expected"),
    [
        (None, "qualification: none"),
        ({"status": "pending", "job_id": "job_1"}, "job_1 already in progress"),
        ({"status": "cells_missing", "missing": ["task:lang/python", "task:code/small"]},
         "add cells and re-register: task:lang/python task:code/small"),
        ({"status": "error", "error": "public_miner_outside_working_set"},
         "gateway refused (public_miner_outside_working_set)"),
    ],
)
def test_qualification_line_shapes(qualification, expected):
    from neurons import miner

    response = {"runner_id": "runr_test"}
    if qualification is not None:
        response["qualification"] = qualification
    assert expected in miner.qualification_line(response)


def test_run_passes_miner_id_and_prints_qualification(cli, gateway, monkeypatch, capsys):
    from neurons import miner

    credentials()
    gateway[1]["/api/runner/v1/registrations"] = (200, {
        "runner_id": "runr_test", "miner_id": "acme-miner",
        "qualification": {"status": "cells_missing", "missing": ["task:lang/python"]},
    })
    monkeypatch.setattr(miner.MinerSkeleton, "run_once", lambda _self: True)
    assert cli.main(["run", "--repo-id", "repo", "--repo-url", "https://example.test/repo.git",
                     "--cell", "task:code", "--solve-command", "true", "--once",
                     "--miner-id", "acme-miner"]) == 0
    assert json.loads(gateway[0][0].content)["miner_id"] == "acme-miner"
    err = capsys.readouterr().err
    assert "miner_id=acme-miner" in err
    assert "qualification: add cells and re-register: task:lang/python" in err


@pytest.mark.parametrize("command", ["run", "register-hotkey"])
def test_existing_commands_get_private_defaults(cli, monkeypatch, command):
    from neurons import miner

    credentials()
    seen = []

    def main(argv, *, token):
        seen.extend(argv)
        args = miner.build_parser(credential_defaults=True).parse_args(argv)
        assert args.gateway == GATEWAY
        assert token == TOKEN
        assert args.token_env is None and args.token_file is None
        assert "ORMAS_MINER_TOKEN" not in os.environ
        return 3

    monkeypatch.setattr(miner, "main", main)
    flags = (["--repo-id", "repo", "--repo-url", "https://example.test/repo.git",
              "--cell", "task:code", "--solve-command", "make fix", "--once"]
             if command == "run" else
             ["--runner-id", "runr_test", "--hotkey-ss58", "address",
              "--sign-command", "signer"])
    assert cli.main([command, *flags]) == 3
    assert TOKEN not in seen
    assert "ORMAS_MINER_TOKEN" not in os.environ
    assert all(flag in seen for flag in flags)


def test_explicit_credentials_override_saved(cli, monkeypatch):
    from neurons import miner

    credentials()
    monkeypatch.setenv("CUSTOM_KEY", "ormr_custom_efgh")

    def main(argv, *, token):
        args = miner.build_parser(credential_defaults=True).parse_args(argv)
        assert args.gateway == "http://127.0.0.1:1234"
        assert args.token_env == "CUSTOM_KEY"
        assert token == "ormr_custom_efgh"
        return 0

    monkeypatch.setattr(miner, "main", main)
    assert cli.main(["run", "--gateway", "http://127.0.0.1:1234",
                     "--token-env", "CUSTOM_KEY"]) == 0


def test_unsafe_credentials_fail_without_disclosure(cli, gateway, capsys):
    path = credentials()
    path.chmod(0o644)
    assert cli.main(["doctor"]) != 0
    output = capsys.readouterr().out
    assert "FAIL credentials:" in output and "0600" in output
    assert TOKEN not in output


def test_gateway_override_requires_independent_key(cli, gateway, capsys):
    credentials()
    assert cli.main(["register", "--gateway", "https://other.example", "--cell", "task:code"]) == 1
    assert gateway[0] == []
    output = capsys.readouterr().err
    assert "gateway" in output and "--token-env" in output and "--token-file" in output
    assert TOKEN not in output


def test_saved_key_accepts_same_resolved_gateway(cli, gateway):
    credentials(gateway=GATEWAY + "/")
    assert cli.main(["register", "--gateway", GATEWAY, "--cell", "task:code"]) == 0
    assert gateway[0][0].headers["Authorization"] == f"Bearer {TOKEN}"


@pytest.mark.parametrize("url,accepted", [
    ("http://localhost:1234", True),
    ("http://127.0.0.1:1234", True),
    ("http://[::1]:1234", True),
    ("https://other.example", True),
    ("http://other.example", False),
    ("http://127.0.0.1.example", False),
    ("http://localhost.example", False),
])
def test_gateway_transport_validation(cli, monkeypatch, capsys, url, accepted):
    monkeypatch.setenv("TEST_MINER_KEY", TOKEN)
    assert (cli.main(["login", "--gateway", url, "--token-env", "TEST_MINER_KEY"]) == 0) is accepted
    path = Path.home() / ".ormas-miner" / "credentials.json"
    assert path.exists() is accepted
    if not accepted:
        assert "HTTPS" in capsys.readouterr().err


@pytest.mark.parametrize("source", ["env", "file", "conventional"])
@pytest.mark.parametrize("fault", ["permissions", "json", "gateway"])
def test_independent_key_survives_unusable_saved_config(cli, gateway, monkeypatch, source, fault):
    saved = credentials(gateway="http://remote.example" if fault == "gateway" else GATEWAY)
    if fault == "permissions":
        saved.chmod(0o644)
    elif fault == "json":
        saved.write_text("{")
    flags = []
    if source == "file":
        key = Path.home() / "key"
        key.write_text(TOKEN)
        key.chmod(0o600)
        flags = ["--token-file", str(key)]
    else:
        name = "CUSTOM_KEY" if source == "env" else "ORMAS_MINER_TOKEN"
        monkeypatch.setenv(name, TOKEN)
        if source == "env":
            flags = ["--token-env", name]
    assert cli.main(["register", "--cell", "task:code", *flags]) == 0
    request = gateway[0][0]
    assert str(request.url).startswith(GATEWAY + "/")
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"


def test_doctor_probe_timeout(cli, gateway):
    credentials()
    assert cli.main(["doctor"]) == 0
    timeout = gateway[0][1].extensions["timeout"]
    assert set(timeout.values()) == {10.0}


def test_doctor_token_file(cli, gateway):
    saved = credentials()
    saved.chmod(0o644)
    key = Path.home() / "key"
    key.write_text(TOKEN)
    key.chmod(0o600)
    assert cli.main(["doctor", "--token-file", str(key)]) == 0
    assert gateway[0][1].headers["Authorization"] == f"Bearer {TOKEN}"


def test_gateway_diagnostics(cli, monkeypatch, capsys):
    from neurons import miner
    from ormas_subnet.client import OrmasGatewayError

    credentials()

    def main(*_args, **_kwargs):
        raise OrmasGatewayError(status_code=409, error_type="registration_error",
                                message="registration is already bound")

    monkeypatch.setattr(miner, "main", main)
    assert cli.main(["run"]) == 1
    error = capsys.readouterr().err
    assert "409" in error and "registration_error" in error
    assert "registration is already bound" in error
    assert TOKEN not in error


def test_http_diagnostics_show_class_only(cli, monkeypatch, capsys):
    from neurons import miner

    credentials()

    def main(*_args, **_kwargs):
        raise httpx.ConnectError(TOKEN)

    monkeypatch.setattr(miner, "main", main)
    assert cli.main(["run"]) == 1
    error = capsys.readouterr().err
    assert "ConnectError" in error
    assert TOKEN not in error


@pytest.mark.parametrize("command", ["register", "register-hotkey"])
def test_installed_help_shows_credential_defaults(cli, capsys, command):
    with pytest.raises(SystemExit) as result:
        cli.main([command, "--help"])
    assert result.value.code == 0
    output = capsys.readouterr().out
    usage = " ".join(output.split("\n\n", 1)[0].split())
    assert "[--gateway GATEWAY]" in usage
    assert "[--token-env TOKEN_ENV | --token-file TOKEN_FILE]" in usage
    assert "default:" in output and "saved" in output


def test_run_uses_in_process_key(cli, gateway, monkeypatch):
    from neurons import miner

    credentials()
    monkeypatch.setattr(miner.MinerSkeleton, "run_once", lambda _self: True)
    assert cli.main(["run", "--repo-id", "repo", "--repo-url", "https://example.test/repo.git",
                     "--cell", "task:code", "--solve-command", "true", "--once"]) == 0
    assert gateway[0][0].headers["Authorization"] == f"Bearer {TOKEN}"
    assert "ORMAS_MINER_TOKEN" not in os.environ


@pytest.mark.parametrize("signer_ok", [True, False])
def test_installed_hotkey_uses_in_process_key(cli, gateway, monkeypatch, capsys, signer_ok):
    from types import SimpleNamespace

    from neurons import miner

    credentials()
    challenge = "test challenge"
    gateway[1]["/api/runner/v1/hotkey/challenge"] = 200, {"challenge": challenge}
    gateway[1]["/api/runner/v1/hotkey"] = 200, {"verified": True}

    def sign(command, *, input, **_kwargs):
        assert command == ["signer"]
        assert input == challenge.encode()
        assert "ORMAS_MINER_TOKEN" not in os.environ
        if not signer_ok:
            raise miner.subprocess.CalledProcessError(1, command)
        return SimpleNamespace(stdout=b"ab" * 64)

    monkeypatch.setattr(miner.subprocess, "run", sign)
    result = cli.main(["register-hotkey", "--runner-id", "runr_test",
                       "--hotkey-ss58", "address", "--sign-command", "signer"])
    assert result == (0 if signer_ok else 1)
    requests = gateway[0]
    assert all(request.headers["Authorization"] == f"Bearer {TOKEN}" for request in requests)
    assert len(requests) == (2 if signer_ok else 1)
    output = capsys.readouterr()
    assert TOKEN not in output.out + output.err
    if signer_ok:
        assert json.loads(requests[1].content)["signature_hex"] == "ab" * 64
        assert json.loads(output.out)["verified"] is True
    else:
        assert "signer" in output.err


@pytest.mark.parametrize("failure", ["gateway", "http"])
def test_installed_hotkey_diagnostics(cli, gateway, monkeypatch, capsys, failure):
    from ormas_subnet.client import OrmasGatewayError, OrmasMinerClient

    credentials()

    def challenge(*_args, **_kwargs):
        if failure == "gateway":
            raise OrmasGatewayError(status_code=409, error_type="conflict_error",
                                    message="hotkey is already bound")
        raise httpx.ConnectError(TOKEN)

    monkeypatch.setattr(OrmasMinerClient, "hotkey_challenge", challenge)
    assert cli.main(["register-hotkey", "--runner-id", "runr_test",
                     "--hotkey-ss58", "address", "--sign-command", "signer"]) == 1
    error = capsys.readouterr().err
    if failure == "gateway":
        assert "409" in error and "conflict_error" in error
        assert "hotkey is already bound" in error
    else:
        assert "ConnectError" in error
    assert TOKEN not in error
    assert gateway[0] == []


def test_standalone_reference_credentials_stay_required(capsys):
    from neurons import miner

    with pytest.raises(SystemExit) as result:
        miner._parse(["--gateway", GATEWAY, "--repo-id", "repo", "--repo-url", "url",
                      "--cell", "task:code", "--solve-command", "true"])
    assert result.value.code == 2
    assert "--token-env --token-file is required" in capsys.readouterr().err
    with pytest.raises(SystemExit) as result:
        miner._parse(["register-hotkey", "--runner-id", "runr_test",
                      "--hotkey-ss58", "address", "--sign-command", "signer"])
    assert result.value.code == 2
    assert "--gateway" in capsys.readouterr().err


def test_install_run_example_uses_assigned_id():
    text = (ROOT / "docs" / "INSTALL.md").read_text()
    paragraph = text.split("The same loop is available as ", 1)[1].split("\n\n", 1)[0]
    example = "ormas-miner run --runner-id <assigned-id> --repo-id <id>"
    assert example in paragraph


def test_doctor_prints_reserved_job_exclusions(cli, gateway, capsys):
    credentials()
    gateway[1]["/api/runner/v1/queue"] = 200, {
        "schema_version": "ormas.runner-queue.v1", "jobs": [],
        "excluded": [
            {"job_id": "job_reserved", "excluded_by": "required_cells_missing",
             "detail": {"cells": ["task:code"]}},
            {"job_id": "job_stale", "excluded_by": "validator_liveness"},
        ],
    }
    assert cli.main(["doctor"]) == 0
    output = capsys.readouterr().out
    assert 'reserved job job_reserved hidden: required_cells_missing {"cells": ["task:code"]}' in output
    assert "reserved job job_stale hidden: validator_liveness" in output
    assert TOKEN not in output


def test_doctor_skips_malformed_exclusions(cli, gateway, capsys):
    credentials()
    gateway[1]["/api/runner/v1/queue"] = 200, {
        "schema_version": "ormas.runner-queue.v1", "jobs": [],
        "excluded": [
            {"job_id": "job_missing_reason"},
            {"excluded_by": "required_cells_missing"},
        ],
    }
    assert cli.main(["doctor"]) == 0
    assert "reserved job" not in capsys.readouterr().out


def test_doctor_queries_requested_runner(cli, gateway):
    credentials()
    assert cli.main(["doctor", "--runner-id", "runr_mine"]) == 0
    assert gateway[0][1].url.params["runner_id"] == "runr_mine"
