"""Local setup and reference miner commands."""
from __future__ import annotations

import argparse
import getpass
import json
import os
import platform
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
                with OrmasMinerClient(gateway, token, http_client=transport) as client:
                    client.list_queue(_AUTH_PROBE_ID)
            check("token", True, "key accepted (account enablement not confirmed)")
        except OrmasGatewayError as exc:
            # A typed lookup miss follows authentication in the queue protocol.
            authenticated = exc.status_code == 404 and exc.error_type == "not_found_error"
            reason = ("key accepted (account enablement not confirmed)" if authenticated
                      else f"gateway rejected probe (HTTP {exc.status_code})")
            check("token", authenticated, reason)
        except (httpx.HTTPError, OSError, ValueError):
            check("token", False, "authentication probe failed")
    else:
        check("token", False, "authentication probe requires credentials and gateway health")
    local_bin = str(Path.home() / ".local" / "bin")
    on_path = local_bin in os.environ.get("PATH", "").split(os.pathsep)
    print(f"{'ok' if on_path else 'warn'} PATH: {local_bin} "
          f"{'is on PATH' if on_path else 'is missing from PATH'}")
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
    )
    with OrmasMinerClient(args.gateway, token) as client:
        miner = reference.MinerSkeleton(client, config, reference.make_shell_solver("true"))
        response = miner.register()
    print(json.dumps(response))
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="ormas-miner", description="Set up and run a miner")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("command", choices=("login", "doctor", "register", "register-hotkey", "run"))
    if not argv or argv[0].startswith("-"):
        parser.parse_args(argv)
    command = parser.parse_args(argv[:1]).command
    try:
        if command in ("login", "doctor"):
            sub = argparse.ArgumentParser(prog=f"ormas-miner {command}")
            sub.add_argument("--gateway", help=f"Gateway URL (default: {DEFAULT_GATEWAY})")
            group = sub.add_mutually_exclusive_group()
            group.add_argument("--token-env", help="Environment variable holding the miner key")
            group.add_argument("--token-file", help="File holding the miner key")
            args = sub.parse_args(argv[1:])
            return _login(args) if command == "login" else _doctor(args)
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
