"""
tests/unit/test_project_pl_routes.py
====================================
Acceptance tests for Batch 077 — Module 7.Wave 10 (pl-rest-routes).

Covers:
  1. GET /api/project/my-day delegates to ``do_my_day``.
  2. GET /api/project/capacity delegates to ``do_capacity``.
  3. GET /api/project/{id}/scope-creep delegates to ``do_detect_scope_creep``.
  4. GET /api/project/{id}/status-report delegates to ``do_status_report``.
  5. Routes return 503 when engine is not connected.
  6. Routes validate namespace_id.
  7. Routes are mounted in the admin app.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_NAMESPACE_ID = "00000000-0000-4000-8000-000000000001"
_PROJECT_ID = "PROJECT:Q123"


def _make_request(
    qp: dict[str, str] | None = None,
    path_params: dict[str, str] | None = None,
) -> MagicMock:
    """Minimal Starlette-like request mock."""
    req = MagicMock()
    req.query_params = qp or {}
    req.path_params = path_params or {}
    return req


# ---------------------------------------------------------------------------
# 1. GET /api/project/my-day
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_my_day_route_success():
    from nce.admin_handlers.project import api_admin_project_my_day

    core_result = {"ok": True, "tasks": []}

    with (
        patch("nce.admin_handlers.project.admin_state") as mock_state,
        patch("nce.admin_handlers.project.validate_agent_id"),
        patch(
            "nce.vertical_modules.project.pl.do_my_day",
            new=AsyncMock(return_value=core_result),
        ) as mock_core,
    ):
        mock_state.engine = MagicMock()
        req = _make_request(qp={"namespace_id": _NAMESPACE_ID, "employee_id": "EMPLOYEE:123"})
        resp = await api_admin_project_my_day(req)

    assert resp.status_code == 200
    data = json.loads(resp.body)
    assert data["ok"] is True
    assert "tasks" in data
    mock_core.assert_awaited_once()


# ---------------------------------------------------------------------------
# 2. GET /api/project/capacity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_capacity_route_success():
    from nce.admin_handlers.project import api_admin_project_capacity

    core_result = {"ok": True, "teams": {}}

    with (
        patch("nce.admin_handlers.project.admin_state") as mock_state,
        patch("nce.admin_handlers.project.validate_agent_id"),
        patch(
            "nce.vertical_modules.project.pl.do_capacity",
            new=AsyncMock(return_value=core_result),
        ) as mock_core,
    ):
        mock_state.engine = MagicMock()
        req = _make_request(qp={"namespace_id": _NAMESPACE_ID, "start_date": "2026-06-23"})
        resp = await api_admin_project_capacity(req)

    assert resp.status_code == 200
    data = json.loads(resp.body)
    assert data["ok"] is True
    assert "teams" in data
    mock_core.assert_awaited_once()


# ---------------------------------------------------------------------------
# 3. GET /api/project/{id}/scope-creep
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scope_creep_route_success():
    from nce.admin_handlers.project import api_admin_project_scope_creep

    core_result = {"ok": True, "change_orders": [], "delta_signed_vs_current": 0.0}

    with (
        patch("nce.admin_handlers.project.admin_state") as mock_state,
        patch("nce.admin_handlers.project.validate_agent_id"),
        patch(
            "nce.vertical_modules.project.insights.do_detect_scope_creep",
            new=AsyncMock(return_value=core_result),
        ) as mock_core,
    ):
        mock_state.engine = MagicMock()
        req = _make_request(
            qp={"namespace_id": _NAMESPACE_ID},
            path_params={"id": _PROJECT_ID},
        )
        resp = await api_admin_project_scope_creep(req)

    assert resp.status_code == 200
    data = json.loads(resp.body)
    assert data["ok"] is True
    assert "change_orders" in data
    mock_core.assert_awaited_once()


# ---------------------------------------------------------------------------
# 4. GET /api/project/{id}/status-report
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_report_route_success():
    from nce.admin_handlers.project import api_admin_project_status_report

    core_result = {"ok": True, "narrative": "A status report", "citations": []}

    with (
        patch("nce.admin_handlers.project.admin_state") as mock_state,
        patch("nce.admin_handlers.project.validate_agent_id"),
        patch(
            "nce.vertical_modules.project.insights.do_status_report",
            new=AsyncMock(return_value=core_result),
        ) as mock_core,
    ):
        mock_state.engine = MagicMock()
        req = _make_request(
            qp={
                "namespace_id": _NAMESPACE_ID,
                "estimated_cost_nok": "150000",
                "estimated_revenue_nok": "200000",
            },
            path_params={"id": _PROJECT_ID},
        )
        resp = await api_admin_project_status_report(req)

    assert resp.status_code == 200
    data = json.loads(resp.body)
    assert data["ok"] is True
    assert data["narrative"] == "A status report"
    mock_core.assert_awaited_once()


# ---------------------------------------------------------------------------
# 5. Missing / Invalid namespace validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_routes_missing_namespace_id():
    from nce.admin_handlers.project import api_admin_project_my_day

    with patch("nce.admin_handlers.project.admin_state") as mock_state:
        mock_state.engine = MagicMock()
        req = _make_request(qp={})
        resp = await api_admin_project_my_day(req)

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_routes_invalid_namespace_id():
    from nce.admin_handlers.project import api_admin_project_my_day

    with (
        patch("nce.admin_handlers.project.admin_state") as mock_state,
        patch(
            "nce.admin_handlers.project.validate_agent_id",
            side_effect=ValueError("bad uuid"),
        ),
    ):
        mock_state.engine = MagicMock()
        req = _make_request(qp={"namespace_id": "invalid-uuid"})
        resp = await api_admin_project_my_day(req)

    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 6. Engine disconnection (503)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_routes_engine_not_connected():
    from nce.admin_handlers.project import api_admin_project_my_day

    with patch("nce.admin_handlers.project.admin_state") as mock_state:
        mock_state.engine = None
        req = _make_request(qp={"namespace_id": _NAMESPACE_ID})
        resp = await api_admin_project_my_day(req)

    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# 7. Mounting validation
# ---------------------------------------------------------------------------


def test_routes_mounted_in_admin_app():
    from nce.admin_app import build_admin_routes

    routes = build_admin_routes()
    paths = {r.path for r in routes}
    assert "/api/project/my-day" in paths
    assert "/api/project/capacity" in paths
    assert "/api/project/{id}/scope-creep" in paths
    assert "/api/project/{id}/status-report" in paths
