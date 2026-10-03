"""Private write-ahead state for one public miner's current lease.

This is a recovery journal, not a job queue. Only the gateway issues work. A
miner must resolve its current record before claiming another lease.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile


class RecoveryRequired(RuntimeError):
    """Retain evidence and refuse new work when recovery cannot be proved."""


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
            if (envelope["sha256"] != hashlib.sha256(_encoded(body)).hexdigest()
                    or body["version"] != 1 or body["gateway"] != self.namespace
                    or body["runner_id"] != self.runner_id
                    or body["stage"] not in {"claimed", "artifact", "completion"}
                    or any(not isinstance(body[key], str) or not body[key]
                           for key in ("task_id", "lease_id", "packet_hash", "base_commit"))):
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
        if self.pending() is not None:
            raise RecoveryRequired("public recovery must finish before a new claim")
        previous = self._read(self._history(draft.task_id, lease.lease_id))
        if previous is not None:
            if previous["packet_hash"] != draft.work_packet_sha256:
                raise RecoveryRequired("repeated public lease changed its packet")
            raise RecoveryRequired("gateway re-served an acknowledged public lease")
        body = {"version": 1, "gateway": self.namespace, "runner_id": self.runner_id,
                "task_id": draft.task_id, "lease_id": lease.lease_id,
                "packet_hash": draft.work_packet_sha256, "base_commit": draft.base_commit,
                "repo_url": draft.repo_url, "stage": "claimed"}
        if getattr(lease, "offer_kind", None) == "limit":
            body["lease"] = lease.to_wire()
        self.save(body)
        return body

    def checkpoint_artifact(self, build, *, result, capture):
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
        body.update(stage="completion", completion=kwargs)
        self.save(body)
        return body

    def acknowledge(self):
        body = self.pending()
        if body is None:
            raise RecoveryRequired("public recovery acknowledgement has no record")
        os.replace(self.path, self._history(body["task_id"], body["lease_id"]))
        _sync_directory(self.root)
