"""Local setup and reference miner commands."""
from __future__ import annotations

import argparse
import getpass
import json
import os
import platform
from datetime import datetime
import shutil
import stat
import sys
import tempfile
import warnings
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from . import __version__
from .client import OrmasGatewayError, OrmasMinerClient, load_token
from .protected_env import (
    MINER_ID, SPLIT_ALLOWED_ENVS, WORKER_DIGEST, json_object, parse_worker_env, provider_host,
    read_input, sealed_json, sealed_text, write_private,
)

DEFAULT_GATEWAY = "https://api.ormas.ai"
_AUTH_PROBE_ID = "runr_000000000000"


def _credentials_path() -> Path:
    return Path.home() / ".ormas-miner" / "credentials.json"


def _read_credentials() -> dict[str, str]:
    path = _credentials_path()
    if path.is_symlink():
        raise ValueError("credentials must be a regular file with mode 0600")
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        return {}
    if not stat.S_ISREG(mode) or stat.S_IMODE(mode) != 0o600:
        raise ValueError("credentials must be a regular file with mode 0600")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("credentials file is unreadable or invalid JSON") from exc
    if not isinstance(data, dict) or not all(isinstance(data.get(k), str)
                                            for k in ("gateway", "token")):
        raise ValueError("credentials need gateway and token strings")
    return data


def _gateway(value: str) -> str:
    url = urlsplit(value)
    if (url.scheme not in ("http", "https") or not url.hostname or url.username
            or url.password or url.query or url.fragment):
        raise ValueError("gateway must be an HTTP or HTTPS URL without credentials")
    if url.scheme == "http" and url.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("gateway must use HTTPS; HTTP is allowed only for loopback hosts")
    return value.rstrip("/")


def _validate_token(token: str) -> str:
    if not token.startswith("ormr_") or len(token) <= len("ormr_"):
        raise ValueError("miner key must start with ormr_ and contain a key")
    return token


def _settings(args: argparse.Namespace) -> tuple[str, str]:
    explicit = args.token_env or args.token_file
    env_token = os.environ.get("ORMAS_MINER_TOKEN")
    independent = bool(explicit or env_token)
    saved = {}
    saved_gateway = None
    if not args.gateway or not independent:
        try:
            saved = _read_credentials()
            if saved:
                saved_gateway = _gateway(saved["gateway"])
        except (OSError, ValueError):
            if not independent:
                raise
    gateway = _gateway(args.gateway or saved_gateway or DEFAULT_GATEWAY)
    if explicit:
        token = load_token(token_env=args.token_env, token_path=args.token_file)
    elif env_token:
        token = env_token
    else:
        if saved and gateway != saved_gateway:
            raise ValueError("saved key is bound to its gateway; use --token-env, --token-file "
                             "or ORMAS_MINER_TOKEN for this gateway")
        token = saved.get("token", "")
    if not token:
        raise ValueError("missing token; run ormas-miner login or set ORMAS_MINER_TOKEN")
    return gateway, _validate_token(token)


def _login(args: argparse.Namespace) -> int:
    gateway = _gateway(args.gateway or DEFAULT_GATEWAY)
    if args.token_env or args.token_file:
        token = load_token(token_env=args.token_env, token_path=args.token_file)
    else:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            token = getpass.getpass("Miner key: ")
    token = _validate_token(token.strip())
    path = _credentials_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise ValueError("credentials directory must be a regular directory")
    os.chmod(path.parent, 0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".credentials-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"gateway": gateway, "token": token}, stream)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print(f"Saved miner key for {gateway} at {path}")
    return 0


def _doctor_registration(registration: dict) -> bool:
    raw_cells = registration.get("cells")
    cells = (
        {cell for cell in raw_cells if isinstance(cell, str)}
        if isinstance(raw_cells, list) else set()
    )
    required = (
        "task:code", "task:lang/python", "task:publication/github-artifact-v1",
        "task:acceptance/operator-run-v3", "task:preflight/deferred-v1",
    )
    missing = [cell for cell in required if cell not in cells]
    sizes = [size for size in ("small", "medium", "large") if f"task:code/{size}" in cells]
    if not sizes:
        missing.append("task:code/small|task:code/medium|task:code/large")
    coverage = "MISSING " + " ".join(missing) if missing else "ok"
    print(f"cells: {coverage} (sizes: {', '.join(sizes) or 'none'})")

    hotkey = registration.get("hotkey")
    if isinstance(hotkey, dict) and hotkey.get("bound") is True:
        print("hotkey: bound" + ("" if hotkey.get("verified") is True else " (unverified)"))
    else:
        print("hotkey: none")
        print("warn hotkey: run ormas-miner register-hotkey")

    cap = registration.get("claim_cap")
    cap = cap if type(cap) is int and cap >= 0 else "unknown"
    qualification = registration.get("qualification")
    status = None
    if isinstance(qualification, dict):
        job = qualification.get("job")
        status = qualification.get("job_status") or qualification.get("status")
        if status is None and isinstance(job, dict):
            status = job.get("status")
    # Job statuses are protocol values; arbitrary response text stays out of diagnostics.
    known_status = isinstance(status, str) and status in (
        "queued", "offering", "running", "done", "failed", "cancelled", "outcome_unknown",
    )
    suffix = f" (job: {status})" if known_status else ""
    print(f"qualification: cap {cap}{suffix}")
    return not missing


def _doctor(args: argparse.Namespace) -> int:
    passed = True

    def check(label: str, good: bool, reason: str) -> None:
        nonlocal passed
        passed = passed and good
        print(f"{'ok' if good else 'FAIL'} {label}: {reason}")

    python_ok = sys.version_info[:2] >= (3, 10)
    check("python", python_ok, "Python >= 3.10" if python_ok else "Python >= 3.10 required")
    git = shutil.which("git")
    check("git", bool(git), "available" if git else "git is missing from PATH")
    token = None
    try:
        gateway, token = _settings(args)
        credential_error = None
    except (OSError, ValueError) as exc:
        credential_error = str(exc)
        gateway = args.gateway or DEFAULT_GATEWAY
    health_ok = False
    try:
        gateway = _gateway(gateway)
        with httpx.Client(base_url=gateway) as client:
            response = client.get("/health")
            response.raise_for_status()
            if not isinstance(response.json(), dict):
                raise ValueError("health response must be a JSON object")
        health_ok = True
        check("gateway", True, f"{gateway}/health reachable")
    except (httpx.HTTPError, OSError, ValueError):
        check("gateway", False, "unreachable or invalid /health response")
    check("credentials", token is not None, credential_error or "miner key present (ormr_)")
    if token is not None and health_ok:
        try:
            with httpx.Client(base_url=gateway, headers={"Authorization": f"Bearer {token}"},
                              timeout=10.0) as transport:
                with OrmasMinerClient(gateway, token, http_client=transport,
                                      device_nonce=args.device_nonce) as client:
                    try:
                        queue = client.list_queue(args.runner_id or _AUTH_PROBE_ID)
                    except OrmasGatewayError as exc:
                        # A typed lookup miss follows authentication in the queue protocol.
                        if exc.status_code != 404 or exc.error_type != "not_found_error":
                            raise
                        queue = {}
                    check("token", True, "key accepted (account enablement not confirmed)")
                    for entry in queue.get("excluded", []):
                        job_id = entry.get("job_id")
                        excluded_by = entry.get("excluded_by")
                        if job_id is None or excluded_by is None:
                            continue
                        detail = (json.dumps(entry["detail"], sort_keys=True)
                                  if entry.get("detail") else "")
                        print(f"reserved job {job_id} hidden: {excluded_by} {detail}".rstrip())
                    try:
                        registration = client.get_registration(args.runner_id)
                        coverage_ok = _doctor_registration(registration)
                        passed = passed and coverage_ok
                    except OrmasGatewayError as exc:
                        # A gateway older than the /runners/me route answers a plain 404
                        # with no typed error; that is a missing feature, not a refusal.
                        if exc.status_code == 404 and not exc.error_type:
                            print("WARN registration: gateway does not expose /runners/me")
                        else:
                            check("registration", False,
                                  f"gateway rejected lookup (HTTP {exc.status_code})")
                    except (httpx.HTTPError, OSError, ValueError):
                        check("registration", False, "lookup failed or invalid registration response")
        except OrmasGatewayError as exc:
            check("token", False, f"gateway rejected probe (HTTP {exc.status_code})")
        except (httpx.HTTPError, OSError, ValueError):
            check("token", False, "authentication probe failed")
    else:
        check("token", False, "authentication probe requires credentials and gateway health")
    local_bin = str(Path.home() / ".local" / "bin")
    on_path = local_bin in os.environ.get("PATH", "").split(os.pathsep)
    print(f"{'ok' if on_path else 'warn'} PATH: {local_bin} "
          f"{'is on PATH' if on_path else 'is missing from PATH'}")
    return 0 if passed else 1


class _PrivateArgumentParser(argparse.ArgumentParser):
    """Argparse's normal help/exit style, without echoing rejected env values."""

    def error(self, message):
        super().error("invalid or missing arguments; see --help")


def _protected_env(args: argparse.Namespace) -> int:
    # This command deliberately uses only a file or saved credentials, never argv keys.
    saved = _read_credentials() if not args.token_file else {}
    api_url = args.api_url or saved.get("gateway") or DEFAULT_GATEWAY
    sealed_text("ORMAS_API_URL", api_url)
    if args.runtime == "production" and api_url != DEFAULT_GATEWAY:
        raise ValueError("ORMAS_API_URL: production requires the exact production gateway origin")
    if args.runtime == "development" and urlsplit(api_url).scheme != "https":
        raise ValueError("ORMAS_API_URL: development requires HTTPS")
    api_url = _gateway(api_url)
    if saved and api_url != _gateway(saved["gateway"]):
        raise ValueError("ORMAS_RUNNER_TOKEN: saved key is bound to its gateway; use --token-file")
    token = (read_input(args.token_file, "ORMAS_RUNNER_TOKEN").removesuffix("\n")
             if args.token_file else saved.get("token", ""))
    sealed_text("ORMAS_RUNNER_TOKEN", token)
    try:
        _validate_token(token)
    except ValueError:
        raise ValueError("ORMAS_RUNNER_TOKEN: missing or invalid miner key") from None
    values = dict.fromkeys(SPLIT_ALLOWED_ENVS, "")
    values.update({
        "ORMAS_API_URL": api_url, "ORMAS_RUNNER_TOKEN": token,
        "ORMAS_MINER_ID": args.miner_id, "ORMAS_RUNTIME": args.runtime,
        "ORMAS_CELL_BOUNDS": args.cell_bounds or "", "ORMAS_APPROVED_BY": args.approved_by or "",
        "ORMAS_TASK_CELLS": " ".join(args.task_cells), "ORMAS_BIND_PROJECT_ID": args.bind_project_id,
        "MINER_WORKER_IMAGE": args.worker_image,
    })
    for key, value in values.items():
        sealed_text(key, value)
    if MINER_ID.fullmatch(args.miner_id) is None:
        raise ValueError("ORMAS_MINER_ID: invalid registration name")
    if (not args.worker_image.rsplit("@", 1)[0] or "@" not in args.worker_image
            or any(char.isspace() for char in args.worker_image)
            or args.worker_image.count("@") != 1
            or WORKER_DIGEST.fullmatch(args.worker_image.rsplit("@", 1)[-1]) is None):
        raise ValueError("MINER_WORKER_IMAGE: must be name@sha256 digest-pinned")
    if not all(cell.startswith("task:") and len(cell) > len("task:")
               and not any(char.isspace() for char in cell) for cell in args.task_cells):
        raise ValueError("ORMAS_TASK_CELLS: expected task qualifications")
    if args.authorization_file:
        if args.cell_bounds or args.approved_by:
            raise ValueError("ORMAS_AUTHORIZATION_JSON: use authorization file OR bounds and approver")
        try:
            record = json_object(read_input(args.authorization_file, "ORMAS_AUTHORIZATION_JSON"))
        except ValueError:
            raise ValueError("ORMAS_AUTHORIZATION_JSON: invalid JSON object") from None
        values["ORMAS_AUTHORIZATION_JSON"] = sealed_json("ORMAS_AUTHORIZATION_JSON", record)
    elif not args.cell_bounds or not args.approved_by:
        raise ValueError("ORMAS_CELL_BOUNDS: supply bounds and --approved-by or --authorization-file")
    try:
        worker = parse_worker_env(read_input(args.worker_env_file, "MINER_WORKER_ENV_JSON"))
    except ValueError:
        raise ValueError("MINER_WORKER_ENV_JSON: invalid worker environment object") from None
    values["MINER_WORKER_ENV_JSON"] = sealed_json("MINER_WORKER_ENV_JSON", worker)
    if args.registry_auth_file:
        values["MINER_WORKER_REGISTRY_AUTH"] = sealed_text(
            "MINER_WORKER_REGISTRY_AUTH", read_input(args.registry_auth_file, "MINER_WORKER_REGISTRY_AUTH").removesuffix("\n"))
    hosts = sorted({provider_host(host) for host in args.provider})
    out = Path(args.out).expanduser()
    files = {out: "".join(f"{key}={values[key]}\n" for key in SPLIT_ALLOWED_ENVS)}
    providers = out.parent / "declared-providers.list"
    pricing = out.parent / "pricing.json"
    if out in (providers, pricing):
        raise ValueError("env output must differ from companion file names")
    files[providers] = "".join(host + "\n" for host in hosts)
    if args.pricing_template:
        files[pricing] = json.dumps({"estimate_usd": "<estimate>", "limit_usd": "<limit>"}, indent=2) + "\n"
    if not args.force and any(path.exists() or path.is_symlink() for path in files):
        raise ValueError("output exists; use --force to overwrite")
    try:
        out.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError:
        raise ValueError("cannot create output directory") from None
    for path, text in files.items():
        write_private(path, text, force=args.force)
    print(out)
    print(f"{len(SPLIT_ALLOWED_ENVS)} keys")
    return 0


# Public counterpart of protected_health's fixed refusal vocabulary; never echo error text.
_RELEASE_REFUSALS = frozenset({
    "challenge_unknown", "release_consumed", "challenge_expired", "verifier_error",
    "collateral_unavailable", "attestation_refused", "mint_error", "seal_error",
    "release_error", "audit_write_failed", "worker_digest_mismatch", "worker_policy_required", "other",
})
_JOB_STATUSES = frozenset({"queued", "offering", "running", "done", "failed", "cancelled", "outcome_unknown", "pending"})


def _diagnostic_time(value) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.isoformat() if parsed.tzinfo is not None else None
    except ValueError:
        return None


def _doctor_protected(args: argparse.Namespace) -> int:
    passed = True

    def line(level: str, topic: str, text: str):
        nonlocal passed
        if level == "fail":
            passed = False
        print(f"{level} {topic}: {text}")

    try:
        gateway, token = _settings(args)
    except (OSError, ValueError):
        line("fail", "credentials", "miner key unavailable or invalid")
        return 1
    try:
        with httpx.Client(base_url=gateway, headers={"Authorization": f"Bearer {token}"},
                          timeout=10.0) as transport:
            with OrmasMinerClient(gateway, token, http_client=transport,
                                  device_nonce=args.device_nonce) as client:
                registration = client.get_registration(args.runner_id)
    except OrmasGatewayError as exc:
        detail = "not registered" if exc.status_code == 404 else f"gateway rejected lookup (HTTP {exc.status_code})"
        line("fail", "registration", detail)
        return 1
    except (httpx.HTTPError, OSError, ValueError):
        line("fail", "registration", "lookup failed or invalid response")
        return 1
    if not isinstance(registration.get("runner_id"), str) or not registration["runner_id"]:
        line("fail", "registration", "invalid response")
        return 1
    line("ok", "registration", "yes")
    protected = registration.get("protected")
    if not isinstance(protected, dict):
        line("fail", "protected", "gateway diagnostics unavailable")
        return 1
    approved = protected.get("slot_approved") is True
    if approved and protected.get("slot_bound") is not True:
        line("warn", "slot", "approved, not yet bound")
    else:
        line("ok" if approved else "fail", "slot", "approved" if approved else "not approved")
    declared = protected.get("declared_worker_digests")
    if not isinstance(declared, list) or any(
            not isinstance(digest, str) or WORKER_DIGEST.fullmatch(digest) is None for digest in declared):
        line("fail", "declared-workers", "invalid gateway response")
        declared = []
    else:
        line("ok" if declared else "warn", "declared-workers", ", ".join(declared) if declared else "none declared")
    policy = protected.get("approved_worker_policy") is True
    line("ok" if policy else "warn", "worker-policy", "yes" if policy else "no policy approved yet")
    status = protected.get("qualification_job_status")
    if status is None:
        line("warn", "qualification", "none")
    elif isinstance(status, str) and status in _JOB_STATUSES:
        level = "ok" if status == "done" else "warn" if status in {"queued", "offering", "running", "pending"} else "fail"
        line(level, "qualification", status)
    else:
        line("fail", "qualification", "unknown")
    release = protected.get("last_release")
    release_at = _diagnostic_time(release.get("at")) if isinstance(release, dict) else None
    refusal = protected.get("last_refusal")
    if protected.get("audit_status") != "ok":
        line("fail", "key-release", "audit unavailable")
    elif refusal is None:
        line("ok", "key-release", "no refusal recorded")
    elif (isinstance(refusal, dict) and isinstance(refusal.get("reason"), str)
          and refusal["reason"] in _RELEASE_REFUSALS and _diagnostic_time(refusal.get("at"))):
        at = _diagnostic_time(refusal["at"])
        resolved = release_at and datetime.fromisoformat(release_at) > datetime.fromisoformat(at)
        line("warn" if resolved else "fail", "key-release", f"{refusal['reason']} {at}")
    else:
        line("fail", "key-release", "invalid gateway response")
    if not declared:
        line("warn", "worker-digest", "none declared")
    elif release is None:
        line("warn", "worker-digest", "no release recorded")
    elif not isinstance(release, dict) or not release_at:
        line("fail", "worker-digest", "invalid gateway response")
    else:
        digest = release.get("worker_image_digest")
        if digest is None:
            line("fail", "worker-digest", "mismatch (release has no worker digest)")
        elif not isinstance(digest, str) or WORKER_DIGEST.fullmatch(digest) is None:
            line("fail", "worker-digest", "invalid gateway response")
        else:
            match = digest in declared
            line("ok" if match else "fail", "worker-digest", f"{'match' if match else 'mismatch'} {digest}")
    return 0 if passed else 1


def _reference_args(argv: list[str], *, hotkey: bool = False) -> tuple[list[str], str]:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--gateway")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--token-env")
    group.add_argument("--token-file")
    args, _ = parser.parse_known_args(argv)
    gateway, token = _settings(args)
    forwarded = list(argv)
    if not args.gateway:
        forwarded.extend(["--gateway", gateway])
    return (["register-hotkey", *forwarded] if hotkey else forwarded), token


def _reference_command(command: str, argv: list[str]) -> int:
    from neurons import miner as reference

    if "--help" in argv or "-h" in argv:
        parser = reference.build_parser(credential_defaults=True)
        parser.prog = "ormas-miner" if command == "register-hotkey" else "ormas-miner " + command
        parser.parse_args(["register-hotkey", *argv] if command == "register-hotkey" else argv)
    forwarded, token = _reference_args(argv, hotkey=command == "register-hotkey")
    if command != "register":
        return reference.main(forwarded, token=token)
    parser = reference.build_parser(credential_defaults=True)
    parser.prog = "ormas-miner register"
    args = parser.parse_args(forwarded)
    if args.command is not None:
        parser.error("use ormas-miner register-hotkey for hotkey registration")
    if not args.cell:
        parser.error("the following arguments are required: --cell")
    config = reference.MinerConfig(
        runner_id=args.runner_id,
        runner_version="ormas-subnet-reference",
        platform=f"{platform.system().lower()}-{platform.machine()}",
        capacity=args.capacity,
        cells=tuple(args.cell),
        workdir_root=Path(args.workdir_root),
        repo_id=args.repo_id or "",
        repo_url=args.repo_url or "",
        miner_id=args.miner_id,
    )
    with OrmasMinerClient(args.gateway, token) as client:
        miner = reference.MinerSkeleton(client, config, reference.make_shell_solver("true"))
        response = miner.register()
    print(json.dumps(response))
    print(reference.qualification_line(response), file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="ormas-miner", description="Set up and run a miner")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("command", choices=("login", "doctor", "protected-env", "register", "register-hotkey", "run"))
    if not argv or argv[0].startswith("-"):
        parser.parse_args(argv)
    command = parser.parse_args(argv[:1]).command
    try:
        if command == "protected-env":
            sub = _PrivateArgumentParser(prog="ormas-miner protected-env")
            sub.add_argument("--api-url", help="Gateway URL; defaults to saved credentials")
            sub.add_argument("--token-file", help="Private file holding the miner key")
            sub.add_argument("--miner-id", required=True)
            sub.add_argument("--runtime", choices=("development", "production"), required=True)
            sub.add_argument("--authorization-file", help="Expense authorization JSON object")
            sub.add_argument("--cell-bounds", help="Approved cell expense bounds")
            sub.add_argument("--approved-by", help="Authorization approver")
            sub.add_argument("--task-cells", action="append", required=True, help="Repeat for each task cell")
            sub.add_argument("--bind-project-id", required=True)
            sub.add_argument("--worker-image", required=True, help="Worker OCI image pinned by SHA-256")
            sub.add_argument("--worker-env-file", required=True, help="Private worker environment JSON object")
            sub.add_argument("--registry-auth-file", help="Optional private registry authentication file")
            sub.add_argument("--provider", action="append", default=[], help="Declared provider DNS host; repeatable")
            sub.add_argument("--pricing-template", action="store_true", help="Write pricing.json placeholders beside env")
            sub.add_argument("--out", required=True, help="Sealed env output path (mode 0600)")
            sub.add_argument("--force", action="store_true", help="Replace env and companion output files")
            return _protected_env(sub.parse_args(argv[1:]))
        if command in ("login", "doctor"):
            parser_class = _PrivateArgumentParser if command == "doctor" and "--protected" in argv else argparse.ArgumentParser
            sub = parser_class(prog=f"ormas-miner {command}")
            sub.add_argument("--gateway", help=f"Gateway URL (default: {DEFAULT_GATEWAY})")
            if command == "doctor":
                sub.add_argument("--protected", action="store_true", help="Read only this miner's gateway Protected status")
                sub.add_argument("--runner-id", help=(
                    "Registered miner ID; without --protected, queue diagnostics refresh last_seen "
                    "and mark a stopped miner live for the liveness window."
                ))
                sub.add_argument("--device-nonce", help="Registered device nonce, when bound")
            group = sub.add_mutually_exclusive_group()
            group.add_argument("--token-env", help="Environment variable holding the miner key")
            group.add_argument("--token-file", help="File holding the miner key")
            args = sub.parse_args(argv[1:])
            if command == "login":
                return _login(args)
            return _doctor_protected(args) if args.protected else _doctor(args)
        return _reference_command(command, argv[1:])
    except KeyboardInterrupt:
        print(f"{command}: interrupted", file=sys.stderr)
        return 130
    except (EOFError, getpass.GetPassWarning):
        print(f"{command}: secure input cancelled or unavailable", file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(f"{command}: {exc}", file=sys.stderr)
        return 1
    except OrmasGatewayError as exc:
        print(f"{command}: {exc}", file=sys.stderr)
        return 1
    except httpx.HTTPError as exc:
        print(f"{command}: gateway request failed ({type(exc).__name__})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
