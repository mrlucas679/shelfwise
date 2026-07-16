from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from shelfwise_contracts import Money

from ...canonical import SourceSystem
from ...provenance import InboundRecord, ValidationResult, raw_payload_hash

FetchJson = Callable[[str, dict[str, str], dict[str, str]], Awaitable[dict[str, Any]]]
PostJson = Callable[[str, dict[str, Any], dict[str, str]], Awaitable[dict[str, Any]]]

_DEFAULT_HTTP_TIMEOUT_S = 20.0


def now_utc() -> datetime:
    return datetime.now(UTC)


def parse_time(value: object) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        parsed = now_utc()
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def parse_quantity(value: object) -> int | Decimal:
    """Parse whole or fractional POS quantities without binary-float coercion."""
    try:
        quantity = Decimal(str(value or "0"))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("quantity is not numeric") from exc
    if not quantity.is_finite():
        raise ValueError("quantity must be finite")
    if quantity == quantity.to_integral_value():
        return int(quantity)
    return quantity


def wrap(
    *,
    tenant_id: str,
    system: SourceSystem,
    object_type: str,
    object_id: str,
    event_time: datetime,
    canonical_type: str,
    canonical: Any,
    validation: ValidationResult,
    raw: dict[str, Any],
) -> InboundRecord:
    return InboundRecord(
        tenant_id=tenant_id,
        source_system=system,
        source_object_type=object_type,
        source_object_id=object_id,
        event_time=event_time,
        raw_payload=raw,
        canonical_type=canonical_type,
        canonical_payload=serialise(canonical),
        correlation_id=raw_payload_hash(raw)[:16],
        validation=validation,
        source_quality=source_quality(validation),
    )


def source_quality(validation: ValidationResult) -> float:
    if not validation.ok:
        return 0.3
    if validation.warnings:
        return 0.6
    return 1.0


def serialise(value: Any) -> Any:
    if isinstance(value, Money):
        return value.to_dict()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): serialise(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [serialise(item) for item in value]
    if is_dataclass(value):
        return serialise(asdict(value))
    if hasattr(value, "value") and isinstance(value.value, str):
        return value.value
    return value


async def http_get_json(
    url: str,
    params: dict[str, str],
    headers: dict[str, str],
) -> dict[str, Any]:
    import httpx

    async with httpx.AsyncClient(timeout=_DEFAULT_HTTP_TIMEOUT_S) as client:
        response = await client.get(url, params=params, headers=headers)
        response.raise_for_status()
        body = response.json()
    return body if isinstance(body, dict) else {"value": body}


async def http_post_json(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
) -> dict[str, Any]:
    import httpx

    async with httpx.AsyncClient(timeout=_DEFAULT_HTTP_TIMEOUT_S) as client:
        response = await client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        body = response.json()
    return body if isinstance(body, dict) else {"result": body}
