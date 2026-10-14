"""Descriptor-based reads for operator-provided credential files."""
from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Union

PathValue = Union[str, os.PathLike[str]]
_PRIVATE_FILE_MODES = frozenset((0o400, 0o600))


def _open_no_symlinks(path: Path, flags: int, *, dir_fd: int | None) -> int:
    """Open ``path`` while refusing symlinks in every traversed component.

    A relative path is anchored at ``dir_fd`` when supplied, otherwise at the
    process working directory.  The anchor itself is trusted by the caller.
    """
    parts = path.parts
    absolute = path.is_absolute()
    if absolute:
        parts = parts[1:]
    if not parts:
        raise OSError("private file path has no final component")

    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory_flags |= getattr(os, "O_CLOEXEC", 0)
    if absolute:
        current = os.open(os.sep, directory_flags)
    elif dir_fd is not None:
        current = os.dup(dir_fd)
    else:
        current = os.open(".", directory_flags)
    try:
        for part in parts[:-1]:
            child = os.open(part, directory_flags, dir_fd=current)
            os.close(current)
            current = child
        return os.open(parts[-1], flags, dir_fd=current)
    finally:
        os.close(current)


def read_private_text(
    path: PathValue,
    *,
    label: str,
    max_bytes: int = 8192,
    strip: bool = False,
    dir_fd: int | None = None,
) -> str:
    """Read a small owner-private regular file without following a symlink.

    Validation and the read use the same descriptor.  This keeps a local peer
    from redirecting a credential path between a pathname check and the read.
    Platforms without the required no-follow descriptor primitives fail closed.
    """
    error = f"{label} must be a private regular file owned by the service user"
    if (
        isinstance(max_bytes, bool)
        or not isinstance(max_bytes, int)
        or max_bytes <= 0
        or not hasattr(os, "geteuid")
        or not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "O_NOFOLLOW")
    ):
        raise ValueError(error)

    flags = os.O_RDONLY | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    fd = -1
    try:
        fd = _open_no_symlinks(Path(path), flags, dir_fd=dir_fd)
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) not in _PRIVATE_FILE_MODES
            or info.st_size > max_bytes
        ):
            raise ValueError(error)

        raw = bytearray()
        while len(raw) <= max_bytes:
            block = os.read(fd, min(65536, max_bytes + 1 - len(raw)))
            if not block:
                break
            raw.extend(block)
        if len(raw) > max_bytes:
            raise ValueError(error)
        try:
            value = bytes(raw).decode("utf-8")
        except UnicodeError:
            raise ValueError(error) from None
        return value.strip() if strip else value
    except ValueError:
        raise
    except OSError:
        raise ValueError(error) from None
    finally:
        if fd >= 0:
            os.close(fd)
