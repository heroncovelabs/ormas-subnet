"""Private write-ahead state for one public miner's current lease.

This is a recovery journal, not a job queue. Only the gateway issues work. A
miner must resolve its current record before claiming another lease.
"""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import tempfile

from .protocol import require_task_id


class RecoveryRequired(RuntimeError):
    """Retain evidence and refuse new work when recovery cannot be proved."""


_COMPLETION_CAPTURE_VERSION = 2
_LEGACY_FAILURE_CAPTURE_KEYS = frozenset({
    "status",
    "scope_ok",
    "file_count",
    "changed_paths",
    "wall_s",
    "failure_class",
    "error",
})
_LEGACY_PUBLIC_FAILURE_STATES = {
    "public_execution_interrupted": "aborted",
    "base_preflight_non_red": "failed",
}


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _private_directory(path):
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise RecoveryRequired("public recovery directory is unsafe")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def _sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class PublicRecovery:
    """Atomic, owner-private state bound to gateway, runner, task and lease."""

    def __init__(self, workdir_root, namespace, runner_id):
        if not namespace or not runner_id:
            raise RecoveryRequired("public recovery requires gateway and runner identity")
        self.namespace, self.runner_id = namespace, runner_id
        parent = Path(workdir_root) / ".ormas-recovery"
        _private_directory(parent)
        self.root = parent / hashlib.sha256(_encoded([namespace, runner_id])).hexdigest()
        _private_directory(self.root)
        self.path = self.root / "pending.json"
        self.artifact = self.root / "artifact.bin"

    @contextmanager
    def lock(self):
        import fcntl
        fd = os.open(self.root / "lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise RecoveryRequired("public recovery lock is unsafe")
            os.fchmod(fd, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RecoveryRequired("another miner owns this recovery journal") from None
            yield
        finally:
            os.close(fd)

    def _read(self, path):
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        try:
            with os.fdopen(fd, "rb") as source:
                metadata = os.fstat(source.fileno())
                if (not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077
                        or metadata.st_uid != os.getuid() or metadata.st_size > 1_048_576):
                    raise RecoveryRequired("public recovery record is unsafe")
                envelope = json.loads(source.read(1_048_577))
            body = envelope["record"]
            capture_version = (
                body.get("completion_capture_version") if isinstance(body, dict) else None
            )
            if (not isinstance(body, dict)
                    or envelope["sha256"] != hashlib.sha256(_encoded(body)).hexdigest()
                    or body["version"] not in {1, 2} or body["gateway"] != self.namespace
                    or body["runner_id"] != self.runner_id
                    or (capture_version is not None and (
                        type(capture_version) is not int
                        or capture_version != _COMPLETION_CAPTURE_VERSION
                    ))
                    or body["stage"] not in {"claimed", "artifact", "completion"}
                    or any(not isinstance(body[key], str) or not body[key]
                           for key in ("task_id", "lease_id", "packet_hash", "base_commit"))):
                raise RecoveryRequired("public recovery identity mismatch")
            try:
                require_task_id(body["task_id"])
            except ValueError:
                raise RecoveryRequired("public recovery identity mismatch") from None
            lease = body.get("lease")
            if lease is not None and (
                not isinstance(lease, Mapping) or lease.get("task_id") != body["task_id"]
            ):
                raise RecoveryRequired("public recovery identity mismatch")
            return body
        except (ValueError, KeyError, TypeError):
            raise RecoveryRequired("public recovery record is corrupt") from None

    def pending(self):
        return self._read(self.path)

    def save(self, body):
        raw = _encoded({"record": body, "sha256": hashlib.sha256(_encoded(body)).hexdigest()})
        if len(raw) > 1_048_576:
            raise RecoveryRequired("public recovery record exceeds its bound")
        fd, name = tempfile.mkstemp(prefix="state-", dir=self.root)
        try:
            with os.fdopen(fd, "wb") as target:
                target.write(raw)
                target.flush()
                os.fsync(target.fileno())
            os.replace(name, self.path)
            _sync_directory(self.root)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def _history(self, task_id, lease_id):
        return self.root / (hashlib.sha256(_encoded([task_id, lease_id])).hexdigest() + ".json")

    def begin(self, lease, draft):
        try:
            draft_task_id = require_task_id(draft.task_id)
        except (AttributeError, ValueError):
            raise RecoveryRequired("public recovery identity mismatch") from None
        # Version-1 recovery callers predate TaskLease.task_id. The live skeleton
        # validates every decoded lease before this call; when an id is present
        # here, bind it again before writing the durable journal.
        lease_task_id = getattr(lease, "task_id", None)
        if lease_task_id is not None:
            try:
                lease_task_id = require_task_id(lease_task_id)
            except ValueError:
                raise RecoveryRequired("public recovery identity mismatch") from None
            if lease_task_id != draft_task_id:
                raise RecoveryRequired("public recovery identity mismatch")
        if self.pending() is not None:
            raise RecoveryRequired("public recovery must finish before a new claim")
        previous = self._read(self._history(draft.task_id, lease.lease_id))
        if previous is not None:
            if previous["packet_hash"] != draft.work_packet_sha256:
                raise RecoveryRequired("repeated public lease changed its packet")
            raise RecoveryRequired("gateway re-served an acknowledged public lease")
        # Version 2 records are created only by miners whose artifact builder
        # independently enforces the frozen allowed/immutable path policy.
        body = {"version": 2, "gateway": self.namespace, "runner_id": self.runner_id,
                "task_id": draft.task_id, "lease_id": lease.lease_id,
                "packet_hash": draft.work_packet_sha256, "base_commit": draft.base_commit,
                "repo_url": draft.repo_url, "stage": "claimed",
                "completion_capture_version": _COMPLETION_CAPTURE_VERSION}
        if getattr(lease, "offer_kind", None) == "limit":
            body["lease"] = lease.to_wire()
        self.save(body)
        return body

    def checkpoint_artifact(self, build, *, result, capture):
        if not isinstance(capture, Mapping) or capture.get("scope_ok") is not True:
            raise RecoveryRequired("public artifact requires passing frozen scope")
        body = self.pending()
        if body is None or body["stage"] != "claimed":
            raise RecoveryRequired("public artifact checkpoint has no claimed lease")
        fd, name = tempfile.mkstemp(prefix="artifact-", dir=self.root)
        try:
            with os.fdopen(fd, "w+b") as target:
                length, digest, tree_sha = build(target)
                target.flush()
                os.fsync(target.fileno())
            os.replace(name, self.artifact)
            _sync_directory(self.root)
            body.update(stage="artifact", result=result, capture=capture,
                        artifact_length=length, artifact_sha256=digest, tree_sha=tree_sha)
            self.save(body)
        finally:
            if os.path.exists(name):
                os.unlink(name)
        return body

    @contextmanager
    def artifact_source(self, body):
        fd = os.open(self.artifact, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as source:
            metadata = os.fstat(source.fileno())
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077
                    or metadata.st_uid != os.getuid()
                    or metadata.st_size != body["artifact_length"]
                    or not 0 < metadata.st_size <= 65 * 1024 * 1024 + 8):
                raise RecoveryRequired("public recovery artifact is unsafe")
            digest = hashlib.sha256()
            for chunk in iter(lambda: source.read(65536), b""):
                digest.update(chunk)
            if digest.hexdigest() != body["artifact_sha256"]:
                raise RecoveryRequired("public recovery artifact changed")
            source.seek(0)
            yield source

    def freeze(self, *, receipt, terminal, capture):
        body = self.pending()
        if body is None:
            raise RecoveryRequired("public completion has no claimed lease")
        kwargs = {"receipt": receipt.to_wire(), "terminal": terminal.to_wire(), "capture": capture}
        if body["stage"] == "completion":
            if _encoded(body["completion"]) != _encoded(kwargs):
                raise RecoveryRequired("ambiguous public completion cannot be replaced")
            return body
        if body.get("completion_capture_version") is None and "error" not in capture:
            body["completion_capture_version"] = _COMPLETION_CAPTURE_VERSION
        body.update(stage="completion", completion=kwargs)
        self.save(body)
        return body

    def completion_for_wire(self, body=None):
        """Return a completion body safe for replay without rewriting its journal.

        Releases before ``completion_capture_version=2`` persisted the local
        exception string as ``capture.error`` even though the gateway has never
        accepted that field.  Only the exact failure shape emitted by those
        releases may be projected: the exception is omitted from the derived
        request while the checksummed journal bytes remain untouched.  Other
        unknown capture shapes stay fail-closed for operator inspection.
        """
        body = self.pending() if body is None else body
        if body is None or body.get("stage") != "completion":
            raise RecoveryRequired("public completion recovery record is missing")
        completion = body.get("completion")
        if not isinstance(completion, dict):
            raise RecoveryRequired("public completion recovery record is corrupt")
        capture = completion.get("capture")
        if body.get("completion_capture_version") == _COMPLETION_CAPTURE_VERSION:
            if isinstance(capture, dict) and "error" in capture:
                raise RecoveryRequired("versioned public capture contains local diagnostics")
            return completion
        if not isinstance(capture, dict) or "error" not in capture:
            if body.get("version") == 1:
                raise RecoveryRequired(
                    "legacy public recovery lacks frozen publication scope authorization"
                )
            return completion
        terminal = completion.get("terminal")
        failure_class = capture.get("failure_class")
        wall_s = capture.get("wall_s")
        expected_state = _LEGACY_PUBLIC_FAILURE_STATES.get(failure_class)
        if (
            set(capture) != _LEGACY_FAILURE_CAPTURE_KEYS
            or capture.get("status") != "failed"
            or capture.get("scope_ok") is not False
            or type(capture.get("file_count")) is not int
            or capture.get("file_count") != 0
            or capture.get("changed_paths") != []
            or not isinstance(wall_s, (int, float))
            or isinstance(wall_s, bool)
            or not math.isfinite(float(wall_s))
            or float(wall_s) < 0
            or not isinstance(capture.get("error"), str)
            or len(capture["error"]) > 500
            or expected_state is None
            or not isinstance(terminal, dict)
            or terminal.get("lease_id") != body.get("lease_id")
            or terminal.get("verification_state") != expected_state
            or terminal.get("result_ref") is not None
            or terminal.get("result_commit") != ""
        ):
            raise RecoveryRequired("legacy public failure capture is not canonical")
        projected = dict(completion)
        projected["capture"] = {key: value for key, value in capture.items() if key != "error"}
        return projected

    def acknowledge(self):
        body = self.pending()
        if body is None:
            raise RecoveryRequired("public recovery acknowledgement has no record")
        os.replace(self.path, self._history(body["task_id"], body["lease_id"]))
        _sync_directory(self.root)
