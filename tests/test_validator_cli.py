"""The public wheel must expose the task validator without a source checkout."""
from __future__ import annotations

import os
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys
import threading

import pytest


def test_public_validator_module_has_a_runnable_cli_without_chain_dependencies(tmp_path):
    source = Path(__file__).resolve().parents[1]
    process = subprocess.run(
        [sys.executable, "-c", (
            "import runpy,sys; sys.modules['bittensor']=None; "
            "sys.modules['tensorbox_spec']=None; "
            "sys.argv=['ormas-validator','--help']; "
            "runpy.run_module('ormas_subnet.validator',run_name='__main__')"
        )],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(source)},
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert process.returncode == 0, process.stderr
    assert "--private-key-file" in process.stdout, "the packaged module does not expose the daemon CLI"
    assert "--token-file" in process.stdout
    assert "--once" in process.stdout
    assert "--wallet" not in process.stdout


def test_validator_cli_registers_and_polls_with_private_file_credentials(tmp_path):
    from ormas_subnet.validator import main

    calls = []
    token = "ormv_synthetic_cli_test"
    key_hex = "13" * 32

    class Gateway(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def respond(self, value):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            body = json.dumps(value).encode()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((self.path, self.headers.get("Authorization"), body))
            self.respond({"validator_id": "val_cli"})

        def do_GET(self):
            calls.append((self.path, self.headers.get("Authorization"), None))
            self.respond({"assignments": []})

    token_file = tmp_path / "bearer"
    key_file = tmp_path / "decision-key"
    token_file.write_text(token)
    key_file.write_text(key_hex)
    token_file.chmod(0o600)
    key_file.chmod(0o600)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status = main([
            "--gateway", f"http://127.0.0.1:{server.server_port}",
            "--token-file", str(token_file), "--private-key-file", str(key_file),
            "--workdir-root", str(tmp_path / "work"), "--once",
        ])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert status == 3, "idle is not an accepted task"
    assert [call[0] for call in calls] == [
        "/api/validator/v1/registrations", "/api/validator/v1/assignments",
    ]
    assert all(call[1] == f"Bearer {token}" for call in calls)
    assert len(calls[0][2]["pubkey_hex"]) == 64
    assert key_hex not in json.dumps(calls), "private key was sent to the gateway"


@pytest.mark.parametrize("unsafe", ["token", "key", "symlink"])
def test_validator_cli_refuses_exposed_credentials_before_network(tmp_path, monkeypatch, unsafe):
    from ormas_subnet import validator

    token = tmp_path / "token"
    key = tmp_path / "key"
    token.write_text("ormv_synthetic_cli_test")
    key.write_text("13" * 32)
    token.chmod(0o600)
    key.chmod(0o600)
    if unsafe == "symlink":
        link = tmp_path / "linked-key"
        link.symlink_to(key)
        key = link
    else:
        (token if unsafe == "token" else key).chmod(0o644)
    monkeypatch.setattr(validator, "OrmasValidatorClient", lambda **_kwargs: pytest.fail("network boundary reached"))
    with pytest.raises(ValueError, match="private"):
        validator.main([
            "--gateway", "http://127.0.0.1:1", "--token-file", str(token),
            "--private-key-file", str(key), "--once",
        ])
