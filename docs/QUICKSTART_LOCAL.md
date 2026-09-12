# Quickstart: run the whole loop locally, offline, no token

This page is for an outside miner operator who has never spoken to us and has
no gateway token yet. It runs the full register → bind → claim → solve →
verify → publish → complete loop, entirely on your own machine, against
[`ormas_subnet/localnet.py`](../ormas_subnet/localnet.py)'s `LocalGateway` — an
in-process, offline stand-in for `/api/runner/v1`. No network host is
contacted anywhere in this page.

## What this proves

- **The full loop actually runs**, start to finish, with real git: a bare
  "client repository" is created, a job is seeded on it, a solver clones it,
  edits it, commits, and the result branch is published back to that
  repository.
- **Completion is honest, not self-declared.** `verification_state` comes from
  actually running `verify_command`; `scope_ok` comes from a real `git diff`
  against `allowed_paths`, never a solver's say-so. Two of the three demo
  scenarios below deliberately fail one of these checks and are honestly not
  paid.
- **Scope enforcement.** A solver that edits a file outside `allowed_paths` is
  caught even when the verifier itself would have passed.

## What this does NOT prove

- **No validators.** `LocalGateway` derives settlement the way the production
  gateway does *today*, for our own trusted miner: from the miner's own
  reported `verification_state` plus the scope check. It does not run the
  reference validator's independent re-check, and it does not implement
  validator-quorum settlement (see `README.md` "Known gaps" and
  `docs/DECISIONS.md` §8 for that path).
- **No pricing.** `LocalGateway`'s `outcome_price_usd` / `customer_billed_usd`
  are fixed demo numbers. Nothing here reflects real firm-ask pricing.
- **No real gateway.** `LocalGateway` has no auth, no persistence beyond one
  process, and hands out exactly one seeded job. It is a protocol-shape
  double, not a reimplementation of the gateway.

## Run it

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e .
python neurons/localnet_demo.py
```

Default `--workdir` is `~/.ormas/localnet-demo/` (created `0700`) — never
`/tmp`, which is world-readable, periodically purged, and shared with every
other user on the box; the wrong place even for a throwaway demo repo.

Expected output (commit shas will differ). Settlement uses the same wire
vocabulary as the real gateway (`docs/protocol.md` § "Settlement
derivation") — `"paid"` or `"no_delivery"` with a `failure_class`, never
`"unpaid"`:

```
scenario:            pass
verification_state:  verified
scope_ok:            True
result_commit:       5be65faca8dd5652f016071f06a8e1540ee73929
changed_paths:       ['out.txt']
settlement:          paid
failure_class:       None
customer_billed_usd: 0.05
```

Exit code `0`.

## The two failure scenarios

```bash
python neurons/localnet_demo.py --scenario noop
python neurons/localnet_demo.py --scenario out-of-scope
```

`noop` — the solver does nothing; `verify_command` still fails against the
untouched base content:

```
scenario:            noop
verification_state:  failed
scope_ok:            True
result_commit:       f9908f7caac539d29b5962b4a09221590de6212f
changed_paths:       []
settlement:          no_delivery
failure_class:       failed
customer_billed_usd: 0.0
```

`out-of-scope` — the solver makes `verify_command` pass, but also edits a file
outside `allowed_paths`; the real `git diff` catches it regardless. This
reference skeleton reports a binary `verification_state` (`verified`/`failed`)
rather than the richer `scope_violation` state, so `failure_class` here is
derived from that state rather than naming `scope_violation` directly —
`scope_ok: False` is the field that actually caught it:

```
scenario:            out-of-scope
verification_state:  failed
scope_ok:            False
result_commit:       5724780da477c0810c192316cc363f05092ddfc0
changed_paths:       ['other.txt', 'out.txt']
settlement:          no_delivery
failure_class:       failed
customer_billed_usd: 0.0
```

Both exit `0` too — the script's exit code reflects whether the scenario
settled the way it is *supposed to* (`paid` for `pass`, `no_delivery` for the
other two), not whether the delivery itself was accepted.

## Smoke tests

Exactly as `docs/INSTALL.md` states them:

```bash
pip install -e . pytest
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider tests
```

`tests/test_localnet_demo.py` drives all three scenarios above in-process (no
shelling out) and asserts the settlement each one produces.

## When you are ready for a real gateway

This page has no token, no project id, no base commit from an operator — it
makes all of that up locally. When you have real ones, see
[`docs/INSTALL.md`](INSTALL.md) to connect to an actual gateway, and
[`docs/CONTRACT.md`](CONTRACT.md) for the commercial and acceptance terms that
govern what "accepted delivery" and payment mean there.
