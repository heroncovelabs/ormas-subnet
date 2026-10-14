"""Resolve privileged helper programs without trusting the service environment."""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path


class TrustedExecutableUnavailable(RuntimeError):
    """No executable with a service-immutable path is available."""


# Deliberately exclude inherited PATH and user package-manager locations.  The
# final entry supports the root-owned /usr/local layout used by BSD systems and
# some operator-built Linux images; the ownership checks below reject it when
# the service account can modify it.
_SYSTEM_EXECUTABLE_DIRS = (Path("/usr/bin"), Path("/bin"), Path("/usr/local/bin"))
_TOOL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*")


def _lineage(path: Path):
    current = path
    while True:
        yield current
        if current.parent == current:
            return
        current = current.parent


def _mode_writable_by_service(info: os.stat_result) -> bool:
    uid = os.geteuid()
    groups = {os.getegid(), *os.getgroups()}
    if info.st_uid == uid:
        return bool(info.st_mode & stat.S_IWUSR)
    if info.st_gid in groups:
        return bool(info.st_mode & stat.S_IWGRP)
    return bool(info.st_mode & stat.S_IWOTH)


def _service_can_replace(path: Path) -> bool:
    """Best-effort check for mode, group, other, and ACL write authority."""
    try:
        info = path.stat()
    except OSError:
        return True
    if _mode_writable_by_service(info):
        return True
    try:
        return os.access(path, os.W_OK, effective_ids=True)
    except (NotImplementedError, TypeError):
        return os.access(path, os.W_OK)


def trusted_executable(name: str) -> Path:
    """Return an absolute, regular executable outside service-writable paths.

    Resolution uses only the fixed system search path above.  Both the lexical
    directory and a symlink's resolved target lineage must be immutable to the
    current service credentials, so a writable parent cannot swap the program
    between checks.  Unsupported layouts fail closed rather than falling back
    to inherited ``PATH``.
    """
    if not isinstance(name, str) or _TOOL_NAME.fullmatch(name) is None:
        raise TrustedExecutableUnavailable("trusted executable unavailable")

    for directory in _SYSTEM_EXECUTABLE_DIRS:
        candidate = directory / name
        try:
            resolved = candidate.resolve(strict=True)
            info = resolved.stat()
        except OSError:
            continue
        if not stat.S_ISREG(info.st_mode) or not os.access(resolved, os.X_OK):
            continue

        # A symlink is replaceable through its lexical parent; its target is
        # replaceable through any writable target ancestor or the file itself.
        protected = (*_lineage(candidate.parent), *_lineage(resolved))
        if any(_service_can_replace(path) for path in protected):
            continue
        return resolved

    raise TrustedExecutableUnavailable(f"trusted {name} executable unavailable")
