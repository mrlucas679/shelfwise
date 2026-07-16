from __future__ import annotations

from pathlib import Path

import pytest

from shelfwise_backend.app import (
    _cookie_secure_setting,
    _reject_insecure_production_cookie_config,
    cors_allowed_origins,
)

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
    assert 'SHELFWISE_COOKIE_SECURE: ${SHELFWISE_COOKIE_SECURE:-true}' in text
    assert "SHELFWISE_ALLOW_INSECURE_COOKIE_IN_DISPOSABLE_CI" in text
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
    assert "for attempt in $(seq 1 10)" in text
    assert '"$$attempt" -eq 10' in text
    assert "sleep 2" in text


def test_production_cookie_config_is_secure_by_default(monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("SHELFWISE_COOKIE_SECURE", raising=False)
    monkeypatch.delenv("SHELFWISE_ALLOW_INSECURE_COOKIE_IN_DISPOSABLE_CI", raising=False)

    _reject_insecure_production_cookie_config()

    assert _cookie_secure_setting() is True


def test_production_cookie_config_rejects_insecure_without_disposable_ci_override(
    monkeypatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SHELFWISE_COOKIE_SECURE", "false")
    monkeypatch.delenv("SHELFWISE_ALLOW_INSECURE_COOKIE_IN_DISPOSABLE_CI", raising=False)

    with pytest.raises(RuntimeError, match="not allowed"):
        _reject_insecure_production_cookie_config()


def test_disposable_ci_is_the_only_insecure_production_cookie_override(monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SHELFWISE_COOKIE_SECURE", "false")
    monkeypatch.setenv("SHELFWISE_ALLOW_INSECURE_COOKIE_IN_DISPOSABLE_CI", "true")

    _reject_insecure_production_cookie_config()

    assert _cookie_secure_setting() is False


def test_frontend_proxy_includes_all_browser_feature_route_prefixes() -> None:
    text = (ROOT / "frontend" / "nginx.conf").read_text(encoding="utf-8")

    assert "^/(auth|" in text
    for prefix in ("intelligence", "scan", "trace", "twin", "voice"):
        assert f"|{prefix}|" in text


def test_credentialed_cors_rejects_wildcard_origin(monkeypatch) -> None:
    monkeypatch.setenv("SHELFWISE_CORS_ORIGINS", "*")

    with pytest.raises(RuntimeError, match="cannot contain"):
        cors_allowed_origins()


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


def test_frontend_image_contains_runtime_endpoint_configuration() -> None:
    text = (ROOT / "frontend" / "Dockerfile").read_text(encoding="utf-8")

    assert "COPY public ./public" in text


def test_make_smoke_exercises_health_trace_approval_products_and_critic() -> None:
    text = (ROOT / "Makefile").read_text(encoding="utf-8")
    smoke = (ROOT / "scripts" / "smoke.py").read_text(encoding="utf-8")
    powershell_smoke = (ROOT / "scripts" / "smoke.ps1").read_text(encoding="utf-8")

    assert "SMOKE OK" in smoke
    assert "python scripts/smoke.py" in text
    assert "python scripts/smoke.py" in powershell_smoke
    assert "/health" in smoke
    assert "/scenarios/golden" in smoke
    assert "/scenarios/critic-rejection" in smoke
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
    assert "--request POST http://127.0.0.1/scenarios/golden" in workflow
    assert "logs --no-color backend postgres migrate frontend" in workflow
    assert "docker-compose.production.yml down --volumes" in workflow


def test_readme_points_to_capability_manifest_for_api_reference() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "capabilities/manifest.json" in readme
    assert "compare_capability_manifests.py" in readme
