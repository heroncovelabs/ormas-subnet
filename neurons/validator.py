"""Reference validator entry point.

Runs the reference validator daemon: poll an assignment, clone the job's repo,
independently re-run the verify command on base (expect non-zero) and result
(expect zero), check scope via ``git diff --name-only``, sign, and post the
decision. This is the untrusted-perimeter twin of ``miner.py`` — it never
trusts the miner's self-report, only its own clone + verify run.

    python neurons/validator.py --gateway https://api.ormas.ai \
        --token-env ORMAS_VALIDATOR_TOKEN --private-key-env ORMAS_VALIDATOR_KEY \
        [--once]

Both the bearer token and the Ed25519 private key (hex) are read from an
environment variable or a file, never from argv.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from ormas_subnet import OrmasValidatorClient, ValidatorConfig, ValidatorDaemon, make_ed25519_signer
from ormas_subnet.client import load_token


def _parse(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Ormas subnet reference validator")
    ap.add_argument("--gateway", required=True, help="Gateway base URL, e.g. https://api.ormas.ai")
    tok = ap.add_mutually_exclusive_group(required=True)
    tok.add_argument("--token-env", help="Env var holding the validator token")
    tok.add_argument("--token-file", help="File holding the validator token")
    key = ap.add_mutually_exclusive_group(required=True)
    key.add_argument("--private-key-env", help="Env var holding the hex Ed25519 private key")
    key.add_argument("--private-key-file", help="File holding the hex Ed25519 private key")
    ap.add_argument("--workdir-root", default=str(Path.cwd() / "ormas-validator-work"))
    ap.add_argument("--once", action="store_true", help="Review one assignment (if any) and exit")
    ap.add_argument("--poll-interval-s", type=float, default=15.0, help="Idle poll interval for --forever")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)
    token = load_token(token_env=args.token_env, token_path=args.token_file)
    private_key_hex = load_token(token_env=args.private_key_env, token_path=args.private_key_file)
    sign_fn, pubkey_hex = make_ed25519_signer(private_key_hex)

    client = OrmasValidatorClient(base_url=args.gateway, token=token)
    daemon = ValidatorDaemon(
        client, ValidatorConfig(workdir_root=Path(args.workdir_root)), sign_fn,
    )
    daemon.register(pubkey_hex=pubkey_hex)

    if args.once:
        return 0 if daemon.run_once() else 3
    while True:
        if not daemon.run_once():
            time.sleep(args.poll_interval_s)


if __name__ == "__main__":
    sys.exit(main())
