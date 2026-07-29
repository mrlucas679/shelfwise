"""One-command startup: provisioning safety, health gating, and failure clarity."""

from __future__ import annotations

import json
import sys
from io import BytesIO
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import start_shelfwise
from start_shelfwise import (
    StartupError,
    compose_command,
    ensure_provisioned,
    wait_for_health,
)


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._body = BytesIO(json.dumps(payload).encode("utf-8"))

    def read(self) -> bytes:
        return self._body.read()

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_: object) -> None:
        return None


def test_first_run_writes_env_and_returns_the_generated_password(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"

    provisioned, owner_email, password = ensure_provisioned(
        company="Boxer Bramley",
        owner_email="Owner@Example.com",
        owner_password=None,
        env_path=env_path,
    )
    written = env_path.read_text(encoding="utf-8")

    assert provisioned is True
    assert owner_email == "owner@example.com"
    assert password
    assert "SHELFWISE_TENANT_ID=boxer_bramley" in written
    assert "SHELFWISE_AUTH_MODE=jwt" in written
    # The plaintext password must never reach the env file - only its scrypt hash.
    assert password not in written
    assert "SHELFWISE_LOGIN_PASSWORD_HASH=" in written


def test_rerun_never_overwrites_existing_secrets(tmp_path: Path) -> None:
    """Regenerating the encryption key would make stored credentials undecryptable."""
    env_path = tmp_path / ".env"
    ensure_provisioned(
        company="Boxer Bramley",
        owner_email="owner@example.com",
        owner_password="first-password",
        env_path=env_path,
    )
    original = env_path.read_text(encoding="utf-8")

    provisioned, owner_email, password = ensure_provisioned(
        company="Someone Else",
        owner_email="attacker@example.com",
        owner_password="different",
        env_path=env_path,
    )

    assert provisioned is False
    assert owner_email is None
    assert password is None
    assert env_path.read_text(encoding="utf-8") == original


def test_an_explicit_password_is_not_echoed_back_as_a_generated_one(tmp_path: Path) -> None:
    """Only a password this script invented should be printed to the operator."""
    env_path = tmp_path / ".env"

    _, _, password = ensure_provisioned(
        company="Boxer Bramley",
        owner_email="owner@example.com",
        owner_password="the-owner-already-knows-this",
        env_path=env_path,
    )

    assert password is None


def test_unprovisioned_run_without_details_explains_what_is_needed(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"

    with pytest.raises(StartupError) as excinfo:
        ensure_provisioned(
            company=None,
            owner_email=None,
            owner_password=None,
            env_path=env_path,
        )

    assert "--company" in str(excinfo.value)
    assert not env_path.exists()


def test_an_empty_env_file_still_counts_as_unprovisioned(tmp_path: Path) -> None:
    """A half-created empty .env must not be mistaken for a configured shop."""
    env_path = tmp_path / ".env"
    env_path.write_text("   \n", encoding="utf-8")

    provisioned, _, _ = ensure_provisioned(
        company="Boxer Bramley",
        owner_email="owner@example.com",
        owner_password="pw",
        env_path=env_path,
    )

    assert provisioned is True
    assert "SHELFWISE_TENANT_ID=boxer_bramley" in env_path.read_text(encoding="utf-8")


def test_health_wait_returns_once_the_backend_reports_ready() -> None:
    responses = [ConnectionError("not up yet"), _FakeResponse({"ok": True, "service": "shelfwise"})]

    def opener(url: str, timeout: float = 5) -> _FakeResponse:
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    payload = wait_for_health(timeout_s=5, interval_s=0, opener=opener)

    assert payload["ok"] is True
    assert responses == []


def test_health_wait_fails_closed_instead_of_hanging_forever() -> None:
    """A stack that never becomes healthy must report failure, not look like success."""

    def opener(url: str, timeout: float = 5) -> _FakeResponse:
        raise ConnectionError("backend refused the connection")

    with pytest.raises(StartupError) as excinfo:
        wait_for_health(timeout_s=0.2, interval_s=0.05, opener=opener)

    assert "did not become healthy" in str(excinfo.value)


def test_health_wait_rejects_a_not_ok_payload() -> None:
    """`ok: false` is a real degraded state and must not be treated as ready."""

    def opener(url: str, timeout: float = 5) -> _FakeResponse:
        return _FakeResponse({"ok": False, "detail": "bus unavailable"})

    with pytest.raises(StartupError):
        wait_for_health(timeout_s=0.2, interval_s=0.05, opener=opener)


def test_missing_docker_gives_an_installable_instruction_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first thing a non-technical shop owner can get wrong deserves a real answer."""
    monkeypatch.setattr(start_shelfwise.shutil, "which", lambda _: None)

    with pytest.raises(StartupError) as excinfo:
        compose_command()

    assert "Docker" in str(excinfo.value)
    assert "install" in str(excinfo.value).lower()


def test_compose_v2_plugin_is_preferred_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(start_shelfwise.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        start_shelfwise.subprocess,
        "run",
        lambda *a, **k: type("R", (), {"returncode": 0})(),
    )

    assert compose_command() == ["docker", "compose"]


def test_legacy_docker_compose_binary_is_used_when_the_plugin_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(start_shelfwise.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        start_shelfwise.subprocess,
        "run",
        lambda *a, **k: type("R", (), {"returncode": 1})(),
    )

    assert compose_command() == ["docker-compose"]
