from __future__ import annotations

import hashlib
from dataclasses import dataclass

MAX_INTAKE_BYTES = 5 * 1024 * 1024
MAX_WEBHOOK_BYTES = 1 * 1024 * 1024
FORMULA_PREFIXES = ("=", "+", "-", "@")

_MAGIC_KINDS: tuple[tuple[bytes, str], ...] = (
    (b"MZ", "executable"),
    (b"\x7fELF", "executable"),
    (b"PK\x03\x04", "zip"),
    (b"PK\x05\x06", "zip"),
    (b"PK\x07\x08", "zip"),
    (b"\x1f\x8b", "gzip"),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "ole_compound_document"),
)


@dataclass(frozen=True, slots=True)
class QuarantineVerdict:
    accepted: bool
    kind: str
    sha256: str
    reason: str
    text: str | None = None
    size_bytes: int = 0
    claimed_mime: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "accepted": self.accepted,
            "kind": self.kind,
            "sha256": self.sha256,
            "reason": self.reason,
            "text": self.text,
            "size_bytes": self.size_bytes,
            "claimed_mime": self.claimed_mime,
        }


def quarantine_intake(
    data: bytes,
    *,
    claimed_mime: str = "",
    max_bytes: int = MAX_INTAKE_BYTES,
) -> QuarantineVerdict:
    """Validate a text intake payload by bytes, not by extension or claimed MIME."""

    digest = hashlib.sha256(data).hexdigest()
    if len(data) > max_bytes:
        return _reject("too_large", digest, "payload exceeds size cap", data, claimed_mime)

    magic_kind = _magic_kind(data)
    if magic_kind is not None:
        return _reject(
            magic_kind,
            digest,
            f"payload starts with {magic_kind} magic bytes",
            data,
            claimed_mime,
        )
    if b"\x00" in data:
        return _reject("binary", digest, "NUL byte found in text payload", data, claimed_mime)
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return _reject("binary", digest, "payload is not strict UTF-8", data, claimed_mime)

    return QuarantineVerdict(
        accepted=True,
        kind="text",
        sha256=digest,
        reason="accepted",
        text=neutralise_formula_text(text),
        size_bytes=len(data),
        claimed_mime=claimed_mime,
    )


def quarantine_webhook_body(
    data: bytes,
    *,
    claimed_mime: str = "application/json",
    max_bytes: int = MAX_WEBHOOK_BYTES,
) -> QuarantineVerdict:
    """Apply the webhook body size cap before source-specific parsing runs."""

    digest = hashlib.sha256(data).hexdigest()
    if len(data) > max_bytes:
        return _reject("too_large", digest, "webhook body exceeds size cap", data, claimed_mime)
    return QuarantineVerdict(
        accepted=True,
        kind="webhook_body",
        sha256=digest,
        reason="accepted",
        size_bytes=len(data),
        claimed_mime=claimed_mime,
    )


def neutralise_formula_text(text: str) -> str:
    """Demote spreadsheet formulas to plain text in comma-separated intake rows."""

    return "\n".join(
        ",".join(neutralise_formula(cell) for cell in line.split(","))
        for line in text.splitlines()
    ) + ("\n" if text.endswith("\n") else "")


def neutralise_formula(value: str) -> str:
    """Prefix spreadsheet formula-like cells so spreadsheet tools treat them as data."""

    stripped = value.lstrip()
    if stripped and stripped.startswith(FORMULA_PREFIXES):
        padding = value[: len(value) - len(stripped)]
        return f"{padding}'{stripped}"
    return value


def _magic_kind(data: bytes) -> str | None:
    for magic, kind in _MAGIC_KINDS:
        if data.startswith(magic):
            return kind
    return None


def _reject(
    kind: str,
    digest: str,
    reason: str,
    data: bytes,
    claimed_mime: str,
) -> QuarantineVerdict:
    return QuarantineVerdict(
        accepted=False,
        kind=kind,
        sha256=digest,
        reason=reason,
        size_bytes=len(data),
        claimed_mime=claimed_mime,
    )
