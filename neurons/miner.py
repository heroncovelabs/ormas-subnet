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

from ormas_subnet import MinerConfig, MinerSkeleton, OrmasMinerClient, load_token
from ormas_subnet.reference_solver import make_shell_solver

_TOKEN_HELP_ENV = "Env var holding the miner token (conventionally ORMAS_MINER_TOKEN)"
_TOKEN_HELP_FILE = "File holding the runner token"

# Args the run path requires; enforced in _parse so the register-hotkey
# subcommand can omit them (argparse required= applies even under a subcommand).
_RUN_REQUIRED = ("gateway", "repo_id", "repo_url", "cell", "solve_command")


def _add_token_group(ap: argparse.ArgumentParser, *, required: bool) -> None:
    tok = ap.add_mutually_exclusive_group(required=required)
    tok.add_argument("--token-env", help=_TOKEN_HELP_ENV)
    tok.add_argument("--token-file", help=_TOKEN_HELP_FILE)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Ormas subnet reference miner")
    ap.add_argument("--gateway", help="Gateway base URL, e.g. https://api.ormas.ai")
    _add_token_group(ap, required=False)
    ap.add_argument(
        "--runner-id", default="",
        help="This miner's gateway-assigned id (runr_<12hex>); omit on first run — "
        "the gateway assigns it and it is printed; pass it on later runs",
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
    hk.add_argument("--gateway", required=True, help="Gateway base URL, e.g. https://api.ormas.ai")
    _add_token_group(hk, required=True)
    hk.add_argument("--runner-id", required=True)
    hk.add_argument("--hotkey-ss58", required=True, help="SS58 address of the hotkey to register")
    hk.add_argument(
        "--sign-command", required=True,
        help="Shell command that reads the challenge bytes on stdin and prints the "
        "sr25519 signature hex on stdout (e.g. a script wrapping a Bittensor wallet "
        "Keypair.sign); the key never enters this package",
    )
    return ap


def _parse(argv: list[str] | None = None) -> argparse.Namespace:
    ap = build_parser()
    ns = ap.parse_args(argv)
    if ns.command is None:
        missing = [f"--{name.replace('_', '-')}" for name in _RUN_REQUIRED if not getattr(ns, name)]
        if missing:
            ap.error("the following arguments are required: " + ", ".join(missing))
        if not (ns.token_env or ns.token_file):
            ap.error("one of the arguments --token-env --token-file is required")
    return ns


def _run_register_hotkey(args: argparse.Namespace) -> int:
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


def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)
    if args.command == "register-hotkey":
        try:
            return _run_register_hotkey(args)
        except Exception as exc:  # noqa: BLE001 - surface any failure with its message
            print(f"register-hotkey: {exc}", file=sys.stderr)
            return 1
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
    )
    miner = MinerSkeleton(client, config, make_shell_solver(args.solve_command))
    miner.register()
    # The gateway assigns the runner id on first registration; surface it so the
    # operator can pass --runner-id on later runs.
    print(f"runner_id={miner.config.runner_id}", file=sys.stderr)
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
