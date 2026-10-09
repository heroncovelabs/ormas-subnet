# Measured Protected miner recipe

These four files travel together in each published release:
- `docker-compose.yml` pins the public miner image by digest.
- `app_compose_wrapper.json` carries the measured Phala wrapper and pre-launch script.
- `manifest.json` lists the admitted OS measurements and compose hashes.
- `compose_hash.py` computes the hash and checks the manifest.

**Check before you pay:** run `python3 compose_hash.py check` here.
`hash`, `app-compose` and `check` apply outside the monorepo; `pin` and `digests` are Ormas operator steps.
Follow [Run a Protected miner on your own confidential VM](../../docs/PROTECTED.md).
