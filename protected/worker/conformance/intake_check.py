"""MIT. Public worker source; standalone intake port, not a deployment mirror.

Behavior copied from privacy/worker_patch.py and local_runner scope helpers.
AST/corpus parity guards the text port; a local delivery wrapper refuses COPY.
The shell text parser accepts COPY, but its apply gate refuses the unchanged source.
"""

import errno
import json
import math
import os
import re
import stat
import sys
import unicodedata
from dataclasses import dataclass
from io import DEFAULT_BUFFER_SIZE

from typing import Sequence


# Python 3.12 in the pinned shell base uses Unicode 15.0.0. Cross-checked in
# monorepo tests against the shell venv; older databases miss controls such as U+0890.
EXPECTED_UNIDATA_VERSION = "15.0.0"
HARNESS_REFUSALS = frozenset(("copy_unsupported", "python_version_mismatch", "unicode_version_mismatch"))


class HarnessIntakeError(ValueError):
    """Local-harness-only code; never a worker_patch or live shell refusal."""

    def __init__(self, code):
        if code not in HARNESS_REFUSALS:
            raise ValueError("Invalid harness refusal code")
        self.code = code
        super().__init__(code)


def require_runtime():
    if sys.version_info < (3, 12):
        raise HarnessIntakeError("python_version_mismatch")
    if unicodedata.unidata_version != EXPECTED_UNIDATA_VERSION:
        raise HarnessIntakeError("unicode_version_mismatch")


require_runtime()  # Refuse before defining/using the version-sensitive parser.


class RunnerError(ValueError):
    pass


def _normalize_repo_rel_path(path: str) -> str | None:
    """Return an anchored repository-relative path, or None when unsafe."""
    if not isinstance(path, str):
        return None
    raw = path.replace("\\", "/").strip()
    if not raw or raw.startswith("/") or raw.startswith("~"):
        return None
    # Windows drive / UNC
    if len(raw) >= 2 and raw[1] == ":":
        return None
    if raw.startswith("//"):
        return None
    parts: list[str] = []
    for part in raw.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            return None
        parts.append(part)
    if not parts:
        return None
    return "/".join(parts)


def _validate_allowlist_pattern(pattern: str) -> str:
    """Reject empty/absolute/traversal/unsafe allowlist patterns; return normalized."""
    if not isinstance(pattern, str) or not pattern.strip():
        raise RunnerError("allowed_paths entries must be nonempty")
    raw = pattern.replace("\\", "/").strip()
    if raw.startswith("/") or raw.startswith("~") or raw.startswith("//"):
        raise RunnerError("allowed_paths must be repository-relative")
    if len(raw) >= 2 and raw[1] == ":":
        raise RunnerError("allowed_paths must be repository-relative")
    # Normalize . segments but keep glob tokens; reject .. always.
    parts: list[str] = []
    for part in raw.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise RunnerError("allowed_paths must not contain path traversal")
        parts.append(part)
    if not parts:
        raise RunnerError("allowed_paths entries must be nonempty")
    return "/".join(parts)


def _segment_glob_match(segment: str, pattern: str) -> bool:
    """Match one path segment; ``*`` and ``?`` never cross ``/``."""
    i = 0
    j = 0
    star = -1
    match_i = 0
    while i < len(segment):
        if j < len(pattern) and pattern[j] == "*":
            star = j
            match_i = i
            j += 1
            continue
        if j < len(pattern) and (pattern[j] == "?" or pattern[j] == segment[i]):
            i += 1
            j += 1
            continue
        if star >= 0:
            j = star + 1
            match_i += 1
            i = match_i
            continue
        return False
    while j < len(pattern) and pattern[j] == "*":
        j += 1
    return j == len(pattern)


def _glob_path_match(path: str, pattern: str) -> bool:
    """Anchored relative glob: ``*`` is one segment; ``**`` spans directories."""
    path_parts = path.split("/") if path else []
    pat_parts = pattern.split("/") if pattern else []

    def match(pi: int, gi: int) -> bool:
        while gi < len(pat_parts):
            token = pat_parts[gi]
            if token == "**":
                if gi == len(pat_parts) - 1:
                    return True
                for skip in range(pi, len(path_parts) + 1):
                    if match(skip, gi + 1):
                        return True
                return False
            if pi >= len(path_parts):
                return False
            if not _segment_glob_match(path_parts[pi], token):
                return False
            pi += 1
            gi += 1
        return pi == len(path_parts)

    return match(0, 0)


def _path_is_allowed(path: str, patterns: Sequence[str]) -> bool:
    """Return True when *path* matches a validated allowlist entry."""
    candidate = _normalize_repo_rel_path(path)
    if candidate is None:
        return False
    for raw in patterns:
        try:
            pattern = _validate_allowlist_pattern(str(raw))
        except RunnerError:
            return False
        if any(ch in pattern for ch in "*?["):
            if _glob_path_match(candidate, pattern):
                return True
            continue
        # Exact entries are exact-only; subtrees require an explicit ``dir/**``.
        if candidate == pattern:
            return True
    return False




_MESSAGES = {
    "artifact_name": "Invalid artifact name",
    "artifact_symlink": "Artifact must not be a symlink",
    "artifact_not_regular": "Artifact must be a readable regular file",
    "artifact_missing": "Artifact is missing",
    "artifact_too_large": "Artifact exceeds the byte cap",
    "artifact_empty": "Artifact is empty",
    "patch_not_utf8": "Patch must be UTF-8",
    "patch_nul": "Patch contains NUL",
    "not_a_git_diff": "Expected a Git unified diff",
    "path_traversal": "Unsafe patch path",
    "path_mismatch": "Patch paths disagree",
    "control_path": "Repository control paths are forbidden",
    "symlink_entry": "Symlink entries are forbidden",
    "gitlink_entry": "Gitlink entries are forbidden",
    "mode_change": "File mode changes are forbidden",
    "binary_patch": "Binary patches are forbidden",
    "quoted_path": "Quoted patch paths are forbidden",
    "path_not_allowed": "Patch exceeds the allowed scope",
    "immutable_path": "Patch touches immutable scope",
    "duplicate_path": "Patch touches a path more than once",
    "malformed_patch": "Malformed unified diff",
}
PATCH_REFUSALS = frozenset(_MESSAGES)


class PatchIntakeError(ValueError):
    """A fixed, non-disclosing patch refusal."""

    def __init__(self, code: str):
        if code not in PATCH_REFUSALS:
            raise ValueError("Invalid patch refusal code")
        self.code = code
        super().__init__(_MESSAGES[code])


@dataclass(frozen=True)
class PatchFile:
    """One admitted file change."""

    path: str
    old_path: str | None
    change: str
    mode: str | None


@dataclass(frozen=True)
class PatchScope:
    """Ordered changes and their complete touched-path view."""

    files: tuple[PatchFile, ...]

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(sorted({
            path for file in self.files for path in (file.path, file.old_path)
            if path is not None
        }))


def read_patch(dir_fd: int, name: str = "patch.diff", *, max_bytes: int) -> bytes:
    """Read a capped regular artifact relative to a caller-owned directory."""
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("Byte cap must be a positive integer")
    if (not isinstance(name, str) or not name or name in {".", ".."}
            or any(character in name for character in ("/", "\\", "\x00"))):
        raise PatchIntakeError("artifact_name")
    fd = None
    try:
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        fd = os.open(name, flags, dir_fd=dir_fd)
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise PatchIntakeError("artifact_not_regular")
        chunks = []
        total = 0
        while True:
            chunk = os.read(fd, min(DEFAULT_BUFFER_SIZE, max_bytes - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise PatchIntakeError("artifact_too_large")
            chunks.append(chunk)
        if not total:
            raise PatchIntakeError("artifact_empty")
        return b"".join(chunks)
    except OSError as error:
        if error.errno == errno.ELOOP:
            code = "artifact_symlink"
        elif error.errno == errno.ENOENT:
            code = "artifact_missing"
        else:
            code = "artifact_not_regular"
        raise PatchIntakeError(code) from None
    finally:
        if fd is not None:
            os.close(fd)


def decode_patch(data: bytes) -> str:
    """Strictly decode UTF-8 without normalizing line endings."""
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        text = None
    # Raise after the handler exits so the raw decode error is not retained.
    if text is None:
        raise PatchIntakeError("patch_not_utf8")
    if "\x00" in text:
        raise PatchIntakeError("patch_nul")
    return text


_HUNK = re.compile(r"@@ -([0-9]+)(?:,([0-9]+))? \+([0-9]+)(?:,([0-9]+))? @@(?: .*)?")
_INDEX = re.compile(r"index [0-9a-fA-F]+\.\.[0-9a-fA-F]+(?: ([0-7]{6}))?")
_SIMILARITY = re.compile(r"(?:dis)?similarity index ([0-9]+)%")
_MODE = re.compile(r"[0-7]{6}")
_MODE_HEADERS = ("new file mode", "deleted file mode", "old mode", "new mode")
_MOVE_HEADERS = ("rename from", "rename to", "copy from", "copy to")
_NO_NEWLINE = "\\ No newline at end of file"


def _path(value: str) -> str:
    """Validate a literal path; never repair worker-supplied syntax."""
    if '"' in value:
        raise PatchIntakeError("quoted_path")
    parts = value.split("/")
    if (not value or value != value.strip() or value.startswith(("/", "~"))
            or "\\" in value or any(part in {"", ".", ".."} for part in parts)
            or any(unicodedata.category(character) in {"Cf", "Cc"} for character in value)
            or (len(value) >= 2 and value[1] == ":")):
        raise PatchIntakeError("path_traversal")
    folded_parts = [part.casefold() for part in parts]
    if ".git" in folded_parts or folded_parts[-1] in {".gitmodules", ".gitattributes"}:
        raise PatchIntakeError("control_path")
    return value


def _prefixed_path(value: str, prefix: str, *, nullable: bool = False) -> str | None:
    if '"' in value:
        raise PatchIntakeError("quoted_path")
    if nullable and value == "/dev/null":
        return None
    if not value.startswith(prefix):
        raise PatchIntakeError("path_mismatch")
    return _path(value[len(prefix):])


def _git_paths(line: str) -> tuple[str, str]:
    raw = line[len("diff --git "):]
    if '"' in raw:
        raise PatchIntakeError("quoted_path")
    sides = raw.split(" b/")
    if len(sides) != 2:
        raise PatchIntakeError("malformed_patch")
    return _prefixed_path(sides[0], "a/"), _prefixed_path("b/" + sides[1], "b/")


def _headers(lines: list[str], position: int) -> tuple[dict, int]:
    headers = {}
    while position < len(lines) and not lines[position].startswith("--- "):
        line = lines[position]
        if line == "GIT binary patch" or line.startswith("Binary files "):
            raise PatchIntakeError("binary_patch")
        key = next((key for key in (*_MODE_HEADERS, *_MOVE_HEADERS)
                    if line.startswith(key + " ")), None)
        if key is not None:
            value = line[len(key) + 1:]
            if key in _MODE_HEADERS:
                if not _MODE.fullmatch(value):
                    raise PatchIntakeError("malformed_patch")
            else:
                value = _path(value)
        elif line.startswith("index "):
            match = _INDEX.fullmatch(line)
            if match is None:
                raise PatchIntakeError("malformed_patch")
            key, value = "index", match[1]
        else:
            match = _SIMILARITY.fullmatch(line)
            if match is None:
                raise PatchIntakeError("malformed_patch")
            try:
                if int(match[1]) > 100:
                    raise PatchIntakeError("malformed_patch")
            except ValueError:
                raise PatchIntakeError("malformed_patch") from None
            key, value = line.split(" index ", 1)
        if key in headers:
            raise PatchIntakeError("malformed_patch")
        headers[key] = value
        position += 1
    return headers, position


def _check_modes(headers: dict) -> None:
    modes = [headers[key] for key in (*_MODE_HEADERS, "index") if key in headers]
    # Inspect every mode before applying the generic mode-change refusal.
    if "120000" in modes:
        raise PatchIntakeError("symlink_entry")
    if "160000" in modes:
        raise PatchIntakeError("gitlink_entry")
    if ("old mode" in headers or "new mode" in headers
            or ("new file mode" in headers and headers["new file mode"] != "100644")
            or ("deleted file mode" in headers
                and headers["deleted file mode"] not in {"100644", "100755"})
            or headers.get("index") not in {None, "100644", "100755"}):
        raise PatchIntakeError("mode_change")


def _file(old: str, new: str, headers: dict, old_marker: str | None,
          new_marker: str | None) -> PatchFile:
    kinds = [kind for kind, keys in (
        ("add", ("new file mode",)), ("delete", ("deleted file mode",)),
        ("rename", ("rename from", "rename to")), ("copy", ("copy from", "copy to")),
    ) if any(key in headers for key in keys)]
    if len(kinds) > 1:
        raise PatchIntakeError("malformed_patch")
    kind = kinds[0] if kinds else "modify"
    if kind in {"rename", "copy"}:
        if kind + " from" not in headers or kind + " to" not in headers:
            raise PatchIntakeError("malformed_patch")
        if headers[kind + " from"] != old or headers[kind + " to"] != new:
            raise PatchIntakeError("path_mismatch")
    elif old != new:
        raise PatchIntakeError("path_mismatch")
    expected_old = None if kind == "add" else old
    expected_new = None if kind == "delete" else new
    if old_marker != expected_old or new_marker != expected_new:
        raise PatchIntakeError("path_mismatch")
    return PatchFile(new, old if kind in {"rename", "copy"} else None,
                     kind, headers.get("new file mode"))


def _hunk(lines: list[str], position: int) -> int:
    match = _HUNK.fullmatch(lines[position])
    if match is None:
        raise PatchIntakeError("malformed_patch")
    try:
        old_start, new_start = int(match[1]), int(match[3])
        old_count = int(match[2]) if match[2] is not None else 1
        new_count = int(match[4]) if match[4] is not None else 1
    except ValueError:
        raise PatchIntakeError("malformed_patch") from None
    if ((not old_count and not new_count)
            or (old_count and not old_start) or (new_count and not new_start)):
        raise PatchIntakeError("malformed_patch")
    position += 1
    old_seen = new_seen = 0
    marker_allowed = False
    while position < len(lines):
        line = lines[position]
        if line.startswith(("@@", "diff --git ")):
            break
        if line == _NO_NEWLINE and marker_allowed:
            marker_allowed = False
        elif line and line[0] in {" ", "-", "+"}:
            old_seen += line[0] in {" ", "-"}
            new_seen += line[0] in {" ", "+"}
            marker_allowed = True
            if old_seen > old_count or new_seen > new_count:
                raise PatchIntakeError("malformed_patch")
        else:
            raise PatchIntakeError("malformed_patch")
        position += 1
    if old_seen != old_count or new_seen != new_count:
        raise PatchIntakeError("malformed_patch")
    return position


def _filesystem_key(path: str) -> str:
    """Fold aliases for refusals only; admitted paths remain literal."""
    return unicodedata.normalize("NFC", path).casefold()


def _patterns(values, code: str) -> tuple[str, ...]:
    try:
        if isinstance(values, (str, bytes)):
            raise PatchIntakeError(code)
        return tuple(_validate_allowlist_pattern(value) for value in values)
    except (RunnerError, TypeError):
        raise PatchIntakeError(code) from None


def _validate_patch_scope(patch_text: str, *, allowed_paths, immutable_paths) -> PatchScope:
    """Validate the entire diff before exposing any admitted scope."""
    if not isinstance(patch_text, str):
        raise PatchIntakeError("not_a_git_diff")
    # Split only on LF: embedded CR and other path controls must stay visible.
    lines = patch_text.split("\n")
    if lines[-1] == "":
        lines.pop()
    lines = [line.removesuffix("\r") for line in lines]
    position = 0
    while position < len(lines) and not lines[position].strip():
        position += 1
    if position == len(lines) or not lines[position].startswith("diff --git "):
        raise PatchIntakeError("not_a_git_diff")
    files = []
    touched = set()
    touched_keys = set()
    while position < len(lines):
        if not lines[position].startswith("diff --git "):
            raise PatchIntakeError("malformed_patch")
        old, new = _git_paths(lines[position])
        headers, position = _headers(lines, position + 1)
        _check_modes(headers)
        if (position + 1 >= len(lines) or not lines[position].startswith("--- ")
                or not lines[position + 1].startswith("+++ ")):
            raise PatchIntakeError("malformed_patch")
        old_marker = _prefixed_path(lines[position][4:], "a/", nullable=True)
        new_marker = _prefixed_path(lines[position + 1][4:], "b/", nullable=True)
        file = _file(old, new, headers, old_marker, new_marker)
        position += 2
        if position >= len(lines) or not lines[position].startswith("@@"):
            raise PatchIntakeError("malformed_patch")
        while position < len(lines) and lines[position].startswith("@@"):
            position = _hunk(lines, position)
        paths = {file.path} if file.old_path is None else {file.path, file.old_path}
        keys = {_filesystem_key(path) for path in paths}
        if (touched.intersection(paths) or touched_keys.intersection(keys)
                or len(keys) != len(paths)):
            raise PatchIntakeError("duplicate_path")
        touched.update(paths)
        touched_keys.update(keys)
        files.append(file)
    immutable = _patterns(immutable_paths, "immutable_path")
    immutable_keys = tuple(_filesystem_key(pattern) for pattern in immutable)
    if (any(_path_is_allowed(path, immutable) for path in touched)
            or any(_path_is_allowed(key, immutable_keys) for key in touched_keys)):
        raise PatchIntakeError("immutable_path")
    allowed = _patterns(allowed_paths, "path_not_allowed")
    if any(not _path_is_allowed(path, allowed) for path in touched):
        raise PatchIntakeError("path_not_allowed")
    return PatchScope(tuple(files))


def validate_patch_scope(patch_text: str, *, allowed_paths, immutable_paths) -> PatchScope:
    """Text parity plus the shell's delivery restriction; COPY cannot be applied.

    copy_unsupported is harness-only: worker_patch still accepts the text, then
    worker_apply refuses apply_scope_mismatch because the copy source is unchanged.
    """
    require_runtime()
    scope = _validate_patch_scope(patch_text, allowed_paths=allowed_paths, immutable_paths=immutable_paths)
    if any(file.change == "copy" for file in scope.files):
        raise HarnessIntakeError("copy_unsupported")
    return scope


class OfferError(ValueError):
    """Fixed, value-free invalid offer answer."""


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise OfferError("Duplicate JSON key")
        result[key] = value
    return result


def _json_object(text):
    try:
        result = json.loads(text, object_pairs_hook=_unique_object)
    except (ValueError, TypeError, RecursionError):
        raise OfferError("Invalid JSON object") from None
    if not isinstance(result, dict):
        raise OfferError("Expected a JSON object")
    return result


def parse_offer(text) -> dict:
    """Decode an exact decline or finite, positive estimate/limit pair."""
    offer = _json_object(text)
    if set(offer) == {"decline"} and offer["decline"] is True:
        return {"decline": True}
    if set(offer) != {"estimate_usd", "limit_usd"}:
        raise OfferError("Invalid offer fields")
    prices = {}
    for key, value in offer.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise OfferError("Offer prices must be numeric")
        try:
            price = float(value)
        except OverflowError:
            raise OfferError("Offer price is not finite") from None
        if not math.isfinite(price) or price <= 0:
            raise OfferError("Offer prices must be finite and positive")
        prices[key] = price
    if prices["estimate_usd"] > prices["limit_usd"]:
        raise OfferError("Offer estimate exceeds limit")
    return prices
