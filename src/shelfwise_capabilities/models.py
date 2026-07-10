from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CapabilityKind(StrEnum):
    """Capability categories covered by the repository contract."""

    AGENT = "agent"
    WORKFLOW = "workflow"
    OPENAPI_ROUTE = "openapi_route"
    CONNECTOR = "connector"
    TOOL = "tool"
    EVENT_TYPE = "event_type"
    EVENT_CONSUMER = "event_consumer"
    STORAGE_BACKEND = "storage_backend"
    BUS_BACKEND = "bus_backend"
    WORKER = "worker"
    WORLDGEN_SCENARIO = "worldgen_scenario"
    MULTIMODAL_FEATURE = "multimodal_feature"
    TRAINING_STAGE = "training_stage"
    FRONTEND_SURFACE = "frontend_surface"
    DEPLOYMENT_PROFILE = "deployment_profile"


class CapabilityStatus(StrEnum):
    """Ordered evidence levels for a capability claim."""

    DECLARATION_ONLY = "declaration_only"
    PARTIAL = "partial"
    IMPLEMENTED = "implemented"
    VERIFIED = "verified"
    DEMO_VERIFIED = "demo_verified"


STATUS_RANK: dict[CapabilityStatus, int] = {
    CapabilityStatus.DECLARATION_ONLY: 0,
    CapabilityStatus.PARTIAL: 1,
    CapabilityStatus.IMPLEMENTED: 2,
    CapabilityStatus.VERIFIED: 3,
    CapabilityStatus.DEMO_VERIFIED: 4,
}


class RelationshipKind(StrEnum):
    """Supported dependency edges between capability records."""

    REQUIRES = "requires"
    CONSUMES = "consumes"
    EXPOSES = "exposes"
    USES = "uses"


class WaiverRule(StrEnum):
    """Contract failures that may be waived temporarily and explicitly."""

    OMISSION = "omission"
    DOWNGRADE = "downgrade"
    RELATIONSHIP_REMOVAL = "relationship_removal"
    MISSING_VERIFICATION = "missing_verification"
    DISCOVERY_MISMATCH = "discovery_mismatch"


class ContractModel(BaseModel):
    """Strict base model used by every committed contract document."""

    model_config = ConfigDict(extra="forbid")


class SourceLocation(ContractModel):
    """Stable source pointer proving where a capability was discovered."""

    path: str = Field(min_length=1)
    line: int = Field(ge=1)
    symbol: str | None = None

    @field_validator("path")
    @classmethod
    def normalize_path(cls, value: str) -> str:
        """Store repository paths with portable separators."""
        return value.replace("\\", "/")


class CapabilityRelationship(ContractModel):
    """Typed edge from one capability to another manifest record."""

    kind: RelationshipKind
    target: str = Field(min_length=3)


class CapabilityBase(ContractModel):
    """Fields shared by every capability category."""

    id: str = Field(pattern=r"^[a-z_]+:.+")
    kind: CapabilityKind
    name: str = Field(min_length=1)
    status: CapabilityStatus
    sources: list[SourceLocation] = Field(min_length=1)
    verification_nodeids: list[str] = Field(default_factory=list)
    relationships: list[CapabilityRelationship] = Field(default_factory=list)
    note: str | None = None


class AgentCapability(CapabilityBase):
    """A decision role declared by contracts or inference metadata."""

    kind: Literal[CapabilityKind.AGENT] = CapabilityKind.AGENT
    agent_name: str
    metadata_only: bool = False


class WorkflowCapability(CapabilityBase):
    """A deterministic workflow entry point."""

    kind: Literal[CapabilityKind.WORKFLOW] = CapabilityKind.WORKFLOW
    entrypoint: str


class OpenAPIRouteCapability(CapabilityBase):
    """One OpenAPI path and HTTP method pair."""

    kind: Literal[CapabilityKind.OPENAPI_ROUTE] = CapabilityKind.OPENAPI_ROUTE
    path: str = Field(pattern=r"^/")
    method: Literal["DELETE", "GET", "PATCH", "POST", "PUT"]
    operation_id: str


class ConnectorCapability(CapabilityBase):
    """Connector state across enum declaration, catalogue, and mapper registration."""

    kind: Literal[CapabilityKind.CONNECTOR] = CapabilityKind.CONNECTOR
    system: str
    declared: bool
    catalogued: bool
    mapped: bool
    transport: str | None = None
    mapper_registered_claim: bool | None = None


class ToolCapability(CapabilityBase):
    """Callable platform or multimodal tool exposed to an agent/runtime."""

    kind: Literal[CapabilityKind.TOOL] = CapabilityKind.TOOL
    tool_name: str
    surface: Literal["multimodal", "platform"]


class EventTypeCapability(CapabilityBase):
    """Canonical event type and its discovered consumers."""

    kind: Literal[CapabilityKind.EVENT_TYPE] = CapabilityKind.EVENT_TYPE
    event_type: str
    consumers: list[str] = Field(default_factory=list)


class EventConsumerCapability(CapabilityBase):
    """A worker branch that consumes one canonical event type."""

    kind: Literal[CapabilityKind.EVENT_CONSUMER] = CapabilityKind.EVENT_CONSUMER
    consumer: str
    event_type: str


class StorageBackendCapability(CapabilityBase):
    """Storage backend selectable through repository factories."""

    kind: Literal[CapabilityKind.STORAGE_BACKEND] = CapabilityKind.STORAGE_BACKEND
    backend: str
    factories: list[str] = Field(min_length=1)


class BusBackendCapability(CapabilityBase):
    """Event-bus backend selectable through the bus factory."""

    kind: Literal[CapabilityKind.BUS_BACKEND] = CapabilityKind.BUS_BACKEND
    backend: str


class WorkerCapability(CapabilityBase):
    """Background worker or worker-loop implementation."""

    kind: Literal[CapabilityKind.WORKER] = CapabilityKind.WORKER
    worker: str


class WorldgenScenarioCapability(CapabilityBase):
    """Named deterministic world-generation scenario."""

    kind: Literal[CapabilityKind.WORLDGEN_SCENARIO] = CapabilityKind.WORLDGEN_SCENARIO
    scenario_id: str
    seed: int


class MultimodalFeatureCapability(CapabilityBase):
    """Speech, vision, or event-intake feature entry point."""

    kind: Literal[CapabilityKind.MULTIMODAL_FEATURE] = CapabilityKind.MULTIMODAL_FEATURE
    feature: str
    entrypoint: str


class TrainingStageCapability(CapabilityBase):
    """Training pipeline stage and whether real runtime execution is verified."""

    kind: Literal[CapabilityKind.TRAINING_STAGE] = CapabilityKind.TRAINING_STAGE
    stage: str
    entrypoint: str
    runtime_verified: bool


class FrontendSurfaceCapability(CapabilityBase):
    """User-visible frontend workspace or operational panel."""

    kind: Literal[CapabilityKind.FRONTEND_SURFACE] = CapabilityKind.FRONTEND_SURFACE
    surface: str


class DeploymentProfileCapability(CapabilityBase):
    """Runnable or configured deployment profile."""

    kind: Literal[CapabilityKind.DEPLOYMENT_PROFILE] = CapabilityKind.DEPLOYMENT_PROFILE
    profile: str
    storage_backend: str
    bus_backend: str
    worker_enabled: bool
    inference_provider: str


Capability = Annotated[
    AgentCapability
    | WorkflowCapability
    | OpenAPIRouteCapability
    | ConnectorCapability
    | ToolCapability
    | EventTypeCapability
    | EventConsumerCapability
    | StorageBackendCapability
    | BusBackendCapability
    | WorkerCapability
    | WorldgenScenarioCapability
    | MultimodalFeatureCapability
    | TrainingStageCapability
    | FrontendSurfaceCapability
    | DeploymentProfileCapability,
    Field(discriminator="kind"),
]


class CapabilityManifest(ContractModel):
    """Normalized, fingerprinted capability snapshot committed to the repository."""

    schema_version: Literal["1.0"] = "1.0"
    generator: Literal["shelfwise_capabilities"] = "shelfwise_capabilities"
    fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    capabilities: list[Capability]


class CapabilityAnnotation(ContractModel):
    """Policy override for evidence status, verification, or an honesty note."""

    status: CapabilityStatus | None = None
    verification_nodeids: list[str] | None = None
    note: str | None = None


class CapabilityWaiver(ContractModel):
    """Explicit exception that expires and names an accountable owner."""

    id: str = Field(pattern=r"^waiver:[a-z0-9_.-]+$")
    capability_id: str = Field(min_length=3)
    rules: list[WaiverRule] = Field(min_length=1)
    reason: str = Field(min_length=12)
    owner: str = Field(min_length=2)
    expires_on: date
    issue: str | None = None


class CapabilityPolicy(ContractModel):
    """Validation policy, verification defaults, annotations, and waivers."""

    schema_version: Literal["1.0"] = "1.0"
    required_kinds: list[CapabilityKind]
    required_capability_ids: list[str]
    verification_required_statuses: list[CapabilityStatus]
    default_verification_nodeids: dict[CapabilityKind, list[str]]
    annotations: dict[str, CapabilityAnnotation] = Field(default_factory=dict)
    max_waiver_days: int = Field(default=45, ge=1, le=180)
    waivers: list[CapabilityWaiver] = Field(default_factory=list)


class DeploymentProfile(ContractModel):
    """Source profile used to discover deployment capabilities deterministically."""

    id: str = Field(pattern=r"^[a-z0-9_.-]+$")
    name: str
    status: CapabilityStatus
    source_paths: list[str] = Field(min_length=1)
    storage_backend: str
    bus_backend: str
    worker_enabled: bool
    inference_provider: str
    note: str | None = None

    @field_validator("source_paths")
    @classmethod
    def normalize_source_paths(cls, values: list[str]) -> list[str]:
        """Store profile source paths with portable separators."""
        return [value.replace("\\", "/") for value in values]


class DeploymentProfileSnapshot(ContractModel):
    """Normalized deployment-profile source document."""

    schema_version: Literal["1.0"] = "1.0"
    profiles: list[DeploymentProfile]
