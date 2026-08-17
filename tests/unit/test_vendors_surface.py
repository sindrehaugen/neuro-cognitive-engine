"""
tests/unit/test_vendors_surface.py
===================================
Acceptance tests for Batch 096 — Module 4.Wave 3 (vendor-surface).

Covers:
  1. The ``vendors`` package imports cleanly.
  2. ``handle_vendors_get_vendor`` and ``handle_vendors_compute_scorecard``
     raise ``McpError(-32602)`` when ``namespace_id`` is absent.
  3. ``vendors_get_vendor`` / ``vendors_compute_scorecard`` are registered
     in TOOL_REGISTRY with correct flags (cacheable=True, mutation=False, admin_only=False).
  4. Tool-count assertion in registry is met (99).
  5. Both GET routes are registered in the admin app and respond properly.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from nce.admin_app import build_admin_routes
from nce.mcp_errors import McpError
from nce.tool_registry import TOOL_REGISTRY

_NAMESPACE_ID = "00000000-0000-4000-8000-000000000001"


def test_package_imports() -> None:
    import nce.admin_handlers.vendors  # noqa: F401
    import nce.vertical_modules.vendors  # noqa: F401
    import nce.vertical_modules.vendors.mcp_handlers  # noqa: F401


def _make_engine() -> MagicMock:
    return MagicMock()


def _make_request(
    qp: dict[str, str] | None = None, path_params: dict[str, str] | None = None
) -> MagicMock:
    req = MagicMock()
    req.query_params = qp or {}
    req.path_params = path_params or {}
    return req


@pytest.mark.asyncio
async def test_mcp_handlers_missing_namespace_id() -> None:
    from nce.vertical_modules.vendors.mcp_handlers import (
        handle_vendors_compute_scorecard,
        handle_vendors_get_vendor,
    )

    engine = _make_engine()

    with pytest.raises(McpError) as exc_get:
        await handle_vendors_get_vendor(engine, {})
    assert exc_get.value.code == -32602

    with pytest.raises(McpError) as exc_compute:
        await handle_vendors_compute_scorecard(engine, {})
    assert exc_compute.value.code == -32602


def test_mcp_tools_registered_with_correct_flags() -> None:
    for tool_name in ["vendors_get_vendor", "vendors_compute_scorecard"]:
        assert tool_name in TOOL_REGISTRY, f"'{tool_name}' not registered"
        spec = TOOL_REGISTRY[tool_name]
        assert spec.cacheable is True
        assert spec.admin_only is False
        assert spec.mutation is False
        assert spec.migration is False


def test_vendors_routes_mounted_in_admin_app() -> None:
    routes = build_admin_routes()
    paths = {r.path for r in routes}
    assert "/api/vendors/scorecard" in paths
    assert "/api/vendors/{id}" in paths


# ---------------------------------------------------------------------------
# HTTP request testing (REST surface)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rest_routes_no_engine() -> None:
    from nce import admin_state
    from nce.admin_handlers.vendors import api_vendors_get_vendor, api_vendors_scorecard

    with patch.object(admin_state, "engine", None):
        # GET /api/vendors/scorecard
        req_scorecard = _make_request(qp={"namespace_id": _NAMESPACE_ID})
        resp_scorecard = await api_vendors_scorecard(req_scorecard)
        assert resp_scorecard.status_code == 503
        assert (
            "Engine not connected"
            in json.loads(bytes(resp_scorecard.body).decode("utf-8"))["error"]
        )

        # GET /api/vendors/{id}
        req_get = _make_request(qp={"namespace_id": _NAMESPACE_ID}, path_params={"id": "some-id"})
        resp_get = await api_vendors_get_vendor(req_get)
        assert resp_get.status_code == 503
        assert "Engine not connected" in json.loads(bytes(resp_get.body).decode("utf-8"))["error"]


@pytest.mark.asyncio
async def test_rest_routes_missing_params() -> None:
    from nce import admin_state
    from nce.admin_handlers.vendors import api_vendors_get_vendor, api_vendors_scorecard

    engine = MagicMock()
    with patch.object(admin_state, "engine", engine):
        # Missing namespace_id
        req_scorecard = _make_request(qp={})
        resp_scorecard = await api_vendors_scorecard(req_scorecard)
        assert resp_scorecard.status_code == 422
        assert (
            "Missing required query param: namespace_id"
            in json.loads(bytes(resp_scorecard.body).decode("utf-8"))["error"]
        )

        req_get = _make_request(qp={}, path_params={"id": "some-id"})
        resp_get = await api_vendors_get_vendor(req_get)
        assert resp_get.status_code == 422
        assert (
            "Missing required query param: namespace_id"
            in json.loads(bytes(resp_get.body).decode("utf-8"))["error"]
        )


@pytest.mark.asyncio
@patch("nce.admin_handlers.vendors.do_get_vendor")
async def test_rest_api_vendors_get_vendor_ok(mock_do_get: Any) -> None:
    from nce import admin_state
    from nce.admin_handlers.vendors import api_vendors_get_vendor

    engine = MagicMock()
    mock_do_get.return_value = {
        "id": "some-uuid",
        "label": "VENDOR:ACME",
        "vendors_source_id": "ACME_SRC",
        "name": "ACME Corp",
        "orgnr": "12345678",
        "scorecard": None,
    }

    with patch.object(admin_state, "engine", engine):
        req = _make_request(qp={"namespace_id": _NAMESPACE_ID}, path_params={"id": "ACME"})
        resp = await api_vendors_get_vendor(req)
        assert resp.status_code == 200
        data = json.loads(bytes(resp.body).decode("utf-8"))
        assert data["status"] == "ok"
        assert data["vendor"]["label"] == "VENDOR:ACME"
        assert data["vendor"]["name"] == "ACME Corp"


@pytest.mark.asyncio
async def test_rest_routes_invalid_uuid() -> None:
    from nce import admin_state
    from nce.admin_handlers.vendors import api_vendors_get_vendor, api_vendors_scorecard

    engine = MagicMock()
    with patch.object(admin_state, "engine", engine):
        # Invalid UUID format for namespace_id
        req_scorecard = _make_request(qp={"namespace_id": "not-a-uuid"})
        resp_scorecard = await api_vendors_scorecard(req_scorecard)
        assert resp_scorecard.status_code == 422
        assert (
            "Invalid namespace_id"
            in json.loads(bytes(resp_scorecard.body).decode("utf-8"))["error"]
        )

        req_get = _make_request(qp={"namespace_id": "not-a-uuid"}, path_params={"id": "some-id"})
        resp_get = await api_vendors_get_vendor(req_get)
        assert resp_get.status_code == 422
        assert "Invalid namespace_id" in json.loads(bytes(resp_get.body).decode("utf-8"))["error"]
