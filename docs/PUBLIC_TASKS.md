# Public tasks and supported environments

This is the September 23 development candidate. Installing it does not activate a
miner, grant a profile qualification, or change the production gateway. The candidate
is scoped to bounded changes to public GitHub repositories. Private-code protection
and confidential-computing guarantees are deferred.

A client supplies the requested change, allowed files, price limit and named
automated check. Its coding agent selects an environment and prepares the task.
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

## Miner selection and independent acceptance

The prepared task exposes its environment, interface, limits, languages, scope,
publication protocol and acceptance policy before a claim. Miners choose which
types and sizes they serve. All required task cells must match, including every
declared language; advertising only `task:code` does not grant universal public-task
support. For a small Python task, the task cells include `task:code`,
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

The gateway freezes the checker count and identities at claim time. Checker
operators must differ from the miner operator and from one another. Each checker
validates the contract, reproduces base failure, checks candidate scope, reruns the
frozen verifier and signs its decision. Public operator-miner work has the same
independent-acceptance requirement. Losing a checker cannot silently reduce the
required count; the existing deadline and neutral-failure rules apply.

## Repository access, publication and recovery

Public miners and checkers clone anonymously. They do not receive the client's
saved GitHub write token. The miner uploads a bounded artifact to the gateway, which
publishes the canonical result branch using the client's saved project access. The
SDK independently fetches and checks the published commit, exact tree and sole base
parent before completing. A changed or ambiguous publication is not replaced with a
different result under the same task identity.

Keep a private, persistent `workdir_root`. The SDK records the claim, artifact and
exact completion in an atomic journal and resolves it before claiming another task.
Restarting the ordinary miner replays recoverable work without another solver call.
An interruption before the artifact was saved reports an unknown outcome; it does
not fabricate a zero-cost receipt. See [restart recovery](INSTALL.md#restart-and-completion-recovery).

The client can reconnect using its job identifier and reuse the saved project for
the next task. Completion remains pending until the frozen independent acceptance
policy is satisfied. The gateway is the receipt and settlement authority.

New languages, interfaces and environments expand through versioned qualifications.
An in-flight task keeps its frozen catalog and checker policy. Changing its check or
environment requires a new agreement. This document does not alter miner payment or
emissions policy; those owner-controlled terms remain separate.
