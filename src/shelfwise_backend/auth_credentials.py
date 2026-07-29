"""Scrypt password hashing shared by `/auth/login` and the shop-provisioning script.

Extracted out of `app.py` so `scripts/provision_new_shop.py` can mint a
`SHELFWISE_LOGIN_PASSWORD_HASH` using the exact same algorithm `/auth/login` verifies with,
rather than a second, driftable implementation.
"""

from __future__ import annotations

import hashlib
import hmac

_SCRYPT_N = 16384
_SCRYPT_R = 8
_SCRYPT_P = 1


def scrypt_password_hash(password: str, *, salt: bytes | None = None) -> str:
    """Create a self-describing scrypt credential without persisting plaintext.

    `salt` is exposed only for deterministic tests; real callers always let it be
    generated from `os.urandom`.
    """
    import os

    resolved_salt = salt if salt is not None else os.urandom(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=resolved_salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P
    )
    return f"scrypt${resolved_salt.hex()}${digest.hex()}"


def login_credentials_valid(
    *, email: str, password: str, configured_email: str, configured_hash: str
) -> bool:
    """Constant-shape verification: hash first, compare both, no early-exit oracle."""
    try:
        scheme, salt_hex, hash_hex = configured_hash.split("$", 2)
        if scheme != "scrypt":
            return False
        expected = bytes.fromhex(hash_hex)
        computed = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=_SCRYPT_N,
            r=_SCRYPT_R,
            p=_SCRYPT_P,
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    email_ok = hmac.compare_digest(email.strip().lower(), configured_email)
    password_ok = hmac.compare_digest(computed, expected)
    return email_ok and password_ok
