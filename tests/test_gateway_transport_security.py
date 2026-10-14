"""Bearer-bearing clients refuse credential exposure through unsafe origins."""
from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from ormas_subnet._gateway_url import require_secure_gateway_url
from ormas_subnet.client import OrmasMinerClient
from ormas_subnet.validator import OrmasValidatorClient, main as validator_main


class _InjectedTransport:
    pass


@pytest.mark.parametrize("url", [
    "https://api.ormas.ai",
    "https://gateway.example:8443/base/",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
    "http://[::1]:8000",
])
def test_secure_gateway_origins_are_accepted(url):
    assert require_secure_gateway_url(url) == url.rstrip("/")


@pytest.mark.parametrize("url", [
    "http://gateway.example",
    "http://127.0.0.1.example",
    "http://localhost.example",
    "http://127.0.0.2",
    "https://user@gateway.example",
    "https://user:pass@gateway.example",
    "https://gateway.example?token=bad",
    "https://gateway.example#fragment",
    " https://gateway.example",
    "https://gateway.example\n",
    "https://gateway.example\\@other.example",
    "https://gateway.example:99999",
    "https://[::1",
    "file:///tmp/socket",
    "local://test",
    "",
])
def test_unsafe_gateway_origins_are_refused(url):
    with pytest.raises(ValueError, match="gateway"):
        require_secure_gateway_url(url)


@pytest.mark.parametrize("client", [OrmasMinerClient, OrmasValidatorClient])
@pytest.mark.parametrize("url", [
    "http://gateway.example",
    "http://127.0.0.1.example",
    "https://user@gateway.example",
    "https://gateway.example?token=bad",
    "https://gateway.example#fragment",
])
def test_clients_refuse_unsafe_origin_before_using_injected_transport(client, url):
    with pytest.raises(ValueError, match="gateway"):
        client(url, "secret", http_client=_InjectedTransport())


@pytest.mark.parametrize("client", [OrmasMinerClient, OrmasValidatorClient])
def test_local_sentinel_requires_injected_transport(client):
    instance = client("local://localnet-demo", "secret", http_client=_InjectedTransport())
    assert instance.base_url == "local://localnet-demo"
    with pytest.raises(ValueError, match="gateway"):
        client("local://localnet-demo", "secret")


@pytest.mark.parametrize("client", [OrmasMinerClient, OrmasValidatorClient])
def test_owned_http_client_never_follows_redirects_or_environment_proxies(client, monkeypatch):
    calls = []

    class FakeClient:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        def close(self):
            pass

    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(Client=FakeClient))
    instance = client("https://gateway.example/", "secret")
    assert instance.base_url == "https://gateway.example"
    assert calls == [{
        "base_url": "https://gateway.example",
        "headers": {"Authorization": "Bearer secret"},
        "timeout": 60.0,
        "follow_redirects": False,
        "trust_env": False,
    }]


@pytest.mark.parametrize("protected", [False, True])
def test_miner_doctor_clients_ignore_environment_proxies(monkeypatch, protected):
    from ormas_subnet import miner_cli

    calls = []

    class Response:
        status_code = 200

        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {"status": "ok"}

    class FakeClient:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        @staticmethod
        def get(*_args, **_kwargs):
            return Response()

    class FakeMinerClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        @staticmethod
        def list_queue(_runner_id):
            return {"jobs": []}

        @staticmethod
        def get_registration(_runner_id):
            if protected:
                return {
                    "runner_id": "runr_test",
                    "protected": {
                        "slot_approved": True,
                        "slot_bound": True,
                        "declared_worker_digests": [],
                        "approved_worker_policy": True,
                        "qualification_job_status": "done",
                        "audit_status": "ok",
                        "last_refusal": None,
                        "last_release": None,
                    },
                }
            return {"runner_id": "runr_test", "cells": [], "claim_cap": 0}

    monkeypatch.setattr(miner_cli, "OrmasMinerClient", FakeMinerClient)
    monkeypatch.setattr(miner_cli.httpx, "Client", FakeClient)
    monkeypatch.setattr(
        miner_cli, "_settings", lambda _args: ("http://localhost:8000", "ormr_test"),
    )
    monkeypatch.setattr(miner_cli.shutil, "which", lambda _name: "/usr/bin/git")
    args = SimpleNamespace(
        gateway="http://localhost:8000",
        runner_id="runr_test",
        device_nonce=None,
    )

    if protected:
        assert miner_cli._doctor_protected(args) == 0
        assert len(calls) == 1
    else:
        # The ordinary doctor creates an anonymous health client and then its
        # bearer-bearing probe client; both are repository owned transports.
        miner_cli._doctor(args)
        assert len(calls) == 2
    assert all(call["trust_env"] is False for call in calls)
    assert all(call["follow_redirects"] is False for call in calls)


def test_validator_cli_refuses_remote_http_before_loading_keys(capsys):
    with pytest.raises(SystemExit) as stopped:
        validator_main([
            "--gateway", "http://gateway.example",
            "--token-env", "MISSING_VALIDATOR_TOKEN",
            "--private-key-env", "MISSING_VALIDATOR_KEY",
            "--once",
        ])
    assert stopped.value.code == 2
    assert "HTTPS" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [
    [
        "--gateway", "http://gateway.example",
        "--token-env", "MISSING_MINER_TOKEN",
        "--repo-id", "repo",
        "--repo-url", "https://example.test/repo.git",
        "--cell", "task:code",
        "--solve-command", "true",
        "--once",
    ],
    [
        "register-hotkey",
        "--gateway", "http://gateway.example",
        "--token-env", "MISSING_MINER_TOKEN",
        "--runner-id", "runr_test",
        "--hotkey-ss58", "synthetic-hotkey",
        "--sign-command", "true",
    ],
])
def test_reference_miner_refuses_remote_http_before_loading_token(argv, monkeypatch, capsys):
    from neurons import miner

    monkeypatch.setattr(
        miner,
        "load_token",
        lambda **_kwargs: pytest.fail("token was loaded before gateway validation"),
    )
    with pytest.raises(SystemExit) as stopped:
        miner.main(argv)
    assert stopped.value.code == 2
    assert "HTTPS" in capsys.readouterr().err
