"""Fail-closed single-slot containment for the reference checker."""
from __future__ import annotations

import multiprocessing

import pytest

from ormas_subnet import validator


@pytest.mark.parametrize("mode", [[], ["--once"], ["--list-only"]])
def test_cli_refuses_parallel_slots_before_reading_secrets_or_starting_runtime(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mode: list[str],
) -> None:
    secret_reads: list[object] = []

    def secret_read(**kwargs: object) -> str:
        secret_reads.append(kwargs)
        raise AssertionError("parallel configuration reached a secret read")

    monkeypatch.setattr(validator, "_load_cli_secret", secret_read)
    monkeypatch.setattr(
        validator,
        "OrmasValidatorClient",
        lambda **_kwargs: pytest.fail("parallel configuration constructed a gateway client"),
    )
    monkeypatch.setattr(
        validator,
        "make_ed25519_signer",
        lambda _key: pytest.fail("parallel configuration loaded a signing key"),
    )

    with pytest.raises(SystemExit) as exc:
        validator.main([
            "--gateway", "https://api.ormas.ai",
            "--token-env", "SYNTHETIC_TOKEN",
            "--private-key-env", "SYNTHETIC_KEY",
            "--slots", "2",
            *mode,
        ])

    assert exc.value.code != 0
    assert secret_reads == []
    assert "--slots must be exactly 1" in capsys.readouterr().err


@pytest.mark.parametrize("slots", [0, 2, -1, True, 1.0, "1"])
def test_direct_serve_refuses_non_exact_single_slot_before_work_or_process_creation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    slots: object,
) -> None:
    class Client:
        def list_assignments(self):
            raise AssertionError("parallel configuration polled assignments")

    def sign(_digest: str) -> str:
        raise AssertionError("parallel configuration reached the signer")

    monkeypatch.setattr(
        multiprocessing,
        "get_context",
        lambda *_args, **_kwargs: pytest.fail("parallel configuration created a process context"),
    )
    daemon = validator.ValidatorDaemon(
        Client(), validator.ValidatorConfig(workdir_root=tmp_path / "work"), sign,
    )

    with pytest.raises(ValueError, match="exactly one slot"):
        daemon.serve(slots, 0.01, until=lambda: True)  # type: ignore[arg-type]


def test_single_slot_serve_stays_inline(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    calls: list[str] = []

    class Client:
        pass

    daemon = validator.ValidatorDaemon(
        Client(), validator.ValidatorConfig(workdir_root=tmp_path / "work"),
        lambda _digest: pytest.fail("idle single-slot run reached the signer"),
    )
    monkeypatch.setattr(
        multiprocessing,
        "get_context",
        lambda *_args, **_kwargs: pytest.fail("single-slot review created a process context"),
    )
    monkeypatch.setattr(daemon, "run_once", lambda: calls.append("run_once") or False)

    daemon.serve(1, 0.01, until=lambda: bool(calls))

    assert calls == ["run_once"]


def test_retired_parallel_entry_points_cannot_be_called(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    side_effects: list[str] = []

    class Client:
        def list_assignments(self):
            side_effects.append("list")

        def post_decision(self, *_args: object, **_kwargs: object) -> None:
            side_effects.append("post")

    def sign(_digest: str) -> str:
        side_effects.append("sign")
        return "ab" * 64

    def process_context(*_args: object, **_kwargs: object) -> None:
        side_effects.append("process")

    daemon = validator.ValidatorDaemon(
        Client(), validator.ValidatorConfig(workdir_root=tmp_path / "work"),
        sign,
    )
    monkeypatch.setattr(multiprocessing, "get_context", process_context)

    for owner, name in [
        (daemon, "_serve_parallel"),
        (validator, "review_assignment"),
        (validator, "_child_process_main"),
        (validator, "_remember_post"),
        (validator, "_retry_pending_posts"),
        (validator, "_reap_reviews"),
        (validator, "_dispatch_reviews"),
        (validator, "_fill_slots"),
        (validator, "_stop_reviews"),
    ]:
        with pytest.raises(AttributeError, match=name):
            getattr(owner, name)

    assert side_effects == []
