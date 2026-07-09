from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from shelfwise_backend.app import app

ROOT = Path(__file__).resolve().parents[1]


def test_dockerignore_excludes_secrets_caches_and_design_docs() -> None:
    text = (ROOT / ".dockerignore").read_text(encoding="utf-8")

    for pattern in [".env", "**/.env", ".git", "plot", "files", "**/.mypy_cache/"]:
        assert pattern in text


def test_backend_container_sandbox_is_declared_in_compose() -> None:
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "no-new-privileges:true" in text
    assert "cap_drop:" in text
    assert "- ALL" in text
    assert "read_only: true" in text
    assert "tmpfs:" in text
    assert "pids_limit: 256" in text


def test_postgres_schema_is_mounted_for_compose_init() -> None:
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "./src/shelfwise_storage/schema.sql" in text
    assert "/docker-entrypoint-initdb.d/01-schema.sql:ro" in text


def test_backend_image_runs_as_non_root_user() -> None:
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "adduser" in text
    assert "USER appuser" in text


def test_make_smoke_exercises_health_trace_approval_products_and_critic() -> None:
    text = (ROOT / "Makefile").read_text(encoding="utf-8")
    smoke = (ROOT / "scripts" / "smoke.py").read_text(encoding="utf-8")
    powershell_smoke = (ROOT / "scripts" / "smoke.ps1").read_text(encoding="utf-8")

    assert "SMOKE OK" in smoke
    assert "python scripts/smoke.py" in text
    assert "python scripts/smoke.py" in powershell_smoke
    assert "/health" in smoke
    assert "/demo/golden" in smoke
    assert "/demo/critic-rejection" in smoke
    assert "/trace/" in smoke
    assert "/approve" in smoke
    assert "/products/attention" in smoke
    assert "/products/search" in smoke
    assert "/intelligence/stock/fefo-split" in smoke
    assert "/intelligence/deliveries/reconcile" in smoke
    assert "/intelligence/suppliers/cover-plan" in smoke
    assert "/intelligence/outcomes/summarize" in smoke


def test_ci_lints_executable_smoke_scripts() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "python -m ruff check src tests scripts" in workflow


def test_readme_connected_api_list_matches_backend_schema() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    section = readme.split("Connected API endpoints:", 1)[1].split("## Smoke", 1)[0]
    documented = sorted(line.strip() for line in section.splitlines() if line.startswith("- `"))

    assert documented == sorted(_backend_endpoint_lines())


def _backend_endpoint_lines() -> list[str]:
    routes: dict[str, set[str]] = defaultdict(set)
    for route in app.routes:
        if not getattr(route, "include_in_schema", True):
            continue
        for method in getattr(route, "methods", []) or []:
            if method in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                routes[str(route.path)].add(method)
    return [
        f"- `{'/'.join(sorted(methods))} http://localhost:8000{path}`"
        for path, methods in sorted(routes.items())
    ]
