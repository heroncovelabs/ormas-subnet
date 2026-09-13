# Security

Report vulnerabilities in this repository, the `ormas-runner-v1` protocol, or the Ormas gateway
(`api.ormas.ai`) to **operations@heroncove.us** — the same monitored address as the SN76 subnet
identity. Please do not open a public issue for a security report.

Include what you found, how to reproduce it, and the version or commit you tested. We acknowledge
reports within three business days and tell you what we intend to do; we will credit you in the fix
unless you ask otherwise. There is no bug bounty programme at this time.

## Scope

- This repository: protocol types, thin client, reference miner skeleton, reference validator.
- The gateway routes this client speaks to (`/api/runner/v1`, `/v1/public/*`).

Out of scope: third-party gateways operated against SN76 miners (see `README.md`, "Other
gateways"), and the miner or validator software you run yourself.

## What never belongs in this repository

Tokens, keys, customer repository names, or private deployment details. The client reads its token
from an environment variable or a file only (`ormas_subnet.client.load_token`); tests use the fixed
fixture `ormr_test`. If you find a real credential in the history, report it to the address above
before anything else.
