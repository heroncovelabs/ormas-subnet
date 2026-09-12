"""Reference miner entry point.

Runs the reference skeleton with the shell solver so a miner operator can prove
the connect → bind → claim → solve → verify → publish → complete loop end to
end before plugging in real mining logic. Replace ``make_shell_solver`` with
your own ``solve(draft, workdir) -> SolveResult`` — that function is the miner.

    python neurons/miner.py --gateway https://api.ormas.ai --token-env ORMAS_MINER_TOKEN \
        --runner-id my-miner --repo-id <repo_id> --repo-url <https-or-ssh-url> \
        --cell task:code --solve-command 'make fix' [--ask-usd 1.20]

The token is read from an environment variable or a file, never from argv.
"""
from __future__ import annotations

import argparse
import platform
import sys
from pathlib import Path

from ormas_subnet import MinerConfig, MinerSkeleton, OrmasMinerClient, load_token
from ormas_subnet.reference_solver import make_shell_solver


def _parse(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Ormas subnet reference miner")
    ap.add_argument("--gateway", required=True, help="Gateway base URL, e.g. https://api.ormas.ai")
    tok = ap.add_mutually_exclusive_group(required=True)
    tok.add_argument("--token-env", help="Env var holding the miner token (conventionally ORMAS_MINER_TOKEN)")
    tok.add_argument("--token-file", help="File holding the runner token")
    ap.add_argument("--runner-id", required=True)
    ap.add_argument("--repo-id", required=True, help="Repo id the gateway bound for this project")
    ap.add_argument("--repo-url", required=True, help="Credential-free clone URL (https or ssh)")
    ap.add_argument("--cell", action="append", required=True, help="Task-type cell this miner serves, e.g. task:code (repeatable)")
    ap.add_argument("--workdir-root", default=str(Path.cwd() / "ormas-work"))
    ap.add_argument("--solve-command", required=True, help="Shell command the reference solver runs in the workdir")
    ap.add_argument("--ask-usd", type=float, default=None, help="Firm ask sent with every claim; omit to let the gateway derive one")
    ap.add_argument("--no-push", action="store_true", help="Record a local: ref instead of pushing the result branch")
    ap.add_argument("--once", action="store_true", help="Run one claim cycle and exit")
    ap.add_argument("--capacity", type=int, default=1)
    ap.add_argument("--bind-project", help="Project id to bind this repo to (same-tenant miners); third-party miners omit this and claim opted-in projects directly")
    ap.add_argument("--bind-base-commit", help="Base commit for --bind-project")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)
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
