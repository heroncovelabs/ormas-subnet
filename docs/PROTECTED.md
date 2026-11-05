# Run a Protected miner on your own confidential VM

Protected runs your miner inside an Intel TDX confidential virtual machine (CVM)
that you rent on Phala Cloud, using the Ormas-published measured compose. The
gateway releases the per-job repository credential only when the CVM's hardware
attestation matches the published manifest. This protects the execution workspace
from the cloud host. External model providers receive the source included in
inference requests under their own data-handling terms; your provider setup must
meet the client's data policy. Repository visibility is public or private;
Standard and Protected are Ormas service levels. A private repository at Protected
uses the attested credential-release path.

## What you need

- An [ormas.ai](https://ormas.ai) account and an `ormr_…` key from **Miner → Runner keys**.
  Save the key privately. The [install guide](INSTALL.md) covers account setup,
  hotkey registration and the Standard onboarding path.
- Your own Phala Cloud account, billing and API key. The commands below use
  `phala` CLI **1.1.22**, Python 3, `curl`, `jq` and Bash.
- Your own digest-pinned worker image, provider API keys and approved expense
  bounds. Confirm the worker digest and CONNECT host policy or relay policy with
  ops during slot approval. Keep keys and relay policy contents private.
- A **Protected slot** approved by Ormas. Email
  [ops@ormas.ai](mailto:ops@ormas.ai) with your miner name and say **Protected**.
  Include the onboarding details in [Mining on Ormas](https://ormas.ai/docs/miners).
- Your own Ormas project id (`proj_…`) for first-boot binding. The measured
  entrypoint creates a seed repository; job checkouts use their released credentials.

Read the [contract](CONTRACT.md) before offering work. Before renting a CVM,
complete slot approval and have **worker policy and expense bounds confirmed by ops**.
Prepare your private keys and approved expense bounds.

## 1. Download one measured release

Ask for the published release commit with your slot approval. Fetch the recipe
from `protected/miner/` in
[heroncovelabs/ormas-subnet](https://github.com/heroncovelabs/ormas-subnet).
Use the same commit for the compose, hash tool, wrapper and manifest:

```bash
release_ref='<published-commit>'
base=https://raw.githubusercontent.com/heroncovelabs/ormas-subnet/$release_ref/protected/miner
mkdir -p protected-miner
cd protected-miner
curl -fsS "$base/docker-compose.split.yml" -o docker-compose.split.yml
curl -fsS "$base/compose_hash.py" -o compose_hash.py
curl -fsS "$base/app_compose_wrapper.json" -o app_compose_wrapper.json
curl -fsS "$base/manifest.json" -o manifest.json
jq -j '.pre_launch_script' app_compose_wrapper.json > prelaunch.sh
python3 compose_hash.py check --compose docker-compose.split.yml --dstack 0.6.1 --kms-pinned
```

Continue when every download and the manifest check succeeds. If a release file
is unavailable, ask ops@ormas.ai to publish the complete recipe before renting.
Keep the compose byte-for-byte as published: comments and whitespace are measured.
The shell image at `ghcr.io/heroncovelabs/ormas-protected-shell` is public and pulls
anonymously. The pre-launch script comes from the pinned wrapper.

This release's production split compose hash (dstack-0.6.1, KMS-pinned v3, shell
`5bbb7186…`) is:

```text
618ba303cf7c582137c5b85c920a8411774d6ddf594bbc4867f78e3881b271af
```

Both `dstack-0.6.1` (tdx.small) and `dstack-0.6.1-tdx.large` OS tuples are
admitted. The **tdx.small tuple is computed only, never booted live**; the
**tdx.large tuple matches a live CVM**. The commands below use tdx.large.

## 2. Write the sealed environment

The measured Ormas shell owns credentials, attestation and publication. Your
worker is a separate OCI image pinned by digest. Start with the
[worker contract](../protected/worker/CONTRACT.md) and
[conformance harness](../protected/worker/conformance/README.md). Passing local
conformance does not prove live confinement or qualify a Protected deployment.
The published split recipe is `protected/miner/docker-compose.split.yml`, with the
shell image pinned by digest. The manifest admits its v2, v3 and KMS-pinned v3
hashes. Use the KMS-pinned v3 form for new production CVMs on dstack-0.6.1.
If the compose, wrapper or manifest is not the published one, stop and request the
complete recipe.

Save your key with `ormas-miner login`. For a CONNECT-mode worker, put worker
provider keys in a private JSON object file; do not put secrets in flags. Input
files must be owned by you, regular files with mode 0400 or 0600, without symlinks.
Generate the complete sealed env:

```sh
ormas-miner protected-env --miner-id '<approved-miner-name>' --runtime production \
  --authorization-file /private/authorization.json \
  --task-cells task:code --task-cells task:service/protected \
  --bind-project-id '<your-project-id>' \
  --worker-image '<your-image>@sha256:<digest>' \
  --worker-env-file /private/worker-env.json \
  --provider '<provider-host>' --pricing-template --out /private/miner.env
```

Repeat `--task-cells` for all approved task qualifications and `--provider` for
each declared provider DNS host. No providers means an empty declaration.
Production requires the exact `https://api.ormas.ai` URL; development also requires
HTTPS. `--token-file` uses a private key file instead of saved credentials. Use `--cell-bounds` and `--approved-by` **instead of**
`--authorization-file` for the CVM-built expense record. Optional
`--registry-auth-file` supplies registry authentication without exposing it in
argv. Registry auth and token files may have one terminal newline, which is
removed; embedded or additional newlines are refused. Every key, including empty values, follows the split compose's measured
`SPLIT_ALLOWED_ENVS` order. Outputs are private; existing files require `--force`.
**Sealed-env parsing rule:** keep `$`, `#`, quotes and embedded newlines out of
free-text values and JSON string contents. JSON syntax quotes are required; do
not surround JSON with shell quotes. Use `miner-operator` for an approver, not a
value containing spaces and `#`. The CLI validates this before writing files.

The split list has **no CONNECT provider-policy or pricing slot**. The separate
relay-policy slot is shell-only; it does not replace CONNECT host approval. Beside the env,
`declared-providers.list` is the declaration to send to ops for
`public-qualifications slot-approve --worker-policy-host …`. For a zero-provider
worker, ops must use `--worker-policy-empty` to approve an empty policy explicitly;
omitting both options leaves the policy unapproved. Submit your image's digest
for repeatable `--worker-digest …` approval. The gateway freezes the
approved provider policy at release; the declaration file is not approval.
At split startup, the shell fetches its own approved hosts before binding the
proxy, freezes them in a private 0600 file, and attests that file's canonical
digest. An approved empty policy denies all providers. A NULL approval keeps
the measured policy; a failed fetch refuses startup. The running proxy never
hot-reloads policy. Approval added or changed after boot therefore mismatches
at credential release until the shell restarts. Doctor shows the gateway's
approval state, not the policy frozen at boot.
`--pricing-template` writes `pricing.json` with `estimate_usd`/`limit_usd`
placeholders: it illustrates the offer field names, not a pricing configuration.
Your worker emits a **per-task** `offer.json` with estimate/limit values, as specified
in the worker contract. The template is not injected into the shell env. Never send ops the env or key files.

For a relay-mode slot, keep provider credentials and routing keys out of the
worker JSON. Seal the policy and provider key for the measured shell instead:

```sh
ormas-miner protected-env --miner-id '<approved-miner-name>' --runtime production \
  --authorization-file /private/authorization.json \
  --task-cells task:code --task-cells task:service/protected \
  --bind-project-id '<your-project-id>' \
  --worker-image '<your-image>@sha256:<digest>' \
  --worker-env-file /private/worker-env.json \
  --relay saygm-frontier --relay-model '<approved-provider-model-id>' \
  --provider-key-file /private/provider.key --out /private/miner.env
```

Use `--relay engy-zdr` for Engy zero-retention models. Repeat `--relay-model` for
each permitted model; there is no default model ladder. Engy accepts only its
ZDR-qualified canonical ids. The shell receives
`ORMAS_PROTECTED_RELAY_POLICY_JSON` and either `SAYGM_API_KEY` or `ENGY_API_KEY`;
the unused key is empty. Neither the policy nor the key enters the worker JSON.
Without relay flags all three relay fields are empty (CONNECT mode).

The CLI also writes private `relay-policy.json` and `relay-policy.digest` files.
Send only the digest to ops for relay slot approval, not the policy, model ids,
binding salt or provider key. Keep the policy for reuse: replace `--relay-model`
with `--relay-policy-file /private/relay-policy.json`, keeping the matching
`--relay` and `--provider-key-file`, to preserve the approved digest. Use
`--force` when replacing existing output files. Regenerating
a policy without that file creates a new salt and needs approval again. Provider
key files may have one terminal newline; embedded or additional newlines are
refused. Relay mode rejects credential and routing keys in worker JSON.

Keep `/private/miner.env` outside version control. Load your Phala API key into the local
shell environment with a hidden prompt; the CLI reads `PHALA_CLOUD_API_KEY`:

```bash
read -r -s -p 'Phala Cloud API key: ' PHALA_CLOUD_API_KEY; printf '\n'
export PHALA_CLOUD_API_KEY
```

## 3. Check the provision hash before paying

Phala's provision endpoint computes the deployment compose hash without creating
a CVM. This manual check uses your own Phala API key and sends no miner secrets.
The local document includes v3's string `manifest_version: "3"` and empty
`init_script`, plus `key_provider: "kms"` and the public Phala production Dstack
KMS CA pin. Phala adds these fields when provisioning production nodes; the
request below sends the workload fields for that step:

```bash
python3 compose_hash.py app-compose --compose docker-compose.split.yml \
  --dstack 0.6.1 --kms-pinned > app-compose.json
# Verify the env key order locally; values stay in the private env file.
test "$(cut -d= -f1 /private/miner.env | jq -Rsc 'split("\n")[:-1]')" = \
  "$(jq -c '.allowed_envs' app-compose.json)" || exit 1
jq '{name:"protected-miner-provcheck",listed:false,instance_type:"tdx.large",
     image:"dstack-0.6.1",kms:"PHALA",prefer_dev:false,
     compose_file: {docker_compose_file,allowed_envs,public_logs,public_sysinfo,
                    public_tcbinfo,secure_time,pre_launch_script,name}}' \
  app-compose.json > provision.json
printf 'header = "X-API-Key: %s"\n' "$PHALA_CLOUD_API_KEY" | \
  curl --config - -fsS -X POST https://cloud-api.phala.com/api/v1/cvms/provision \
    -H 'X-Phala-Version: 2026-06-23' -H 'User-Agent: phala-cli/1.1.22' \
    -H 'Accept: application/json' -H 'Content-Type: application/json' \
    --data-binary @provision.json > provision-result.json
expected=$(python3 compose_hash.py hash --compose docker-compose.split.yml \
  --dstack 0.6.1 --kms-pinned)
actual=$(jq -er '.compose_hash' provision-result.json)
test "$actual" = "$expected" || exit 1
python3 compose_hash.py check --compose docker-compose.split.yml --dstack 0.6.1 --kms-pinned
```

Proceed only after the returned hash equals the local hash and the manifest
admits it. A mismatch calls for correcting the recipe before a paid deployment.

## 4. Deploy and qualify

```bash
phala deploy -n '<your-cvm-name>' -c docker-compose.split.yml -t tdx.large \
  --image dstack-0.6.1 --no-dev-os --no-public-logs --no-public-sysinfo \
  --pre-launch-script prelaunch.sh -e /private/miner.env
```

Keep both the explicit OS image and pinned pre-launch script. Save the returned
CVM id and app id. Phala's dashboard confirms boot; gateway registration confirms
that the miner started. The measured entrypoint binds and registers automatically.

After slot approval, registration with the required task cells creates the
qualification profile and enqueues one **reserved $1 qualification job** for your
miner. The $1 is the job's spending ceiling; payment follows accepted delivery
under the [contract](CONTRACT.md). Keep the CVM running until the receipt settles
`paid`. **Miner → Runner keys** shows key activity. Ask ops@ormas.ai to confirm
the registration, reserved job and paid receipt if qualification stalls; include
your miner name and CVM/app ids, keeping your env file private.

**Cap 0 → 50** means the key starts register-only, with the reserved qualification
job as its claim exception. Once that job settles paid, the gateway raises the
key's daily claim cap to 50. Ordinary work then uses your advertised task cells,
qualification and capacity. Follow the [install guide](INSTALL.md) to bind your
SN76 hotkey for chain rewards; keep chain signing keys outside the CVM.

Check your registration after boot:

```sh
ormas-miner doctor --protected
```

Doctor reads only your gateway registration: slot approval, declared digests,
worker-policy presence, qualification job, last release refusal and last released
worker digest. Name-only slot approval is `warn slot: approved, not yet bound`;
declared digests and policy come only from the bound slot, as at the release gate.
An approved empty policy is `ok`; no policy approval is `warn`. Pending qualification
or no release yet also warns. `fail` requires correction. An older gateway without these diagnostics fails explicitly.
This checks the gateway's view, not your local Docker state.

## Legacy monolith (existing CVMs only)

The monolith recipe `docker-compose.yml` (`ac40e43d…`) has **no dstack-0.6.1
form**. It is admitted only for CVMs already running on dstack 0.5.x, which are
scheduled for retirement. Phala no longer offers dstack-0.5.9 for new CVMs.
Do not use the monolith for a new CVM. Move to the split recipe
above; do not change an existing monolith's measured env order to the split order.
The public image is `ghcr.io/heroncovelabs/ormas-protected-miner`.

The following env reference is for those existing monolith CVMs only.

Create the file privately, then edit it locally:

```bash
umask 077
touch miner.env
chmod 600 miner.env
```

Include **every key below, in this order**, including empty values. Phala builds
`allowed_envs` from the file's key order, and that order is hashed. Keep one
`KEY=value` per line, with a final newline. Replace every `<…>` placeholder before
deploying; `ormr_…` below stands for your locally saved key.

```dotenv
ORMAS_API_URL=https://api.ormas.ai
ORMAS_RUNNER_TOKEN=ormr_…
ORMAS_MINER_ID=<your-approved-miner-name>
ORMAS_RUNTIME=production
ORMAS_AUTHORIZATION_JSON=
ORMAS_CELL_BOUNDS=<catalog-cell-alias>=<your-approved-usd-bound>
ORMAS_APPROVED_BY=miner-operator
ORMAS_PRICING_JSON=<your-completed-ops-pricing-template>
ORMAS_CELLS=<catalog-cell-alias>
ORMAS_TASK_CELLS=task:code task:code/small task:lang/python task:acceptance/operator-run-v3 task:preflight/deferred-v1 task:publication/github-artifact-v1 task:service/protected
ORMAS_BIND_PROJECT_ID=proj_…
ENGY_API_KEY=
SAYGM_API_KEY=
XAI_API_KEY=
OPENROUTER_API_KEY=
```

Fill the provider keys your configuration uses and leave the others empty.
`ORMAS_CELLS` is a space-separated selection from the measured image's catalog.
`ORMAS_CELL_BOUNDS` gives each selected alias a positive USD expense bound,
with at most two decimal places: `alias=amount alias=amount`. Choose your own
bounds. The catalog defines the execution aliases and pricing JSON schema.
Fill `ORMAS_PRICING_JSON` using the template Ormas sends privately with slot
approval. Compact the completed JSON object or list of objects onto one line.

**Sealed-env parsing rule:** keep `$`, `#`, quotes and embedded newlines out of free-text
values, including `ORMAS_APPROVED_BY`. Use `miner-operator`, for example. The
production value `miner #1` truncated an authorization value and prevented
registration. `$` is also refused to prevent Compose interpolation. Write compact JSON directly after `=` with its required JSON
syntax quotes; keep its string contents free of `$`, `#`, quote characters and
newlines, and use a single line without surrounding shell quotes.

With `ORMAS_AUTHORIZATION_JSON` empty, the entrypoint builds the expense record
inside the CVM from `ORMAS_CELL_BOUNDS` and `ORMAS_APPROVED_BY`. It binds your key
hash, gateway URL, and the CVM's app id and compose hash from dstack. It writes
private `0600` configuration files before starting the miner. A supplied
`ORMAS_AUTHORIZATION_JSON` is the alternative: keep the bounds and approver keys
empty when using it. Include the same keys in either case.

## Updating a release or rotating keys

A new image, compose or wrapper produces a new compose hash. Download the new
published release, repeat the provision check and redeploy with that release's
exact recipe. Confirm the new miner registers before retiring the old CVM.
Previous hashes remain admitted during a transition window, then leave the
manifest. Check the published manifest and release instructions for the window.

For key, pricing or expense-record changes under the same measured recipe,
edit the full env file and replace the sealed environment (use the actual private
env path for an existing monolith):

```bash
phala envs update --cvm-id '<your-cvm-id>' -e /private/miner.env
```

Re-send every key in the measured order. Runner keys expire after 30 days; mint
and install a replacement through **Miner → Runner keys**, then revoke the old
key. Keep the same approved miner name.

## Costs

You pay Phala's hourly CVM bill and your own model-provider spend. The
`tdx.large` used by this guide was estimated at about **$0.2348/hour** in the
2026-10-14 dstack-0.6.1 test ledger. For comparison, production `tdx.small` was
about **$0.058/hour** on 2026-10-08. Check Phala's current rate before deploying.
Include host cost in your firm price or limit-offer economics.
An accepted firm offer charges its price; an accepted limit offer charges your
settled price within its limit. You bear costs above the limit and costs of failed
deliveries. See [Economics](economics.md).

## Troubleshooting

- **Silent miner:** confirm `PHALA_CLOUD_API_KEY` is exported and check the env
  parsing rule. For boot diagnosis, use a throwaway **debug twin** with the same
  image and pinned script, public logs enabled, and isolated diagnostic
  credentials. Public logs change the measured hash, so this twin receives no
  Protected repository credential. Keep client work off it, inspect the boot
  refusal, then delete it: `phala cvms delete --cvm-id '<debug-twin-id>' -y`.
- **`unknown fleet cell` (crash loop right after boot):** `ORMAS_CELLS` contains
  an alias outside the legacy monolith image's catalog. Get the current aliases and
  matching pricing template from ops, correct all cell references, and update
  the complete sealed env file.
- **`provider_expense_bound_unavailable`:** check the authorization fields, alias
  coverage, key and gateway binding, and your approved expense bounds. Use either
  the in-CVM bounds/approver path or a complete supplied record. Send the corrected
  full env file with `phala envs update`.
- **Refused attestation:** compare the provision hash, local hash and manifest.
  Start with `--pre-launch-script`, `--image dstack-0.6.1` and the
  KMS-pinned v3 hash flags, then the env key order and compose bytes. Restore the published recipe and repeat preflight.

## What you and Ormas can see

You control your CVM bill, env values and keys. You can
see deployment metadata and qualification/payment status. The production recipe
keeps public container logs and system information off; TDX protects guest
memory from the cloud host, and repository credentials unseal inside the
attested runtime. Model-account history or exports can expose submitted source,
so provider data handling remains part of the confidentiality boundary.

Ormas operates the credential-release gateway, acceptance and publication paths.
It sees job status and delivery evidence and handles repository credentials and
result publication. The published recipe's egress list is currently declarative.
Attestation proves the measured runtime; provider retention and destination
controls require their own evidence.
