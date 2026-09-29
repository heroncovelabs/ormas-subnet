"""Caller-owned external observation; all repositories and keys are synthetic."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile


def observe(source: str, scenario: str) -> dict:
    sys.path.insert(0, str(Path(source).resolve()))
    from ormas_subnet.validator import (
        ValidatorConfig, ValidatorDaemon, canonical_evidence_fields,
        evidence_digest_hex, make_ed25519_signer,
    )
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    with tempfile.TemporaryDirectory(prefix="validator-intake-") as temp:
        root = Path(temp)
        repo = root / "source"
        repo.mkdir()
        def git(*args: str) -> str:
            return subprocess.run(["git", *args], cwd=repo, check=True,
                                  capture_output=True, text=True).stdout.strip()
        git("init")
        git("config", "user.name", "Synthetic acceptance")
        git("config", "user.email", "acceptance@example.invalid")
        (repo / "state.txt").write_text("base")
        (repo / "tracked.txt").write_text("original")
        (repo / ".gitignore").write_text("ignored-artifact\n.venv/\n")
        (repo / "check.py").write_text(
            "from pathlib import Path\n"
            "import sys\n"
            f"scenario = {scenario!r}\n"
            "base = Path('state.txt').read_text() == 'base'\n"
            "artifact = Path('ignored-artifact' if scenario == 'ignored' else 'artifact')\n"
            "if base:\n"
            "    if scenario in ('untracked', 'ignored', 'false_accept', 'toolchain'): artifact.write_text('base-only')\n"
            "    if scenario == 'tracked': Path('tracked.txt').write_text('base-test-dirt')\n"
            "    raise SystemExit(0 if scenario == 'already_green' else 1)\n"
            "if scenario == 'false_accept': raise SystemExit(0 if artifact.exists() else 1)\n"
            "if scenario in ('untracked', 'ignored', 'toolchain') and artifact.exists(): raise SystemExit(1)\n"
            "if scenario == 'tracked' and Path('tracked.txt').read_text() != 'result': raise SystemExit(1)\n"
            "if scenario == 'toolchain' and not Path('.venv/qualification-marker').exists(): raise SystemExit(1)\n"
            "raise SystemExit(0)\n"
        )
        git("add", "-A")
        git("commit", "-m", "synthetic base")
        base = git("rev-parse", "HEAD")
        (repo / "state.txt").write_text("result")
        if scenario == "tracked":
            (repo / "tracked.txt").write_text("result")
        git("add", "-A")
        git("commit", "-m", "synthetic result")
        result = git("rev-parse", "HEAD")
        fields = canonical_evidence_fields(
            job_id="synthetic-job", miner_id="synthetic-miner", base_commit=base,
            result_commit=result, repo_url=str(repo),
            verify_command=f"{shlex.quote(sys.executable)} check.py",
            allowed_paths=["state.txt", "tracked.txt"],
            immutable_paths=["state.txt"] if scenario == "immutable" else ["check.py"],
        )
        assignment = dict(fields, assignment_id="synthetic-assignment",
                          evidence_digest_sha256=evidence_digest_hex(fields))
        class Client:
            def __init__(self):
                self.served = False
                self.decisions = []
            def list_assignments(self):
                if self.served:
                    return []
                self.served = True
                return [assignment]
            def post_decision(self, assignment_id, *, decision, signature_hex):
                self.decisions.append(dict(assignment_id=assignment_id,
                                           decision=decision, signature_hex=signature_hex))
        if scenario == "toolchain":
            import ormas_subnet.validator as module
            def provision(_spec, workdir):
                # Local provision seam: no downloads; tests cleanup preserves dependencies.
                venv = workdir / ".venv"
                (venv / "bin").mkdir(parents=True, exist_ok=True)
                (venv / "qualification-marker").write_text("installed")
                return str(venv / "bin")
            module.provision_toolchain = provision
        client = Client()
        sign_fn, pubkey = make_ed25519_signer("11" * 32)
        daemon = ValidatorDaemon(client, ValidatorConfig(workdir_root=root / "work"), sign_fn)
        processed = daemon.run_once()
        idle = not daemon.run_once()
        decision = client.decisions[0]
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(pubkey)).verify(
            bytes.fromhex(decision["signature_hex"]),
            assignment["evidence_digest_sha256"].encode(),
        )
        return dict(decision=decision["decision"], processed=processed, idle=idle,
                    count=len(client.decisions), signature_valid=True,
                    source_clean=not bool(git("status", "--porcelain")),
                    source_head_unchanged=git("rev-parse", "HEAD") == result)


if __name__ == "__main__":
    print(json.dumps(observe(sys.argv[1], sys.argv[2]), sort_keys=True))
