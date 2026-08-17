"""
tests/unit/test_sales_read_routes.py
======================================
Acceptance tests for Batch 084 — Module 5.Wave 5 (read-routes).

Covers all 12 Sales REST read endpoints under /api/sales/...
Verifies parameters, auth, routing, and error status mapping.
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import json
import time
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient

from nce.admin_app import app
from nce.config import cfg


@pytest.fixture(autouse=True)
def bypass_lifespan():
    """Bypass Starlette app lifespan to avoid real DB connections at startup."""

    @asynccontextmanager
    async def dummy_lifespan(app):
        yield

    original_lifespan = app.router.lifespan_context
    app.router.lifespan_context = dummy_lifespan
    yield
    app.router.lifespan_context = original_lifespan


def _make_signature(key: str, method: str, path: str, timestamp: int, body: bytes = b"") -> str:
    parts = [method.upper(), path, str(timestamp)]
    if body:
        parts.append(hashlib.sha256(body).hexdigest())
    canonical = "\n".join(parts)
    return _hmac.new(key.encode(), canonical.encode(), hashlib.sha256).hexdigest()


def _valid_headers(key: str, method: str, path: str, body: bytes = b"") -> dict[str, str]:
    ts = int(time.time())
    sig = _make_signature(key, method, path, ts, body)
    return {
        "X-NCE-Timestamp": str(ts),
        "Authorization": f"HMAC-SHA256 {sig}",
    }


_NAMESPACE_ID = "00000000-0000-4000-8000-000000000001"
_API_KEY = cfg.NCE_API_KEY or "test-key"


# ---------------------------------------------------------------------------
# Happy path tests for GET endpoints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_admin_sales_customers_ok():
    mock_engine = MagicMock()
    headers = _valid_headers(_API_KEY, "GET", "/api/sales/customers")

    with (
        patch("nce.admin_state.engine", mock_engine),
        patch("nce.config.cfg.NCE_ADMIN_MTLS_ENABLED", False),
        patch("nce.admin_handlers.sales.do_list_customers", new_callable=AsyncMock) as mock_do,
    ):
        mock_do.return_value = {"items": [], "total": 0}
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.get(
                f"/api/sales/customers?namespace_id={_NAMESPACE_ID}&q=test&size=10&page=1&include_deleted=true",
                headers=headers,
            )
            assert r.status_code == 200
            assert r.json() == {"items": [], "total": 0}
            mock_do.assert_called_once_with(
                mock_engine,
                {
                    "namespace_id": _NAMESPACE_ID,
                    "q": "test",
                    "size": 10,
                    "page": 1,
                    "include_deleted": True,
                },
            )


@pytest.mark.asyncio
async def test_api_admin_sales_customer_profile_ok():
    mock_engine = MagicMock()
    headers = _valid_headers(_API_KEY, "GET", "/api/sales/customers/ACC123")

    with (
        patch("nce.admin_state.engine", mock_engine),
        patch("nce.config.cfg.NCE_ADMIN_MTLS_ENABLED", False),
        patch("nce.admin_handlers.sales.do_customer_profile", new_callable=AsyncMock) as mock_do,
    ):
        mock_do.return_value = {"company": {"name": "Test Company"}}
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.get(
                f"/api/sales/customers/ACC123?namespace_id={_NAMESPACE_ID}",
                headers=headers,
            )
            assert r.status_code == 200
            assert r.json() == {"company": {"name": "Test Company"}}
            mock_do.assert_called_once_with(
                mock_engine,
                {
                    "namespace_id": _NAMESPACE_ID,
                    "accountid": "ACC123",
                },
            )


@pytest.mark.asyncio
async def test_api_admin_sales_overview_ok():
    mock_engine = MagicMock()
    headers = _valid_headers(_API_KEY, "GET", "/api/sales/overview")

    with (
        patch("nce.admin_state.engine", mock_engine),
        patch("nce.config.cfg.NCE_ADMIN_MTLS_ENABLED", False),
        patch("nce.admin_handlers.sales.do_sales_overview", new_callable=AsyncMock) as mock_do,
    ):
        mock_do.return_value = {"stages": []}
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.get(
                f"/api/sales/overview?namespace_id={_NAMESPACE_ID}",
                headers=headers,
            )
            assert r.status_code == 200
            assert r.json() == {"stages": []}
            mock_do.assert_called_once_with(
                mock_engine,
                {
                    "namespace_id": _NAMESPACE_ID,
                },
            )


@pytest.mark.asyncio
async def test_api_admin_sales_seller_detail_ok():
    mock_engine = MagicMock()
    headers = _valid_headers(_API_KEY, "GET", "/api/sales/seller-detail/john-doe")

    with (
        patch("nce.admin_state.engine", mock_engine),
        patch("nce.config.cfg.NCE_ADMIN_MTLS_ENABLED", False),
        patch("nce.admin_handlers.sales.do_seller_detail", new_callable=AsyncMock) as mock_do,
    ):
        mock_do.return_value = {"user": "john-doe", "wonCount": 5}
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.get(
                f"/api/sales/seller-detail/john-doe?namespace_id={_NAMESPACE_ID}",
                headers=headers,
            )
            assert r.status_code == 200
            assert r.json() == {"user": "john-doe", "wonCount": 5}
            mock_do.assert_called_once_with(
                mock_engine,
                {
                    "namespace_id": _NAMESPACE_ID,
                    "user": "john-doe",
                },
            )


@pytest.mark.asyncio
async def test_api_admin_sales_dashboard_mine_ok():
    mock_engine = MagicMock()
    headers = _valid_headers(_API_KEY, "GET", "/api/sales/dashboard")

    with (
        patch("nce.admin_state.engine", mock_engine),
        patch("nce.config.cfg.NCE_ADMIN_MTLS_ENABLED", False),
        patch("nce.admin_handlers.sales.do_sales_dashboard", new_callable=AsyncMock) as mock_do,
    ):
        mock_do.return_value = {"pipeline": {}}
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.get(
                f"/api/sales/dashboard?namespace_id={_NAMESPACE_ID}&owner=john-doe&today=2026-06-24",
                headers=headers,
            )
            assert r.status_code == 200
            assert r.json() == {"pipeline": {}}
            mock_do.assert_called_once_with(
                mock_engine,
                {
                    "namespace_id": _NAMESPACE_ID,
                    "user": "john-doe",
                    "today": "2026-06-24",
                },
            )


@pytest.mark.asyncio
async def test_api_admin_sales_dashboard_team_ok():
    mock_engine = MagicMock()
    headers = _valid_headers(_API_KEY, "GET", "/api/sales/dashboard")

    with (
        patch("nce.admin_state.engine", mock_engine),
        patch("nce.config.cfg.NCE_ADMIN_MTLS_ENABLED", False),
        patch("nce.admin_handlers.sales.do_sales_dashboard", new_callable=AsyncMock) as mock_do,
    ):
        mock_do.return_value = {"pipeline": {}}
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.get(
                f"/api/sales/dashboard?namespace_id={_NAMESPACE_ID}&team=true",
                headers=headers,
            )
            assert r.status_code == 200
            assert r.json() == {"pipeline": {}}
            mock_do.assert_called_once_with(
                mock_engine,
                {
                    "namespace_id": _NAMESPACE_ID,
                    "user": "admin",
                },
            )


@pytest.mark.asyncio
async def test_api_admin_sales_stats_ok():
    mock_engine = MagicMock()
    headers = _valid_headers(_API_KEY, "GET", "/api/sales/stats")

    with (
        patch("nce.admin_state.engine", mock_engine),
        patch("nce.config.cfg.NCE_ADMIN_MTLS_ENABLED", False),
        patch("nce.admin_handlers.sales.do_sales_stats", new_callable=AsyncMock) as mock_do,
    ):
        mock_do.return_value = {"byItAv": {}}
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.get(
                f"/api/sales/stats?namespace_id={_NAMESPACE_ID}&period=quarter&offset=2&today=2026-06-24",
                headers=headers,
            )
            assert r.status_code == 200
            assert r.json() == {"byItAv": {}}
            mock_do.assert_called_once_with(
                mock_engine,
                {
                    "namespace_id": _NAMESPACE_ID,
                    "period": "quarter",
                    "offset": 2,
                    "today": "2026-06-24",
                },
            )


@pytest.mark.asyncio
async def test_api_admin_sales_manager_ok():
    mock_engine = MagicMock()
    headers = _valid_headers(_API_KEY, "GET", "/api/sales/manager")

    with (
        patch("nce.admin_state.engine", mock_engine),
        patch("nce.config.cfg.NCE_ADMIN_MTLS_ENABLED", False),
        patch("nce.admin_handlers.sales.do_sales_manager", new_callable=AsyncMock) as mock_do,
    ):
        mock_do.return_value = {"byAm": []}
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.get(
                f"/api/sales/manager?namespace_id={_NAMESPACE_ID}&period=year&offset=1",
                headers=headers,
            )
            assert r.status_code == 200
            assert r.json() == {"byAm": []}
            mock_do.assert_called_once_with(
                mock_engine,
                {
                    "namespace_id": _NAMESPACE_ID,
                    "period": "year",
                    "offset": 1,
                    "today": None,
                },
            )


@pytest.mark.asyncio
async def test_api_admin_sales_agreements_ok():
    mock_engine = MagicMock()
    headers = _valid_headers(_API_KEY, "GET", "/api/sales/agreements")

    with (
        patch("nce.admin_state.engine", mock_engine),
        patch("nce.config.cfg.NCE_ADMIN_MTLS_ENABLED", False),
        patch("nce.admin_handlers.sales.do_list_agreements", new_callable=AsyncMock) as mock_do,
    ):
        mock_do.return_value = {"items": []}
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.get(
                f"/api/sales/agreements?namespace_id={_NAMESPACE_ID}&q=contract&size=5",
                headers=headers,
            )
            assert r.status_code == 200
            assert r.json() == {"items": []}
            mock_do.assert_called_once_with(
                mock_engine,
                {
                    "namespace_id": _NAMESPACE_ID,
                    "q": "contract",
                    "size": 5,
                    "page": 0,
                    "include_deleted": False,
                },
            )


@pytest.mark.asyncio
async def test_api_admin_sales_agreement_detail_ok():
    mock_engine = MagicMock()
    headers = _valid_headers(_API_KEY, "GET", "/api/sales/agreements/AGR789")

    with (
        patch("nce.admin_state.engine", mock_engine),
        patch("nce.config.cfg.NCE_ADMIN_MTLS_ENABLED", False),
        patch("nce.admin_handlers.sales.do_agreement_detail", new_callable=AsyncMock) as mock_do,
    ):
        mock_do.return_value = {"msdyn_name": "Test Agreement"}
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.get(
                f"/api/sales/agreements/AGR789?namespace_id={_NAMESPACE_ID}",
                headers=headers,
            )
            assert r.status_code == 200
            assert r.json() == {"msdyn_name": "Test Agreement"}
            mock_do.assert_called_once_with(
                mock_engine,
                {
                    "namespace_id": _NAMESPACE_ID,
                    "agreementid": "AGR789",
                },
            )


@pytest.mark.asyncio
async def test_api_admin_sales_quote_detail_ok():
    mock_engine = MagicMock()
    headers = _valid_headers(_API_KEY, "GET", "/api/sales/quotes/Q456")

    with (
        patch("nce.admin_state.engine", mock_engine),
        patch("nce.config.cfg.NCE_ADMIN_MTLS_ENABLED", False),
        patch("nce.admin_handlers.sales.do_quote_detail", new_callable=AsyncMock) as mock_do,
    ):
        mock_do.return_value = {"name": "Test Quote"}
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.get(
                f"/api/sales/quotes/Q456?namespace_id={_NAMESPACE_ID}",
                headers=headers,
            )
            assert r.status_code == 200
            assert r.json() == {"name": "Test Quote"}
            mock_do.assert_called_once_with(
                mock_engine,
                {
                    "namespace_id": _NAMESPACE_ID,
                    "quoteid": "Q456",
                },
            )


@pytest.mark.asyncio
async def test_api_admin_sales_targets_get_ok():
    mock_engine = MagicMock()
    headers = _valid_headers(_API_KEY, "GET", "/api/sales/targets")

    with (
        patch("nce.admin_state.engine", mock_engine),
        patch("nce.config.cfg.NCE_ADMIN_MTLS_ENABLED", False),
        patch("nce.admin_handlers.sales.do_get_targets", new_callable=AsyncMock) as mock_do,
    ):
        mock_do.return_value = {"john-doe": {}}
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.get(
                f"/api/sales/targets?namespace_id={_NAMESPACE_ID}",
                headers=headers,
            )
            assert r.status_code == 200
            assert r.json() == {"john-doe": {}}
            mock_do.assert_called_once_with(
                mock_engine,
                {
                    "namespace_id": _NAMESPACE_ID,
                },
            )


# ---------------------------------------------------------------------------
# Targets PUT endpoint tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_admin_sales_targets_put_ok():
    mock_engine = MagicMock()
    body = {
        "namespace_id": _NAMESPACE_ID,
        "owner_slug": "john-doe",
        "metric": "meetings_monthly",
        "value": 15.0,
    }
    body_bytes = json.dumps(body).encode()
    headers = _valid_headers(_API_KEY, "PUT", "/api/sales/targets", body_bytes)

    headers["Content-Type"] = "application/json"
    with (
        patch("nce.admin_state.engine", mock_engine),
        patch("nce.config.cfg.NCE_ADMIN_MTLS_ENABLED", False),
        patch("nce.admin_handlers.sales.do_set_target", new_callable=AsyncMock) as mock_do,
    ):
        mock_do.return_value = {"ok": True}
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.put(
                "/api/sales/targets",
                content=body_bytes,
                headers=headers,
            )
            assert r.status_code == 200
            assert r.json() == {"ok": True}
            mock_do.assert_called_once_with(
                mock_engine,
                {
                    "namespace_id": _NAMESPACE_ID,
                    "owner_slug": "john-doe",
                    "metric": "meetings_monthly",
                    "value": 15.0,
                },
            )


# ---------------------------------------------------------------------------
# Error handling / validation tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_sales_missing_namespace_id():
    mock_engine = MagicMock()
    headers = _valid_headers(_API_KEY, "GET", "/api/sales/customers")

    with (
        patch("nce.admin_state.engine", mock_engine),
        patch("nce.config.cfg.NCE_ADMIN_MTLS_ENABLED", False),
    ):
        with TestClient(app, raise_server_exceptions=False) as client:
            # Query param missing
            r = client.get("/api/sales/customers", headers=headers)
            assert r.status_code == 422
            assert "Missing required query param" in r.json()["error"]

            # Query param invalid format
            r = client.get("/api/sales/customers?namespace_id=not-a-uuid", headers=headers)
            assert r.status_code == 422
            assert "Invalid namespace_id" in r.json()["error"]


@pytest.mark.asyncio
async def test_api_sales_engine_not_connected():
    headers = _valid_headers(_API_KEY, "GET", "/api/sales/customers")

    with (
        patch("nce.admin_state.engine", None),
        patch("nce.config.cfg.NCE_ADMIN_MTLS_ENABLED", False),
    ):
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.get(f"/api/sales/customers?namespace_id={_NAMESPACE_ID}", headers=headers)
            assert r.status_code == 503
            assert r.json()["error"] == "Engine not connected"


@pytest.mark.asyncio
async def test_api_sales_customer_profile_not_found():
    mock_engine = MagicMock()
    headers = _valid_headers(_API_KEY, "GET", "/api/sales/customers/UNKNOWN")

    with (
        patch("nce.admin_state.engine", mock_engine),
        patch("nce.config.cfg.NCE_ADMIN_MTLS_ENABLED", False),
        patch("nce.admin_handlers.sales.do_customer_profile", new_callable=AsyncMock) as mock_do,
    ):
        mock_do.return_value = {"error": "unknown_company", "detail": "not found"}
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.get(
                f"/api/sales/customers/UNKNOWN?namespace_id={_NAMESPACE_ID}",
                headers=headers,
            )
            assert r.status_code == 404
            assert r.json()["error"] == "unknown_company"


@pytest.mark.asyncio
async def test_api_sales_targets_put_invalid_metric():
    mock_engine = MagicMock()
    body = {
        "namespace_id": _NAMESPACE_ID,
        "owner_slug": "john-doe",
        "metric": "invalid_metric",
        "value": 15.0,
    }
    body_bytes = json.dumps(body).encode()
    headers = _valid_headers(_API_KEY, "PUT", "/api/sales/targets", body_bytes)

    headers["Content-Type"] = "application/json"
    with (
        patch("nce.admin_state.engine", mock_engine),
        patch("nce.config.cfg.NCE_ADMIN_MTLS_ENABLED", False),
    ):
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.put(
                "/api/sales/targets",
                content=body_bytes,
                headers=headers,
            )
            assert r.status_code == 422
            assert "valid metric" in r.json()["error"]
