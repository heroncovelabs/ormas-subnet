"""Pure path and scope grammar shared by public artifact builders."""

import posixpath
import re
import unicodedata
from typing import Sequence


class RunnerError(RuntimeError):
    """Base error for allowlist validation."""


# Secret entries from dogfood_onboarding.DEFAULT_EXCLUDE_FILE_PATTERNS.
SECRET_PATH_PATTERNS = (".env", ".env.*", "*.key", "*.pem")


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
    "secret_path": "Patch touches a secret path",
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


def _root_covering(path: str) -> bool:
    normalized = posixpath.normpath(path)
    return (normalized in {".", "/", "*", "**", ".."}
            or normalized.startswith(("/", "../"))
            or re.match(r"[*?\[]", normalized) is not None
            or all(part in {"*", "**"} for part in normalized.split("/")))
