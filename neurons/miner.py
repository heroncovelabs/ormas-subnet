"""Reference miner entry point.

Runs the reference skeleton with the shell solver so a miner operator can prove
the connect → bind → claim → solve → verify → publish → complete loop end to
end before plugging in real mining logic. Replace ``make_shell_solver`` with
your own ``solve(draft, workdir) -> SolveResult`` — that function is the miner.

    python neurons/miner.py --gateway https://api.ormas.ai --token-env ORMAS_MINER_TOKEN \
        --runner-id runr_0123456789ab --repo-id <repo_id> --repo-url <https-or-ssh-url> \
        --cell task:code --solve-command 'make fix' [--ask-usd 1.20]

``--runner-id``: omit on first run — the gateway assigns it (`runr_<12hex>`)
and it is printed (stderr ``runner_id=<assigned>``); pass it on later runs.
A self-chosen id the gateway never issued to your token is refused 404.

The token is read from an environment variable or a file, never from argv.

Subcommand ``register-hotkey`` records the miner's chain hotkey mapping on the
gateway (challenge → sign → verify). The hotkey never enters this package —
``--sign-command`` names an external program that reads the challenge bytes on
stdin and prints the signature hex on stdout, so the key stays in the miner's
own tooling (e.g. a script wrapping a Bittensor wallet ``Keypair.sign``):

    python neurons/miner.py register-hotkey --gateway https://api.ormas.ai \
        --token-env ORMAS_MINER_TOKEN --runner-id my-miner \
        --hotkey-ss58 <ss58> --sign-command 'my-signer --hotkey alice'
"""
from __future__ import annotations

import argparse
import json
import platform
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx

from ormas_subnet import MinerConfig, MinerSkeleton, OrmasMinerClient, load_token
from ormas_subnet.client import OrmasGatewayError
from ormas_subnet.reference_solver import make_shell_solver

_TOKEN_HELP_ENV = "Env var holding the miner token (conventionally ORMAS_MINER_TOKEN)"
_TOKEN_HELP_FILE = "File holding the runner token"

# Args the run path requires; enforced in _parse so the register-hotkey
# subcommand can omit them (argparse required= applies even under a subcommand).
_RUN_REQUIRED = ("gateway", "repo_id", "repo_url", "cell", "solve_command")


def _add_token_group(
    ap: argparse.ArgumentParser, *, required: bool, credential_defaults: bool = False,
) -> None:
    tok = ap.add_mutually_exclusive_group(required=required)
    default = " (default: ORMAS_MINER_TOKEN or saved key)" if credential_defaults else ""
    tok.add_argument("--token-env", help=_TOKEN_HELP_ENV + default)
    tok.add_argument("--token-file", help=_TOKEN_HELP_FILE + default)


def build_parser(*, credential_defaults: bool = False) -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Ormas subnet reference miner")
    gateway_help = "Gateway base URL, e.g. https://api.ormas.ai"
    if credential_defaults:
        gateway_help += " (default: saved gateway or https://api.ormas.ai)"
    ap.add_argument("--gateway", help=gateway_help)
    _add_token_group(ap, required=False, credential_defaults=credential_defaults)
    ap.add_argument(
        "--runner-id", default="",
        help="This miner's gateway-assigned id (runr_<12hex>); omit on first run — "
        "the gateway assigns it and it is printed; pass it on later runs",
    )
    ap.add_argument(
        "--miner-id", default=None,
        help="Public miner identity (lowercase [a-z0-9-], 3-40 chars); must match your "
        "approved miner slot for the self-serve qualification job",
    )
    ap.add_argument("--repo-id", help="Repo id the gateway bound for this project")
    ap.add_argument("--repo-url", help="Credential-free clone URL (https or ssh)")
    ap.add_argument(
        "--cell", action="append",
        help="Task-type cell this miner serves, e.g. task:code (repeatable)",
    )
    ap.add_argument("--workdir-root", default=str(Path.cwd() / "ormas-work"))
    ap.add_argument("--solve-command", help="Shell command the reference solver runs in the workdir")
    ap.add_argument(
        "--ask-usd", type=float, default=None,
        help="Firm ask sent with every claim; omit to let the gateway derive one",
    )
    ap.add_argument(
        "--no-push", action="store_true",
        help="Record a local: ref instead of pushing the result branch",
    )
    ap.add_argument("--once", action="store_true", help="Run one claim cycle and exit")
    ap.add_argument("--capacity", type=int, default=1)
    ap.add_argument(
        "--bind-project",
        help="Project id to bind this repo to (same-tenant miners); "
        "third-party miners omit this and claim opted-in projects directly",
    )
    ap.add_argument("--bind-base-commit", help="Base commit for --bind-project")

    sub = ap.add_subparsers(dest="command")
    hk = sub.add_parser(
        "register-hotkey",
        help="Record the chain hotkey mapping for this miner on the gateway",
        description="Register the miner's chain hotkey: the gateway mints a one-time "
        "challenge, --sign-command signs it (challenge bytes on stdin, signature hex "
        "on stdout — the key stays in your own tooling), and the gateway verifies and "
        "records the mapping.",
    )
    hk.add_argument("--gateway", required=not credential_defaults, help=gateway_help)
    _add_token_group(hk, required=not credential_defaults, credential_defaults=credential_defaults)
    hk.add_argument("--runner-id", required=True)
    hk.add_argument("--hotkey-ss58", required=True, help="SS58 address of the hotkey to register")
    hk.add_argument(
        "--sign-command", required=True,
        help="Shell command that reads the challenge bytes on stdin and prints the "
        "sr25519 signature hex on stdout (e.g. a script wrapping a Bittensor wallet "
        "Keypair.sign); the key never enters this package",
    )
    return ap


def qualification_line(response: Any) -> str:
    """One line a miner can act on, from the registration's ``qualification`` key.

    The gateway adds the key only on a slot-approved cap-0 token with the
    self-serve proof configured; its absence is reported as such.
    """
    qual = response.get("qualification") if isinstance(response, dict) else None
    if not isinstance(qual, dict):
        return "qualification: none (no reserved proof job for this registration)"
    status = qual.get("status")
    if status == "enqueued":
        return f"qualification: proof job {qual.get('job_id')} reserved for you; keep running"
    if status == "pending":
        return f"qualification: proof job {qual.get('job_id')} already in progress"
    if status == "cells_missing":
        missing = " ".join(str(c) for c in qual.get("missing") or [])
        return f"qualification: add cells and re-register: {missing}".rstrip()
    if status == "error":
        return f"qualification: gateway refused ({qual.get('error')}); contact ops@ormas.ai"
    return f"qualification: {status}"


def _parse(
    argv: list[str] | None = None, *, credential_defaults: bool = False,
) -> argparse.Namespace:
    ap = build_parser(credential_defaults=credential_defaults)
    ns = ap.parse_args(argv)
    if ns.command is None:
        missing = [f"--{name.replace('_', '-')}" for name in _RUN_REQUIRED if not getattr(ns, name)]
        if missing:
            ap.error("the following arguments are required: " + ", ".join(missing))
        if not credential_defaults and not (ns.token_env or ns.token_file):
            ap.error("one of the arguments --token-env --token-file is required")
    return ns


def _run_register_hotkey(args: argparse.Namespace, *, token: str | None = None) -> int:
    if token is None:
        token = load_token(token_env=args.token_env, token_path=args.token_file)

    def sign_fn(challenge_bytes: bytes) -> str:
        proc = subprocess.run(
            shlex.split(args.sign_command),
            input=challenge_bytes,
            capture_output=True,
            check=True,
        )
        return proc.stdout.decode("utf-8").strip()

    with OrmasMinerClient(base_url=args.gateway, token=token) as client:
        result = client.register_hotkey(
            args.runner_id, hotkey_ss58=args.hotkey_ss58, sign_fn=sign_fn,
        )
    print(json.dumps(result))
    return 0


def main(argv: list[str] | None = None, *, token: str | None = None) -> int:
    args = _parse(argv, credential_defaults=token is not None)
    if args.command == "register-hotkey":
        try:
            return _run_register_hotkey(args, token=token)
        except Exception as exc:  # noqa: BLE001 - surface any failure with its message
            if token is not None and isinstance(exc, (OrmasGatewayError, httpx.HTTPError)):
                raise
            print(f"register-hotkey: {exc}", file=sys.stderr)
            return 1
    if token is None:
        token = load_token(token_env=args.token_env, token_path=args.token_file)
    client = OrmasMinerClient(base_url=args.gateway, token=token)
    config = MinerConfig(
        runner_id=args.runner_id,
        runner_version="ormas-subnet-reference",
        platform=f"{platform.system().lower()}-{platform.machine()}",
        capacity=args.capacity,
        cells=tuple(args.cell),
        workdir_root=Path(args.workdir_root),
        repo_id=args.repo_id,
        repo_url=args.repo_url,
        push_remote=None if args.no_push else "origin",
        ask_usd=args.ask_usd,
        miner_id=args.miner_id,
    )
    miner = MinerSkeleton(client, config, make_shell_solver(args.solve_command))
    response = miner.register()
    # The gateway assigns the runner id on first registration; surface it so the
    # operator can pass --runner-id on later runs. Echo miner_id on the same
    # line when the gateway bound a chosen public identity.
    line = f"runner_id={miner.config.runner_id}"
    echoed = response.get("miner_id") if isinstance(response, dict) else None
    if echoed:
        line += f" miner_id={echoed}"
    print(line, file=sys.stderr)
    print(qualification_line(response), file=sys.stderr)
    if args.bind_project:
        if not args.bind_base_commit:
            ap_err = "--bind-base-commit is required with --bind-project"
            print(ap_err, file=sys.stderr)
            return 2
        miner.bind(project_id=args.bind_project, base_commit=args.bind_base_commit)
    if args.once:
        return 0 if miner.run_once() else 3
    miner.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
