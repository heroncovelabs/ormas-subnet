# Public tasks and supported environments

This is the September 23 development candidate. Installing it does not activate a
miner, grant a profile qualification, or change the production gateway. The candidate
is scoped to bounded changes to public GitHub repositories. Private-code protection
and confidential-computing guarantees are deferred.

A client supplies the requested change, allowed files, spending limit and named
automated check. A miner offers a firm price or a limit for the whole task. The
client's coding agent selects an environment and prepares the task.
Preparation freezes the dependency locks, acceptance files, limits and verifier,
then proves that the base fails the intended assertion. Ormas generates the scratch
execution wrapper. The client does not write or paste a work packet.

## What the profiles cover

The versioned [support catalog](../ormas_subnet/client_assets/outcomes_support.v1.json)
is the authority for exact images, versions, limits and status. The website's support
page is generated from that same catalog. A development profile is not evidence of
available production capacity.

| Profile | Observable behavior | Frozen dependencies |
| --- | --- | --- |
| `linux-python-pytest-v1` | A Python external test checks a candidate command's output and exit status. | Hashed `requirements.lock` |
| `linux-node-test-v1` | A Node test checks a candidate command's output and exit status. | `package.json` and `package-lock.json` |
| `linux-node-browser-v1` | A Playwright test checks rendered command output. | Exact npm locks and the catalog's browser driver |
| `linux-node-browser-http-v1` | A Playwright test interacts with an actual Node HTTP app. | Exact npm locks, browser driver and one declared service |

The qualified development host is a nonroot Linux/amd64 user with Docker. Locked
public dependencies are collected before verification; verification has no general
internet access. A language name alone does not qualify every framework, package or
native extension. Symlinks, submodules, private registries, undeclared system
dependencies, GPUs and additional services need a different qualified profile.

The candidate and acceptance driver run in separate containers. Candidate code may
produce output or HTTP responses; it cannot write the driver's test report. Ordinary
unit suites are useful development checks, but importing candidate code into the
acceptance process does not establish this independent boundary.

## Named checks

For a Python CLI, a frozen check can use the supplied observation helper:

```python
from ormas_acceptance import run

def test_normalized_name():
    result = run(["python3", "cli.py", "  ALICE  "])
    assert result["returncode"] == 0
    assert result["output"].strip() == "alice"
```

Every failing case must fail at a plain assert (AssertionError). An exception,
an import error, or extra output mixed into the result is refused as not an
assertion, and the miner does no work.

The client names `python3 -m pytest tests/test_cli.py`. Its agent supplies the
committed lock and profile selection. Ormas installs the helper in the isolated
driver. A relative Python interpreter still requires the existing explicit toolchain
declaration; wrapping does not bypass that check.

For the HTTP profile, the selection additionally declares one service, for example
`{"argv": ["node", "server.cjs"], "port": 3000}`. The service entrypoint is a
relative `.js`, `.cjs` or `.mjs` file. It must listen on the declared `PORT` and be
ready within 20 seconds. Use the browser fixture without an existing page/context:

```javascript
const {test, expect} = require('@playwright/test');
const {openApp} = require('ormas_acceptance');

test('loads the requested value', async ({browser}) => {
  const page = await openApp(browser);
  await page.getByRole('button', {name: 'Load value'}).click();
  await expect(page.locator('#value')).toHaveText('1');
});
```

The client names that Playwright file. `openApp` connects to the frozen
`http://ormas-app.invalid` origin through Ormas's fixed observation channel. The app
has no published host port and shares no network with the driver. HTTP responses and
ordinary page interactions are supported; external sites, TLS, WebSockets,
downloads, service workers, non-HTTP document navigation and streaming are outside
this initial profile. Limits include 128 observations, 32 KiB request bodies and
16 KiB headers; the catalog also bounds time, source size, memory and total output.

Missing dependencies, unsupported behavior, service startup failures and checker
timeouts are neutral setup failures. They cannot establish fail-on-base or count as
miner overclaim. A completed assertion failure is different: the environment ran
the agreed check and the candidate did not satisfy it.

## Miner selection and validator acceptance

Before offering, miners see the privacy-safe queue envelope listed in
[protocol.md](protocol.md). It includes task shape and the
service level and repository visibility when available. The full draft,
publication protocol and exact acceptance policy arrive after claim. Miners
choose which types and sizes they serve. All required task cells must match,
including every declared language; advertising only `task:code` does not grant
universal bounded-packet (public-profile) support. For a small Python task, the task cells include `task:code`,
`task:code/small` and `task:lang/python`. The current SDK also advertises its public
publication and independent-acceptance protocols when language cells are present.

In the [installation example](INSTALL.md#run-the-skeleton-with-the-reference-solver),
set `cells=("task:code", "task:code/small", "task:lang/python")` for that task class.
Keep the gateway-assigned identity on restart and the same private work directory.
The draft's public repository URL supplies the clone source; no repository bind or
client write credential is needed. Replace the reference solver with your own
implementation before offering real work. Ask the gateway operator to qualify the
specific profiles your host and solver support; a broader cell list alone cannot
enable them.

A registration or heartbeat is not a qualification. The gateway must have an
unexpired proof tied to the miner's credential, exact catalog and profile, and
independently qualified checkers with available capacity. Current qualifications
allow one assignment per actor. Onboarding remains by invitation during this pilot.
The miner's per-job model choice and routing remain its own implementation.

The gateway freezes the policy at admission and checker identities at claim.
V1 requires checker operators to differ from the miner and one another. V2 is an
explicit **operator-run alpha**: one qualified validator belonging to the operator
named in the policy reruns the tests and signs the result. That operator may also
own the miner. This does not establish independent ownership or control. Each
validator reproduces base failure, checks candidate scope and reruns the frozen
verifier. Losing it cannot reduce the required count; deadline and no-charge rules
still apply.

V2 and v3 need separately approved protocol qualifications. A miner must explicitly add
the cell for the policy the gateway runs (`--cell` on the reference miner, `--task-cell`
on the operator miner): `task:acceptance/operator-run-v2` for v2, or
`task:acceptance/operator-run-v3` for v3, where checker capacity is an operator-wide slot
total on the qualification (`ormas.public-acceptance-contract.v3`). `api.ormas.ai` runs
`operator-run-v3`; a miner advertising only the v2 cell is excluded before bidding. The
SDK does not opt miners into these terms automatically. Standing asks and per-job offers convey policy-class consent; the
full per-job owner and timeout arrive in the draft. The queue envelope omits the
exact acceptance policy. The controlled operator pilot can use its known terms.
General third-party bidding still needs pre-bid disclosure of that policy before
claiming that all terms were individually quoted.

## Repository access, publication and recovery

For bounded-packet (public-profile) jobs, public-repository jobs clone anonymously;
private-repository jobs use a per-job read credential for both miners and validators.
They do not receive the client's saved GitHub write token. The miner uploads a
bounded artifact to the gateway, which
publishes the canonical result branch using the client's saved project access. The
SDK independently fetches and checks the published commit, exact tree and sole base
parent before completing. A changed or ambiguous publication is not replaced with a
different result under the same task identity.

Keep a private, persistent `workdir_root`. The SDK records the claim, artifact and
exact completion in an atomic journal and resolves it before claiming another task.
The saved lease includes its accepted offer, and a limit completion saves the exact
settled price. Restart resumes recoverable work without solving it again. Replaying
a saved completion keeps its exact settled price and calls no pricing callback;
recovery from the artifact stage reuses the saved result and calls the deterministic
`settle_fn` again.
An interruption before the artifact was saved reports an unknown outcome; it does
not fabricate a zero-cost receipt. See [restart recovery](INSTALL.md#restart-and-completion-recovery).

The client can reconnect using its job identifier and reuse the saved project for
the next task. Its job response shows `acceptance_policy`, including the alpha mode
and validator operator. Completion remains pending until the frozen acceptance
policy is satisfied. The gateway is the receipt and settlement authority.

New languages, interfaces and environments expand through versioned qualifications.
An in-flight task keeps its frozen catalog and checker policy. Changing its check or
environment requires a new agreement.

## Offers and payment

Phase 1 accepts on arrival: the first offer within the client's undisclosed
spending limit wins; an offer above it is recorded and skipped and the job stays
queued. A firm offer charges exactly
`price_usd`; a limit offer states `estimate_usd` and `limit_usd`
(`0 < estimate_usd <= limit_usd`) and charges exactly the miner's
`settled_price_usd`, at or below `limit_usd`, after validator acceptance. Failed deliveries are unpaid.
Legacy asks keep their firm-price settlement.

Estimate the expected cost of your usual recovery chain plus margin, and set the
limit at your worst-case chain. Settle at actual metered chain cost plus margin within that ceiling; you bear any
excess. The SDK caps your `settle_fn` result at the limit and defaults to the
limit when that callback is unset. The hook must be deterministic and must not
raise. A raising hook never reprices the delivery: on the public (bounded-packet)
path the published work is held for recovery, no completion is sent, and every
later poll re-raises until the hook returns a valid price; only a task without a
public packet completes as `failed`. See
[economics.md](economics.md) for an example.
[History-based ranking](protocol.md#offer-ranking) and chain emissions are planned.

Standard/Protected is the Ormas service level. Public/private repository is
GitHub visibility; each service level has its own requirements.
