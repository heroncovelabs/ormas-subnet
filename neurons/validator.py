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

import sys

from ormas_subnet.validator import main


if __name__ == "__main__":
    sys.exit(main())
