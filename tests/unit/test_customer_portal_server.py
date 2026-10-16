"""
tests/unit/test_customer_portal_server.py
=========================================
Tests for the standalone Customer Portal ASGI server, health probes,
compose isolation contract, and Caddy edge routing (Wave CP-5).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import portal_server
import yaml
from starlette.testclient import TestClient

from nce.vertical_modules.customer_portal.app import build_customer_portal_app

_REPO_ROOT = Path(__file__).resolve().parents[2]
_COMPOSE_PATH = _REPO_ROOT / "docker-compose.yml"
_CADDYFILE_PATH = _REPO_ROOT / "Caddyfile"


def test_portal_server_app_structure() -> None:
    """Verify portal_server exports a Starlette app with dedicated portal routes."""
    app = portal_server.app
    client = TestClient(app)

    # Health check
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["surface"] == "customer_portal"

    # Verify no admin routes mounted
    routes = [route.path for route in app.routes if hasattr(route, "path")]
    for route in routes:
        assert not route.startswith("/api/admin"), f"Admin route leaked into portal: {route}"
        assert not route.startswith("/tasks"), f"A2A route leaked into portal: {route}"
    assert "/healthz" not in routes


def test_portal_health_engine_healthy() -> None:
    """When engine reports healthy, /health returns 200 ok."""
    mock_engine = MagicMock()
    mock_engine.check_health = AsyncMock(return_value={"status": "ok"})
    app = build_customer_portal_app(engine=mock_engine)
    client = TestClient(app)

    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "surface": "customer_portal"}


def test_portal_health_engine_degraded() -> None:
    """When engine reports degraded/down, /health returns 503."""
    mock_engine = MagicMock()
    mock_engine.check_health = AsyncMock(
        return_value={"status": "down", "reason": "postgres_unreachable"}
    )
    app = build_customer_portal_app(engine=mock_engine)
    client = TestClient(app)

    resp = client.get("/health")
    assert resp.status_code == 503
    data = resp.json()
    assert data["status"] == "down"
    assert data["reason"] == "postgres_unreachable"


def test_portal_health_engine_exception() -> None:
    """When engine check_health raises an exception, /health returns 503."""
    mock_engine = MagicMock()
    mock_engine.check_health = AsyncMock(side_effect=RuntimeError("connection dropped"))
    app = build_customer_portal_app(engine=mock_engine)
    client = TestClient(app)

    resp = client.get("/health")
    assert resp.status_code == 503
    assert resp.json()["status"] == "down"
    assert resp.json()["reason"] == "health_probe_raised"


def test_caddyfile_portal_routing() -> None:
    """Verify Caddyfile defines reverse-proxy for /api/portal/* to customer-portal:8005."""
    content = _CADDYFILE_PATH.read_text(encoding="utf-8")
    assert "handle /api/portal/*" in content
    assert "reverse_proxy customer-portal:8005" in content
    assert "max_size 10485760" in content
    assert "header_up X-Forwarded-For {remote_host}" in content
    assert "header_up X-Forwarded-Proto {scheme}" in content
    assert "header_up X-Real-IP {remote_host}" in content


def test_compose_customer_portal_service_contract() -> None:
    """Verify docker-compose.yml defines customer-portal with isolation requirements."""
    doc = yaml.safe_load(_COMPOSE_PATH.read_text(encoding="utf-8"))
    services = doc["services"]
    assert "customer-portal" in services

    portal = services["customer-portal"]
    assert portal["container_name"] == "nce-customer-portal"
    assert "8005" in str(portal["ports"])

    # Non-root user confinement
    assert portal.get("user") == "10001:10001"
    assert "no-new-privileges:true" in portal.get("security_opt", [])

    # Memory limit
    assert portal["deploy"]["resources"]["limits"]["memory"] == "512M"

    # Environment: thread pins and stripped admin credentials
    env = portal["environment"]
    assert env["OMP_NUM_THREADS"] == "${OMP_NUM_THREADS:-2}"
    assert env["MKL_NUM_THREADS"] == "${MKL_NUM_THREADS:-2}"
    assert env["TOKENIZERS_PARALLELISM"] == "${TOKENIZERS_PARALLELISM:-false}"
    assert env["NCE_MASTER_KEY"] == ""
    assert env["NCE_ADMIN_API_KEY"] == ""
    assert env["NCE_MASTER_KEY_FILE"] == "/run/secrets/nce_master_key"

    # Healthcheck hitting /health on port 8005
    hc = portal["healthcheck"]["test"]
    assert any("8005/health" in part for part in hc)

    # Independent lifecycle: Caddy does not depend on customer-portal
    caddy_depends = services["caddy"].get("depends_on", {})
    assert "customer-portal" not in caddy_depends, (
        "Caddy must NOT have a healthy dependency on customer-portal (independent lifecycle)"
    )


def test_customer_rate_limit_middleware_enforces_sliding_window(monkeypatch) -> None:
    """Verify CustomerRateLimitMiddleware enforces per-client sliding-window rate limits and returns 429."""
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from nce.vertical_modules.customer_portal import app as portal_app_mod
    from nce.vertical_modules.customer_portal.app import CustomerRateLimitMiddleware

    clock = [1000.0]
    monkeypatch.setattr(portal_app_mod.time, "monotonic", lambda: clock[0])

    async def _ping(_request):
        return JSONResponse({"ok": True})

    test_app = Starlette(
        routes=[Route("/health", _ping, methods=["GET"])],
        middleware=[Middleware(CustomerRateLimitMiddleware, max_requests_per_minute=3)],
    )
    client = TestClient(test_app)

    # 3 requests within 60s succeed for client A
    for _ in range(3):
        resp = client.get("/health", headers={"Authorization": "Bearer token-a"})
        assert resp.status_code == 200

    # 4th request within 60s for client A is rejected with 429
    resp_exceeded = client.get("/health", headers={"Authorization": "Bearer token-a"})
    assert resp_exceeded.status_code == 429
    assert resp_exceeded.json() == {"error": "rate_limit_exceeded"}

    # Client B (different Authorization header) has an independent bucket
    resp_other = client.get("/health", headers={"Authorization": "Bearer token-b"})
    assert resp_other.status_code == 200

    # Unauthenticated client falls back to request.client.host
    for _ in range(3):
        assert client.get("/health").status_code == 200
    assert client.get("/health").status_code == 429

    # Advance clock past the 60s window; client A is allowed again
    clock[0] = 1061.0
    resp_after_window = client.get("/health", headers={"Authorization": "Bearer token-a"})
    assert resp_after_window.status_code == 200
