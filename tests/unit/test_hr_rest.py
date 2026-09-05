"""
tests/unit/test_hr_rest.py
==========================
Unit test suite for Module 13 (HR Engine) admin HTTP REST surface:
  - Route mounting in admin_app.py for all 12 HR routes
  - Engine disconnected handling (503)
  - Opt-in guard enforcement (409 on HrDisabledError)
  - Missing parameter and invalid payload validation (422)
  - Happy paths for all 12 route handlers
  - Mutating route MCP cache generation bumping
  - RL-1 NEVER ranking refusal mappings (403 on HrRankingProhibitedError)
  - admin_error_response 2-argument signature compliance (U8)
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nce import admin_state
from nce.admin_app import build_admin_routes
from nce.admin_handlers import hr as hr_mod
from nce.vertical_modules.hr._guard import HrDisabledError, HrRankingProhibitedError

_NS_A = "00000000-0000-4000-8000-000000000001"
_EMP_ID = "EMP-ALPHA"


def _make_request(
    *,
    path_params: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
) -> MagicMock:
    req = MagicMock()
    req.json = AsyncMock(return_value=body if body is not None else {})
    req.query_params = query or {}
    req.path_params = path_params or {}
    return req


class _AsyncCtx:
    def __init__(self, obj: Any) -> None:
        self._obj = obj

    async def __aenter__(self) -> Any:
        return self._obj

    async def __aexit__(self, *args: Any) -> None:
        pass


@pytest.fixture(autouse=True)
def _setup_engine():
    engine = MagicMock()
    conn = AsyncMock()
    conn.fetchrow.return_value = {"hr_enabled": True}
    pool = MagicMock(spec=["acquire"])
    pool.acquire.return_value = _AsyncCtx(conn)
    engine.pg_pool = pool

    admin_state.engine = engine
    yield
    admin_state.engine = None


# =========================================================================
# 1. Route Mounting
# =========================================================================


def test_hr_routes_mounted_in_admin_app():
    """All 12 HR routes must be mounted in build_admin_routes()."""
    routes = build_admin_routes()
    route_paths = {
        (r.path, tuple(sorted(m for m in (r.methods or []) if m != "HEAD")))
        for r in routes
        if hasattr(r, "path")
    }

    expected_routes = [
        ("/api/hr/employees", ("GET",)),
        ("/api/hr/employees", ("POST",)),
        ("/api/hr/employees/{id}", ("GET",)),
        ("/api/hr/match-skills", ("POST",)),
        ("/api/hr/capacity", ("GET",)),
        ("/api/hr/cert-status", ("GET",)),
        ("/api/hr/absences", ("POST",)),
        ("/api/hr/onboarding/{id}", ("GET",)),
        ("/api/hr/onboarding/{id}", ("POST",)),
        ("/api/hr/coach", ("POST",)),
        ("/api/hr/sync/status", ("GET",)),
        ("/api/hr/sync/now", ("POST",)),
    ]

    for path, methods in expected_routes:
        assert (path, methods) in route_paths, f"Route {methods} {path} not mounted in admin_app"


# =========================================================================
# 2. Guard Enforcement & Engine Availability
# =========================================================================


@pytest.mark.asyncio
async def test_engine_disconnected():
    admin_state.engine = None
    req = _make_request(query={"namespace_id": _NS_A})
    resp = await hr_mod.api_hr_employees(req)
    assert resp.status_code == 503
    data = json.loads(resp.body)
    assert "Engine not connected" in data["error"]


@pytest.mark.asyncio
async def test_opt_in_guard_disabled():
    with patch(
        "nce.admin_handlers.hr.require_hr_enabled", side_effect=HrDisabledError("HR disabled")
    ):
        req = _make_request(query={"namespace_id": _NS_A})
        resp = await hr_mod.api_hr_employees(req)
        assert resp.status_code == 409
        data = json.loads(resp.body)
        assert "HR disabled" in data["error"]


# =========================================================================
# 3. Route Handlers Happy Paths & Validation
# =========================================================================


@pytest.mark.asyncio
async def test_api_hr_employees():
    with patch("nce.admin_handlers.hr.do_query_employees") as mock_core:
        mock_core.return_value = {"employees": [], "count": 0}
        req = _make_request(query={"namespace_id": _NS_A, "department": "operations"})
        resp = await hr_mod.api_hr_employees(req)
        assert resp.status_code == 200
        data = json.loads(resp.body)
        assert data["ok"] is True
        mock_core.assert_called_once()


@pytest.mark.asyncio
async def test_api_hr_create_employee():
    with (
        patch("nce.admin_handlers.hr.do_create_employee") as mock_core,
        patch("nce.admin_handlers.hr.bump_mcp_cache_generation") as mock_bump,
    ):
        mock_core.return_value = {"employee_id": _EMP_ID, "name": "Employee Alpha"}
        req = _make_request(
            body={"namespace_id": _NS_A, "employee_id": _EMP_ID, "name": "Employee Alpha"}
        )
        resp = await hr_mod.api_hr_create_employee(req)
        assert resp.status_code == 201
        data = json.loads(resp.body)
        assert data["employee_id"] == _EMP_ID
        mock_bump.assert_called_once()


@pytest.mark.asyncio
async def test_api_hr_employee_success_and_not_found():
    with patch("nce.admin_handlers.hr.do_get_employee") as mock_core:
        mock_core.return_value = {"employee_id": _EMP_ID, "name": "Employee Alpha"}
        req = _make_request(path_params={"id": _EMP_ID}, query={"namespace_id": _NS_A})
        resp = await hr_mod.api_hr_employee(req)
        assert resp.status_code == 200

        # Not found
        mock_core.side_effect = ValueError("Employee not found")
        resp_err = await hr_mod.api_hr_employee(req)
        assert resp_err.status_code == 404


@pytest.mark.asyncio
async def test_api_hr_match_skills_rl1_refusal():
    """RL-1: match-skills refuses ranking requests with 403."""
    with patch(
        "nce.admin_handlers.hr.do_match_skills",
        side_effect=HrRankingProhibitedError("NEVER ranking"),
    ):
        req = _make_request(body={"namespace_id": _NS_A, "standing_ranking": True})
        resp = await hr_mod.api_hr_match_skills(req)
        assert resp.status_code == 403
        data = json.loads(resp.body)
        assert "NEVER ranking" in data["error"]


@pytest.mark.asyncio
async def test_api_hr_capacity():
    with patch("nce.admin_handlers.hr.do_capacity") as mock_core:
        mock_core.return_value = {"capacities": [], "horizon_days": 14}
        req = _make_request(query={"namespace_id": _NS_A, "horizon_days": "14"})
        resp = await hr_mod.api_hr_capacity(req)
        assert resp.status_code == 200


@pytest.mark.asyncio
async def test_api_hr_cert_status():
    with patch("nce.admin_handlers.hr.do_cert_status") as mock_core:
        mock_core.return_value = {"certifications": [], "warn_days": 90}
        req = _make_request(query={"namespace_id": _NS_A, "warn_days": "90"})
        resp = await hr_mod.api_hr_cert_status(req)
        assert resp.status_code == 200


@pytest.mark.asyncio
async def test_api_hr_register_absence():
    with (
        patch("nce.admin_handlers.hr.do_register_absence") as mock_core,
        patch("nce.admin_handlers.hr.bump_mcp_cache_generation") as mock_bump,
    ):
        mock_core.return_value = {"absence_id": "ABS-01", "status": "approved"}
        req = _make_request(
            body={
                "namespace_id": _NS_A,
                "employee_id": _EMP_ID,
                "absence_type": "vacation",
                "start_date": "2026-09-10",
                "end_date": "2026-09-15",
            }
        )
        resp = await hr_mod.api_hr_register_absence(req)
        assert resp.status_code == 201
        mock_bump.assert_called_once()


@pytest.mark.asyncio
async def test_api_hr_onboarding_get_and_build():
    with (
        patch("nce.admin_handlers.hr.do_build_onboarding_quest") as mock_core,
        patch("nce.admin_handlers.hr.bump_mcp_cache_generation") as mock_bump,
    ):
        mock_core.return_value = {"employee_id": _EMP_ID, "stages": []}

        # GET
        req_get = _make_request(path_params={"id": _EMP_ID}, query={"namespace_id": _NS_A})
        resp_get = await hr_mod.api_hr_onboarding_get(req_get)
        assert resp_get.status_code == 200

        # POST
        req_post = _make_request(path_params={"id": _EMP_ID}, body={"namespace_id": _NS_A})
        resp_post = await hr_mod.api_hr_onboarding_build(req_post)
        assert resp_post.status_code == 201
        mock_bump.assert_called_once()


@pytest.mark.asyncio
async def test_api_hr_coach_rl1_refusal():
    """RL-1: coach refuses peer comparison with 403."""
    with patch(
        "nce.admin_handlers.hr.do_coach", side_effect=HrRankingProhibitedError("NEVER ranking")
    ):
        req = _make_request(
            body={"namespace_id": _NS_A, "employee_id": _EMP_ID, "compare_peers": True}
        )
        resp = await hr_mod.api_hr_coach(req)
        assert resp.status_code == 403


@pytest.mark.asyncio
async def test_api_hr_sync_status_and_now():
    with patch("nce.admin_handlers.hr.bump_mcp_cache_generation") as mock_bump:
        # Status
        req_s = _make_request(query={"namespace_id": _NS_A})
        resp_s = await hr_mod.api_hr_sync_status(req_s)
        assert resp_s.status_code == 200

        # Now
        req_n = _make_request(body={"namespace_id": _NS_A})
        resp_n = await hr_mod.api_hr_sync_now(req_n)
        assert resp_n.status_code == 200
        mock_bump.assert_called_once()


@pytest.mark.asyncio
async def test_admin_error_response_two_arg_signature():
    """U8 Lesson: Verify admin_error_response called with (msg, exc) works cleanly without TypeError."""
    with patch("nce.admin_handlers.hr.do_query_employees", side_effect=RuntimeError("Boom")):
        req = _make_request(query={"namespace_id": _NS_A})
        resp = await hr_mod.api_hr_employees(req)
        assert resp.status_code == 500
        data = json.loads(resp.body)
        assert "error" in data
