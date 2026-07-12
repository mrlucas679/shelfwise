from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class Role(StrEnum):
    OWNER = "owner"
    EXECUTIVE = "executive"
    MANAGER = "manager"
    INVENTORY = "inventory"
    ANALYST = "analyst"
    AUDITOR = "auditor"


@dataclass(frozen=True, slots=True)
class TenantContext:
    tenant_id: str
    user_id: str
    role: Role

    def to_dict(self) -> dict[str, str]:
        return {
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "role": self.role.value,
        }


def default_tenant_context() -> TenantContext:
    tenant_id = os.getenv("SHELFWISE_TENANT_ID") or os.getenv("TENANT_ID") or "local"
    return TenantContext(
        tenant_id=tenant_id.strip() or "local",
        user_id="local",
        role=Role.OWNER,
    )


def verify_bearer_token(authorization: str | None, *, secret: str) -> TenantContext:
    if not authorization or not authorization.startswith("Bearer "):
        raise ValueError("missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    return tenant_context_from_jwt(token, secret=secret)


def tenant_context_from_jwt(token: str, *, secret: str) -> TenantContext:
    if not secret:
        raise ValueError("tenant auth secret is not configured")
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("invalid token shape")
    header_raw, payload_raw, signature_raw = parts
    header = _loads(header_raw)
    if header.get("alg") != "HS256":
        raise ValueError("unsupported token algorithm")
    expected = _sign(f"{header_raw}.{payload_raw}", secret)
    if not hmac.compare_digest(expected, signature_raw):
        raise ValueError("invalid token signature")

    payload = _loads(payload_raw)
    exp = payload.get("exp")
    if exp is not None and int(exp) < int(time.time()):
        raise ValueError("token expired")
    tenant_id = _required_string(payload, "tenant_id")
    user_id = _required_string(payload, "user_id")
    role = Role(_required_string(payload, "role"))
    return TenantContext(tenant_id=tenant_id, user_id=user_id, role=role)


def encode_hs256_token(claims: dict[str, Any], *, secret: str) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    header_raw = _dumps(header)
    payload_raw = _dumps(claims)
    signature = _sign(f"{header_raw}.{payload_raw}", secret)
    return f"{header_raw}.{payload_raw}.{signature}"


def _required_string(payload: dict[str, Any], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"token missing {field_name}")
    return value.strip()


def _loads(value: str) -> dict[str, Any]:
    try:
        decoded = base64.urlsafe_b64decode(_pad(value)).decode("utf-8")
        data = json.loads(decoded)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError("invalid token encoding") from exc
    if not isinstance(data, dict):
        raise ValueError("invalid token payload")
    return data


def _dumps(value: dict[str, Any]) -> str:
    raw = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")


def _sign(signing_input: str, secret: str) -> str:
    digest = hmac.new(
        secret.encode("utf-8"),
        signing_input.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest).decode("utf-8").rstrip("=")


def _pad(value: str) -> bytes:
    return (value + "=" * (-len(value) % 4)).encode("utf-8")
