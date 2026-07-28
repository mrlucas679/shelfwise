from __future__ import annotations

import pytest

from scripts.provision_new_shop import (
    main,
    provision_new_shop,
    slugify_tenant_id,
)
from shelfwise_backend.auth_credentials import login_credentials_valid


def _env_value(fragment: str, key: str) -> str:
    line = next(line for line in fragment.splitlines() if line.startswith(f"{key}="))
    return line.split("=", 1)[1]


def test_slugify_tenant_id_normalizes_company_name() -> None:
    assert slugify_tenant_id("Boxer  Bramley!!") == "boxer_bramley"
    assert slugify_tenant_id("  Kasi Grocer (Pty) Ltd  ") == "kasi_grocer_pty_ltd"


def test_slugify_rejects_a_name_with_no_usable_characters() -> None:
    with pytest.raises(ValueError):
        slugify_tenant_id("!!!")


def test_provisioned_password_hash_verifies_with_the_login_check() -> None:
    result = provision_new_shop(
        company_name="Boxer Bramley",
        owner_email="Owner@Boxer.example",
        owner_password="a real passphrase",
    )

    assert result.tenant_id == "boxer_bramley"
    assert result.owner_email == "owner@boxer.example"
    assert "SHELFWISE_LOGIN_PASSWORD_HASH=scrypt$" in result.env_fragment
    assert "a real passphrase" not in result.env_fragment

    configured_hash = _env_value(result.env_fragment, "SHELFWISE_LOGIN_PASSWORD_HASH")
    assert login_credentials_valid(
        email="owner@boxer.example",
        password="a real passphrase",
        configured_email="owner@boxer.example",
        configured_hash=configured_hash,
    )
    assert not login_credentials_valid(
        email="owner@boxer.example",
        password="wrong password",
        configured_email="owner@boxer.example",
        configured_hash=configured_hash,
    )


def test_omitted_password_generates_one_and_it_also_verifies() -> None:
    result = provision_new_shop(company_name="Kasi Grocer", owner_email="owner@kasi.example")

    configured_hash = _env_value(result.env_fragment, "SHELFWISE_LOGIN_PASSWORD_HASH")
    assert login_credentials_valid(
        email="owner@kasi.example",
        password=result.owner_password,
        configured_email="owner@kasi.example",
        configured_hash=configured_hash,
    )


def test_each_run_generates_a_distinct_tenant_secret() -> None:
    first = provision_new_shop(company_name="Shop A", owner_email="a@example.com")
    second = provision_new_shop(company_name="Shop A", owner_email="a@example.com")

    assert _env_value(first.env_fragment, "TENANT_AUTH_SECRET") != _env_value(
        second.env_fragment, "TENANT_AUTH_SECRET"
    )


def test_cli_main_writes_env_file(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("EXISTING_VAR=1\n", encoding="utf-8")

    exit_code = main(
        [
            "--company",
            "Boxer Bramley",
            "--owner-email",
            "owner@boxer.example",
            "--owner-password",
            "a real passphrase",
            "--write-env",
            str(env_path),
        ]
    )

    assert exit_code == 0
    written = env_path.read_text(encoding="utf-8")
    assert "EXISTING_VAR=1" in written
    assert "SHELFWISE_TENANT_ID=boxer_bramley" in written
    captured = capsys.readouterr()
    assert "boxer_bramley" in captured.out
