from __future__ import annotations

import time

from starlette.requests import Request

from shelfwise_backend.deps import _write_rate_limit_identity
from shelfwise_backend.tenant import encode_hs256_token


def _request(headers: dict[str, str]) -> Request:
    encoded = [
        (key.lower().encode(), value.encode()) for key, value in headers.items()
    ]
    scope = {
        "type": "http",
        "headers": encoded,
        "client": ("203.0.113.5", 1234),
        "method": "POST",
        "path": "/scenarios/golden",
    }
    return Request(scope)


def test_write_rate_limit_keys_on_verified_tenant_not_the_shared_secret(monkeypatch) -> None:
    """Two different tenants authenticated with the same JWT secret (the normal case - the
    API key/secret is shared infrastructure, not per-tenant) must land in different rate
    buckets, or one tenant's write traffic can exhaust another tenant's write budget."""
    monkeypatch.setenv("SHELFWISE_AUTH_MODE", "jwt")
    monkeypatch.setenv("TENANT_AUTH_SECRET", "secret")

    def _token(tenant: str) -> str:
        return encode_hs256_token(
            {
                "tenant_id": tenant,
                "user_id": "u",
                "role": "manager",
                "exp": int(time.time()) + 3600,
            },
            secret="secret",
        )

    request_a = _request({"authorization": f"Bearer {_token('tenant-a')}"})
    request_b = _request({"authorization": f"Bearer {_token('tenant-b')}"})

    identity_a = _write_rate_limit_identity(request_a)
    identity_b = _write_rate_limit_identity(request_b)

    assert identity_a != identity_b
    assert "tenant-a" in identity_a
    assert "tenant-b" in identity_b


def test_write_rate_limit_falls_back_to_shared_identity_outside_jwt_mode(monkeypatch) -> None:
    monkeypatch.setenv("SHELFWISE_AUTH_MODE", "off")
    request = _request({})

    identity = _write_rate_limit_identity(request)

    assert identity.startswith("ip:")
