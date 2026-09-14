"""The reference skeleton serves a repository it never configured, using the job-scoped
``repo_credential`` the gateway puts on an unbound claim draft (Ormas launch item 2).

Protocol addition 2026-09-14 (mirrors the gateway's ``TaskDraft``):

    "repo_credential": {"kind": "ssh_deploy_key", "private_key": "<openssh pem>", "fingerprint": "SHA256:…"}

Absent on the wire when the miner's bind already covers the repo. Contract for the skeleton:
* ``TaskDraft`` gains ``repo_credential: Mapping | None = None``; ``to_wire`` omits it when None
  (byte-identical), ``from_wire`` defaults it.
* When the draft carries a credential AND a non-empty ``repo_url``, ``_clone_and_checkout``
  clones ``draft.repo_url`` (not ``MinerConfig.repo_url``) and ``_publish`` pushes to that same
  URL, both with ``GIT_SSH_COMMAND`` pointing at a 0600 key file that exists only for the
  duration of the git call. The key is never written anywhere else and never logged.
* No credential → today's behaviour (clone ``config.repo_url``, push ``config.push_remote``).
"""
from __future__ import annotations

import subprocess
from pathlib import Path


from ormas_subnet import skeleton as sk
from ormas_subnet.client import OrmasMinerClient
from ormas_subnet.localnet import LocalGateway
from ormas_subnet.protocol import TaskDraft
from ormas_subnet.reference_solver import make_shell_solver
from ormas_subnet.skeleton import MinerConfig, MinerSkeleton

from tests.test_skeleton import _init_repo, requires_git

FAKE_KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nZmFrZQ==\n-----END OPENSSH PRIVATE KEY-----\n"
CRED = {"kind": "ssh_deploy_key", "private_key": FAKE_KEY, "fingerprint": "SHA256:fake"}


def _draft(**over) -> TaskDraft:
    base = dict(task_id="t", runner_id="r", repo_id="", base_commit="a" * 40, brief="b", verify_command="true",
                allowed_paths=[], budget_usd=1.0, work_packet={}, work_packet_sha256="b" * 64, attempt=0,
                parent_job_id="", repair_findings=[])
    base.update(over)
    return TaskDraft(**base)


def test_dto_carries_optional_credential_and_omits_it_when_absent() -> None:
    plain = _draft(repo_url="git@github.com:acme/demo.git")
    assert "repo_credential" not in plain.to_wire()
    assert TaskDraft.from_wire(plain.to_wire()).repo_credential is None
    with_cred = _draft(repo_url="git@github.com:acme/demo.git", repo_credential=CRED)
    wire = with_cred.to_wire()
    assert wire["repo_credential"] == CRED
    assert TaskDraft.from_wire(wire).repo_credential["fingerprint"] == "SHA256:fake"


class _CredentialGateway(LocalGateway):
    """LocalGateway that also serves a repo_credential on the draft."""

    def __init__(self, *a, repo_credential=None, **kw):
        super().__init__(*a, **kw)
        self.repo_credential = repo_credential

    def _handle_claim(self, body):  # type: ignore[override]
        resp = super()._handle_claim(body)
        if resp.status_code == 200 and self.repo_credential is not None:
            body = resp.json()
            body["draft"]["repo_credential"] = self.repo_credential
            return type(resp)(200, body)
        return resp


@requires_git
def test_credential_draft_clones_draft_repo_url_with_the_key_and_leaves_no_key_behind(tmp_path: Path, monkeypatch) -> None:
    upstream, base = _init_repo(tmp_path)
    seen: list[dict] = []
    real_run = subprocess.run

    def spy_run(argv, **kwargs):
        if argv[:1] == ["git"] and argv[1:2] in (["clone"], ["push"]):
            env = kwargs.get("env") or {}
            seen.append({"argv": list(argv), "ssh": env.get("GIT_SSH_COMMAND", ""),
                         "key_exists": _key_path(env.get("GIT_SSH_COMMAND", "")) is not None
                         and Path(_key_path(env["GIT_SSH_COMMAND"])).exists()})
        return real_run(argv, **kwargs)

    monkeypatch.setattr(sk.subprocess, "run", spy_run)
    gateway = _CredentialGateway(task_id="task_1", base_commit=base, verify_command="true",
                                 repo_url=str(upstream), repo_id="", repo_credential=CRED)
    client = OrmasMinerClient(base_url="https://fake.invalid", token="ormr_test", http_client=gateway)
    config = MinerConfig(runner_id="miner-1", runner_version="0.0.1", platform="linux", capacity=1,
                         cells=("code-edit-small",), workdir_root=tmp_path / "work", repo_id="repo1",
                         repo_url=str(tmp_path / "does-not-exist"),  # must NOT be cloned
                         push_remote=None, ask_usd=None)
    skeleton = MinerSkeleton(client, config, make_shell_solver(["sh", "-c", "echo mined >> out.txt"]))
    skeleton.register()
    assert skeleton.run_once() is True

    clones = [s for s in seen if s["argv"][1] == "clone"]
    assert clones and str(upstream) in clones[0]["argv"]
    assert "IdentitiesOnly=yes" in clones[0]["ssh"] and clones[0]["key_exists"]
    assert not Path(_key_path(clones[0]["ssh"])).exists()  # gone after the call
    assert gateway.completed["terminal"]["verification_state"] == "verified"


def _key_path(ssh_cmd: str) -> str | None:
    parts = ssh_cmd.split()
    for i, p in enumerate(parts):
        if p == "-i" and i + 1 < len(parts):
            return parts[i + 1]
    return None


@requires_git
def test_no_credential_keeps_configured_clone_source(tmp_path: Path) -> None:
    upstream, base = _init_repo(tmp_path)
    gateway = LocalGateway(task_id="task_1", base_commit=base, verify_command="true",
                           repo_url="git@github.com:someone/else.git", repo_id="repo1")
    client = OrmasMinerClient(base_url="https://fake.invalid", token="ormr_test", http_client=gateway)
    config = MinerConfig(runner_id="miner-1", runner_version="0.0.1", platform="linux", capacity=1,
                         cells=("code-edit-small",), workdir_root=tmp_path / "work", repo_id="repo1",
                         repo_url=str(upstream), push_remote=None, ask_usd=None)
    skeleton = MinerSkeleton(client, config, make_shell_solver(["sh", "-c", "echo mined >> out.txt"]))
    skeleton.register()
    skeleton.bind(project_id="proj_abc", base_commit=base)
    assert skeleton.run_once() is True
    assert gateway.completed["terminal"]["verification_state"] == "verified"
