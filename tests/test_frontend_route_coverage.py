from __future__ import annotations

import re
from pathlib import Path

from shelfwise_backend.app import app

ROOT = Path(__file__).resolve().parents[1]
FRONTEND_APP = ROOT / "frontend" / "src" / "App.tsx"
CONTRACT_BLOCK_RE = re.compile(
    r"const\s+(OPERATION_READ_ENDPOINTS|GATED_ENDPOINTS)\s*=\s*\[(.*?)\]\n",
    re.DOTALL,
)
CONST_ROUTE_RE = re.compile(r"""const\s+[A-Z0-9_]+\s*=\s*["'`](/[^"'`]+)["'`]""")
PATH_PROP_RE = re.compile(r"""path:\s*["'`](/[^"'`]+)["'`]""")
METHOD_PROP_RE = re.compile(r"""method:\s*["'`]([A-Z/]+)["'`]""")
ROUTE_LITERAL_RE = re.compile(r"""["'`](/[^"'`]+)["'`]""")
REQUEST_HELPERS = ("fetchIfAvailable", "fetchOptional", "fetchDemo", "fetchJson", "postChat")


def _backend_schema_paths() -> list[str]:
    return sorted(
        {
            str(route.path)
            for route in app.routes
            if getattr(route, "include_in_schema", True)
        }
    )


def _backend_schema_method_pairs() -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for route in app.routes:
        if not getattr(route, "include_in_schema", True):
            continue
        path = str(route.path)
        for method in getattr(route, "methods", []) or []:
            if method in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                pairs.add((path, method))
    return pairs


def _normalize_route(path: str) -> str:
    path = path.split("?", 1)[0]
    return re.sub(r"\$\{[^}]+\}", "{param}", path)


def _registered_route_paths(source: str) -> set[str]:
    return {path for path, _method in _registered_route_method_pairs(source)}


def _registered_route_method_pairs(source: str) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for _name, block in CONTRACT_BLOCK_RE.findall(source):
        for line in block.splitlines():
            path = PATH_PROP_RE.search(line)
            method = METHOD_PROP_RE.search(line)
            if path is None:
                continue
            assert method is not None, f"registered endpoint missing method: {line}"
            for method_value in method.group(1).split("/"):
                pairs.add((_normalize_route(path.group(1)), method_value))
    return pairs


def _registered_block_route_paths(source: str, block_name: str) -> set[str]:
    for name, block in CONTRACT_BLOCK_RE.findall(source):
        if name != block_name:
            continue
        return {
            _normalize_route(match.group(1))
            for match in PATH_PROP_RE.finditer(block)
        }
    raise AssertionError(f"{block_name} block not found")


def _request_route_paths(source: str) -> set[str]:
    return {path for path, _method in _request_route_method_pairs(source)}


def _request_route_method_pairs(source: str) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for helper in REQUEST_HELPERS:
        cursor = 0
        while True:
            index = source.find(helper, cursor)
            if index < 0:
                break
            open_paren = source.find("(", index)
            if open_paren < 0:
                break
            snippet = source[open_paren : open_paren + 500]
            match = ROUTE_LITERAL_RE.search(snippet)
            if match:
                path = _normalize_route(match.group(1))
                pairs.add((path, _helper_method(helper)))
            cursor = open_paren + 1
    return pairs


def _helper_method(helper: str) -> str:
    if helper == "postChat":
        return "POST"
    return "GET"


def _frontend_route_contract() -> set[str]:
    source = FRONTEND_APP.read_text(encoding="utf-8")
    return _registered_route_paths(source) | _request_route_paths(source)


def _frontend_route_method_contract() -> set[tuple[str, str]]:
    source = FRONTEND_APP.read_text(encoding="utf-8")
    return _registered_route_method_pairs(source) | _request_route_method_pairs(source)


def test_frontend_connects_every_backend_schema_path() -> None:
    frontend_paths = _frontend_route_contract()
    missing = [
        path
        for path in _backend_schema_paths()
        if path not in frontend_paths
    ]

    assert missing == []


def test_frontend_connects_every_backend_schema_method() -> None:
    frontend_pairs = _frontend_route_method_contract()
    missing = sorted(_backend_schema_method_pairs() - frontend_pairs)

    assert missing == []


def test_frontend_route_registries_only_reference_backend_schema_paths() -> None:
    source = FRONTEND_APP.read_text(encoding="utf-8")
    registered_paths = _registered_route_paths(source)
    backend_paths = set(_backend_schema_paths())

    assert registered_paths
    assert sorted(registered_paths - backend_paths) == []


def test_frontend_route_registries_only_reference_backend_schema_methods() -> None:
    source = FRONTEND_APP.read_text(encoding="utf-8")
    registered_pairs = _registered_route_method_pairs(source)
    backend_pairs = _backend_schema_method_pairs()

    assert registered_pairs
    assert sorted(registered_pairs - backend_pairs) == []


def test_frontend_read_registry_shows_operational_read_surfaces() -> None:
    source = FRONTEND_APP.read_text(encoding="utf-8")
    read_paths = _registered_block_route_paths(source, "OPERATION_READ_ENDPOINTS")

    expected = {
        "/data/seed/summary",
        "/decisions",
        "/learning",
        "/writeback/tasks",
        "/events",
        "/events/bus",
        "/trace/{correlation_id}",
        "/detective/root-cause/{target_id}",
        "/detective/root-cause-sql",
        "/tools/platform/audit",
        "/cold-chain/feed",
        "/connectors/systems",
        "/connectors/me",
        "/connectors/inbound-records",
        "/tenants/me",
        "/mlops/accountability",
        "/mlops/observability",
        "/worker/status",
    }

    assert sorted(expected - read_paths) == []
