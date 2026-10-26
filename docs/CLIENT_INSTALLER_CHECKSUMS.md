# Ormas client installer checksums

This public repository is an independent distribution channel for checksums of
the Ormas client installer served by `ormas.ai`. Fetch this page through GitHub,
not through `ormas.ai`, before executing a downloaded installer. A match detects
an installer replacement confined to the website or its CDN; it is not a
substitute for reviewing the script or protecting both publishing accounts.

## `client-0.0.5-r1`

Published 2026-10-25. SHA-256 values:

```text
ca03d6a1178aa48a95fd5297df58fe1929fffc3f8f8421f39c867952ca5d8fb7  install-client.sh
9327b2d35c853c357c753626249cbb71db0eb693ee069f611ba91a87556906ea  requirements.txt
7b487569593b7ac1b7d8a16f37c0b84c48055fda34fabb356c9c4cf35ade06ab  python-runtime-manifest.json
fd4da4ad85100164a2b69b21088c15d000902ae5311c677b5d67993da44923b5  tensorbox_spec-0.0.5-py3-none-any.whl
```

Verify the bootstrap script before running it:

```bash
curl --fail --silent --show-error --location --proto '=https' \
  https://ormas.ai/install-client.sh -o install-client.sh
printf '%s  %s\n' \
  ca03d6a1178aa48a95fd5297df58fe1929fffc3f8f8421f39c867952ca5d8fb7 \
  install-client.sh | shasum -a 256 --check
bash install-client.sh
```

Stop if the checksum does not match. Each future installer revision must receive
a new immutable section here; existing entries must not be edited or removed.
