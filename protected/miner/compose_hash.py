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
  pin           write each service's image digest, fill the manifest's tdx tuple;
                repeat --image-digest SERVICE=sha256:… for split images. A single
                --image-digest sha256:… retains the legacy same-image monolith form.
  check         exit 1 unless the manifest admits the recomputed hash
  digests       print the gateway's plugin, egress-list and worker-policy digests;
                monolith plugin = grok sha256 from pins.env. Split shell plugin =
                measured entrypoint sha256, never the worker image. Split policy = canonical
                egress-list sha256, or unset when absent, matching prepare_split.
                Monolith digests output is unchanged.
                --write-readme refreshes the README block (monolith pin does this too)

All commands accept --compose FILE (default docker-compose.yml). The selected
compose's services select the measured env list, not the file's basename.

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
# Only split startup inputs, including _authorization's alternative record inputs.
# The mode is a literal in measured compose bytes, never an allowed deployer override.
SPLIT_ALLOWED_ENVS = [
    "ORMAS_API_URL", "ORMAS_RUNNER_TOKEN", "ORMAS_MINER_ID", "ORMAS_RUNTIME",
    "ORMAS_AUTHORIZATION_JSON", "ORMAS_CELL_BOUNDS", "ORMAS_APPROVED_BY",
    "ORMAS_TASK_CELLS", "ORMAS_BIND_PROJECT_ID",
    "MINER_WORKER_IMAGE", "MINER_WORKER_ENV_JSON", "MINER_WORKER_REGISTRY_AUTH",
]
OVERRIDES = {
    "public_logs": False,
    "public_sysinfo": False,
    "public_tcbinfo": True,
    "name": "",
}
# Public mirror of the Fly build (design D3, 2026-10-08): CVMs pull anonymously, no registry token.
IMAGE_REPO = "ghcr.io/heroncovelabs/ormas-protected-miner"
PLACEHOLDER = "sha256:PLACEHOLDER_IMAGE_DIGEST"
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
# These measured files use block-style services with four-space image scalars.
# Pin by offsets, not YAML reserialization: comments/whitespace are measured too.
_SERVICE = re.compile(r"^  ([a-zA-Z0-9_-]+):\s*(?:#.*)?$", re.M)
_IMAGE_LINE = re.compile(r"^(    image: *)([^\s@]+)@([^\s#]+)( *\n|$)", re.M)


# Deliberate duplicate of entrypoint.WORKER_POLICY_FILE, cross-checked by tests.
# Keep the hash tool stdlib-only without importing/executing startup code.
WORKER_POLICY_FILE = Path("/opt/ormas/worker-policy.list")

def service_images(compose_text: str) -> dict[str, str]:
    """Service -> pinned image reference; preserve each service's own repository."""
    services = compose_text.split("services:\n", 1)[1].split("\nvolumes:", 1)[0]
    headers = list(_SERVICE.finditer(services))
    images = {}
    for index, header in enumerate(headers):
        end = headers[index + 1].start() if index + 1 < len(headers) else len(services)
        block = services[header.end():end]
        found = list(_IMAGE_LINE.finditer(block))
        if len(found) != 1 or header[1] in images:
            raise SystemExit("compose: expected one digest-pinned image per service")
        images[header[1]] = found[0][2] + "@" + found[0][3]
    if not images:
        raise SystemExit("compose: no service images found")
    return images


def app_compose(compose_path: Path = COMPOSE, wrapper_path: Path = WRAPPER) -> dict:
    wrapper = json.loads(Path(wrapper_path).read_text())
    text = Path(compose_path).read_text()
    envs = SPLIT_ALLOWED_ENVS if "ormas-shell" in service_images(text) else ALLOWED_ENVS
    return dict(wrapper, **OVERRIDES, allowed_envs=envs, docker_compose_file=text)


def compose_hash(compose_path: Path = COMPOSE, wrapper_path: Path = WRAPPER) -> str:
    # Serialization copied verbatim from scripts/phala_proof/fill_manifest.py:app_compose_hash
    # (the proof matched both live CVMs with it); tests assert the two stay equal.
    doc = app_compose(compose_path, wrapper_path)
    blob = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()


def image_digests(compose_text: str) -> dict[str, str]:
    """Service -> content digest, without a one-image/repository assumption."""
    return {name: image.rsplit("@", 1)[1] for name, image in service_images(compose_text).items()}


def image_digest(compose_text: str) -> str:
    """Legacy same-image accessor; split callers use image_digests instead."""
    images = service_images(compose_text)
    if any(not image.startswith(IMAGE_REPO + "@") for image in images.values()):
        raise SystemExit(f"compose: every image must be {IMAGE_REPO}@…")
    found = {image.rsplit("@", 1)[1] for image in images.values()}
    if len(found) != 1:
        raise SystemExit("compose: multiple image digests; use service-specific pins")
    return found.pop()


def _binding():
    # Loaded by path so this tool stays stdlib-only; same derivation the entrypoint uses.
    spec = importlib.util.spec_from_file_location("_protected_binding", BINDING_MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def gateway_digests(pins: Path = PINS, egress_list: Path = EGRESS_LIST,
                    *, compose_path: Path = COMPOSE) -> dict[str, str | None]:
    """Expected gateway env values; the plugin digest is None until pins.env exists."""
    split = "ormas-shell" in service_images(Path(compose_path).read_text())
    entrypoint = HERE / "entrypoint.py"
    if split and not entrypoint.is_file():
        raise ValueError("split digests require measured entrypoint.py; the standalone public mirror does not ship it")
    binding = _binding()
    plugin = None
    if pins.is_file():
        for line in pins.read_text().splitlines():
            if line.startswith("PIN_GROK_SHA256="):
                plugin = line.split("=", 1)[1].strip() or None
    digests = {binding.PLUGIN_DIGEST_ENV: plugin,
               binding.EGRESS_LIST_DIGEST_ENV: binding.derive_egress_list_digest(egress_list)}
    if split:
        # Independent expectation preserves the deployed monolith's plugin binding.
        digests.pop(binding.PLUGIN_DIGEST_ENV)
        digests["ORMAS_PROTECTED_SHELL_PLUGIN_DIGEST"] = binding.derive_plugin_digest(entrypoint)
        digests["ORMAS_PROTECTED_WORKER_POLICY_DIGEST"] = (
            binding.derive_egress_list_digest(WORKER_POLICY_FILE)
            if WORKER_POLICY_FILE.is_file() else "unset")
    return digests


def _digest_lines(digests: dict[str, str | None]) -> list[str]:
    return [f"{name}={value}" if value else f"{name}=<unknown: pins.env not recorded yet>"
            for name, value in digests.items()]


def write_readme(digests: dict[str, str | None], readme: Path = README) -> None:
    text = readme.read_text()
    start, end = text.index(README_BEGIN), text.index(README_END)
    block = README_BEGIN + "\n```\n" + "\n".join(_digest_lines(digests)) + "\n```\n"
    readme.write_text(text[:start] + block + text[end:])


def _pin(args: argparse.Namespace) -> int:
    text = Path(args.compose).read_text()
    images = service_images(text)
    supplied = args.image_digest
    if len(supplied) == 1 and "=" not in supplied[0]:
        if "ormas-shell" in images or len(set(images.values())) != 1:
            raise SystemExit("pin: repeat --image-digest SERVICE=sha256:… for every service")
        image_digest(text)  # legacy form must retain the original repository check
        pins = dict.fromkeys(images, supplied[0])
    else:
        pins = {}
        for value in supplied:
            name, sep, digest = value.partition("=")
            if not sep or name in pins:
                raise SystemExit("pin: expected unique SERVICE=sha256:… pins")
            pins[name] = digest
        if pins.keys() != images.keys():
            raise SystemExit("pin: provide exactly one image digest for every service")
    if any(not _DIGEST.fullmatch(digest) for digest in pins.values()):
        raise SystemExit("pin: image digests must be sha256:<64 lowercase hex>")
    mr = json.loads(Path(args.expected_mr).read_text()) if Path(args.expected_mr).is_file() else None
    if mr is None:
        raise SystemExit(f"pin: expected-MR file not found: {args.expected_mr}")
    names = iter(images)
    text = _IMAGE_LINE.sub(
        lambda m: f"{m[1]}{m[2]}@{pins[next(names)]}{m[4]}", text)
    if PLACEHOLDER in text:
        raise SystemExit("pin: compose still carries the placeholder digest")
    Path(args.compose).write_text(text)
    digest = compose_hash(Path(args.compose), Path(args.wrapper))

    manifest_path = Path(args.manifest)
    data = json.loads(manifest_path.read_text())
    # The manifest's top-level image_digest is the pushed image's content digest
    # (informational: the measurement is the compose hash below), so the stand-in
    # value is retired here and its note dropped.
    primary = "ormas-shell" if "ormas-shell" in pins else "miner"
    data["image_digest"] = pins[primary].split(":", 1)[1]
    if "ormas-shell" in pins:
        data["image_digests"] = {name: digest.split(":", 1)[1] for name, digest in pins.items()}
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
    pinned_images = service_images(text)
    print(json.dumps({"image": pinned_images[primary], "images": pinned_images, "compose_hash": digest}))
    digests = gateway_digests(compose_path=Path(args.compose))
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
            p.add_argument("--image-digest", required=True, action="append",
                           help="sha256:… (legacy monolith), or repeat SERVICE=sha256:…")
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
        try:
            digests = gateway_digests(compose_path=Path(args.compose))
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print("\n".join(_digest_lines(digests)))
        if args.write_readme:
            write_readme(digests)
        return 0
    return _pin(args) if args.cmd == "pin" else _check(args)


if __name__ == "__main__":
    sys.exit(main())
