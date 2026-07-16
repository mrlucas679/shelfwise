"""Persistent platform-skill manifests extending the existing earned-skill module.

Closes the plan's flagged Section 39/41 gap: `shelfwise_mlops.skills` mines Skill
playbooks from outcome history (a learning/governance artifact), but nothing implemented
the tool/skill CATALOGUE the assistant discovers progressively at conversation time, nor
the promotion lifecycle that gates which skills discovery may surface. This module owns
both:

- `SkillManifest` (blueprint 41.4): a validated, versioned declaration of one assistant
  capability - which agent owns it, which real tools it needs, who may use it, and the
  evaluation bar it must clear.
- Registries (in-memory + Postgres) storing manifests per tenant with a global default
  catalogue seeded from the platform's real read-only tool surface.
- `discover()`: deterministic progressive discovery - rank manifests by trigger-term
  hits against the question, filter by role and lifecycle status, cap the result - so
  the model only ever sees the skills relevant to this turn instead of every tool.
- `promote()` / `retire()`: the lifecycle gate. Discovery surfaces only promoted skills;
  a draft cannot reach a conversation until it passes its own evaluation bar.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from threading import Lock
from typing import Any

from shelfwise_storage import auto_schema_enabled, connect, jsonb
from shelfwise_storage.rls import apply_tenant_rls

_GLOBAL_TENANT = "__platform__"


@dataclass(frozen=True, slots=True)
class SkillManifest:
    id: str
    version: str
    name: str
    description: str
    domain_owner: str
    allowed_roles: tuple[str, ...]
    trigger_terms: tuple[str, ...]
    required_entity_types: tuple[str, ...]
    required_tools: tuple[str, ...]
    risk_tier: str
    read_only: bool
    max_context_tokens: int
    critic_required: bool
    hitl_required: bool
    source_refs: tuple[str, ...]
    evaluation_ids: tuple[str, ...]
    minimum_pass_rate: float
    tenant_id: str | None = None
    status: str = "draft"

    def validate(self, *, known_agents: set[str], known_tools: set[str]) -> None:
        """Reject orphaned, permission-expanding, or untestable skills."""
        if self.domain_owner not in known_agents:
            raise ValueError("skill domain owner is not an existing agent")
        if not set(self.required_tools).issubset(known_tools):
            raise ValueError("skill references an unknown tool")
        if not self.source_refs or not self.evaluation_ids:
            raise ValueError("skill requires sources and evaluations")
        if not 0.0 <= self.minimum_pass_rate <= 1.0:
            raise ValueError("invalid skill pass rate")
        if not self.read_only and not self.hitl_required:
            raise ValueError("write-capable skills require HITL")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "name": self.name,
            "description": self.description,
            "domain_owner": self.domain_owner,
            "allowed_roles": list(self.allowed_roles),
            "trigger_terms": list(self.trigger_terms),
            "required_entity_types": list(self.required_entity_types),
            "required_tools": list(self.required_tools),
            "risk_tier": self.risk_tier,
            "read_only": self.read_only,
            "max_context_tokens": self.max_context_tokens,
            "critic_required": self.critic_required,
            "hitl_required": self.hitl_required,
            "source_refs": list(self.source_refs),
            "evaluation_ids": list(self.evaluation_ids),
            "minimum_pass_rate": self.minimum_pass_rate,
            "tenant_id": self.tenant_id,
            "status": self.status,
        }


def _manifest_from_dict(payload: dict[str, Any]) -> SkillManifest:
    return SkillManifest(
        id=str(payload["id"]),
        version=str(payload["version"]),
        name=str(payload["name"]),
        description=str(payload["description"]),
        domain_owner=str(payload["domain_owner"]),
        allowed_roles=tuple(payload.get("allowed_roles") or ()),
        trigger_terms=tuple(payload.get("trigger_terms") or ()),
        required_entity_types=tuple(payload.get("required_entity_types") or ()),
        required_tools=tuple(payload.get("required_tools") or ()),
        risk_tier=str(payload["risk_tier"]),
        read_only=bool(payload["read_only"]),
        max_context_tokens=int(payload["max_context_tokens"]),
        critic_required=bool(payload["critic_required"]),
        hitl_required=bool(payload["hitl_required"]),
        source_refs=tuple(payload.get("source_refs") or ()),
        evaluation_ids=tuple(payload.get("evaluation_ids") or ()),
        minimum_pass_rate=float(payload["minimum_pass_rate"]),
        tenant_id=payload.get("tenant_id"),
        status=str(payload.get("status") or "draft"),
    )


class InMemorySkillRegistry:
    """Process-local registry used by the default zero-config runtime."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._manifests: dict[tuple[str, str], SkillManifest] = {}

    def upsert(
        self, manifest: SkillManifest, *, known_agents: set[str], known_tools: set[str]
    ) -> SkillManifest:
        manifest.validate(known_agents=known_agents, known_tools=known_tools)
        key = (manifest.tenant_id or _GLOBAL_TENANT, manifest.id)
        with self._lock:
            self._manifests[key] = manifest
            return manifest

    def get(self, skill_id: str, *, tenant_id: str | None = None) -> SkillManifest | None:
        with self._lock:
            scoped = self._manifests.get((tenant_id or _GLOBAL_TENANT, skill_id))
            if scoped is not None:
                return scoped
            return self._manifests.get((_GLOBAL_TENANT, skill_id))

    def list(self, *, tenant_id: str | None = None) -> list[SkillManifest]:
        with self._lock:
            rows = [
                manifest
                for (owner, _skill_id), manifest in self._manifests.items()
                if owner in {_GLOBAL_TENANT, tenant_id or _GLOBAL_TENANT}
            ]
            return sorted(rows, key=lambda manifest: manifest.id)

    def set_status(
        self, skill_id: str, status: str, *, tenant_id: str | None = None
    ) -> SkillManifest | None:
        with self._lock:
            for owner in (tenant_id or _GLOBAL_TENANT, _GLOBAL_TENANT):
                key = (owner, skill_id)
                manifest = self._manifests.get(key)
                if manifest is not None:
                    updated = replace(manifest, status=status)
                    self._manifests[key] = updated
                    return updated
            return None

    def clear(self) -> None:
        with self._lock:
            self._manifests.clear()


class PostgresSkillRegistry:
    """Durable registry protected by tenant RLS; platform-global rows use a fixed owner."""

    def __init__(self, database_url: str) -> None:
        if not database_url:
            raise ValueError("DATABASE_URL is required for PostgresSkillRegistry")
        self._database_url = database_url
        if auto_schema_enabled():
            self._ensure_schema()

    def upsert(
        self, manifest: SkillManifest, *, known_agents: set[str], known_tools: set[str]
    ) -> SkillManifest:
        manifest.validate(known_agents=known_agents, known_tools=known_tools)
        owner = manifest.tenant_id or _GLOBAL_TENANT
        with connect(self._database_url, tenant_id=owner) as conn:
            conn.execute(
                """
                insert into shelfwise_skill_manifests
                    (tenant_id, skill_id, version, domain_owner, status, manifest, created_at)
                values (%s, %s, %s, %s, %s, %s, %s)
                on conflict (tenant_id, skill_id) do update
                set version = excluded.version,
                    domain_owner = excluded.domain_owner,
                    status = excluded.status,
                    manifest = excluded.manifest
                """,
                (
                    owner,
                    manifest.id,
                    manifest.version,
                    manifest.domain_owner,
                    manifest.status,
                    jsonb(manifest.to_dict()),
                    datetime.now(UTC).isoformat(),
                ),
            )
            conn.commit()
        return manifest

    def get(self, skill_id: str, *, tenant_id: str | None = None) -> SkillManifest | None:
        for owner in (tenant_id or _GLOBAL_TENANT, _GLOBAL_TENANT):
            with connect(self._database_url, tenant_id=owner) as conn:
                row = conn.execute(
                    """
                    select manifest from shelfwise_skill_manifests
                    where tenant_id = %s and skill_id = %s
                    """,
                    (owner, skill_id),
                ).fetchone()
            if row:
                return _manifest_from_dict(row["manifest"])
        return None

    def list(self, *, tenant_id: str | None = None) -> list[SkillManifest]:
        manifests: dict[str, SkillManifest] = {}
        for owner in (_GLOBAL_TENANT, tenant_id or _GLOBAL_TENANT):
            with connect(self._database_url, tenant_id=owner) as conn:
                rows = conn.execute(
                    "select manifest from shelfwise_skill_manifests where tenant_id = %s",
                    (owner,),
                ).fetchall()
            for row in rows:
                manifest = _manifest_from_dict(row["manifest"])
                manifests[manifest.id] = manifest
        return sorted(manifests.values(), key=lambda manifest: manifest.id)

    def set_status(
        self, skill_id: str, status: str, *, tenant_id: str | None = None
    ) -> SkillManifest | None:
        current = self.get(skill_id, tenant_id=tenant_id)
        if current is None:
            return None
        updated = replace(current, status=status)
        owner = updated.tenant_id or _GLOBAL_TENANT
        with connect(self._database_url, tenant_id=owner) as conn:
            conn.execute(
                """
                update shelfwise_skill_manifests
                set status = %s, manifest = %s
                where tenant_id = %s and skill_id = %s
                """,
                (status, jsonb(updated.to_dict()), owner, skill_id),
            )
            conn.commit()
        return updated

    def clear(self) -> None:
        with connect(self._database_url, tenant_id=_GLOBAL_TENANT) as conn:
            conn.execute(
                "delete from shelfwise_skill_manifests where tenant_id = %s", (_GLOBAL_TENANT,)
            )
            conn.commit()

    def _ensure_schema(self) -> None:
        with connect(self._database_url, tenant_id=_GLOBAL_TENANT) as conn:
            conn.execute(
                """
                create table if not exists shelfwise_skill_manifests (
                    tenant_id text not null,
                    skill_id text not null,
                    version text not null,
                    domain_owner text not null,
                    status text not null,
                    manifest jsonb not null,
                    created_at timestamptz not null,
                    primary key (tenant_id, skill_id)
                )
                """
            )
            apply_tenant_rls(conn, ("shelfwise_skill_manifests",))
            conn.commit()


def create_skill_registry() -> InMemorySkillRegistry | PostgresSkillRegistry:
    backend = os.getenv("SHELFWISE_STORE_BACKEND", "memory").strip().lower()
    if backend == "memory":
        return InMemorySkillRegistry()
    if backend == "postgres":
        return PostgresSkillRegistry(os.getenv("DATABASE_URL", ""))
    raise ValueError(f"unsupported SHELFWISE_STORE_BACKEND: {backend}")


def discover(
    registry: Any,
    *,
    question: str,
    role: str,
    tenant_id: str | None = None,
    limit: int = 3,
) -> list[SkillManifest]:
    """Deterministic progressive discovery: rank promoted skills by trigger-term hits.

    Only promoted manifests are discoverable - that is the entire point of the lifecycle
    gate. Ranking is trigger-term hit count (ties broken by skill id for determinism);
    role filtering is enforced here, before anything reaches a prompt.
    """
    if limit < 1:
        raise ValueError("limit must be at least 1")
    lowered = question.lower()
    scored: list[tuple[int, SkillManifest]] = []
    for manifest in registry.list(tenant_id=tenant_id):
        if manifest.status != "promoted":
            continue
        if manifest.allowed_roles and role not in manifest.allowed_roles:
            continue
        hits = sum(1 for term in manifest.trigger_terms if term.lower() in lowered)
        if hits > 0:
            scored.append((hits, manifest))
    scored.sort(key=lambda pair: (-pair[0], pair[1].id))
    return [manifest for _hits, manifest in scored[:limit]]


def promote(
    registry: Any,
    skill_id: str,
    *,
    measured_pass_rate: float,
    tenant_id: str | None = None,
) -> SkillManifest:
    """Flip draft -> promoted only when the skill clears its own evaluation bar."""
    manifest = registry.get(skill_id, tenant_id=tenant_id)
    if manifest is None:
        raise ValueError(f"unknown skill: {skill_id}")
    if manifest.status == "retired":
        raise ValueError("a retired skill cannot be promoted; re-register a new version")
    if measured_pass_rate < manifest.minimum_pass_rate:
        raise ValueError(
            f"measured pass rate {measured_pass_rate} is below the skill's required "
            f"{manifest.minimum_pass_rate}"
        )
    return registry.set_status(skill_id, "promoted", tenant_id=tenant_id)


def retire(registry: Any, skill_id: str, *, tenant_id: str | None = None) -> SkillManifest:
    manifest = registry.set_status(skill_id, "retired", tenant_id=tenant_id)
    if manifest is None:
        raise ValueError(f"unknown skill: {skill_id}")
    return manifest


def default_skill_manifests() -> tuple[SkillManifest, ...]:
    """The platform's built-in read-only skill catalogue, mapped to REAL tools/agents.

    Every manifest here validates against the actual platform tool surface and ships
    promoted: these are the capabilities the running cascades already exercise daily,
    with the platform test suite as their standing evaluation.
    """

    def manifest(
        skill_id: str,
        name: str,
        description: str,
        domain_owner: str,
        trigger_terms: tuple[str, ...],
        required_tools: tuple[str, ...],
    ) -> SkillManifest:
        return SkillManifest(
            id=skill_id,
            version="1.0.0",
            name=name,
            description=description,
            domain_owner=domain_owner,
            allowed_roles=("manager", "owner", "associate"),
            trigger_terms=trigger_terms,
            required_entity_types=("sku",),
            required_tools=required_tools,
            risk_tier="low",
            read_only=True,
            max_context_tokens=1_000,
            critic_required=False,
            hitl_required=False,
            source_refs=("capabilities/manifest.json",),
            evaluation_ids=("tests/test_model_tool_calling.py",),
            minimum_pass_rate=1.0,
            tenant_id=None,
            status="promoted",
        )

    return (
        manifest(
            "stock_position_lookup",
            "Stock position lookup",
            "Answer on-hand, location, and expiry questions for one SKU from measured stock.",
            "inventory",
            ("stock", "on hand", "units", "inventory", "how many"),
            ("get_stock",),
        ),
        manifest(
            "demand_forecast_lookup",
            "Demand forecast lookup",
            "Report the forecast daily demand and payday-adjusted horizon for one SKU.",
            "demand",
            ("demand", "forecast", "sell", "how fast", "daily units"),
            ("get_demand_forecast",),
        ),
        manifest(
            "expiry_risk_review",
            "Expiry risk review",
            "Explain expiry risk and time-to-expiry economics for one SKU.",
            "expiry",
            ("expiry", "expire", "shelf life", "waste", "write-off"),
            ("get_expiry_risk",),
        ),
        manifest(
            "markdown_simulation",
            "Markdown simulation",
            "Simulate the profit impact of a candidate markdown before recommending it.",
            "simulation",
            ("markdown", "discount", "price cut", "promotion"),
            ("simulate_markdown",),
        ),
        manifest(
            "cold_chain_status",
            "Cold chain status",
            "Report measured cold-chain risk and outage posture for a storage area.",
            "cold_chain",
            ("cold chain", "fridge", "freezer", "temperature", "outage", "loadshedding"),
            ("get_cold_chain_status",),
        ),
        manifest(
            "reorder_policy_review",
            "Reorder policy review",
            "Explain the computed reorder point, order quantity, and stockout exposure.",
            "procurement",
            ("reorder", "order more", "replenish", "purchase order", "supplier order"),
            ("get_reorder_policy", "get_supplier_ranking"),
        ),
        manifest(
            "price_integrity_check",
            "Price integrity check",
            "Check a sale price against the catalogue and flag exceptions for review.",
            "sales",
            ("price", "charged", "overcharge", "price check", "till"),
            ("check_price_integrity",),
        ),
        manifest(
            "delivery_status_lookup",
            "Delivery status lookup",
            "Report open purchase orders and inbound delivery coverage for one SKU.",
            "procurement",
            ("delivery", "shipment", "inbound", "arriving", "on order"),
            ("get_delivery_status",),
        ),
    )
