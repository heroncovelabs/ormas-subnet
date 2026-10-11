"""MIT. Public worker source; local Docker boundary checks, not a gateway client.

No provider calls, credentials or worker output are logged. The three negative
workers use the reference Dockerfile's pinned Python base, not the candidate's
entrypoint/runtime. Passing is not a live proxy, attestation or acceptance proof.
"""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import uuid

try:
    import intake_check
except ValueError as error:
    if getattr(error, "code", None) not in {"python_version_mismatch", "unicode_version_mismatch"}:
        raise
    print(f"FAIL {error.code}")
    raise SystemExit(1) from None


class CheckError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def fixture(work, mode):
    policy = {"repo_base_sha": "a" * 40, "allowed_paths": ["src/**"], "immutable_paths": [],
              "max_turns": 1, "max_attempts": 1, "continuation_cost_authorization_ticks": 1,
              "allowed_tools": ["terminal"]}
    packet = {"task_id": "local-fixture", "attempt": 1, "base_commit": "a" * 40,
              "brief": "Append a comment to `src/example.py`.", "allowed_paths": ["src/**"],
              "immutable_paths": [], "verify_command": "python3 -c 'pass'",
              "acceptance_criteria": ["A comment is appended"], "execution_policy": policy,
              "work_packet_sha256": "b" * 64, "parent_job_id": "parent",
              "repair_findings": [], "repair_evidence": {}}
    work.chmod(0o777)  # uid 65534 must write the laptop-owned bind mount.
    for name in (".home", ".tmp"):
        (work / name).mkdir(mode=0o777)
        (work / name).chmod(0o777)
    if mode == "solve":
        (work / "repo/src").mkdir(parents=True)
        (work / "repo/src/example.py").write_bytes(b"old\n")
        (work / "packet.json").write_text(json.dumps(packet), encoding="utf-8")
    else:
        # Privacy-safe shape from protected_worker._OFFER_FIELDS; no task/source prose.
        envelope = {"size_class": "small", "attempt_limit": 1, "turn_budget": 1,
                    "archetype": "code-edit-small", "languages": ["python"],
                    "task_text_chars": 42, "acceptance_criteria_count": 1,
                    "immutable_paths_count": 0, "allowed_paths_count": 1,
                    "verify_command_category": "other", "source_path_bucket": "f1/d1",
                    "service_level": "protected", "repository_visibility": "public"}
        (work / "offer-request.json").write_text(json.dumps(envelope), encoding="utf-8")
    for path in work.rglob("*"):
        path.chmod(0o777 if path.is_dir() else 0o666)
    return packet  # retained privately; never trust the worker's rewritten JSON.


NEGATIVE_WORKER = '''import pathlib, sys
work = pathlib.Path('/work')
kind = sys.argv[1]
if kind == 'symlink':
    (work / 'patch.diff').symlink_to('repo/src/example.py')
else:
    path = 'outside.py' if kind == 'scope' else 'src/new.py'
    mode = '100755' if kind == 'mode' else '100644'
    text = (f'diff --git a/{path} b/{path}\\nnew file mode {mode}\\n'
            f'--- /dev/null\\n+++ b/{path}\\n@@ -0,0 +1 @@\\n+new\\n')
    (work / 'patch.diff').write_text(text, encoding='utf-8')
'''


def run_image(work, image, mode, wall_time_s, negative=None):
    name = "ormas-conformance-" + uuid.uuid4().hex
    argv = ["docker", "run", "--rm", "--name", name, "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--user", "65534:65534", "--network", "none",
            "-v", f"{work.resolve()}:/work", "-e", f"MINER_WORKER_MODE={mode}",
            "-e", "ORMAS_WORK=/work", "-e", "HOME=/work/.home", "-e", "TMPDIR=/work/.tmp"]
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        argv += ["-e", f"{key}=http://proxy.invalid:3128"]
    argv += ["-e", "NO_PROXY=", "-e", "no_proxy="]
    if negative:
        (work / "negative_worker.py").write_text(NEGATIVE_WORKER, encoding="utf-8")
        (work / "negative_worker.py").chmod(0o644)
        argv += ["--entrypoint", "python3", image, "-I", "/work/negative_worker.py", negative]
    else:
        argv.append(image)
    try:
        return subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=wall_time_s, check=False).returncode
    except subprocess.TimeoutExpired:
        raise CheckError("wall_time") from None
    except OSError:
        raise CheckError("docker_unavailable") from None
    finally:
        # Also stop any surviving worker after timeout/interruption; never leave it running.
        try:
            cleanup = subprocess.run(["docker", "rm", "-f", name], stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            # --rm normally makes this return 1 (already gone); inspect on cleanup failure.
            if cleanup.returncode:
                probe = subprocess.run(["docker", "inspect", name], stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, check=False)
                if probe.returncode == 0:
                    raise CheckError("cleanup_failed")
        except OSError:
            raise CheckError("docker_unavailable") from None


def reset_permissions(work, base):
    """Trusted pinned helper restores access to Linux uid-65534-owned outputs."""
    argv = ["docker", "run", "--rm", "--read-only", "--network", "none", "--user", "0:0",
            "--cap-drop", "ALL", "--cap-add", "FOWNER", "--cap-add", "DAC_OVERRIDE",
            "--security-opt", "no-new-privileges", "-v", f"{work.resolve()}:/work",
            base, "chmod", "-R", "a+rwX", "/work"]
    try:
        result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, check=False)
    except OSError:
        raise CheckError("cleanup_failed") from None
    if result.returncode:
        raise CheckError("cleanup_failed")


@contextmanager
def workspace(base):
    work = Path(tempfile.mkdtemp(prefix="ormas-worker-"))
    try:
        yield work
    finally:
        # run_image stops the worker first. chmod precedes removal even on failure
        # or interruption; if chmod fails, refuse and retain the fixture, not a PASS.
        reset_permissions(work, base)
        shutil.rmtree(work)


def check_result(work, mode, exit_code, max_bytes, packet):
    name = "patch.diff" if mode == "solve" else "offer.json"
    if exit_code == 2:
        if os.path.lexists(work / name):
            raise CheckError("unexpected_artifact")
        return "declined"
    if exit_code != 0:
        raise CheckError("worker_failure")
    fd = os.open(work, os.O_RDONLY)
    try:
        data = intake_check.read_patch(fd, name, max_bytes=max_bytes)
        text = intake_check.decode_patch(data)
        if mode == "solve":
            intake_check.validate_patch_scope(text, allowed_paths=packet["allowed_paths"],
                                              immutable_paths=packet["immutable_paths"])
        else:
            try:
                intake_check.parse_offer(text)
            except intake_check.OfferError:
                raise CheckError("offer_invalid") from None
    except (intake_check.PatchIntakeError, intake_check.HarnessIntakeError) as error:
        raise CheckError(error.code) from None
    finally:
        os.close(fd)
    return "artifact"


def positive_integer(text):
    try:
        number = int(text)
        if number > 0:
            return number
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("positive integer required")


def main(argv=None):
    try:
        intake_check.require_runtime()
    except intake_check.HarnessIntakeError as error:
        print(f"FAIL {error.code}")
        return 1
    parser = argparse.ArgumentParser(description="Local worker conformance; no gateway/provider access")
    parser.add_argument("image", help="candidate Docker image")
    parser.add_argument("--max-patch-bytes", type=positive_integer, required=True,
                        help="operator-supplied artifact byte cap (also applies to offers)")
    parser.add_argument("--wall-time-seconds", type=positive_integer,
                        help="optional operator-supplied per-invocation wall-time; no default")
    args = parser.parse_args(argv)
    dockerfile = Path(__file__).resolve().parents[1] / "Dockerfile"
    base = re.search(r"python:3\.12-slim@sha256:[0-9a-f]{64}", dockerfile.read_text())[0]
    checks = [("solve", None, None), ("offer", None, None),
              ("solve", "symlink", "artifact_symlink"),
              ("solve", "scope", "path_not_allowed"), ("solve", "mode", "mode_change")]
    failed = False
    for mode, negative, expected in checks:
        try:
            with workspace(base) as work:
                packet = fixture(work, mode)
                code = run_image(work, base if negative else args.image, mode,
                                 args.wall_time_seconds, negative=negative)
                try:
                    result = check_result(work, mode, code, args.max_patch_bytes, packet)
                except CheckError as error:
                    if expected != error.code:
                        raise
                    result = error.code
                else:
                    if expected is not None:
                        raise CheckError("negative_not_refused")
            print(f"PASS {negative or mode}_{result}")
        except CheckError as error:
            print(f"FAIL {error.code}")
            failed = True
        except (OSError, ValueError, TypeError, KeyError):
            print("FAIL fixture_invalid")
            failed = True
    return int(failed)


if __name__ == "__main__":
    sys.exit(main())
