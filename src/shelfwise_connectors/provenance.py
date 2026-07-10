from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .canonical import SourceSystem


@dataclass(frozen=True, slots=True)
class ValidationResult:
    ok: bool = True
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def fail(self, message: str) -> ValidationResult:
        return ValidationResult(
            ok=False,
            errors=(*self.errors, message),
            warnings=self.warnings,
        )

    def warn(self, message: str) -> ValidationResult:
        return ValidationResult(
            ok=self.ok,
            errors=self.errors,
            warnings=(*self.warnings, message),
        )

    def to_dict(self) -> dict[str, object]:
        return {"ok": self.ok, "errors": list(self.errors), "warnings": list(self.warnings)}


@dataclass(frozen=True, slots=True)
class InboundRecord:
    tenant_id: str
    source_system: SourceSystem
    source_object_type: str
    source_object_id: str
    event_time: datetime
    raw_payload: dict[str, Any]
    canonical_type: str
    correlation_id: str
    canonical_payload: dict[str, Any] = field(default_factory=dict)
    validation: ValidationResult = field(default_factory=ValidationResult)
    source_quality: float = 1.0
    actor: str | None = None

    @property
    def payload_hash(self) -> str:
        return raw_payload_hash(self.raw_payload)

    def to_dict(self) -> dict[str, object]:
        return {
            "tenant_id": self.tenant_id,
            "source_system": self.source_system.value,
            "source_object_type": self.source_object_type,
            "source_object_id": self.source_object_id,
            "event_time": self.event_time.isoformat(),
            "raw_payload_hash": self.payload_hash,
            "canonical_type": self.canonical_type,
            "canonical_payload": self.canonical_payload,
            "correlation_id": self.correlation_id,
            "validation": self.validation.to_dict(),
            "source_quality": self.source_quality,
            "actor": self.actor,
        }


def raw_payload_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
