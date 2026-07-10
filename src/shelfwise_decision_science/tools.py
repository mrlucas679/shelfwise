from __future__ import annotations

from dataclasses import asdict, is_dataclass
from decimal import Decimal
from typing import Any

from shelfwise_contracts import Money, SourceRef


def serialise_tool_result(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Money):
        return value.to_dict()
    if isinstance(value, SourceRef):
        return value.to_dict()
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return serialise_tool_result(value.to_dict())
    if isinstance(value, list | tuple):
        return [serialise_tool_result(item) for item in value]
    if isinstance(value, dict):
        return {str(key): serialise_tool_result(item) for key, item in value.items()}
    if is_dataclass(value):
        return serialise_tool_result(asdict(value))
    return value
