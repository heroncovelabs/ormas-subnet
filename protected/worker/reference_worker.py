"""MIT. Public source, not a mirror. Python >= 3.10, stdlib only.
Trivial solve appends a comment; offer declines unless sealed pricing is set.
Never log input values. This exercises the boundary, not useful mining logic.
"""
import difflib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import unicodedata

def safe_path(path):
    parts = path.split("/")
    return (bool(path) and path == path.strip() and not path.startswith(("/", "~"))
            and not any(c in path for c in ('"', "\\")) and " b/" not in path
            and not (len(path) >= 2 and path[1] == ":")
            and not any(p in {"", ".", ".."} for p in parts)
            and not any(unicodedata.category(c) in {"Cc", "Cf"} for c in path)
            and ".git" not in [p.casefold() for p in parts]
            and parts[-1].casefold() not in {".gitmodules", ".gitattributes"})

def allowed(path, patterns):
    def match(parts, tokens):
        if not tokens:
            return not parts
        if tokens[0] == "**":
            return any(match(parts[i:], tokens[1:]) for i in range(len(parts) + 1))
        regex = re.escape(tokens[0]).replace(r"\*", "[^/]*").replace(r"\?", "[^/]")
        return bool(parts and re.fullmatch(regex, parts[0]) and match(parts[1:], tokens[1:]))
    for pattern in patterns:
        tokens = [p for p in pattern.replace("\\", "/").strip().split("/") if p not in {"", "."}]
        if pattern.strip().startswith(("/", "~")) or ".." in tokens:
            raise ValueError("path_invalid")
        if match(path.split("/"), tokens):
            return True
    return False

def render_change(path, old, new, old_mode="100644", new_mode="100644"):
    """Render one add/modify/delete; preserve content CRs and missing final LF."""
    if not safe_path(path):
        raise ValueError("path_invalid")
    if ((old is not None and old_mode not in {"100644", "100755"})
            or (new is not None and new_mode not in {"100644", "100755"})
            or (old is None and new_mode != "100644")
            or (old is not None and new is not None and old_mode != new_mode)):
        raise ValueError("mode_change")
    if old == new:
        return ""
    if old == b"" and new is None or old is None and new == b"":
        raise ValueError("empty_file")
    def lines(data):
        if data is None:
            return []
        if b"\0" in data:
            raise ValueError("binary_patch")
        parts = data.decode("utf-8").split("\n")
        return [p + "\n" for p in parts[:-1]] + ([parts[-1]] if parts[-1] else [])
    header = f"diff --git a/{path} b/{path}\n"
    if old is None:
        header += f"new file mode 100644\n--- /dev/null\n+++ b/{path}\n"
    elif new is None:
        header += f"deleted file mode {old_mode}\n--- a/{path}\n+++ /dev/null\n"
    else:
        header += f"--- a/{path}\n+++ b/{path}\n"
    hunks = list(difflib.unified_diff(lines(old), lines(new), n=3, lineterm="\n"))[2:]
    return header + "".join(line if line.endswith("\n") else
                           line + "\n\\ No newline at end of file\n" for line in hunks)

def read_json(path):
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "r", encoding="utf-8") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("input_invalid")
        result = json.load(stream)
    if not isinstance(result, dict):
        raise ValueError("input_invalid")
    return result

def write_artifact(work, name, data):
    fd, temporary = tempfile.mkstemp(prefix=".artifact-", dir=work)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o644)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, work / name)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

def solve(work):
    packet = read_json(work / "packet.json")
    for token in packet["brief"].split():
        path = token.strip("`'\",.;:()")
        if not safe_path(path) or not allowed(path, packet["allowed_paths"]):
            continue
        def fold(value):
            return unicodedata.normalize("NFC", value).casefold()
        if allowed(fold(path), [fold(p) for p in packet["immutable_paths"]]):
            continue
        target = work / "repo" / path
        if not target.exists():
            continue
        relative = Path("repo") / path
        if any((work / entry).is_symlink() for entry in (relative, *relative.parents)):
            continue
        info = target.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("not_regular")
        old = target.read_bytes()
        eol = b"\r\n" if b"\r\n" in old else b"\n"
        new = old + (eol if old and not old.endswith(b"\n") else b"") + b"# reference worker" + eol
        mode = "100755" if info.st_mode & 0o111 else "100644"
        text = render_change(path, old, new, mode, mode)
        target.write_bytes(new)
        write_artifact(work, "patch.diff", text.encode("utf-8"))
        return 0
    print("no_patch", file=sys.stderr)
    return 2
def main():
    work = Path(os.environ.get("ORMAS_WORK", "/work"))
    mode = os.environ.get("MINER_WORKER_MODE", "solve")
    try:
        if mode == "solve":
            return solve(work)
        if mode != "offer":
            raise ValueError("mode_invalid")
        read_json(work / "offer-request.json")
        raw = os.environ.get("REFERENCE_WORKER_PRICE_USD")
        answer = {"decline": True}
        if raw is not None:
            try:
                price = float(raw)
                if not math.isfinite(price) or price <= 0:
                    raise ValueError()
            except (ValueError, OverflowError):
                print("offer_invalid", file=sys.stderr)
                return 1
            answer = {"estimate_usd": price, "limit_usd": price}
        write_artifact(work, "offer.json", (json.dumps(answer, allow_nan=False) + "\n").encode())
        return 0
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        print("worker_failure", file=sys.stderr)
        return 1
if __name__ == "__main__":
    sys.exit(main())
