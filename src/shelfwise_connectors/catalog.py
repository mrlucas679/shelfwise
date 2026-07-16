from __future__ import annotations

from dataclasses import dataclass

from .canonical import SourceSystem


@dataclass(frozen=True, slots=True)
class ConnectorCapability:
    system: SourceSystem
    label: str
    transport: str
    priority: int
    read_supported: bool
    webhook_supported: bool
    mapper_registered: bool
    write_back_mode: str

    def to_dict(self) -> dict[str, object]:
        return {
            "system": self.system.value,
            "label": self.label,
            "transport": self.transport,
            "priority": self.priority,
            "read_supported": self.read_supported,
            "webhook_supported": self.webhook_supported,
            "mapper_registered": self.mapper_registered,
            "write_back_mode": self.write_back_mode,
        }


_CATALOG = [
    ConnectorCapability(SourceSystem.CSV, "CSV export", "file", 1, True, False, True, "task_only"),
    ConnectorCapability(SourceSystem.ODOO, "Odoo", "poll", 2, True, False, True, "task_only"),
    ConnectorCapability(SourceSystem.SQUARE, "Square", "webhook", 2, True, True, True, "task_only"),
    ConnectorCapability(
        SourceSystem.SAP,
        "SAP S/4HANA or B1",
        "poll",
        3,
        True,
        False,
        True,
        "task_only",
    ),
    ConnectorCapability(
        SourceSystem.SHOPIFY,
        "Shopify",
        "webhook",
        3,
        True,
        True,
        True,
        "task_only",
    ),
    ConnectorCapability(SourceSystem.SYSPRO, "SYSPRO", "poll", 3, True, False, True, "task_only"),
    ConnectorCapability(
        SourceSystem.LIGHTSPEED,
        "Lightspeed",
        "webhook",
        3,
        True,
        True,
        True,
        "task_only",
    ),
]


def list_connector_capabilities() -> list[ConnectorCapability]:
    return list(_CATALOG)


def connector_status_for_policy(policy: dict[str, object]) -> list[dict[str, object]]:
    raw_allowed = policy.get("allowed_systems", [])
    allowed_items = raw_allowed if isinstance(raw_allowed, list) else [raw_allowed]
    allowed = {
        str(item).strip().lower()
        for item in allowed_items
        if str(item).strip()
    }
    rows: list[dict[str, object]] = []
    for capability in list_connector_capabilities():
        item = capability.to_dict()
        enabled = capability.system.value in allowed
        item["enabled_for_tenant"] = enabled
        item["status"] = _status(capability, enabled)
        rows.append(item)
    return rows


def _status(capability: ConnectorCapability, enabled: bool) -> str:
    if enabled and capability.mapper_registered:
        return "enabled"
    if enabled and not capability.mapper_registered:
        return "configured_but_mapper_missing"
    if capability.mapper_registered:
        return "available"
    return "roadmap"
