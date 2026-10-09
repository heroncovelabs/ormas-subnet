"""The checker's Protected path: advertise, attest per assignment, clone with the sealed token.

Design: coordination/attested_validator_8c10850e_2026_10_08.md (monorepo). A fake evidence
function stands in for the dstack guest agent; a fake gateway seals with the shim's own
sealer (the monorepo parity test pins it to the gateway's).
"""
from __future__ import annotations

import base64
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest

from ormas_subnet import outcomes_support
from ormas_subnet import protected_attestation as pa
from ormas_subnet.validator import (
    OrmasValidatorClient,
    ValidatorConfig,
    ValidatorDaemon,
    canonical_evidence_fields,
    evidence_digest_hex,
    main,
    make_ed25519_signer,
)

from tests.test_skeleton import _FakeResponse, _init_repo, requires_git

REPO_URL = "https://github.com/acme/private-repo.git"
TOKEN = "ghs_protected-dummy-token"
EXPIRES = "2999-01-01T00:00:00+00:00"
EVIDENCE = {"kind": "tdx", "quote_b64": base64.b64encode(b"quote").decode(), "event_log_json": "[]"}


class _ProtectedGateway:
    """Validator routes for one Protected assignment: stub, challenge, sealed release, full row."""

    def __init__(self, full: dict[str, Any], *, job_id: str = "job_p", attempt: int = 2,
                 kind: str = pa.SEALED_CREDENTIAL_KIND) -> None:
        self.full = full
        self.job_id, self.attempt, self.kind = job_id, attempt, kind
        self.released = False
        self.registered: dict[str, Any] | None = None
        self.challenges: list[str] = []
        self.releases: list[dict[str, Any]] = []
        self.decisions: list[dict[str, Any]] = []
        self.nonce = bytes(range(32))

    def post(self, path: str, json: dict[str, Any] | None = None, headers: Any = None):
        body = json or {}
        if path == "/api/validator/v1/registrations":
            self.registered = body
            return _FakeResponse(200, {"validator_id": "val_p"})
        if path.endswith("/attestation-challenge"):
            self.challenges.append(path)
            return _FakeResponse(200, {"nonce": self.nonce.hex(), "issued_at": "now",
                                       "job_id": self.job_id, "attempt": self.attempt})
        if path.endswith("/repo-credential"):
            self.releases.append(body)
            pub = base64.b64decode(body["enclave_pubkey"])
            self.released = True
            return _FakeResponse(200, {"repo_credential": {
                "kind": self.kind, "scheme": pa.SEALED_CREDENTIAL_SCHEME,
                "sealed": base64.b64encode(pa.seal(TOKEN.encode(), pub)).decode(),
                "expires_at": EXPIRES}})
        if path.endswith("/decisions"):
            self.decisions.append(body)
            return _FakeResponse(200, {"decision": body["decision"], "quorum": body["decision"]})
        raise AssertionError(f"unhandled path: {path}")

    def get(self, path: str):
        assert path == "/api/validator/v1/assignments"
        if self.decisions:
            return _FakeResponse(200, {"assignments": []})
        if not self.released:
            return _FakeResponse(200, {"assignments": [{
                "assignment_id": self.full["assignment_id"], "service_level": "protected",
                "attestation_required": True}]})
        return _FakeResponse(200, {"assignments": [self.full]})


def _full_row(base: str, result: str) -> dict[str, Any]:
    fields = canonical_evidence_fields(
        job_id="job_p", miner_id="miner:other", base_commit=base, result_commit=result,
        repo_url=REPO_URL, verify_command="grep -q mined out.txt", allowed_paths=["out.txt"],
        immutable_paths=[])
    return {"assignment_id": "asg_p", **fields, "evidence_digest_sha256": evidence_digest_hex(fields),
            "repo_credential": None, "service_level": "protected"}


def _result_commit(repo: Path) -> str:
    (repo / "out.txt").write_text("base\nmined\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "result"], cwd=repo, check=True, capture_output=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()


def _daemon(gateway, tmp_path, *, evidence_fn=None, protected=True):
    client = OrmasValidatorClient(base_url="https://fake.invalid", token="ormv_test", http_client=gateway)
    sign_fn, pub = make_ed25519_signer("11" * 32)
    seen: list[bytes] = []

    def evidence(report_data: bytes) -> dict:
        seen.append(report_data)
        return EVIDENCE

    attestor = (pa.ProtectedAssignmentAttestor(client, evidence_fn or evidence,
                                               signer_pubkey=bytes.fromhex(pub))
                if protected else None)
    daemon = ValidatorDaemon(client, ValidatorConfig(workdir_root=tmp_path / "vw"), sign_fn,
                             protected=attestor)
    daemon.register(pubkey_hex=pub)
    return daemon, seen


SIGNER = bytes.fromhex(make_ed25519_signer("11" * 32)[1])


# ── shim ───────────────────────────────────────────────────────────────────


def test_seal_round_trip_and_tamper_refusal():
    priv, pub = pa.generate_keypair()
    sealed = pa.seal(b"secret-token", pub)
    assert pa.open_sealed(sealed, priv) == b"secret-token"
    tampered = sealed[:-1] + bytes([sealed[-1] ^ 1])
    with pytest.raises(ValueError):
        pa.open_sealed(tampered, priv)
    with pytest.raises(ValueError):
        pa.open_sealed(sealed, pa.generate_keypair()[0])


def test_report_data_binds_challenge_assignment_and_key():
    _, pub = pa.generate_keypair()
    nonce = b"\x01" * 32
    data = pa.report_data_for_assignment(nonce, job_id="j", attempt=1, assignment_id="a",
                                         enclave_pubkey=pub, signer_pubkey=SIGNER)
    assert len(data) == 64 and data[:32] == nonce
    for change in ({"job_id": "k"}, {"attempt": 2}, {"assignment_id": "b"},
                   {"signer_pubkey": b"\xcd" * 32}):
        kwargs = {"job_id": "j", "attempt": 1, "assignment_id": "a", "signer_pubkey": SIGNER,
                  **change}
        assert pa.report_data_for_assignment(nonce, enclave_pubkey=pub, **kwargs) != data
    with pytest.raises(ValueError):
        pa.report_data_for_assignment(b"short", job_id="j", attempt=1, assignment_id="a",
                                      enclave_pubkey=pub, signer_pubkey=SIGNER)
    with pytest.raises(ValueError):
        pa.report_data_for_assignment(nonce, job_id="j", attempt=1, assignment_id="a",
                                      enclave_pubkey=pub, signer_pubkey=b"short")


def test_attestor_requires_the_checker_decision_key():
    for bad in (b"", b"\xab" * 31, "ab" * 32):
        with pytest.raises(ValueError):
            pa.ProtectedAssignmentAttestor(object(), lambda _rd: {}, signer_pubkey=bad)


def test_evidence_source_env():
    assert pa.evidence_fn_from_env({}) is None
    assert isinstance(pa.evidence_fn_from_env({"ORMAS_PROTECTED_EVIDENCE_SOURCE": "dstack"}),
                      pa.DstackQuoteClient)
    with pytest.raises(ValueError):
        pa.evidence_fn_from_env({"ORMAS_PROTECTED_EVIDENCE_SOURCE": "fake"})


def test_dstack_client_returns_the_gateway_wire_shape():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.read()))
        return httpx.Response(200, json={"quote": "abcd", "event_log": "[{\"e\":1}]"})

    client = pa.DstackQuoteClient(transport=httpx.MockTransport(handler))
    wire = client(b"\x02" * 64)
    assert wire == {"kind": "tdx", "quote_b64": base64.b64encode(bytes.fromhex("abcd")).decode(),
                    "event_log_json": "[{\"e\":1}]"}
    assert seen[0][0] == "/GetQuote" and ("02" * 64).encode() in seen[0][1]
    with pytest.raises(pa.ProtectedAttestationError):
        client(b"\x02" * 63)
    bad = pa.DstackQuoteClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"quote": "zz"})))
    with pytest.raises(pa.ProtectedAttestationError, match="quote_malformed"):
        bad(b"\x02" * 64)


# ── daemon ─────────────────────────────────────────────────────────────────


def test_standard_registration_body_is_unchanged(tmp_path):
    gateway = _ProtectedGateway(_full_row("a" * 40, "b" * 40))
    _daemon(gateway, tmp_path, protected=False)
    assert "task_cells" not in gateway.registered


def test_protected_checker_advertises_the_protected_cell(tmp_path):
    gateway = _ProtectedGateway(_full_row("a" * 40, "b" * 40))
    _daemon(gateway, tmp_path)
    assert gateway.registered["task_cells"] == ["task:service/protected"]


def test_standard_checker_ignores_protected_stubs(tmp_path):
    gateway = _ProtectedGateway(_full_row("a" * 40, "b" * 40))
    daemon, _ = _daemon(gateway, tmp_path, protected=False)
    assert daemon.run_once() is False
    assert gateway.challenges == [] and gateway.decisions == []


@requires_git
def test_protected_checker_attests_then_reviews_with_the_sealed_token(tmp_path, monkeypatch):
    repo, base = _init_repo(tmp_path)
    result = _result_commit(repo)
    gateway = _ProtectedGateway(_full_row(base, result))
    clones = []

    def fake_protected_git(args, *, cwd, repo_url, token, expires_at):
        clones.append((list(args), repo_url, token, expires_at))
        local = [str(repo) if a == repo_url else a for a in args]
        subprocess.run(["git", *local], cwd=cwd, check=True, capture_output=True)

    monkeypatch.setattr(outcomes_support, "protected_repository_git", fake_protected_git)
    daemon, seen = _daemon(gateway, tmp_path)
    assert daemon.run_once() is True  # stub: attest
    assert gateway.decisions == [] and len(gateway.releases) == 1
    pub = base64.b64decode(gateway.releases[0]["enclave_pubkey"])
    assert seen == [pa.report_data_for_assignment(
        gateway.nonce, job_id="job_p", attempt=2, assignment_id="asg_p", enclave_pubkey=pub,
        signer_pubkey=SIGNER)]
    assert gateway.releases[0]["evidence"] == EVIDENCE
    assert daemon.run_once() is True  # full row: clone, verify, sign, post
    assert [d["decision"] for d in gateway.decisions] == ["accept"]
    assert clones == [(["clone", "--no-checkout", REPO_URL, str(tmp_path / "vw" / "asg_p")],
                       REPO_URL, TOKEN, EXPIRES)]
    assert len(gateway.releases) == 1  # the cached token served the clone
    assert daemon._protected_tokens == {}
    assert daemon.run_once() is False


def test_refused_release_waits_for_the_next_poll(tmp_path):
    gateway = _ProtectedGateway(_full_row("a" * 40, "b" * 40), kind="github_app_read_token")
    daemon, _ = _daemon(gateway, tmp_path)
    assert daemon.run_once() is False
    assert gateway.decisions == [] and daemon._protected_tokens == {}


def test_failing_evidence_source_never_reaches_release(tmp_path):
    def broken(_report_data):
        raise OSError("no socket at /var/run/dstack.sock")

    gateway = _ProtectedGateway(_full_row("a" * 40, "b" * 40))
    daemon, _ = _daemon(gateway, tmp_path, evidence_fn=broken)
    assert daemon.run_once() is False
    assert gateway.releases == []


def test_protected_mode_refuses_parallel_slots(tmp_path):
    gateway = _ProtectedGateway(_full_row("a" * 40, "b" * 40))
    daemon, _ = _daemon(gateway, tmp_path)
    with pytest.raises(ValueError):
        daemon.serve(2, 0.01, until=lambda: True)


@pytest.mark.parametrize("env, argv_extra, message", [
    ({"ORMAS_PROTECTED_EVIDENCE_SOURCE": "dstack"}, ["--slots", "2"], "--slots must be 1"),
    ({"ORMAS_PROTECTED_EVIDENCE_SOURCE": "fake"}, [], "must be 'dstack'"),
])
def test_cli_refuses_unsafe_protected_configuration(monkeypatch, capsys, env, argv_extra, message):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("V_TOKEN", "ormv_cli")
    monkeypatch.setenv("V_KEY", "13" * 32)
    with pytest.raises(SystemExit) as exc:
        main(["--gateway", "https://fake.invalid", "--token-env", "V_TOKEN",
              "--private-key-env", "V_KEY", *argv_extra])
    assert exc.value.code == 2
    assert message in capsys.readouterr().err


class _StuckStubGateway(_ProtectedGateway):
    """The release succeeds but the listing keeps showing the stub (another gateway machine)."""

    def get(self, path: str):
        return _FakeResponse(200, {"assignments": [{
            "assignment_id": self.full["assignment_id"], "service_level": "protected",
            "attestation_required": True}]})


def test_an_attested_stub_is_not_reattested_while_its_token_is_live(tmp_path):
    gateway = _StuckStubGateway(_full_row("a" * 40, "b" * 40))
    daemon, _ = _daemon(gateway, tmp_path)
    assert daemon.run_once() is True
    assert daemon.run_once() is False  # the serve loop sleeps instead of spinning
    assert len(gateway.challenges) == len(gateway.releases) == 1


class _BlockedThenStandardGateway(_ProtectedGateway):
    """A stub the gateway refuses to release, listed before a Standard assignment."""

    def __init__(self, full, standard):
        super().__init__(full, kind="github_app_read_token")
        self.standard = standard

    def get(self, path: str):
        rows = [{"assignment_id": "asg_stuck", "service_level": "protected",
                 "attestation_required": True}]
        if not self.decisions:
            rows.append(self.standard)
        return _FakeResponse(200, {"assignments": rows})


@requires_git
def test_a_refused_stub_does_not_block_the_standard_assignment_behind_it(tmp_path):
    repo, base = _init_repo(tmp_path)
    result = _result_commit(repo)
    fields = canonical_evidence_fields(
        job_id="job_s", miner_id="miner:other", base_commit=base, result_commit=result,
        repo_url=str(repo), verify_command="grep -q mined out.txt", allowed_paths=["out.txt"],
        immutable_paths=[])
    standard = {"assignment_id": "asg_s", **fields,
                "evidence_digest_sha256": evidence_digest_hex(fields), "repo_credential": None}
    gateway = _BlockedThenStandardGateway(_full_row(base, result), standard)
    daemon, _ = _daemon(gateway, tmp_path)
    assert daemon.run_once() is True
    assert [d["decision"] for d in gateway.decisions] == ["accept"]
    assert daemon._protected_tokens == {}
