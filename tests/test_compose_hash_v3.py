"""Published split bytes reproduce dstack v2, v3, and production KMS-pinned v3."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RECIPE = ROOT / "protected" / "miner"
VECTORS = [
    ([], "f36515ef902b61c6f1a682895e6d9489fec1e5ba79a250b8686d08c5f09d03e3"),
    (["--dstack", "0.6.1"],
     "dd7c936b1031167de28950904cd55be2564ba43093c93ee19f76c9f01c3e3418"),
    (["--dstack", "0.6.1", "--kms-pinned"],
     "618ba303cf7c582137c5b85c920a8411774d6ddf594bbc4867f78e3881b271af"),
]
MONOLITH = "ac40e43d91fc2b47944b3e5f7e94d538fae23977914e80a888a86f9e3aca0805"


@pytest.fixture
def tool():
    spec = importlib.util.spec_from_file_location("compose_hash_v3", RECIPE / "compose_hash.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("flags,expected", VECTORS, ids=("v2", "v3", "kms-v3"))
def test_published_hash_and_admission(tool, capsys, flags, expected):
    paths = ["--compose", str(RECIPE / "docker-compose.split.yml"),
             "--wrapper", str(RECIPE / "app_compose_wrapper.json")]
    assert tool.main(["hash", *paths, *flags]) == 0
    assert capsys.readouterr().out.strip() == expected
    assert tool.main(["check", *paths, "--manifest", str(RECIPE / "manifest.json"), *flags]) == 0
    assert capsys.readouterr().out == f"ok {expected}\n"
    assert tool.main(["app-compose", *paths, *flags]) == 0
    doc = json.loads(capsys.readouterr().out)
    blob = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    assert hashlib.sha256(blob).hexdigest() == expected
    assert doc["docker_compose_file"] == (RECIPE / "docker-compose.split.yml").read_text()
    if flags:
        assert doc["manifest_version"] == "3" and doc["init_script"] == []
    else:
        assert doc["manifest_version"] == 2 and "init_script" not in doc
    if "--kms-pinned" in flags:
        assert doc["key_provider"] == "kms"
        assert doc["key_provider_id"] == tool.PHALA_PRODUCTION_KMS_CA_SPKI_HEX
    else:
        assert "key_provider" not in doc and "key_provider_id" not in doc


def test_v3_changes_only_the_required_fields(tool):
    split = RECIPE / "docker-compose.split.yml"
    legacy = tool.app_compose(split)
    v3 = tool.app_compose(split, dstack="0.6.1")
    assert v3 == dict(legacy, manifest_version="3", init_script=[])
    assert tool.app_compose(split, dstack="0.6.1", kms_pinned=True) == dict(
        v3, key_provider="kms", key_provider_id=tool.PHALA_PRODUCTION_KMS_CA_SPKI_HEX,
    )


def test_kms_pin_requires_v3(tool):
    with pytest.raises(SystemExit, match="requires --dstack 0.6.1"):
        tool.main(["hash", "--compose", str(RECIPE / "docker-compose.split.yml"), "--kms-pinned"])


@pytest.mark.parametrize("flags,expected", VECTORS, ids=("v2", "v3", "kms-v3"))
@pytest.mark.parametrize("keep_deployed", [False, True], ids=("first-pin", "keep-live"))
def test_pin_computes_the_selected_form(
    tool, tmp_path, monkeypatch, capsys, flags, expected, keep_deployed,
):
    compose = tmp_path / "split.yml"
    original = (RECIPE / "docker-compose.split.yml").read_bytes()
    compose.write_bytes(original)
    manifest = tmp_path / "manifest.json"
    others = [h for _, h in VECTORS if h != expected] if keep_deployed else []
    data = {"tdx": {"allowed_compose_hashes_hex": [MONOLITH, *others]}}
    if keep_deployed:
        data.update(image_digests={}, _notes=[f"Transition: {h} split recipe" for h in others])
    manifest.write_text(json.dumps(data))
    keep_flags = [value for h in others for value in ("--keep-split", h)]
    mr = tmp_path / "mr.json"
    mr.write_text(json.dumps(dict(name="test-os", mrtd="00", rtmr0="00", rtmr1="00", rtmr2="00")))
    monkeypatch.setattr(tool, "gateway_digests", lambda **kwargs: {})
    pins = [item for service, image in tool.service_images(original.decode()).items()
            for item in ("--image-digest", f"{service}={image.rsplit('@', 1)[1]}")]
    assert tool.main(["pin", "--compose", str(compose), "--manifest", str(manifest),
                      "--expected-mr", str(mr), *pins, *flags, *keep_flags]) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[0])["compose_hash"] == expected
    assert json.loads(manifest.read_text())["tdx"]["allowed_compose_hashes_hex"] == [
        MONOLITH, *others, expected,
    ]
    assert compose.read_bytes() == original


def _admitted():
    hashes = [MONOLITH, VECTORS[0][1], VECTORS[1][1]]
    notes = [f"Transition: {h} split recipe" for h in hashes[1:]]
    return {"tdx": {"allowed_compose_hashes_hex": hashes}, "_notes": notes, "image_digests": {}}


def test_keep_split_preserves_deployed_forms_and_noop(tool):
    data = _admitted()
    live = tuple(data["tdx"]["allowed_compose_hashes_hex"][1:])
    admitted, notes = tool._replace_split_pins(data, VECTORS[2][1], MONOLITH, live)
    assert admitted == [MONOLITH, *live, VECTORS[2][1]]
    assert notes[:2] == data["_notes"]
    data["tdx"]["allowed_compose_hashes_hex"] = admitted
    data["_notes"] = notes
    assert tool._replace_split_pins(data, VECTORS[2][1], MONOLITH) == (admitted, notes)


@pytest.mark.parametrize("keep", [MONOLITH, "f" * 64])
def test_keep_split_refuses_unapproved_hashes(tool, keep):
    with pytest.raises(SystemExit, match="must be an admitted split hash"):
        tool._replace_split_pins(_admitted(), VECTORS[2][1], MONOLITH, (keep,))


def test_keep_split_requires_transition_note(tool):
    data = _admitted()
    data["_notes"] = []
    with pytest.raises(SystemExit, match="must be an admitted split hash"):
        tool._replace_split_pins(data, VECTORS[2][1], MONOLITH, (VECTORS[0][1],))


def test_refuses_tabs_in_the_new_split_recipe(tool):
    text = (RECIPE / "docker-compose.split.yml").read_text()
    with pytest.raises(SystemExit, match="tabs are not supported"):
        tool.service_images(text.replace("    restart:", "\t    restart:", 1))
