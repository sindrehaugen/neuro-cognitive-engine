"""Unit tests for Wave MK-2: marketing_retract_testimonial surface completion.

Verifies:
  1. handle_marketing_retract_testimonial MCP tool handler (Actor, mutation=True, admin_only=True).
  2. POST /api/marketing/testimonials/retract REST handler and routing.
  3. Proper cache invalidation via bump_mcp_cache_generation on mutation.
  4. Error handling (missing namespace, missing testimonial_id, module disabled).
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from starlette.requests import Request

from nce.admin_handlers._shared import admin_state
from nce.admin_handlers.marketing import api_marketing_retract_testimonial
from nce.mcp_errors import McpError
from nce.vertical_modules.marketing.mcp_handlers import (
    handle_marketing_retract_testimonial,
)

_NAMESPACE_ID = "00000000-0000-4000-8000-000000000001"
_TESTIMONIAL_ID = str(uuid4())


def _make_mock_engine() -> MagicMock:
    engine = MagicMock()
    conn = AsyncMock()
    conn.fetchrow.return_value = {"marketing_source_id": "marketing:testimonial:test1234"}
    conn.execute.return_value = "UPDATE 1"
    ctx = AsyncMock()
    ctx.__aenter__.return_value = conn
    ctx.__aexit__.return_value = None

    pool = MagicMock()
    pool.acquire.return_value = ctx
    engine.pg_pool = pool
    engine.pool = pool
    return engine


def _make_request(body: dict[str, Any]) -> Request:
    raw_body = json.dumps(body).encode("utf-8")

    async def _receive():
        return {"type": "http.request", "body": raw_body, "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/marketing/testimonials/retract",
        "headers": [(b"content-type", b"application/json")],
    }
    return Request(scope, receive=_receive)


# ---------------------------------------------------------------------------
# MCP Handler Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_handle_marketing_retract_testimonial_success() -> None:
    """MCP tool successfully retracts testimonial and returns valid JSON."""
    engine = _make_mock_engine()
    params = {
        "namespace_id": _NAMESPACE_ID,
        "testimonial_id": _TESTIMONIAL_ID,
        "reason": "Customer requested right-to-retract under GDPR Art. 17",
    }

    with patch(
        "nce.vertical_modules.marketing.mcp_handlers.require_marketing_enabled",
        new_callable=AsyncMock,
    ):
        raw_result = await handle_marketing_retract_testimonial(engine, params)

    res = json.loads(raw_result)
    assert res["ok"] is True
    assert res["status"] == "retracted"
    assert res["consent"] is False
    assert res["testimonial_id"] == _TESTIMONIAL_ID
    assert "GDPR Art. 17" in res["reason"]


@pytest.mark.asyncio
async def test_mcp_handle_marketing_retract_testimonial_requires_namespace() -> None:
    """MCP tool rejects call with missing namespace_id."""
    engine = _make_mock_engine()
    with pytest.raises(McpError):
        await handle_marketing_retract_testimonial(engine, {"testimonial_id": _TESTIMONIAL_ID})


@pytest.mark.asyncio
async def test_mcp_handle_marketing_retract_testimonial_disabled() -> None:
    """MCP tool refuses call when marketing engine is disabled in namespace."""
    from nce.vertical_modules.marketing._guard import MarketingDisabledError

    engine = _make_mock_engine()
    params = {"namespace_id": _NAMESPACE_ID, "testimonial_id": _TESTIMONIAL_ID}

    with patch(
        "nce.vertical_modules.marketing.mcp_handlers.require_marketing_enabled",
        side_effect=MarketingDisabledError("Marketing engine is not enabled"),
    ):
        with pytest.raises(McpError) as exc_info:
            await handle_marketing_retract_testimonial(engine, params)
        assert "not enabled" in str(exc_info.value)


# ---------------------------------------------------------------------------
# REST Route Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rest_api_marketing_retract_testimonial_success() -> None:
    """REST endpoint retracts testimonial, returns 200, and bumps cache generation."""
    engine = _make_mock_engine()
    admin_state.engine = engine

    req = _make_request(
        {
            "namespace_id": _NAMESPACE_ID,
            "testimonial_id": _TESTIMONIAL_ID,
            "reason": "Customer opted out of web testimonials",
        }
    )

    with (
        patch("nce.admin_handlers.marketing.require_marketing_enabled", new_callable=AsyncMock),
        patch(
            "nce.admin_handlers.marketing.bump_mcp_cache_generation", new_callable=AsyncMock
        ) as mock_bump,
    ):
        resp = await api_marketing_retract_testimonial(req)

    assert resp.status_code == 200
    data = json.loads(resp.body)
    assert data["ok"] is True
    assert data["status"] == "retracted"
    assert data["consent"] is False
    assert data["testimonial_id"] == _TESTIMONIAL_ID
    mock_bump.assert_awaited_once_with(engine, route="api_marketing_retract_testimonial")


@pytest.mark.asyncio
async def test_rest_api_marketing_retract_testimonial_missing_id() -> None:
    """REST endpoint returns 422 when testimonial_id is missing."""
    engine = _make_mock_engine()
    admin_state.engine = engine

    req = _make_request(
        {
            "namespace_id": _NAMESPACE_ID,
        }
    )

    with patch("nce.admin_handlers.marketing.require_marketing_enabled", new_callable=AsyncMock):
        resp = await api_marketing_retract_testimonial(req)

    assert resp.status_code == 422
    data = json.loads(resp.body)
    assert "testimonial_id is required" in data["error"]


@pytest.mark.asyncio
async def test_rest_api_marketing_retract_testimonial_disabled() -> None:
    """REST endpoint returns 409 when marketing is disabled."""
    from nce.vertical_modules.marketing._guard import MarketingDisabledError

    engine = _make_mock_engine()
    admin_state.engine = engine

    req = _make_request(
        {
            "namespace_id": _NAMESPACE_ID,
            "testimonial_id": _TESTIMONIAL_ID,
        }
    )

    with patch(
        "nce.admin_handlers.marketing.require_marketing_enabled",
        side_effect=MarketingDisabledError("Marketing disabled for tenant"),
    ):
        resp = await api_marketing_retract_testimonial(req)

    assert resp.status_code == 409
    data = json.loads(resp.body)
    assert "Marketing disabled" in data["error"]


def test_rest_route_mounted_in_admin_app() -> None:
    """Verify /api/marketing/testimonials/retract route is mounted in admin_app."""
    from nce.admin_app import create_admin_app

    app = create_admin_app()

    found = False
    for route in app.routes:
        if getattr(route, "path", None) == "/api/marketing/testimonials/retract":
            found = True
            assert "POST" in route.methods
            break
    assert found, "Route /api/marketing/testimonials/retract not mounted in admin_app"
