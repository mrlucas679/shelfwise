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


def test_production_compose_has_one_public_origin_and_reserves_vllm_ports() -> None:
    text = (ROOT / "docker-compose.production.yml").read_text(encoding="utf-8")

    assert 'APP_ENV: production' in text
    assert 'SHELFWISE_AUTH_MODE: jwt' in text
    assert 'SHELFWISE_PUBLIC_DEMO_SESSION: "true"' in text
    assert '- "80:80"' in text
    assert '- "8000:8000"' not in text
    assert '- "5432:5432"' not in text
    assert '- "6379:6379"' not in text
    assert "host.docker.internal:host-gateway" in text
    assert "host.docker.internal:8000" in text
    assert "host.docker.internal:8001" in text
    assert 'SHELFWISE_AUTO_SCHEMA: "false"' in text
    assert "service_completed_successfully" in text
    assert "psql -h postgres" in text


def test_frontend_proxy_includes_browser_session_route() -> None:
    text = (ROOT / "frontend" / "nginx.conf").read_text(encoding="utf-8")

    assert "^/(auth|" in text


def test_postgres_schema_is_mounted_for_compose_init() -> None:
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "./src/shelfwise_storage/schema.sql" in text
    assert "/docker-entrypoint-initdb.d/01-schema.sql:ro" in text


def test_backend_image_runs_as_non_root_user() -> None:
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "adduser" in text
    assert "USER appuser" in text


def test_backend_image_contains_seeded_runtime_datasets() -> None:
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "COPY data ./data" in text


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


def test_ci_boots_and_smokes_production_public_origin() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "docker-compose.production.yml up --build -d --wait" in workflow
    assert "--request POST http://127.0.0.1/auth/session" in workflow
    assert "--request POST http://127.0.0.1/demo/golden" in workflow
    assert "logs --no-color backend postgres migrate frontend" in workflow
    assert "docker-compose.production.yml down --volumes" in workflow


def test_readme_connected_api_list_matches_backend_schema() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    section = readme.split("Connected API endpoints:", 1)[1].split("## Smoke", 1)[0]
    documented = sorted(line.strip() for line in section.splitlines() if line.startswith("- `"))

    assert documented == sorted(_backend_endpoint_lines())


def _backend_endpoint_lines() -> list[str]:
    routes: dict[str, set[str]] = defaultdict(set)
    for path, operations in app.openapi()["paths"].items():
        for method in operations:
            upper = method.upper()
            if upper in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                routes[path].add(upper)
    return [
        f"- `{'/'.join(sorted(methods))} http://localhost:8000{path}`"
        for path, methods in sorted(routes.items())
    ]
