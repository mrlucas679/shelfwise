from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class RootCauseNode:
    id: str
    kind: str
    label: str
    parent_ids: tuple[str, ...]
    tenant_id: str

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "parent_ids": list(self.parent_ids),
            "tenant_id": self.tenant_id,
        }


@dataclass(frozen=True, slots=True)
class RootCauseAnalysis:
    target_id: str
    found: bool
    paths: tuple[tuple[RootCauseNode, ...], ...]
    root_events: tuple[RootCauseNode, ...]
    method: str

    def to_dict(self) -> dict[str, object]:
        return {
            "target_id": self.target_id,
            "found": self.found,
            "paths": [[node.to_dict() for node in path] for path in self.paths],
            "root_events": [node.to_dict() for node in self.root_events],
            "method": self.method,
        }


def analyze_root_cause(
    target_id: str,
    *,
    events: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    max_depth: int = 6,
) -> RootCauseAnalysis:
    """Trace a decision/event back through caused_by, causation_id, and correlation_id links."""

    if max_depth <= 0:
        raise ValueError("max_depth must be positive")
    graph = _build_graph(events=events, decisions=decisions)
    target = graph.get(target_id)
    if target is None:
        target = _find_by_correlation(target_id, graph)
    if target is None:
        return RootCauseAnalysis(
            target_id=target_id,
            found=False,
            paths=(),
            root_events=(),
            method=_METHOD,
        )

    paths = _walk_to_roots(target, graph, max_depth=max_depth)
    roots = {
        path[-1].id: path[-1]
        for path in paths
        if path and path[-1].kind == "event" and not path[-1].parent_ids
    }
    return RootCauseAnalysis(
        target_id=target_id,
        found=True,
        paths=tuple(paths),
        root_events=tuple(sorted(roots.values(), key=lambda node: node.id)),
        method=_METHOD,
    )


def root_cause_cte_sql() -> str:
    """Postgres equivalent for the in-memory detective traversal."""

    return """
with recursive lineage(node_id, node_type, parent_id, depth, path) as (
    select
        d.id,
        'decision',
        parent.value,
        0,
        array[d.id]
    from shelfwise_decisions d
    cross join lateral jsonb_array_elements_text(d.payload->'caused_by') as parent(value)
    where d.tenant_id = current_setting('app.tenant_id', true)
      and d.id = %(target_id)s

    union all

    select
        e.id,
        'event',
        nullif(e.payload->>'causation_id', ''),
        lineage.depth + 1,
        lineage.path || e.id
    from shelfwise_events e
    join lineage
      on e.id = lineage.parent_id
      or e.payload->>'correlation_id' = lineage.parent_id
    where e.tenant_id = current_setting('app.tenant_id', true)
      and lineage.depth < %(max_depth)s
      and not e.id = any(lineage.path)
)
select node_id, node_type, parent_id, depth, path
from lineage
order by depth, node_type, node_id
"""


_METHOD = "bounded_bfs_event_decision_causality_recursive_cte_compatible"


def _build_graph(
    *,
    events: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
) -> dict[str, RootCauseNode]:
    graph: dict[str, RootCauseNode] = {}
    for event in events:
        event_id = str(event.get("id") or "")
        if not event_id:
            continue
        causation_id = str(event.get("causation_id") or "").strip()
        node = RootCauseNode(
            id=event_id,
            kind="event",
            label=str(event.get("type") or "event"),
            parent_ids=(causation_id,) if causation_id else (),
            tenant_id=str(event.get("tenant_id") or "default"),
        )
        graph[event_id] = node
        correlation_id = str(event.get("correlation_id") or "").strip()
        if correlation_id:
            graph.setdefault(correlation_id, node)
    for decision in decisions:
        decision_id = str(decision.get("id") or "")
        if not decision_id:
            continue
        caused_by = tuple(str(item) for item in decision.get("caused_by") or () if item)
        action = decision.get("action") if isinstance(decision.get("action"), dict) else {}
        graph[decision_id] = RootCauseNode(
            id=decision_id,
            kind="decision",
            label=f"{decision.get('status', 'decision')}:{action.get('type', 'unknown')}",
            parent_ids=caused_by,
            tenant_id=str(decision.get("tenant_id") or "default"),
        )
    return graph


def _find_by_correlation(
    target_id: str,
    graph: dict[str, RootCauseNode],
) -> RootCauseNode | None:
    for node in graph.values():
        if target_id in node.parent_ids:
            return node
    return None


def _walk_to_roots(
    target: RootCauseNode,
    graph: dict[str, RootCauseNode],
    *,
    max_depth: int,
) -> tuple[tuple[RootCauseNode, ...], ...]:
    paths: list[tuple[RootCauseNode, ...]] = []
    queue: deque[tuple[RootCauseNode, ...]] = deque([(target,)])
    while queue:
        path = queue.popleft()
        node = path[-1]
        if len(path) - 1 >= max_depth or not node.parent_ids:
            paths.append(path)
            continue
        expanded = False
        for parent_id in node.parent_ids:
            parent = graph.get(parent_id) or _find_by_correlation(parent_id, graph)
            if parent is None or parent.id in {item.id for item in path}:
                continue
            queue.append((*path, parent))
            expanded = True
        if not expanded:
            paths.append(path)
    return tuple(paths)
