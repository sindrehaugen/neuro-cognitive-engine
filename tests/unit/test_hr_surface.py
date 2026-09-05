"""
tests/unit/test_hr_surface.py
=============================
Unit test suite for Module 13 HR Engine MCP surface:
  - ToolSpec registrations in TOOL_REGISTRY (cacheable, admin_only, mutation).
  - All 8 MCP handlers in nce.vertical_modules.hr.mcp_handlers.
  - Opt-in gate enforcement (_check_hr_enabled).
  - RL-1 NEVER ranking enforcement in MCP surface (hr_match_skills, hr_coach).
  - Schema consistency in mcp_stdio_tools.py.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nce.mcp_errors import MCP_SCOPE_FORBIDDEN, McpError
from nce.mcp_stdio_tools import TOOLS
from nce.tool_registry import (
    ADMIN_ONLY_TOOLS,
    CACHEABLE_TOOLS,
    MUTATION_TOOLS,
    TOOL_REGISTRY,
)
from nce.vertical_modules.hr.mcp_handlers import (
    handle_hr_build_onboarding_quest,
    handle_hr_capacity,
    handle_hr_cert_status,
    handle_hr_coach,
    handle_hr_get_employee,
    handle_hr_log_one_on_one,
    handle_hr_match_skills,
    handle_hr_register_absence,
)

_NS_A = "00000000-0000-4000-8000-000000000001"

_HR_TOOLS = (
    "hr_get_employee",
    "hr_match_skills",
    "hr_capacity",
    "hr_cert_status",
    "hr_register_absence",
    "hr_build_onboarding_quest",
    "hr_log_one_on_one",
    "hr_coach",
)


class _AsyncCtx:
    def __init__(self, obj: object) -> None:
        self._obj = obj

    async def __aenter__(self) -> object:
        return self._obj

    async def __aexit__(self, *args: object) -> None:
        pass


def _make_mock_engine(hr_enabled: bool = True) -> MagicMock:
    conn = AsyncMock()
    conn.fetchrow.return_value = {"hr_enabled": hr_enabled}
    pool = MagicMock(spec=["acquire"])
    pool.acquire.return_value = _AsyncCtx(conn)

    engine = MagicMock()
    engine.pg_pool = pool
    return engine


# =========================================================================
# 1. Registry & Flag Verification
# =========================================================================


def test_hr_tools_registered_in_tool_registry():
    """All 8 HR tools must be registered in TOOL_REGISTRY."""
    for tool_name in _HR_TOOLS:
        assert tool_name in TOOL_REGISTRY, f"Tool {tool_name!r} missing from TOOL_REGISTRY"


def test_hr_tools_flags():
    """Verify exact flags for all 8 HR tools."""
    # Mutations (3 tools)
    assert "hr_register_absence" in MUTATION_TOOLS
    assert "hr_build_onboarding_quest" in MUTATION_TOOLS
    assert "hr_log_one_on_one" in MUTATION_TOOLS

    # Admin only (2 tools)
    assert "hr_build_onboarding_quest" in ADMIN_ONLY_TOOLS
    assert "hr_log_one_on_one" in ADMIN_ONLY_TOOLS

    # Cacheable (5 tools)
    assert "hr_get_employee" in CACHEABLE_TOOLS
    assert "hr_match_skills" in CACHEABLE_TOOLS
    assert "hr_capacity" in CACHEABLE_TOOLS
    assert "hr_cert_status" in CACHEABLE_TOOLS
    assert "hr_coach" in CACHEABLE_TOOLS


def test_hr_tools_advertised_in_stdio_tools():
    """All 8 HR tools must be present in mcp_stdio_tools.TOOLS with input schemas."""
    advertised = {t.name: t for t in TOOLS}
    for tool_name in _HR_TOOLS:
        assert tool_name in advertised, f"Tool {tool_name!r} missing from mcp_stdio_tools.TOOLS"
        t = advertised[tool_name]
        assert t.description, f"Tool {tool_name!r} has empty description"
        assert t.inputSchema, f"Tool {tool_name!r} has no inputSchema"
        assert "namespace_id" in t.inputSchema.get("properties", {})


# =========================================================================
# 2. Handler Execution & Scoping
# =========================================================================


@pytest.mark.asyncio
async def test_handle_hr_get_employee():
    engine = _make_mock_engine(hr_enabled=True)
    with patch("nce.vertical_modules.hr.mcp_handlers.do_get_employee") as mock_do:
        mock_do.return_value = {"employee_id": "EMP-ALPHA", "name": "Employee Alpha"}
        raw = await handle_hr_get_employee(
            engine,
            {"namespace_id": _NS_A, "employee_id": "EMP-ALPHA", "caller_role": "peer"},
        )
        res = json.loads(raw)
        assert res["employee_id"] == "EMP-ALPHA"
        mock_do.assert_called_once()


@pytest.mark.asyncio
async def test_handle_hr_match_skills_success():
    engine = _make_mock_engine(hr_enabled=True)
    with patch("nce.vertical_modules.hr.mcp_handlers.do_match_skills") as mock_do:
        mock_do.return_value = {"eligible_set": [], "eligible_count": 0}
        raw = await handle_hr_match_skills(
            engine,
            {"namespace_id": _NS_A, "required_skills": ["Dante routing"]},
        )
        res = json.loads(raw)
        assert "eligible_set" in res
        mock_do.assert_called_once()


@pytest.mark.asyncio
async def test_handle_hr_match_skills_refuses_ranking():
    """RL-1: Passing ranking parameters to hr_match_skills must raise McpError."""
    engine = _make_mock_engine(hr_enabled=True)
    with pytest.raises(McpError) as exc_info:
        await handle_hr_match_skills(
            engine,
            {"namespace_id": _NS_A, "standing_ranking": True},
        )
    assert exc_info.value.code == MCP_SCOPE_FORBIDDEN


@pytest.mark.asyncio
async def test_handle_hr_capacity():
    engine = _make_mock_engine(hr_enabled=True)
    with patch("nce.vertical_modules.hr.mcp_handlers.do_capacity") as mock_do:
        mock_do.return_value = {"capacities": [], "horizon_days": 14}
        raw = await handle_hr_capacity(
            engine,
            {"namespace_id": _NS_A, "department": "operations"},
        )
        res = json.loads(raw)
        assert "capacities" in res


@pytest.mark.asyncio
async def test_handle_hr_cert_status():
    engine = _make_mock_engine(hr_enabled=True)
    with patch("nce.vertical_modules.hr.mcp_handlers.do_cert_status") as mock_do:
        mock_do.return_value = {"certifications": [], "expiring_soon_count": 0}
        raw = await handle_hr_cert_status(
            engine,
            {"namespace_id": _NS_A, "warn_days": 60},
        )
        res = json.loads(raw)
        assert "certifications" in res


@pytest.mark.asyncio
async def test_handle_hr_register_absence():
    engine = _make_mock_engine(hr_enabled=True)
    with patch("nce.vertical_modules.hr.mcp_handlers.do_register_absence") as mock_do:
        mock_do.return_value = {
            "absence_id": "ABS-01",
            "employee_id": "EMP-ALPHA",
            "status": "approved",
        }
        raw = await handle_hr_register_absence(
            engine,
            {
                "namespace_id": _NS_A,
                "employee_id": "EMP-ALPHA",
                "absence_type": "vacation",
                "start_date": "2026-09-10",
                "end_date": "2026-09-15",
            },
        )
        res = json.loads(raw)
        assert res["absence_id"] == "ABS-01"


@pytest.mark.asyncio
async def test_handle_hr_build_onboarding_quest():
    engine = _make_mock_engine(hr_enabled=True)
    with patch("nce.vertical_modules.hr.mcp_handlers.do_build_onboarding_quest") as mock_do:
        mock_do.return_value = {
            "employee_id": "EMP-ALPHA",
            "stages": [],
            "total_tasks": 12,
        }
        raw = await handle_hr_build_onboarding_quest(
            engine,
            {
                "namespace_id": _NS_A,
                "employee_id": "EMP-ALPHA",
                "role": "technician",
            },
        )
        res = json.loads(raw)
        assert res["total_tasks"] == 12


@pytest.mark.asyncio
async def test_handle_hr_log_one_on_one():
    engine = _make_mock_engine(hr_enabled=True)
    with patch("nce.vertical_modules.hr.mcp_handlers.do_log_one_on_one") as mock_do:
        mock_do.return_value = {
            "employee_id": "EMP-ALPHA",
            "interviewer_id": "EMP-MGR",
            "status": "recorded",
        }
        raw = await handle_hr_log_one_on_one(
            engine,
            {
                "namespace_id": _NS_A,
                "employee_id": "EMP-ALPHA",
                "interviewer_id": "EMP-MGR",
                "notes": "Discussed Q-SYS training goals.",
            },
        )
        res = json.loads(raw)
        assert res["status"] == "recorded"


@pytest.mark.asyncio
async def test_handle_hr_coach_success():
    engine = _make_mock_engine(hr_enabled=True)
    with patch("nce.vertical_modules.hr.mcp_handlers.do_coach") as mock_do:
        mock_do.return_value = {
            "employee_id": "EMP-ALPHA",
            "growth_recommendations": [],
        }
        raw = await handle_hr_coach(
            engine,
            {"namespace_id": _NS_A, "employee_id": "EMP-ALPHA"},
        )
        res = json.loads(raw)
        assert res["employee_id"] == "EMP-ALPHA"


@pytest.mark.asyncio
async def test_handle_hr_coach_refuses_ranking():
    """RL-1: Passing compare_peers or standing_ranking to hr_coach must raise McpError."""
    engine = _make_mock_engine(hr_enabled=True)
    with pytest.raises(McpError) as exc_info:
        await handle_hr_coach(
            engine,
            {"namespace_id": _NS_A, "employee_id": "EMP-ALPHA", "compare_peers": True},
        )
    assert exc_info.value.code == MCP_SCOPE_FORBIDDEN


@pytest.mark.asyncio
async def test_handle_hr_disabled_namespace():
    """Calling an HR tool on an un-opted namespace raises McpError(MCP_SCOPE_FORBIDDEN)."""
    engine = _make_mock_engine(hr_enabled=False)
    with pytest.raises(McpError) as exc_info:
        await handle_hr_get_employee(
            engine,
            {"namespace_id": _NS_A, "employee_id": "EMP-ALPHA"},
        )
    assert exc_info.value.code == MCP_SCOPE_FORBIDDEN
    assert "HR vertical is not enabled" in exc_info.value.message
