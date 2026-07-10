from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SyntheticTag(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    synthetic: bool = True
    seed: int
    generator_version: str = "0.1.0"


class GoldenScenario(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    category: str
    tenant_id: str
    trigger_event: dict[str, Any]
    context: dict[str, Any]
    expected: dict[str, Any]
    source_records: list[dict[str, Any]] = Field(default_factory=list)
    invariants: list[str] = Field(default_factory=list)
    tag: SyntheticTag
