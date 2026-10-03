"""Private caller records for opt-in retained verifier evidence.

This standard-library file is mirrored byte-for-byte in the independently
distributed public reference package. Neither package imports the other.
It records execution, never acceptance, billing, or a second job queue.
"""
from contextlib import contextmanager
try:
    import fcntl
except ImportError:
    fcntl = None
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import uuid

ROOT_ENV = "ORMAS_REVIEW_ROOT"
CALL_ENV = "ORMAS_REVIEW_CALL"
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def private_directory(path):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("evidence directory must be absolute")
    fd = os.open("/", DIR_FLAGS)
    try:
        for part in path.parts[1:]:
            child = os.open(part, DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError("evidence directory must be owned and private")
        return fd
    except BaseException:
        os.close(fd)
        raise


def write_json(fd, name, data):
    raw = (json.dumps(data, sort_keys=True) + "\n").encode()
    out = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                  0o600, dir_fd=fd)
    with os.fdopen(out, "wb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def evidence_root(command):
    values = {}
    for token in shlex.split(command):
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", token):
            break
        name, value = token.split("=", 1)
        if name in (ROOT_ENV, CALL_ENV):
            if name in values:
                raise ValueError("ambiguous evidence context")
            values[name] = value
    if CALL_ENV in values:
        raise ValueError("caller context cannot be supplied by the packet")
    return values.get(ROOT_ENV)


@contextmanager
def verification_call(command, env, *, cwd, identity):
    """Register before launch and close afterward, even when stdout is discarded.

    The command opts in with ROOT_ENV. Identity comes only from the enclosing
    job/attempt or validator assignment; an absent identity refuses before launch.
    A crash leaves an open caller record, explicitly unproved to the observer.
    """
    child_env = dict(env)
    child_env.pop(CALL_ENV, None)
    child_env.pop(ROOT_ENV, None)
    root = evidence_root(command)
    if root is None:
        yield child_env
        return
    if fcntl is None:
        raise ValueError("retained evidence requires file locking")
    doc = dict(identity or {})
    argv = shlex.split(command)
    while argv and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", argv[0]):
        argv.pop(0)
    if len(argv) < 3 or argv[1] != "-c":
        raise ValueError("retained evidence requires the embedded Python adapter")
    doc["adapter_arguments_sha256"] = hashlib.sha256(
        json.dumps(argv[3:], separators=(",", ":")).encode()).hexdigest()
    if not isinstance(doc.get("job_id"), str) or not doc["job_id"]:
        raise ValueError("missing execution job identity")
    if doc.get("role") in ("worker", "miner_verifier"):
        if re.fullmatch(r"[0-9a-f]{32}", str(doc.get("execution_run_id", ""))) is None:
            raise ValueError("missing execution run identity")
        if type(doc.get("attempt")) is not int or doc["attempt"] < 1:
            raise ValueError("missing worker attempt")
        if re.fullmatch(r"[0-9a-f]{64}", str(doc.get("work_packet_sha256", ""))) is None:
            raise ValueError("missing execution packet digest")
    elif doc.get("role") == "validator":
        if not doc.get("assignment_id") or doc.get("phase") not in ("base", "result"):
            raise ValueError("missing validator assignment")
        if re.fullmatch(r"[0-9a-f]{64}", str(doc.get("evidence_digest_sha256", ""))) is None:
            raise ValueError("missing validator evidence digest")
    else:
        raise ValueError("unknown evidence caller")
    root_path = Path(root)
    checkout = Path(cwd).resolve()
    if root_path == checkout or checkout in root_path.parents:
        raise ValueError("evidence must be outside the checkout")
    parent = private_directory(root_path)
    call_id = "call-" + uuid.uuid4().hex
    try:
        os.mkdir(call_id, 0o700, dir_fd=parent)
        fd = os.open(call_id, DIR_FLAGS, dir_fd=parent)
    finally:
        os.close(parent)
    lock = None
    try:
        os.fchmod(fd, 0o700)
        lock = os.open("lock", os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                       0o600, dir_fd=fd)
        os.fchmod(lock, 0o600)
        doc.update(schema="ormas-verifier-caller-v1", call_id=call_id,
                   command_sha256=hashlib.sha256(command.encode()).hexdigest())
        write_json(fd, "caller.json", doc)
        child_env[ROOT_ENV] = root
        child_env[CALL_ENV] = str(root_path / call_id)
        try:
            yield child_env
        finally:
            # Nonblocking: a detached/in-flight verifier is not a closed call.
            # It stays open and unproved; never wait indefinitely or kill it here.
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                names = sorted(n for n in os.listdir(fd) if n.startswith("invocation-"))
                write_json(fd, "closed.json", {"schema": "ormas-verifier-closed-v1",
                                               "call_id": call_id, "invocations": names})
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
    finally:
        if lock is not None:
            os.close(lock)
        os.close(fd)
