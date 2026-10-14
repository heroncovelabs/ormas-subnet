"""Credential files are validated and read through one no-follow descriptor."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from ormas_subnet._private_file import read_private_text
from ormas_subnet.client import load_token
from ormas_subnet.protected_env import read_input


def _private(path: Path, value: str = "synthetic-secret") -> Path:
    path.write_text(value)
    path.chmod(0o600)
    return path


@pytest.mark.parametrize("mode", [0o400, 0o600])
def test_private_regular_file_is_read(mode: int, tmp_path: Path) -> None:
    path = _private(tmp_path / "token")
    path.chmod(mode)
    assert load_token(token_path=path) == "synthetic-secret"
    assert read_input(str(path), "SYNTHETIC") == "synthetic-secret"


def test_token_strips_but_protected_input_preserves_text(tmp_path: Path) -> None:
    path = _private(tmp_path / "input", "  synthetic value  \n")
    assert load_token(token_path=path) == "synthetic value"
    assert read_input(str(path), "SYNTHETIC") == "  synthetic value  \n"


@pytest.mark.parametrize("mode", [0o000, 0o640, 0o644, 0o660, 0o666])
def test_exposed_mode_is_rejected(mode: int, tmp_path: Path) -> None:
    path = _private(tmp_path / "token")
    path.chmod(mode)
    with pytest.raises(ValueError, match="private regular file"):
        load_token(token_path=path)
    with pytest.raises(ValueError, match="private regular file"):
        read_input(str(path), "SYNTHETIC")


def test_symlink_is_rejected_even_when_target_is_private(tmp_path: Path) -> None:
    target = _private(tmp_path / "target")
    link = tmp_path / "token"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="private regular file"):
        load_token(token_path=link)
    with pytest.raises(ValueError, match="private regular file"):
        read_input(str(link), "SYNTHETIC")


def test_intermediate_symlink_is_rejected_even_when_target_is_private(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    target = _private(real / "token")
    link = tmp_path / "linked-directory"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError, match="private regular file"):
        load_token(token_path=link / target.name)
    with pytest.raises(ValueError, match="private regular file"):
        read_input(str(link / target.name), "SYNTHETIC")


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO unavailable")
def test_fifo_is_rejected_without_blocking(tmp_path: Path) -> None:
    path = tmp_path / "token"
    os.mkfifo(path, 0o600)
    with pytest.raises(ValueError, match="private regular file"):
        load_token(token_path=path)


def test_oversized_inputs_are_rejected(tmp_path: Path) -> None:
    token = _private(tmp_path / "token", "x" * 8193)
    with pytest.raises(ValueError, match="private regular file"):
        load_token(token_path=token)

    protected = _private(tmp_path / "protected", "x" * (1024 * 1024 + 1))
    with pytest.raises(ValueError, match="private regular file"):
        read_input(str(protected), "SYNTHETIC")


def test_directory_and_invalid_utf8_are_rejected(tmp_path: Path) -> None:
    directory = tmp_path / "directory"
    directory.mkdir(mode=0o700)
    with pytest.raises(ValueError, match="private regular file"):
        load_token(token_path=directory)

    invalid = tmp_path / "invalid"
    invalid.write_bytes(b"\xff\xfe")
    invalid.chmod(0o600)
    with pytest.raises(ValueError, match="private regular file"):
        read_private_text(invalid, label="test")


def test_wrong_owner_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _private(tmp_path / "token")
    monkeypatch.setattr(
        "ormas_subnet._private_file.os.geteuid", lambda: path.stat().st_uid + 1,
    )
    with pytest.raises(ValueError, match="private regular file"):
        read_private_text(path, label="test")


def test_errors_do_not_echo_secret_contents(tmp_path: Path) -> None:
    secret = "do-not-echo-this-value"
    path = _private(tmp_path / "token", secret)
    path.chmod(0o644)
    with pytest.raises(ValueError) as excinfo:
        load_token(token_path=path)
    assert secret not in str(excinfo.value)
