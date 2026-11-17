"""Regression for Protected packet p6-sdk-publish-renews-lease (r2).

Extracted from the acceptance bridge contract: publication renews the lease first and retries exactly once on a
409 public_publication_attempt_unavailable. Run from public_subnet (cwd) like the SDK suites:
  PYTHONPATH=public_subnet .venv/bin/python -m pytest -q public_subnet/tests/test_p6_sdk_publish_renews_lease.py
"""
from pathlib import Path
import hashlib
from ormas_subnet import skeleton as sdk
from ormas_subnet.client import OrmasGatewayError
from tests.test_skeleton_public_recovery import _RecoveryClient, _lease_and_draft, _config, _successful_result

def test_publication_renews_and_retries_once(monkeypatch, tmp_path):
    lease, draft = _lease_and_draft()
    draft.work_packet["execution_requirements"] = {"repository_visibility": "public"}
    artifact = b"public-artifact"
    published = {"result_ref": f"refs/heads/ormas/job/{draft.task_id}", "result_commit": "d" * 40,
                 "artifact_sha256": hashlib.sha256(artifact).hexdigest(), "tree_sha": "f" * 40}
    def build(_draft, _workdir, _result, target):
        target.write(artifact); target.seek(0)
        return len(artifact), published["artifact_sha256"], published["tree_sha"]
    def git(argv, *, cwd):
        if argv[0] == "fetch": return b""
        if argv[0] == "rev-parse": return published["tree_sha"].encode()
        return f"{published['result_commit']} {draft.base_commit}".encode()
    monkeypatch.setattr(sdk, "_build_public_artifact", build)
    monkeypatch.setattr(sdk, "_public_git", git)
    class Client(_RecoveryClient):
        def heartbeat_task(self, task_id, runner_id, lease_token, *, renew):
            assert (task_id, runner_id, lease_token, renew) == (draft.task_id, draft.runner_id, lease.lease_id, True)
            self.events.append("heartbeat(renew=True)")
            if self.renewal_error: raise self.renewal_error
            return {"ok": True}
        def publish_result(self, task_id, runner_id, lease_token, *, source, length):
            source.seek(0)
            assert len(source.read()) == length
            self.events.append("publish_result")
            value = self.publication_results.pop(0)
            if isinstance(value, Exception): raise value
            return value
    def error(kind, status=409):
        return OrmasGatewayError(status_code=status, error_type=kind, message=kind)
    unavailable = error("public_publication_attempt_unavailable")
    second = error("public_publication_attempt_unavailable")
    lost, other = error("lease_lost"), error("other", 503)
    wrong_status = error("public_publication_attempt_unavailable", 503)
    pair = ["heartbeat(renew=True)", "publish_result"]
    cases = [([published], None, None, pair),
             ([unavailable, published], None, None, pair * 2),
             ([unavailable, second], None, second, pair * 2),
             ([lost], None, lost, pair), ([published], lost, lost, pair[:1]),
             ([other], None, other, pair), ([wrong_status], None, wrong_status, pair)]
    for responses, renewal_error, expected_error, calls in cases:
        client = Client(lease, draft, [], responses)
        client.renewal_error = renewal_error
        runner = sdk.MinerSkeleton(client, _config(tmp_path), lambda *_: _successful_result())
        caught = None
        try: outcome = runner._publish_public(lease, draft, tmp_path, _successful_result(),
                                                capture={"scope_ok": True})
        except Exception as exc: caught = exc
        assert client.events == calls, "publish must renew the lease first"
        assert caught is expected_error
        if expected_error is None:
            assert (outcome[0], outcome[1].result_commit) == (published["result_ref"], published["result_commit"])
    section = (Path(__file__).resolve().parents[1] / "docs/protocol.md").read_text().split("### Public artifact publication", 1)[1].split("### ", 1)[0]
    for phrase in ("publication timeout remaining", "renews the lease immediately before publishing",
                   "public_publication_attempt_unavailable", "retry once"):
        assert phrase in section, phrase
