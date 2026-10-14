"""Protected release inventory fails closed on unreviewed Compose semantics."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_DIR = ROOT / "protected" / "miner"


def _module():
    path = COMPOSE_DIR / "compose_hash.py"
    spec = importlib.util.spec_from_file_location("protected_compose_hash", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_checked_in_recipes_have_exact_reviewed_service_sets() -> None:
    module = _module()
    monolith = module.service_images((COMPOSE_DIR / "docker-compose.yml").read_text())
    split = module.service_images((COMPOSE_DIR / "docker-compose.split.yml").read_text())
    assert list(monolith) == ["scratch-init", "miner"]
    assert list(split) == ["scratch-init", "ormas-shell"]


def test_checked_in_recipe_hashes_remain_admitted() -> None:
    module = _module()
    admitted = json.loads((COMPOSE_DIR / "manifest.json").read_text())["tdx"][
        "allowed_compose_hashes_hex"
    ]
    actual = [
        module.compose_hash(COMPOSE_DIR / "docker-compose.yml"),
        module.compose_hash(COMPOSE_DIR / "docker-compose.split.yml"),
    ]
    assert actual == [
        "ac40e43d91fc2b47944b3e5f7e94d538fae23977914e80a888a86f9e3aca0805",
        "40f4e967eb72517ea0c792b72361b309670ff30151dba6a365c503a8f461263e",
    ]
    assert actual == admitted


def test_digest_only_repin_preserves_approved_recipe_shape() -> None:
    module = _module()
    original = (COMPOSE_DIR / "docker-compose.yml").read_text()
    current = next(iter(module.service_images(original).values())).rsplit("@", 1)[1]
    repinned = original.replace(current, "sha256:" + "a" * 64)
    assert module.service_images(repinned) == {
        "scratch-init": "ghcr.io/heroncovelabs/ormas-protected-miner@sha256:" + "a" * 64,
        "miner": "ghcr.io/heroncovelabs/ormas-protected-miner@sha256:" + "a" * 64,
    }


@pytest.mark.parametrize(
    "mutation",
    [
        '  "unreviewed": {image: attacker.invalid/x@sha256:' + "a" * 64 + "}\n",
        "  unreviewed: &extra\n    image: attacker.invalid/x@sha256:" + "a" * 64 + "\n",
        "  unreviewed: *extra\n",
        "  {unreviewed: {image: attacker.invalid/x@sha256:" + "a" * 64 + "}}\n",
    ],
    ids=("quoted-flow", "anchor", "alias", "flow-service-map"),
)
def test_unreviewed_service_syntax_cannot_escape_inventory(mutation: str) -> None:
    module = _module()
    original = (COMPOSE_DIR / "docker-compose.yml").read_text()
    crafted = original.replace("services:\n", "services:\n" + mutation, 1)
    with pytest.raises(SystemExit, match="compose"):
        module.service_images(crafted)


def test_known_service_cannot_gain_unreviewed_authority() -> None:
    module = _module()
    original = (COMPOSE_DIR / "docker-compose.split.yml").read_text()
    crafted = original.replace(
        "    restart: unless-stopped\n",
        "    restart: unless-stopped\n    privileged: true\n",
        1,
    )
    with pytest.raises(SystemExit, match="recipe shape"):
        module.service_images(crafted)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda text: text.replace("services:\n", '"services":\n', 1),
        lambda text: text.replace(
            "    image: ", '    "image": attacker.invalid/x@sha256:' + "a" * 64 + "\n    image: ", 1,
        ),
        lambda text: text.replace("    image: ", "    image : ", 1),
        lambda text: text.replace(
            "    image: ", "    image: attacker.invalid/x@sha256:" + "a" * 64 + "\n    image: ", 1,
        ),
    ],
    ids=(
        "quoted-top-level", "duplicate-alternate-image-key", "spaced-image-key",
        "duplicate-plain-image-key",
    ),
)
def test_alternate_yaml_keys_cannot_escape_release_inventory(mutation) -> None:
    module = _module()
    original = (COMPOSE_DIR / "docker-compose.yml").read_text()
    with pytest.raises(SystemExit, match="compose"):
        module.service_images(mutation(original))
