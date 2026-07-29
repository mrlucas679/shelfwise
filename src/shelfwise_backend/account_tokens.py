"""Single-use signed tokens for workforce invitation and password recovery."""

from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from .tenant import decode_hs256_claims, encode_hs256_token

AccountTokenPurpose = Literal["activation", "password_reset"]


@dataclass(frozen=True, slots=True)
class IssuedAccountToken:
    """Transient raw token plus the digest and expiry safe to persist."""

    token: str
    token_hash: str
    expires_at: str


@dataclass(frozen=True, slots=True)
class AccountTokenClaims:
    """Verified identity boundary carried by an account lifecycle token."""

    tenant_id: str
    account_id: str


def issue_account_token(
    *,
    purpose: AccountTokenPurpose,
    tenant_id: str,
    account_id: str,
    secret: str,
    lifetime_seconds: int,
) -> IssuedAccountToken:
    """Mint a short-lived token while exposing only its digest for persistence."""
    if not secret:
        raise ValueError("Tenant authentication is unavailable")
    if lifetime_seconds < 60:
        raise ValueError("Account token lifetime must be at least 60 seconds")
    expires = int(time.time()) + lifetime_seconds
    token = encode_hs256_token(
        {
            "purpose": purpose,
            "tenant_id": tenant_id,
            "account_id": account_id,
            "nonce": secrets.token_urlsafe(24),
            "exp": expires,
        },
        secret=secret,
    )
    return IssuedAccountToken(
        token=token,
        token_hash=token_digest(token),
        expires_at=datetime.fromtimestamp(expires, tz=UTC).isoformat(),
    )


def verify_account_token(
    token: str,
    *,
    purpose: AccountTokenPurpose,
    secret: str,
) -> AccountTokenClaims:
    """Verify signature, expiry, purpose, and required account identity claims."""
    claims = decode_hs256_claims(token, secret=secret)
    if claims.get("purpose") != purpose:
        raise ValueError("Invalid account token")
    tenant_id = _required_claim(claims, "tenant_id")
    account_id = _required_claim(claims, "account_id")
    _required_claim(claims, "nonce")
    return AccountTokenClaims(tenant_id=tenant_id, account_id=account_id)


def token_digest(token: str) -> str:
    """Hash an opaque lifecycle token before comparing it with stored state."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _required_claim(claims: dict[str, object], name: str) -> str:
    value = claims.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Invalid account token")
    return value.strip()
