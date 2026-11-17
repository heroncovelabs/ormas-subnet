"""The public artifact path grammar must work without the private package."""
from __future__ import annotations

import ast
import os
import subprocess
import sys
import textwrap
from pathlib import Path

PUBLIC_ROOT = Path(__file__).resolve().parents[1]


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output([
        "git", "-c", "user.name=Ormas tests", "-c", "user.email=tests@ormas.invalid", *args,
    ], cwd=repo).decode().strip()


def test_public_raw_path_builders_run_without_tensorbox_spec(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    (repo / "value.txt").write_text("base\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "--quiet", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "value.txt").write_text("candidate\n")
    (repo / "src").mkdir()
    (repo / "src/new.txt").write_text("new material\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "--quiet", "-m", "candidate")
    candidate = _git(repo, "rev-parse", "HEAD")

    probe = textwrap.dedent(r"""
        import hashlib
        import io
        import json
        import sys

        class BlockPrivate:
            def find_spec(self, fullname, path=None, target=None):
                if fullname == "tensorbox_spec" or fullname.startswith("tensorbox_spec."):
                    raise ModuleNotFoundError("private imports are forbidden: " + fullname)
                return None

        sys.meta_path.insert(0, BlockPrivate())
        from ormas_subnet import outcomes_support as support

        repo, base, candidate = sys.argv[1:]
        changed = ["src/new.txt", "value.txt"]

        def outcome(operation):
            try:
                operation()
            except Exception as exc:
                return type(exc).__name__, str(exc), getattr(exc, "code", None)
            return None

        for allowed in (["**"], ["src/", "value.txt"]):
            policy = {"repo_base_sha": base, "allowed_paths": allowed,
                      "immutable_paths": ["tests/"]}
            assert support._validate_raw_file_paths(changed, policy) is None
            bounds = {"max_bytes": 4096, "max_files": 2}
            packets = [
                (support.build_raw_file_artifact, policy, bounds),
                (support.build_graded_artifact, {
                    "acceptance_level": "graded", "graded_contract": {},
                    "execution_policy": policy, "delivery_limits": bounds,
                }, {}),
                (support.build_public_artifact, {
                    "execution_policy": policy,
                    "execution_requirements": {
                        "limits": {"max_bytes": bounds["max_bytes"]},
                        "publication": {"max_changed_files": bounds["max_files"]},
                    },
                }, {}),
            ]
            # Packet validation is orthogonal; exercise the real builder and Git repository.
            support.validate_public_execution_packet = lambda _packet: None
            for builder, argument, keywords in packets:
                target = io.BytesIO()
                if builder is support.build_raw_file_artifact:
                    length, digest, _tree = builder(
                        argument, repo, candidate, target, **bounds)
                else:
                    length, digest, _tree = builder(argument, repo, candidate, target, **keywords)
                raw = target.getvalue()
                assert length == len(raw) and digest == hashlib.sha256(raw).hexdigest()
                header_length = int.from_bytes(raw[:8], "big")
                header = json.loads(raw[8:8 + header_length])
                assert [entry["path"] for entry in header["files"]] == changed

            if allowed == ["**"]:
                assert outcome(lambda: support._validate_raw_file_paths(
                    ["config/.env.local"], policy)) == (
                        "PatchIntakeError", "Patch touches a secret path", "secret_path")
                assert outcome(lambda: support._validate_raw_file_paths(
                    [".gitmodules"], policy)) == (
                        "PatchIntakeError", "Repository control paths are forbidden", "control_path")
            else:
                assert outcome(lambda: support._validate_raw_file_paths(
                    ["outside.txt"], policy)) == (
                        "ValueError", "public_artifact_out_of_scope", None)
                assert outcome(lambda: support._validate_raw_file_paths(
                    ["outside.txt"], policy, enforce_allowed_paths=False)) is None

        leaked = sorted(name for name in sys.modules
                        if name == "tensorbox_spec" or name.startswith("tensorbox_spec."))
        assert leaked == [], leaked
        print("PUBLIC_RAW_PATHS_OK")
    """)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PUBLIC_ROOT)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run(
        [sys.executable, "-c", probe, str(repo), base, candidate],
        cwd=PUBLIC_ROOT, env=env, capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines()[-1] == "PUBLIC_RAW_PATHS_OK"


def test_public_package_has_no_static_tensorbox_spec_imports() -> None:
    offenders = []
    for path in sorted((PUBLIC_ROOT / "ormas_subnet").rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""] if node.level == 0 else []
            else:
                continue
            if any(name == "tensorbox_spec" or name.startswith("tensorbox_spec.")
                   for name in names):
                offenders.append(f"{path.relative_to(PUBLIC_ROOT)}:{node.lineno}")
    assert offenders == [], "private imports found at " + ", ".join(offenders)
