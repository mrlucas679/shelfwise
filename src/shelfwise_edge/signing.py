from __future__ import annotations

import hashlib
import hmac

MAX_EDGE_BODY_BYTES = 512_000


def verify_signed_body(body: bytes, signature: str, secret: bytes) -> bool:
    """Verify an HMAC-SHA256 body signature without logging or retaining the payload."""
    if len(body) > MAX_EDGE_BODY_BYTES or not secret:
        return False
    if not signature.startswith("sha256="):
        return False
    supplied = signature.removeprefix("sha256=")
    if len(supplied) != 64:
        return False
    expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(supplied, expected)
