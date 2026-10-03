"""Parallel assignment review for the reference checker (card ac0967ba).

Child reviews are fakes: they sleep, raise, or exit. Overlap is measured with
timestamps and an exclusive in-flight file, not a wall-clock cutoff.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Callable

import pytest

from ormas_subnet.skeleton import GitError
from ormas_subnet.validator import (
    OrmasValidatorClient,
    ValidatorConfig,
    ValidatorDaemon,
    canonical_evidence_fields,
    evidence_digest_hex,
)
from tests.test_skeleton import _FakeResponse


def parallel_fake_review(workdir_root: str, assignment: dict[str, Any]) -> str:
    """Picklable stand-in for clone + decide. Behavior is selected by env."""
    del workdir_root
    root = Path(os.environ["VALIDATOR_PARALLEL_REVIEW_ROOT"])
    aid = str(assignment["assignment_id"])
    attempt_path = root / f"attempts-{aid}"
    fd = os.open(attempt_path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o644)
    try:
        os.write(fd, b"x")
    finally:
        os.close(fd)
    mode = os.environ.get("VALIDATOR_PARALLEL_MODE", "overlap")
    if mode == "kill" and aid == os.environ.get("VALIDATOR_PARALLEL_KILL_ID"):
        if attempt_path.stat().st_size == 1:
            os._exit(1)
        return "accept"
    if mode == "giterror" and aid == os.environ.get("VALIDATOR_PARALLEL_GITERROR_ID"):
        raise GitError("repository clone failed")
    if mode == "starve":
        crash_ids = set(filter(None, os.environ.get("VALIDATOR_PARALLEL_CRASH_IDS", "").split(",")))
        if aid in crash_ids:
            os._exit(1)
        return "accept"
    if mode == "wait-list":
        deadline = time.time() + 8
        while time.time() < deadline and not (root / "listed-again").exists():
            time.sleep(0.01)
        return "accept"
    if mode == "hold-peer":
        if aid == os.environ.get("VALIDATOR_PARALLEL_HOLD_ID"):
            deadline = time.time() + 8
            while time.time() < deadline and not (root / "peer-posted").exists():
                time.sleep(0.01)
        return "accept"
    if mode == "overlap":
        entry = root / f"entered-{aid}"
        try:
            entered = os.open(entry, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            (root / "double").write_text(aid)
            return str(assignment.get("fake_decision") or "accept")
        else:
            os.write(entered, str(time.time()).encode())
            os.close(entered)
        expect = int(os.environ.get("VALIDATOR_PARALLEL_EXPECT", "3"))
        # Hang fuse only. The pass condition is timestamp overlap, not this bound.
        deadline = time.time() + 8
        while time.time() < deadline:
            if len(list(root.glob("entered-*"))) >= expect or (root / "release").exists():
                (root / "release").write_text("1")
                break
            time.sleep(0.01)
        (root / f"left-{aid}").write_text(str(time.time()))
    return str(assignment.get("fake_decision") or "accept")


class _MultiGateway:
    """Validator routes for several assignments, same shape as ``_FakeValidatorGateway``."""

    def __init__(self, assignments: list[dict[str, Any]]) -> None:
        self._pending = [dict(item) for item in assignments]
        self.decisions: list[dict[str, Any]] = []

    def post(self, path: str, json: dict[str, Any] | None = None, headers: Any = None) -> _FakeResponse:
        del headers
        body = json or {}
        if path == "/api/validator/v1/registrations":
            return _FakeResponse(200, {"validator_id": "val_test"})
        if path.endswith("/decisions"):
            assignment_id = path.rstrip("/").split("/")[-2]
            self.decisions.append({"assignment_id": assignment_id, **body})
            self._pending = [item for item in self._pending if item.get("assignment_id") != assignment_id]
            return _FakeResponse(200, {"decision": body.get("decision")})
        raise AssertionError(f"unhandled path: {path}")

    def get(self, path: str) -> _FakeResponse:
        if path == "/api/validator/v1/assignments":
            return _FakeResponse(200, {"assignments": [dict(item) for item in self._pending]})
        raise AssertionError(f"unexpected GET: {path}")


def _assignment(assignment_id: str, *, repo: Path, decision: str) -> dict[str, Any]:
    fields = canonical_evidence_fields(
        job_id=f"job_{assignment_id}",
        miner_id="miner:other-tenant",
        base_commit="a" * 40,
        result_commit="b" * 40,
        repo_url=str(repo),
        verify_command="true",
        allowed_paths=["out.txt"],
        immutable_paths=[],
    )
    return {
        "assignment_id": assignment_id,
        **fields,
        "evidence_digest_sha256": evidence_digest_hex(fields),
        "fake_decision": decision,
    }


def _mismatch(assignment_id: str, *, repo: Path) -> dict[str, Any]:
    assignment = _assignment(assignment_id, repo=repo, decision="accept")
    assignment["evidence_digest_sha256"] = "0" * 64
    return assignment


def _drive(daemon: ValidatorDaemon, *, slots: int, poll_interval_s: float, until: Callable[[], bool]) -> None:
    serve = getattr(daemon, "serve", None)
    if serve is not None:
        serve(slots, poll_interval_s, until=until)
        return
    while True:
        did = daemon.run_once()
        if until():
            return
        if not did:
            return


def _daemon(tmp_path: Path, gateway: _MultiGateway, sign_log: list[dict[str, Any]]) -> ValidatorDaemon:
    client = OrmasValidatorClient(base_url="https://fake.invalid", token="ormv_test", http_client=gateway)

    def sign_fn(digest: str) -> str:
        sign_log.append({"pid": os.getpid(), "digest": digest})
        return "ab" * 64

    return ValidatorDaemon(client, ValidatorConfig(workdir_root=tmp_path / "validator-work"), sign_fn)


def _until_decisions(gateway: _MultiGateway, want: dict[str, str]):
    started = time.time()

    def until() -> bool:
        by_id = {row["assignment_id"]: row["decision"] for row in gateway.decisions}
        if all(by_id.get(key) == value for key, value in want.items()):
            return True
        # Wrong terminal decisions are a failure, not a reason to wait out the fuse.
        if set(want) <= set(by_id) and time.time() - started > 0.5:
            return True
        return time.time() - started > 20

    return until


def _assert_signed_only_in_parent(sign_log: list[dict[str, Any]], decisions: list[dict[str, Any]]) -> None:
    assert sign_log, "signer was never called"
    assert [row["pid"] for row in sign_log] == [os.getpid()] * len(decisions)
    assert len(sign_log) == len(decisions)


def _sequence(tmp_path: Path, *, slots: int | None) -> tuple[list[str], list[dict[str, Any]], _MultiGateway]:
    gateway = _MultiGateway([_mismatch("asgn_seq", repo=tmp_path / "missing.git")])
    events: list[str] = []
    sign_log: list[dict[str, Any]] = []
    daemon = _daemon(tmp_path, gateway, sign_log)
    client = daemon.client
    orig_list = client.list_assignments
    orig_post = client.post_decision

    def list_assignments() -> list[dict[str, Any]]:
        events.append("list_assignments")
        return orig_list()

    def post_decision(assignment_id: str, *, decision: str, signature_hex: str) -> dict[str, Any]:
        events.append("post_decision")
        return orig_post(assignment_id, decision=decision, signature_hex=signature_hex)

    client.list_assignments = list_assignments  # type: ignore[method-assign]
    client.post_decision = post_decision  # type: ignore[method-assign]
    real_sign = daemon.sign_fn

    def sign_fn(digest: str) -> str:
        events.append("sign")
        return real_sign(digest)

    daemon.sign_fn = sign_fn
    if slots is None:
        assert daemon.run_once() is True
    else:
        _drive(daemon, slots=slots, poll_interval_s=0.05, until=lambda: len(gateway.decisions) >= 1)
    return events, sign_log, gateway


def test_slots_one_keeps_todays_call_sequence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import multiprocessing

    contexts: list[Any] = []
    real_context = multiprocessing.get_context

    def spy_context(*args: Any, **kwargs: Any):
        contexts.append(args[0] if args else kwargs.get("method"))
        return real_context(*args, **kwargs)

    monkeypatch.setattr(multiprocessing, "get_context", spy_context)
    via_slots, sign_log, gateway = _sequence(tmp_path / "slots", slots=1)
    direct, direct_log, direct_gateway = _sequence(tmp_path / "direct", slots=None)
    assert via_slots == ["list_assignments", "sign", "post_decision"]
    assert direct == via_slots
    assert [row["decision"] for row in gateway.decisions] == [row["decision"] for row in direct_gateway.decisions]
    assert contexts == []
    _assert_signed_only_in_parent(sign_log, gateway.decisions)
    _assert_signed_only_in_parent(direct_log, direct_gateway.decisions)


def test_three_assignments_are_in_review_together_and_never_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ormas_subnet import validator

    root = tmp_path / "review"
    root.mkdir()
    monkeypatch.setenv("VALIDATOR_PARALLEL_REVIEW_ROOT", str(root))
    monkeypatch.setenv("VALIDATOR_PARALLEL_MODE", "overlap")
    monkeypatch.setenv("VALIDATOR_PARALLEL_EXPECT", "3")
    monkeypatch.setattr(validator, "review_assignment", parallel_fake_review, raising=False)
    repo = tmp_path / "missing.git"
    ids = ["asgn_a", "asgn_b", "asgn_c"]
    gateway = _MultiGateway([_assignment(aid, repo=repo, decision="accept") for aid in ids])
    sign_log: list[dict[str, Any]] = []
    daemon = _daemon(tmp_path, gateway, sign_log)
    _drive(
        daemon, slots=3, poll_interval_s=0.05,
        until=_until_decisions(gateway, {aid: "accept" for aid in ids}),
    )
    by_id = {row["assignment_id"]: row["decision"] for row in gateway.decisions}
    assert by_id == {aid: "accept" for aid in ids}
    enters = {aid: float((root / f"entered-{aid}").read_text()) for aid in ids}
    leaves = {aid: float((root / f"left-{aid}").read_text()) for aid in ids}
    assert max(enters.values()) < min(leaves.values())
    assert not (root / "double").exists()
    _assert_signed_only_in_parent(sign_log, gateway.decisions)


def test_giterror_in_one_child_posts_error_and_siblings_keep_their_decisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ormas_subnet import validator

    root = tmp_path / "review"
    root.mkdir()
    monkeypatch.setenv("VALIDATOR_PARALLEL_REVIEW_ROOT", str(root))
    monkeypatch.setenv("VALIDATOR_PARALLEL_MODE", "giterror")
    monkeypatch.setenv("VALIDATOR_PARALLEL_GITERROR_ID", "asgn_bad")
    monkeypatch.setattr(validator, "review_assignment", parallel_fake_review, raising=False)
    repo = tmp_path / "missing.git"
    gateway = _MultiGateway([
        _assignment("asgn_bad", repo=repo, decision="accept"),
        _assignment("asgn_ok", repo=repo, decision="accept"),
        _assignment("asgn_no", repo=repo, decision="reject"),
    ])
    sign_log: list[dict[str, Any]] = []
    daemon = _daemon(tmp_path, gateway, sign_log)
    want = {"asgn_bad": "error", "asgn_ok": "accept", "asgn_no": "reject"}
    _drive(daemon, slots=3, poll_interval_s=0.05, until=_until_decisions(gateway, want))
    by_id = {row["assignment_id"]: row["decision"] for row in gateway.decisions}
    assert by_id == want
    _assert_signed_only_in_parent(sign_log, gateway.decisions)


def test_killed_child_posts_nothing_and_is_redispatched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ormas_subnet import validator

    root = tmp_path / "review"
    root.mkdir()
    monkeypatch.setenv("VALIDATOR_PARALLEL_REVIEW_ROOT", str(root))
    monkeypatch.setenv("VALIDATOR_PARALLEL_MODE", "kill")
    monkeypatch.setenv("VALIDATOR_PARALLEL_KILL_ID", "asgn_kill")
    monkeypatch.setattr(validator, "review_assignment", parallel_fake_review, raising=False)
    repo = tmp_path / "missing.git"
    gateway = _MultiGateway([
        _assignment("asgn_kill", repo=repo, decision="accept"),
        _assignment("asgn_ok", repo=repo, decision="accept"),
    ])
    sign_log: list[dict[str, Any]] = []
    daemon = _daemon(tmp_path, gateway, sign_log)
    want = {"asgn_kill": "accept", "asgn_ok": "accept"}
    _drive(daemon, slots=3, poll_interval_s=0.05, until=_until_decisions(gateway, want))
    by_id = {row["assignment_id"]: row["decision"] for row in gateway.decisions}
    assert by_id == want
    assert (root / "attempts-asgn_kill").stat().st_size == 2
    assert not any(row["decision"] == "error" for row in gateway.decisions)
    _assert_signed_only_in_parent(sign_log, gateway.decisions)


class _FlakyPostGateway(_MultiGateway):
    """First post for one assignment fails; later posts succeed."""

    def __init__(self, assignments: list[dict[str, Any]], *, root: Path, fail_id: str) -> None:
        super().__init__(assignments)
        self._root = root
        self._fail_id = fail_id
        self._fails_left = 1

    def post(self, path: str, json: dict[str, Any] | None = None, headers: Any = None) -> _FakeResponse:
        body = json or {}
        if path.endswith("/decisions"):
            assignment_id = path.rstrip("/").split("/")[-2]
            if assignment_id == self._fail_id and self._fails_left:
                self._fails_left -= 1
                raise RuntimeError("post failed")
            if assignment_id == self._fail_id:
                (self._root / "peer-posted").write_text("1")
        return super().post(path, json=body, headers=headers)


class _FlakyListGateway(_MultiGateway):
    """The first list works. Later lists raise after marking that they ran."""

    def __init__(self, assignments: list[dict[str, Any]], *, root: Path) -> None:
        super().__init__(assignments)
        self._root = root
        self.lists = 0

    def get(self, path: str) -> _FakeResponse:
        if path == "/api/validator/v1/assignments":
            self.lists += 1
            if self.lists > 1:
                (self._root / "listed-again").write_text(str(self.lists))
                raise RuntimeError("list failed")
        return super().get(path)


def _attempts(root: Path, assignment_id: str) -> int:
    path = root / f"attempts-{assignment_id}"
    if not path.exists():
        return 0
    return path.stat().st_size


def test_post_failure_retries_without_dropping_the_other_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    from ormas_subnet import validator

    root = tmp_path / "review"
    root.mkdir()
    monkeypatch.setenv("VALIDATOR_PARALLEL_REVIEW_ROOT", str(root))
    monkeypatch.setenv("VALIDATOR_PARALLEL_MODE", "hold-peer")
    monkeypatch.setenv("VALIDATOR_PARALLEL_HOLD_ID", "asgn_ok")
    monkeypatch.setattr(validator, "review_assignment", parallel_fake_review, raising=False)
    repo = tmp_path / "missing.git"
    fail = _assignment("asgn_fail", repo=repo, decision="accept")
    hold = _assignment("asgn_ok", repo=repo, decision="accept")
    gateway = _FlakyPostGateway([fail, hold], root=root, fail_id="asgn_fail")
    sign_log: list[dict[str, Any]] = []
    daemon = _daemon(tmp_path, gateway, sign_log)
    want = {"asgn_fail": "accept", "asgn_ok": "accept"}
    caught: Exception | None = None
    with caplog.at_level(logging.WARNING):
        try:
            _drive(daemon, slots=2, poll_interval_s=0.05, until=_until_decisions(gateway, want))
        except Exception as exc:
            caught = exc
    by_id = {row["assignment_id"]: row["decision"] for row in gateway.decisions}
    assert by_id == want
    assert caught is None
    assert _attempts(root, "asgn_fail") == 1
    assert _attempts(root, "asgn_ok") == 1
    fail_signs = [row for row in sign_log if row["digest"] == fail["evidence_digest_sha256"]]
    assert len(fail_signs) == 1
    assert fail_signs[0]["pid"] == os.getpid()
    assert "ormv_test" not in caplog.text
    assert "post_decision" in caplog.text and "RuntimeError" in caplog.text


def test_list_failure_leaves_in_flight_children_to_finish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    from ormas_subnet import validator

    root = tmp_path / "review"
    root.mkdir()
    monkeypatch.setenv("VALIDATOR_PARALLEL_REVIEW_ROOT", str(root))
    monkeypatch.setenv("VALIDATOR_PARALLEL_MODE", "wait-list")
    monkeypatch.setattr(validator, "review_assignment", parallel_fake_review, raising=False)
    repo = tmp_path / "missing.git"
    gateway = _FlakyListGateway(
        [
            _assignment("asgn_a", repo=repo, decision="accept"),
            _assignment("asgn_b", repo=repo, decision="accept"),
        ],
        root=root,
    )
    sign_log: list[dict[str, Any]] = []
    daemon = _daemon(tmp_path, gateway, sign_log)
    want = {"asgn_a": "accept", "asgn_b": "accept"}
    caught: Exception | None = None
    with caplog.at_level(logging.WARNING):
        try:
            _drive(daemon, slots=2, poll_interval_s=0.05, until=_until_decisions(gateway, want))
        except Exception as exc:
            caught = exc
    by_id = {row["assignment_id"]: row["decision"] for row in gateway.decisions}
    assert by_id == want
    assert caught is None
    assert "ormv_test" not in caplog.text
    assert "list_assignments" in caplog.text and "RuntimeError" in caplog.text
    _assert_signed_only_in_parent(sign_log, gateway.decisions)


def test_crashing_assignments_do_not_starve_a_healthy_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ormas_subnet import validator

    root = tmp_path / "review"
    root.mkdir()
    monkeypatch.setenv("VALIDATOR_PARALLEL_REVIEW_ROOT", str(root))
    monkeypatch.setenv("VALIDATOR_PARALLEL_MODE", "starve")
    monkeypatch.setenv("VALIDATOR_PARALLEL_CRASH_IDS", "asgn_crash_a,asgn_crash_b")
    monkeypatch.setattr(validator, "review_assignment", parallel_fake_review, raising=False)
    repo = tmp_path / "missing.git"
    gateway = _MultiGateway([
        _assignment("asgn_crash_a", repo=repo, decision="accept"),
        _assignment("asgn_crash_b", repo=repo, decision="accept"),
        _assignment("asgn_ok", repo=repo, decision="accept"),
    ])
    sign_log: list[dict[str, Any]] = []
    daemon = _daemon(tmp_path, gateway, sign_log)

    def until() -> bool:
        if any(row["assignment_id"] == "asgn_ok" and row["decision"] == "accept" for row in gateway.decisions):
            return True
        crashes = _attempts(root, "asgn_crash_a") + _attempts(root, "asgn_crash_b")
        return crashes >= 6

    caught: Exception | None = None
    try:
        _drive(daemon, slots=2, poll_interval_s=0.05, until=until)
    except Exception as exc:
        caught = exc
    posted = {row["assignment_id"]: row["decision"] for row in gateway.decisions}
    assert posted.get("asgn_ok") == "accept"
    assert caught is None
    assert _attempts(root, "asgn_ok") == 1
    assert _attempts(root, "asgn_crash_a") + _attempts(root, "asgn_crash_b") <= 4
    _assert_signed_only_in_parent(sign_log, gateway.decisions)
