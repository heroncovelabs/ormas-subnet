#!/usr/bin/env python3
"""App-compose hash for the published Protected miner compose (card fc32bec8).

dstack extends sha256(app-compose.json) into RTMR3 (event `compose-hash`). Phala builds
app-compose from our docker-compose.yml plus its own wrapper fields; the pinned copy of
those fields is scripts/phala_proof/app_compose_wrapper.json (pre-launch script v0.0.20).
The Protected deployment overrides four of them (design decision 3 and 6):

  allowed_envs   = ALLOWED_ENVS, in this order. The phala CLI sets it to the `-e` keys in
                   input order (`allowed_envs: envs.map(e => e.key)`); dstack's hash sorts
                   object keys but never list items, so the order is measured.
  public_logs    = false, public_sysinfo = false, public_tcbinfo = true
  name           = ""  (the CLI's app-compose always sets name "", as in the proof)

Subcommands:
  hash          print the compose hash
  app-compose   print the app-compose JSON that is hashed
  pin           write the image digest into the compose, fill the manifest's tdx tuple
  check         exit 1 unless the manifest admits the recomputed hash
  digests       print the gateway's ORMAS_PROTECTED_PLUGIN_DIGEST / _EGRESS_LIST_DIGEST
                (plugin = the grok binary's sha256 from pins.env; egress = egress.list);
                --write-readme refreshes the README block (pin does this too)

Stdlib only.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
COMPOSE = HERE / "docker-compose.yml"
WRAPPER = HERE / "app_compose_wrapper.json" if (HERE / "app_compose_wrapper.json").is_file() else REPO / "scripts/phala_proof/app_compose_wrapper.json"
MANIFEST = HERE / "manifest.json" if (HERE / "manifest.json").is_file() else REPO / "tensorbox_spec/privacy/manifests/ormas_protected_miner_v1.json"
EXPECTED_MR = REPO / "docs/evidence/phala_tdx_2026_10_07/expected_prod.json"
PINS = HERE / "pins.env"
EGRESS_LIST = HERE / "egress.list"
README = HERE / "README.md"
BINDING_MODULE = REPO / "tensorbox_spec/privacy/protected_binding.py"
README_BEGIN = "<!-- gateway-digests:begin (compose_hash.py digests --write-readme) -->"
README_END = "<!-- gateway-digests:end -->"

ALLOWED_ENVS = [
    "ORMAS_API_URL", "ORMAS_RUNNER_TOKEN", "ORMAS_MINER_ID", "ORMAS_RUNTIME",
    "ORMAS_AUTHORIZATION_JSON", "ORMAS_CELL_BOUNDS", "ORMAS_APPROVED_BY",
    "ORMAS_PRICING_JSON", "ORMAS_CELLS", "ORMAS_TASK_CELLS",
    "ORMAS_BIND_PROJECT_ID",
    "ENGY_API_KEY", "SAYGM_API_KEY", "XAI_API_KEY", "OPENROUTER_API_KEY",
]
OVERRIDES = {
    "allowed_envs": ALLOWED_ENVS,
    "public_logs": False,
    "public_sysinfo": False,
    "public_tcbinfo": True,
    "name": "",
}
# Public mirror of the Fly build (design D3, 2026-10-08): CVMs pull anonymously, no registry token.
IMAGE_REPO = "ghcr.io/heroncovelabs/ormas-protected-miner"
PLACEHOLDER = "sha256:PLACEHOLDER_IMAGE_DIGEST"
_IMAGE_LINE = re.compile(r"^(\s*image:\s*)" + re.escape(IMAGE_REPO) + r"@(\S+)\s*$", re.M)
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def app_compose(compose_path: Path = COMPOSE, wrapper_path: Path = WRAPPER) -> dict:
    wrapper = json.loads(Path(wrapper_path).read_text())
    return dict(wrapper, **OVERRIDES, docker_compose_file=Path(compose_path).read_text())


def compose_hash(compose_path: Path = COMPOSE, wrapper_path: Path = WRAPPER) -> str:
    # Serialization copied verbatim from scripts/phala_proof/fill_manifest.py:app_compose_hash
    # (the proof matched both live CVMs with it); tests assert the two stay equal.
    doc = app_compose(compose_path, wrapper_path)
    blob = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()


def image_digest(compose_text: str) -> str:
    """The one pinned digest: every service image line of our repository must carry it."""
    found = {m[1] for m in _IMAGE_LINE.findall(compose_text)}
    if len(found) != 1:
        raise SystemExit(f"compose: expected one {IMAGE_REPO}@… digest on every image line")
    images = re.findall(r"^\s*image:\s*(\S+)\s*$", compose_text, re.M)
    if any(not image.startswith(IMAGE_REPO + "@") for image in images):
        raise SystemExit(f"compose: every image must be {IMAGE_REPO}@…")
    return found.pop()


def _binding():
    # Loaded by path so this tool stays stdlib-only; same derivation the entrypoint uses.
    spec = importlib.util.spec_from_file_location("_protected_binding", BINDING_MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def gateway_digests(pins: Path = PINS, egress_list: Path = EGRESS_LIST) -> dict[str, str | None]:
    """Expected gateway env values; the plugin digest is None until pins.env exists."""
    binding = _binding()
    plugin = None
    if pins.is_file():
        for line in pins.read_text().splitlines():
            if line.startswith("PIN_GROK_SHA256="):
                plugin = line.split("=", 1)[1].strip() or None
    return {binding.PLUGIN_DIGEST_ENV: plugin,
            binding.EGRESS_LIST_DIGEST_ENV: binding.derive_egress_list_digest(egress_list)}


def _digest_lines(digests: dict[str, str | None]) -> list[str]:
    return [f"{name}={value}" if value else f"{name}=<unknown: pins.env not recorded yet>"
            for name, value in digests.items()]


def write_readme(digests: dict[str, str | None], readme: Path = README) -> None:
    text = readme.read_text()
    start, end = text.index(README_BEGIN), text.index(README_END)
    block = README_BEGIN + "\n```\n" + "\n".join(_digest_lines(digests)) + "\n```\n"
    readme.write_text(text[:start] + block + text[end:])


def _pin(args: argparse.Namespace) -> int:
    if not _DIGEST.match(args.image_digest):
        raise SystemExit("pin: --image-digest must be sha256:<64 lowercase hex>")
    mr = json.loads(Path(args.expected_mr).read_text()) if Path(args.expected_mr).is_file() else None
    if mr is None:
        raise SystemExit(f"pin: expected-MR file not found: {args.expected_mr}")
    text = Path(args.compose).read_text()
    image_digest(text)
    text = _IMAGE_LINE.sub(lambda m: f"{m.group(1)}{IMAGE_REPO}@{args.image_digest}", text)
    if PLACEHOLDER in text:
        raise SystemExit("pin: compose still carries the placeholder digest")
    Path(args.compose).write_text(text)
    digest = compose_hash(Path(args.compose), Path(args.wrapper))

    manifest_path = Path(args.manifest)
    data = json.loads(manifest_path.read_text())
    # The manifest's top-level image_digest is the pushed image's content digest
    # (informational: the measurement is the compose hash below), so the stand-in
    # value is retired here and its note dropped.
    data["image_digest"] = args.image_digest.split(":", 1)[1]
    data["_notes"] = [n for n in data.get("_notes", []) if "image_digest is a stand-in" not in n]
    tdx = data.setdefault("tdx", {})
    entry = {"name": mr["name"], "mrtd_hex": mr["mrtd"], "rtmr0_hex": mr["rtmr0"],
             "rtmr1_hex": mr["rtmr1"], "rtmr2_hex": mr["rtmr2"]}
    images = [i for i in tdx.get("allowed_os_images", []) if i.get("name") != entry["name"]]
    tdx["allowed_os_images"] = images + [entry]
    tdx["allowed_compose_hashes_hex"] = [digest]
    tdx.setdefault("allowed_tcb_statuses", ["UpToDate"])
    tdx["require_no_debug"] = True
    tdx.pop("allowed_rtmr3_hex", None)
    manifest_path.write_text(json.dumps(data, indent=2) + "\n")
    print(json.dumps({"image": f"{IMAGE_REPO}@{args.image_digest}", "compose_hash": digest}))
    digests = gateway_digests()
    print("\n".join(_digest_lines(digests)))
    if Path(args.compose) == COMPOSE:
        write_readme(digests)
    return 0


def _check(args: argparse.Namespace) -> int:
    text = Path(args.compose).read_text()
    digest = compose_hash(Path(args.compose), Path(args.wrapper))
    if PLACEHOLDER in text:
        print(f"compose not pinned (placeholder image digest); compose hash {digest}")
        return 1
    admitted = json.loads(Path(args.manifest).read_text()).get("tdx", {}).get(
        "allowed_compose_hashes_hex", [])
    if digest not in admitted:
        print(f"manifest does not admit compose hash {digest}")
        return 1
    print(f"ok {digest}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("hash", "app-compose", "pin", "check", "digests"):
        p = sub.add_parser(name)
        p.add_argument("--compose", default=str(COMPOSE))
        p.add_argument("--wrapper", default=str(WRAPPER))
        if name in ("pin", "check"):
            p.add_argument("--manifest", default=str(MANIFEST))
        if name == "pin":
            p.add_argument("--image-digest", required=True)
            p.add_argument("--expected-mr", default=str(EXPECTED_MR))
        if name == "digests":
            p.add_argument("--write-readme", action="store_true")
    args = ap.parse_args(argv)
    if args.cmd == "hash":
        print(compose_hash(Path(args.compose), Path(args.wrapper)))
        return 0
    if args.cmd == "app-compose":
        print(json.dumps(app_compose(Path(args.compose), Path(args.wrapper)), indent=2))
        return 0
    if args.cmd == "digests":
        digests = gateway_digests()
        print("\n".join(_digest_lines(digests)))
        if args.write_readme:
            write_readme(digests)
        return 0
    return _pin(args) if args.cmd == "pin" else _check(args)


if __name__ == "__main__":
    sys.exit(main())
