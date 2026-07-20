from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_ci_does_not_use_secrets_in_step_conditions() -> None:
    """Keep optional live credentials out of GitHub Actions step-level conditions."""
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    conditions = [line for line in workflow.splitlines() if line.strip().startswith("if:")]

    assert conditions
    assert all("secrets." not in line for line in conditions)
    assert 'if [ -z "$LLM_ROUTINE_BASE_URL" ]' in workflow


def test_ci_scopes_insecure_cookie_override_to_http_smoke() -> None:
    """Only the disposable HTTP smoke may opt out of Secure session cookies."""
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    boot, smoke = workflow.split(
        "      - name: Smoke production public origin",
        maxsplit=1,
    )
    smoke, _ = smoke.split(
        "      - name: Track 3 live response and language gate",
        maxsplit=1,
    )

    assert 'SHELFWISE_COOKIE_SECURE: "true"' in boot
    assert 'SHELFWISE_COOKIE_SECURE: "false"' in smoke
    assert 'SHELFWISE_ALLOW_INSECURE_COOKIE_IN_DISPOSABLE_CI: "true"' in smoke
    assert "SHELFWISE_ALLOW_INSECURE_COOKIE_IN_DISPOSABLE_CI" not in boot
    assert (
        "timeout 90s docker compose -f docker-compose.production.yml up --build -d --wait"
        in boot
    )
    assert "docker compose -f docker-compose.production.yml logs --no-color" in boot
    assert "docker compose -f docker-compose.production.yml up -d --wait" in smoke
    assert "python scripts/deployment_shakedown.py" in smoke
