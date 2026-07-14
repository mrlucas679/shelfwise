from __future__ import annotations

import ast
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from .models import (
    AgentCapability,
    BusBackendCapability,
    Capability,
    CapabilityAnnotation,
    CapabilityManifest,
    CapabilityPolicy,
    CapabilityRelationship,
    CapabilityStatus,
    ConnectorCapability,
    DeploymentProfileCapability,
    DeploymentProfileSnapshot,
    EventConsumerCapability,
    EventTypeCapability,
    FrontendSurfaceCapability,
    MultimodalFeatureCapability,
    OpenAPIRouteCapability,
    RelationshipKind,
    SourceLocation,
    StorageBackendCapability,
    ToolCapability,
    TrainingStageCapability,
    WorkerCapability,
    WorkflowCapability,
    WorldgenScenarioCapability,
)
from .serialization import build_manifest

_HTTP_METHODS = ("delete", "get", "patch", "post", "put")


def route_capability_id(method: str, path: str) -> str:
    """Build the stable capability id for an OpenAPI method/path pair."""
    return f"openapi_route:{method.lower()}:{path}"


def discover_manifest(
    repo_root: Path,
    policy: CapabilityPolicy,
    profiles: DeploymentProfileSnapshot,
) -> CapabilityManifest:
    """Discover all governed capabilities without importing application modules."""
    root = repo_root.resolve()
    capabilities: list[Capability] = []
    capabilities.extend(_discover_agents(root))
    capabilities.extend(_discover_workflows(root))
    capabilities.extend(_discover_openapi_routes(root))
    capabilities.extend(_discover_connectors(root))
    capabilities.extend(_discover_tools(root))
    capabilities.extend(_discover_workers(root))
    capabilities.extend(_discover_events(root))
    capabilities.extend(_discover_storage_backends(root))
    capabilities.extend(_discover_bus_backends(root))
    capabilities.extend(_discover_worldgen_scenarios(root))
    capabilities.extend(_discover_multimodal_features(root))
    capabilities.extend(_discover_training_stages(root))
    capabilities.extend(_discover_frontend_surfaces(root))
    capabilities.extend(_discover_deployment_profiles(root, profiles))
    annotated = [_apply_policy(item, policy) for item in capabilities]
    return build_manifest(annotated)


def _discover_agents(root: Path) -> list[AgentCapability]:
    """Discover contract agents plus inference-only routing metadata."""
    contract_path = root / "src/shelfwise_contracts/__init__.py"
    inference_path = root / "src/shelfwise_inference/config.py"
    orchestration_path = root / "src/shelfwise_inference/orchestration.py"
    declared = _enum_members(contract_path, "AgentName")
    routed = _string_collection(inference_path, "STRONG_AGENT_NAMES")
    orchestration_classes = (
        _class_locations(orchestration_path) if orchestration_path.is_file() else {}
    )
    orchestrator_line = orchestration_classes.get("AgentOrchestrator")
    names = sorted(set(declared) | set(routed))
    capabilities: list[AgentCapability] = []
    for name in names:
        sources: list[SourceLocation] = []
        if name in declared:
            sources.append(_source(root, contract_path, declared[name], "AgentName"))
        if name in routed:
            sources.append(
                _source(
                    root,
                    inference_path,
                    _line_for(inference_path, rf'["\']{re.escape(name)}["\']'),
                    "STRONG_AGENT_NAMES",
                )
            )
        if name == "orchestrator" and orchestrator_line is not None:
            sources.append(
                _source(
                    root,
                    orchestration_path,
                    orchestrator_line,
                    "AgentOrchestrator",
                )
            )
        metadata_only = name not in declared and orchestrator_line is None
        status = CapabilityStatus.VERIFIED
        if metadata_only:
            status = CapabilityStatus.DECLARATION_ONLY
        elif name == "orchestrator":
            status = CapabilityStatus.PARTIAL
        capabilities.append(
            AgentCapability(
                id=f"agent:{name}",
                name=name.replace("_", " ").title(),
                status=status,
                sources=sources,
                agent_name=name,
                metadata_only=metadata_only,
            )
        )
    return capabilities


def _discover_workflows(root: Path) -> list[WorkflowCapability]:
    """Discover cascade and decision-check workflow entry points."""
    path = root / "src/shelfwise_backend/cascade.py"
    functions = _function_locations(path)
    workflow_names = sorted(
        name
        for name in functions
        if name.startswith("run_") and (name.endswith("_cascade") or name.endswith("_check"))
    )
    agent_dependencies = {
        "run_catalog_price_check": ("sales", "critic", "executive"),
        "run_cold_chain_cascade": ("cold_chain", "critic", "executive"),
        "run_critic_rejection_cascade": ("critic", "executive"),
        "run_expiry_risk_check": ("expiry", "critic", "executive"),
        "run_golden_cascade": (
            "inventory",
            "demand",
            "expiry",
            "opportunity",
            "simulation",
            "critic",
            "executive",
        ),
        "run_procurement_cascade": ("procurement", "critic", "executive"),
        "run_sales_cascade": ("sales", "critic", "executive"),
    }
    capabilities: list[WorkflowCapability] = []
    for function_name in workflow_names:
        relationships = [
            CapabilityRelationship(kind=RelationshipKind.REQUIRES, target=f"agent:{agent}")
            for agent in agent_dependencies.get(function_name, ())
        ]
        slug = function_name.removeprefix("run_").removesuffix("_cascade")
        capabilities.append(
            WorkflowCapability(
                id=f"workflow:{slug}",
                name=slug.replace("_", " ").title(),
                status=CapabilityStatus.VERIFIED,
                sources=[_source(root, path, functions[function_name], function_name)],
                relationships=relationships,
                entrypoint=f"shelfwise_backend.cascade:{function_name}",
            )
        )
    return capabilities


def _discover_openapi_routes(root: Path) -> list[OpenAPIRouteCapability]:
    """Discover route decorators and router prefixes as OpenAPI pairs."""
    paths = (
        root / "src/shelfwise_backend/app.py",
        root / "src/shelfwise_backend/intelligence_api.py",
        root / "src/shelfwise_backend/routes_twin.py",
        root / "src/shelfwise_multimodal/router.py",
    )
    feature_by_path = {
        "/voice/in": "multimodal_feature:speech_to_text",
        "/voice/out": "multimodal_feature:text_to_speech",
        "/scan/image": "multimodal_feature:image_scan",
    }
    capabilities: list[OpenAPIRouteCapability] = []
    for path in paths:
        text = _read(path)
        prefixes = [
            (match.start(), match.group(1))
            for match in re.finditer(
                r"router\s*=\s*APIRouter\(\s*prefix\s*=\s*[\"']([^\"']+)[\"']",
                text,
            )
        ]
        route_pattern = re.compile(
            rf"@(app|router)\.({'|'.join(_HTTP_METHODS)})\(\s*[\"']([^\"']+)[\"']",
            re.MULTILINE,
        )
        for match in route_pattern.finditer(text):
            owner, method, local_path = match.groups()
            prefix = ""
            if owner == "router":
                prior = [value for offset, value in prefixes if offset <= match.start()]
                prefix = prior[-1] if prior else ""
            full_path = f"{prefix}{local_path}"
            function_match = re.search(
                r"(?:async\s+)?def\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\(",
                text[match.end() :],
            )
            operation_id = function_match.group(1) if function_match else "unknown"
            relationships = []
            feature_id = feature_by_path.get(full_path)
            if feature_id:
                relationships.append(
                    CapabilityRelationship(kind=RelationshipKind.USES, target=feature_id)
                )
            capabilities.append(
                OpenAPIRouteCapability(
                    id=route_capability_id(method, full_path),
                    name=f"{method.upper()} {full_path}",
                    status=CapabilityStatus.VERIFIED,
                    sources=[
                        _source(
                            root,
                            path,
                            text.count("\n", 0, match.start()) + 1,
                            operation_id,
                        )
                    ],
                    relationships=relationships,
                    path=full_path,
                    method=method.upper(),
                    operation_id=operation_id,
                )
            )
    return capabilities


def _discover_connectors(root: Path) -> list[ConnectorCapability]:
    """Reconcile connector enum, catalogue, and concrete mapper discovery."""
    canonical_path = root / "src/shelfwise_connectors/canonical.py"
    catalog_path = root / "src/shelfwise_connectors/catalog.py"
    registry_path = root / "src/shelfwise_connectors/connectors/systems/registry.py"
    csv_path = root / "src/shelfwise_data/csv_connector.py"
    declared = _enum_members(canonical_path, "SourceSystem")
    catalogued = _catalogue_connectors(catalog_path)
    mapped = _registry_connectors(registry_path)
    csv_classes = _class_locations(csv_path)
    if "CsvConnector" in csv_classes:
        mapped["csv"] = (csv_classes["CsvConnector"], "CsvConnector")

    capabilities: list[ConnectorCapability] = []
    for system in sorted(declared):
        catalogue = catalogued.get(system)
        mapper = mapped.get(system)
        is_catalogued = catalogue is not None
        is_mapped = mapper is not None
        if is_catalogued and is_mapped:
            status = CapabilityStatus.VERIFIED
        elif is_catalogued or is_mapped:
            status = CapabilityStatus.PARTIAL
        else:
            status = CapabilityStatus.DECLARATION_ONLY
        sources = [
            _source(root, canonical_path, declared[system], f"SourceSystem.{system.upper()}")
        ]
        if catalogue:
            sources.append(_source(root, catalog_path, catalogue["line"], "_CATALOG"))
        if mapper:
            mapper_path = csv_path if system == "csv" else registry_path
            sources.append(_source(root, mapper_path, mapper[0], mapper[1]))
        capabilities.append(
            ConnectorCapability(
                id=f"connector:{system}",
                name=str(catalogue["label"] if catalogue else system.replace("_", " ").title()),
                status=status,
                sources=sources,
                system=system,
                declared=True,
                catalogued=is_catalogued,
                mapped=is_mapped,
                transport=str(catalogue["transport"]) if catalogue else None,
                mapper_registered_claim=(
                    bool(catalogue["mapper_registered"]) if catalogue else None
                ),
            )
        )
    return capabilities


def _discover_tools(root: Path) -> list[ToolCapability]:
    """Discover read-only platform tools and multimodal tool callables."""
    platform_path = root / "src/shelfwise_backend/tools/mcp_surface.py"
    multimodal_path = root / "src/shelfwise_multimodal/tools.py"
    capabilities: list[ToolCapability] = []
    tree = _parse(platform_path)
    if tree is not None:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _call_name(node.func) != "PlatformTool":
                continue
            if not node.args or not isinstance(node.args[0], ast.Constant):
                continue
            tool_name = str(node.args[0].value)
            capabilities.append(
                ToolCapability(
                    id=f"tool:platform:{tool_name}",
                    name=tool_name.replace("_", " ").title(),
                    status=CapabilityStatus.VERIFIED,
                    sources=[_source(root, platform_path, node.lineno, "PlatformTool")],
                    tool_name=tool_name,
                    surface="platform",
                )
            )
    feature_relationships = {
        "scan_tool": "multimodal_feature:image_scan",
        "voice_in_tool": "multimodal_feature:speech_to_text",
        "voice_out_tool": "multimodal_feature:text_to_speech",
    }
    for name, line in sorted(_function_locations(multimodal_path).items()):
        if not name.endswith("_tool"):
            continue
        relationships = []
        if name in feature_relationships:
            relationships.append(
                CapabilityRelationship(
                    kind=RelationshipKind.USES,
                    target=feature_relationships[name],
                )
            )
        capabilities.append(
            ToolCapability(
                id=f"tool:multimodal:{name}",
                name=name.replace("_", " ").title(),
                status=CapabilityStatus.VERIFIED,
                sources=[_source(root, multimodal_path, line, name)],
                relationships=relationships,
                tool_name=name,
                surface="multimodal",
            )
        )
    return capabilities


def _discover_workers(root: Path) -> list[WorkerCapability]:
    """Discover concrete worker classes and the loop service."""
    worker_dir = root / "src/shelfwise_backend/worker"
    discovered: list[tuple[str, Path, int]] = []
    for path in sorted(worker_dir.glob("*.py")):
        for name, line in _class_locations(path).items():
            if name.endswith("Worker") or name == "WorkerLoopService":
                discovered.append((name, path, line))
    capabilities: list[WorkerCapability] = []
    for name, path, line in sorted(discovered):
        worker_id = _snake_case(name)
        relationships = []
        if name == "WorkerLoopService":
            relationships.append(
                CapabilityRelationship(
                    kind=RelationshipKind.REQUIRES,
                    target="worker:cascade_worker",
                )
            )
        capabilities.append(
            WorkerCapability(
                id=f"worker:{worker_id}",
                name=name,
                status=CapabilityStatus.VERIFIED,
                sources=[_source(root, path, line, name)],
                relationships=relationships,
                worker=name,
            )
        )
    return capabilities


def _discover_events(root: Path) -> list[Capability]:
    """Discover canonical event declarations and worker consumption branches."""
    contract_path = root / "src/shelfwise_contracts/__init__.py"
    worker_path = root / "src/shelfwise_backend/worker/worker.py"
    declared = _enum_members(contract_path, "EventType")
    worker_text = _read(worker_path)
    consumed_names = {
        match.group(1).lower()
        for match in re.finditer(r"EventType\.([A-Z][A-Z0-9_]*)", worker_text)
    }
    capabilities: list[Capability] = []
    for event_type in sorted(declared):
        consumer_id = f"event_consumer:cascade_worker:{event_type}"
        is_consumed = event_type in consumed_names
        capabilities.append(
            EventTypeCapability(
                id=f"event_type:{event_type}",
                name=event_type.replace("_", " ").title(),
                status=(CapabilityStatus.VERIFIED if is_consumed else CapabilityStatus.PARTIAL),
                sources=[
                    _source(root, contract_path, declared[event_type], f"EventType.{event_type}")
                ],
                event_type=event_type,
                consumers=[consumer_id] if is_consumed else [],
            )
        )
        if not is_consumed:
            continue
        line = _line_for(worker_path, rf"EventType\.{event_type.upper()}\b")
        capabilities.append(
            EventConsumerCapability(
                id=consumer_id,
                name=f"Cascade worker consumes {event_type}",
                status=CapabilityStatus.VERIFIED,
                sources=[_source(root, worker_path, line, "default_cascade_handler")],
                relationships=[
                    CapabilityRelationship(
                        kind=RelationshipKind.CONSUMES,
                        target=f"event_type:{event_type}",
                    ),
                    CapabilityRelationship(
                        kind=RelationshipKind.REQUIRES,
                        target="worker:cascade_worker",
                    ),
                ],
                consumer="CascadeWorker.default_cascade_handler",
                event_type=event_type,
            )
        )
    return capabilities


def _discover_storage_backends(root: Path) -> list[StorageBackendCapability]:
    """Discover every storage backend selected by a SHELFWISE_STORE_BACKEND factory."""
    sources: dict[str, list[tuple[Path, int, str]]] = defaultdict(list)
    for path in sorted((root / "src").rglob("*.py")):
        text = _read(path)
        if "SHELFWISE_STORE_BACKEND" not in text:
            continue
        for match in re.finditer(r"if\s+backend\s*==\s*[\"']([a-z0-9_]+)[\"']", text):
            sources[match.group(1)].append(
                (
                    path,
                    text.count("\n", 0, match.start()) + 1,
                    _nearest_function(text, match.start()),
                )
            )
    capabilities: list[StorageBackendCapability] = []
    for backend in sorted(sources):
        rows = sources[backend]
        capabilities.append(
            StorageBackendCapability(
                id=f"storage_backend:{backend}",
                name=f"{backend.title()} storage",
                status=CapabilityStatus.VERIFIED,
                sources=[_source(root, path, line, symbol) for path, line, symbol in rows],
                backend=backend,
                factories=[symbol for _, _, symbol in rows],
            )
        )
    return capabilities


def _discover_bus_backends(root: Path) -> list[BusBackendCapability]:
    """Discover selectable in-memory and Redis event-bus backends."""
    path = root / "src/shelfwise_backend/event_bus.py"
    text = _read(path)
    capabilities: list[BusBackendCapability] = []
    for match in re.finditer(r"if\s+backend\s*==\s*[\"']([a-z0-9_]+)[\"']", text):
        backend = match.group(1)
        capabilities.append(
            BusBackendCapability(
                id=f"bus_backend:{backend}",
                name=f"{backend.title()} event bus",
                status=CapabilityStatus.VERIFIED,
                sources=[
                    _source(
                        root,
                        path,
                        text.count("\n", 0, match.start()) + 1,
                        "create_event_bus",
                    )
                ],
                backend=backend,
            )
        )
    return capabilities


def _discover_worldgen_scenarios(root: Path) -> list[WorldgenScenarioCapability]:
    """Discover named scenarios and deterministic seeds from SCENARIOS."""
    path = root / "src/shelfwise_worldgen/scenarios.py"
    tree = _parse(path)
    if tree is None:
        return []
    capabilities: list[WorldgenScenarioCapability] = []
    for node in ast.walk(tree):
        value: ast.AST | None = None
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "SCENARIOS"
                for target in node.targets
            )
        ) or (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "SCENARIOS"
        ):
            value = node.value
        if not isinstance(value, ast.Dict):
            continue
        for key, scenario in zip(value.keys, value.values, strict=True):
            if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                continue
            seed = _call_keyword_int(scenario, "seed")
            capabilities.append(
                WorldgenScenarioCapability(
                    id=f"worldgen_scenario:{key.value}",
                    name=key.value.replace("_", " ").title(),
                    status=CapabilityStatus.VERIFIED,
                    sources=[_source(root, path, scenario.lineno, "SCENARIOS")],
                    scenario_id=key.value,
                    seed=seed,
                )
            )
    return capabilities


def _discover_multimodal_features(root: Path) -> list[MultimodalFeatureCapability]:
    """Discover stable public speech, vision, and voice-intake entry points."""
    specs = {
        "image_scan": ("vision.py", "scan_image"),
        "speech_to_text": ("stt.py", "transcribe"),
        "text_to_speech": ("tts.py", "synthesize"),
        "voice_event_intake": ("voice_intake.py", "to_event_candidate"),
    }
    capabilities: list[MultimodalFeatureCapability] = []
    for feature, (file_name, function_name) in sorted(specs.items()):
        path = root / "src/shelfwise_multimodal" / file_name
        functions = _function_locations(path)
        if function_name not in functions:
            continue
        module = file_name.removesuffix(".py")
        capabilities.append(
            MultimodalFeatureCapability(
                id=f"multimodal_feature:{feature}",
                name=feature.replace("_", " ").title(),
                status=CapabilityStatus.VERIFIED,
                sources=[_source(root, path, functions[function_name], function_name)],
                feature=feature,
                entrypoint=f"shelfwise_multimodal.{module}:{function_name}",
            )
        )
    return capabilities


def _discover_training_stages(root: Path) -> list[TrainingStageCapability]:
    """Discover coarse training stages from stable public entry points."""
    specs = {
        "evaluate": ("evaluate.py", "run_evaluation", CapabilityStatus.PARTIAL, False),
        "preflight": ("preflight.py", "run_preflight", CapabilityStatus.IMPLEMENTED, False),
        "serving_check": (
            "serving_check.py",
            "run_serving_check",
            CapabilityStatus.PARTIAL,
            False,
        ),
        "shakedown": ("shakedown.py", "run_shakedown", CapabilityStatus.PARTIAL, False),
        "simulation_dataset": (
            "simulation.py",
            "build_shakedown_datasets",
            CapabilityStatus.VERIFIED,
            True,
        ),
        "train": ("train.py", "run_training", CapabilityStatus.PARTIAL, False),
    }
    capabilities: list[TrainingStageCapability] = []
    for stage, (file_name, function_name, status, runtime_verified) in sorted(specs.items()):
        path = root / "src/shelfwise/training" / file_name
        functions = _function_locations(path)
        if function_name not in functions:
            continue
        module = file_name.removesuffix(".py")
        capabilities.append(
            TrainingStageCapability(
                id=f"training_stage:{stage}",
                name=stage.replace("_", " ").title(),
                status=status,
                sources=[_source(root, path, functions[function_name], function_name)],
                stage=stage,
                entrypoint=f"shelfwise.training.{module}:{function_name}",
                runtime_verified=runtime_verified,
            )
        )
    return capabilities


def _discover_frontend_surfaces(root: Path) -> list[FrontendSurfaceCapability]:
    """Discover typed workspace/sidebar surfaces and persistent chat controls."""
    path = root / "frontend/src/App.tsx"
    text = _read(path)
    surfaces = set(_typescript_union_values(text, "WorkspaceSurface"))
    surfaces.update(_typescript_union_values(text, "SidebarPage"))
    components = _typescript_function_locations(text)
    if "Composer" in components:
        surfaces.add("chat")
    if "ApprovalPanel" in components:
        surfaces.add("approval_queue")

    route_dependencies = {
        "approval_queue": (
            route_capability_id("get", "/decisions"),
            route_capability_id("post", "/decisions/{decision_id}/approve"),
            route_capability_id("post", "/decisions/{decision_id}/reject"),
        ),
        "chat": (route_capability_id("post", "/chat"),),
        "cold-chain": (route_capability_id("get", "/cold-chain/feed"),),
        "connections": (route_capability_id("get", "/connectors/systems"),),
        "deliveries": (
            route_capability_id("post", "/intelligence/deliveries/reconcile"),
        ),
        "operations": (
            route_capability_id("get", "/health"),
            route_capability_id("get", "/readiness"),
        ),
        "products": (
            route_capability_id("get", "/products/attention"),
            route_capability_id("get", "/products/search"),
        ),
        "results": (
            route_capability_id("get", "/decisions"),
            route_capability_id("get", "/mlops/accountability"),
        ),
        "sell-first": (
            route_capability_id("post", "/intelligence/stock/fefo-split"),
        ),
        "settings": (route_capability_id("get", "/tenants/me"),),
        "to-order": (route_capability_id("get", "/products/attention"),),
    }
    capabilities: list[FrontendSurfaceCapability] = []
    for surface in sorted(surfaces):
        component = {
            "approval_queue": "ApprovalPanel",
            "chat": "Composer",
        }.get(surface, "WorkspaceSurface" if surface != "settings" else "SidebarPage")
        line = components.get(component, _line_for(path, rf"[\"']{re.escape(surface)}[\"']"))
        relationships = [
            CapabilityRelationship(kind=RelationshipKind.USES, target=target)
            for target in route_dependencies.get(surface, ())
        ]
        capabilities.append(
            FrontendSurfaceCapability(
                id=f"frontend_surface:{surface}",
                name=surface.replace("-", " ").replace("_", " ").title(),
                status=CapabilityStatus.VERIFIED,
                sources=[_source(root, path, line, component)],
                relationships=relationships,
                surface=surface,
            )
        )
    return capabilities


def _discover_deployment_profiles(
    root: Path,
    profiles: DeploymentProfileSnapshot,
) -> list[DeploymentProfileCapability]:
    """Convert the normalized profile source document into manifest capabilities."""
    capabilities: list[DeploymentProfileCapability] = []
    for profile in profiles.profiles:
        relationships = [
            CapabilityRelationship(
                kind=RelationshipKind.REQUIRES,
                target=f"storage_backend:{profile.storage_backend}",
            ),
            CapabilityRelationship(
                kind=RelationshipKind.REQUIRES,
                target=f"bus_backend:{profile.bus_backend}",
            ),
        ]
        if profile.worker_enabled:
            relationships.append(
                CapabilityRelationship(
                    kind=RelationshipKind.REQUIRES,
                    target="worker:worker_loop_service",
                )
            )
        capabilities.append(
            DeploymentProfileCapability(
                id=f"deployment_profile:{profile.id}",
                name=profile.name,
                status=profile.status,
                sources=[
                    SourceLocation(path=source_path, line=1, symbol=profile.id)
                    for source_path in profile.source_paths
                ],
                relationships=relationships,
                note=profile.note,
                profile=profile.id,
                storage_backend=profile.storage_backend,
                bus_backend=profile.bus_backend,
                worker_enabled=profile.worker_enabled,
                inference_provider=profile.inference_provider,
            )
        )
    return capabilities


def _apply_policy(capability: Capability, policy: CapabilityPolicy) -> Capability:
    """Apply explicit annotations and verification defaults to one discovered record."""
    annotation = policy.annotations.get(capability.id, CapabilityAnnotation())
    status = annotation.status or capability.status
    if annotation.verification_nodeids is not None:
        verification = annotation.verification_nodeids
    elif status in policy.verification_required_statuses:
        verification = policy.default_verification_nodeids.get(capability.kind, [])
    else:
        verification = []
    return capability.model_copy(
        update={
            "status": status,
            "verification_nodeids": list(verification),
            "note": annotation.note if annotation.note is not None else capability.note,
        }
    )


def _enum_members(path: Path, class_name: str) -> dict[str, int]:
    """Return string enum values and declaration lines from one class."""
    tree = _parse(path)
    if tree is None:
        return {}
    values: dict[str, int] = {}
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        for child in node.body:
            if not isinstance(child, ast.Assign) or not isinstance(child.value, ast.Constant):
                continue
            if not isinstance(child.value.value, str):
                continue
            values[child.value.value] = child.lineno
    return values


def _string_collection(path: Path, variable_name: str) -> dict[str, int]:
    """Return string values and a stable line from a named set/list/tuple assignment."""
    tree = _parse(path)
    if tree is None:
        return {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == variable_name
            for target in node.targets
        ):
            continue
        if not isinstance(node.value, (ast.List, ast.Set, ast.Tuple)):
            return {}
        return {
            str(item.value): item.lineno
            for item in node.value.elts
            if isinstance(item, ast.Constant) and isinstance(item.value, str)
        }
    return {}


def _catalogue_connectors(path: Path) -> dict[str, dict[str, Any]]:
    """Extract ConnectorCapability constructor rows from the connector catalogue."""
    tree = _parse(path)
    if tree is None:
        return {}
    rows: dict[str, dict[str, Any]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _call_name(node.func) != "ConnectorCapability":
            continue
        if len(node.args) < 8 or not isinstance(node.args[0], ast.Attribute):
            continue
        system = node.args[0].attr.lower()
        rows[system] = {
            "label": _constant_value(node.args[1]),
            "transport": _constant_value(node.args[2]),
            "mapper_registered": _constant_value(node.args[6]),
            "line": node.lineno,
        }
    return rows


def _registry_connectors(path: Path) -> dict[str, tuple[int, str]]:
    """Extract SourceSystem keys from mapper registry dictionaries."""
    tree = _parse(path)
    if tree is None:
        return {}
    rows: dict[str, tuple[int, str]] = {}
    for node in tree.body:
        if not isinstance(node, ast.AnnAssign) or not isinstance(node.target, ast.Name):
            continue
        if node.target.id not in {"POLL_MAPPERS", "WEBHOOK_MAPPERS"}:
            continue
        if not isinstance(node.value, ast.Dict):
            continue
        for key in node.value.keys:
            if isinstance(key, ast.Attribute):
                rows[key.attr.lower()] = (key.lineno, node.target.id)
    return rows


def _typescript_union_values(text: str, type_name: str) -> list[str]:
    """Extract string literals from a TypeScript union alias."""
    match = re.search(
        rf"type\s+{re.escape(type_name)}\s*=\s*(.*?)(?=\n(?:type|const|function|class|interface)\s)",
        text,
        re.DOTALL,
    )
    if not match:
        return []
    return re.findall(r"[\"']([^\"']+)[\"']", match.group(1))


def _typescript_function_locations(text: str) -> dict[str, int]:
    """Return named TypeScript function component lines."""
    return {
        match.group(1): text.count("\n", 0, match.start()) + 1
        for match in re.finditer(r"^function\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", text, re.MULTILINE)
    }


def _function_locations(path: Path) -> dict[str, int]:
    """Return Python function lines using a conflict-tolerant source scan."""
    text = _read(path)
    return {
        match.group(1): text.count("\n", 0, match.start()) + 1
        for match in re.finditer(
            r"^(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(",
            text,
            re.MULTILINE,
        )
    }


def _class_locations(path: Path) -> dict[str, int]:
    """Return Python class lines using a conflict-tolerant source scan."""
    text = _read(path)
    return {
        match.group(1): text.count("\n", 0, match.start()) + 1
        for match in re.finditer(
            r"^class\s+([A-Za-z_][A-Za-z0-9_]*)\b",
            text,
            re.MULTILINE,
        )
    }


def _nearest_function(text: str, offset: int) -> str:
    """Return the closest function name declared before a source offset."""
    matches = list(
        re.finditer(
            r"^(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(",
            text[:offset],
            re.MULTILINE,
        )
    )
    return matches[-1].group(1) if matches else "module"


def _call_keyword_int(node: ast.AST, keyword_name: str) -> int:
    """Read a required integer keyword from an AST call."""
    if not isinstance(node, ast.Call):
        raise ValueError(f"expected call with keyword {keyword_name}")
    for keyword in node.keywords:
        if keyword.arg == keyword_name and isinstance(keyword.value, ast.Constant):
            return int(keyword.value.value)
    raise ValueError(f"missing integer keyword {keyword_name}")


def _constant_value(node: ast.AST) -> Any:
    """Return a literal AST value used by static discovery."""
    if not isinstance(node, ast.Constant):
        raise ValueError("capability catalogue values must be literals")
    return node.value


def _call_name(node: ast.AST) -> str:
    """Return the final identifier from a call target."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _parse(path: Path) -> ast.Module | None:
    """Parse a Python source file, returning None for unrelated unresolved conflicts."""
    try:
        return ast.parse(_read(path), filename=str(path))
    except SyntaxError:
        return None


def _line_for(path: Path, pattern: str) -> int:
    """Return the first matching source line, defaulting safely to line one."""
    text = _read(path)
    match = re.search(pattern, text)
    return text.count("\n", 0, match.start()) + 1 if match else 1


def _source(root: Path, path: Path, line: int, symbol: str | None) -> SourceLocation:
    """Build a portable repository-relative source location."""
    return SourceLocation(path=path.relative_to(root).as_posix(), line=line, symbol=symbol)


def _snake_case(value: str) -> str:
    """Convert a class name into a stable capability-id component."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", value).lower()


def _read(path: Path) -> str:
    """Read source text with deterministic UTF-8 decoding."""
    return path.read_text(encoding="utf-8")
