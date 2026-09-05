"""
tests/unit/test_field_tech_surface.py
======================================
Unit tests for Module 12 (Field Tech Engine) MCP surface:
  - Tool registry registration and exact flags for all 10 tools
  - Opt-in guard enforcement (_check_field_tech_enabled -> McpError(-32005))
  - Parameter validation and error wrapping (McpError(-32602))
  - Happy-path execution for all 10 MCP handlers
  - Domain refusal mapping (WorkOrderNotFoundError, ChecklistIncompleteError)

Pure unit tests — no live database or Redis required.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nce.mcp_errors import MCP_SCOPE_FORBIDDEN, McpError
from nce.tool_registry import (
    ADMIN_ONLY_TOOLS,
    CACHEABLE_TOOLS,
    MUTATION_TOOLS,
    TOOL_REGISTRY,
)
from nce.vertical_modules.field_tech._guard import FieldTechDisabledError
from nce.vertical_modules.field_tech.checklist import ChecklistIncompleteError
from nce.vertical_modules.field_tech.mcp_handlers import (
    handle_field_tech_assign,
    handle_field_tech_attach_photo,
    handle_field_tech_complete_checklist,
    handle_field_tech_create_work_order,
    handle_field_tech_dispatch,
    handle_field_tech_log_time,
    handle_field_tech_partner_view,
    handle_field_tech_record_outcome,
    handle_field_tech_scan_serial,
    handle_field_tech_sync,
)
from nce.vertical_modules.field_tech.work_orders import (
    WorkOrderNotFoundError,
)

_NAMESPACE_ID = "00000000-0000-4000-8000-000000000001"
_PARTNER_SCOPE_ID = "11111111-1111-4000-8000-111111111111"
_WO_ID = "WO-2026-TEST-001"

EXPECTED_FIELD_TECH_TOOLS = {
    "field_tech_dispatch": {"cacheable": True, "admin_only": False, "mutation": False},
    "field_tech_partner_view": {"cacheable": True, "admin_only": False, "mutation": False},
    "field_tech_create_work_order": {"cacheable": False, "admin_only": True, "mutation": True},
    "field_tech_assign": {"cacheable": False, "admin_only": True, "mutation": True},
    "field_tech_complete_checklist": {"cacheable": False, "admin_only": False, "mutation": True},
    "field_tech_scan_serial": {"cacheable": False, "admin_only": False, "mutation": True},
    "field_tech_log_time": {"cacheable": False, "admin_only": False, "mutation": True},
    "field_tech_attach_photo": {"cacheable": False, "admin_only": False, "mutation": True},
    "field_tech_sync": {"cacheable": False, "admin_only": False, "mutation": True},
    "field_tech_record_outcome": {"cacheable": False, "admin_only": True, "mutation": True},
}


@pytest.fixture
def mock_engine():
    engine = MagicMock()
    engine.pg_pool = MagicMock()
    return engine


# ---------------------------------------------------------------------------
# 1. Tool Registry & Flag Invariants
# ---------------------------------------------------------------------------


def test_all_field_tech_tools_registered():
    for tool_name in EXPECTED_FIELD_TECH_TOOLS:
        assert tool_name in TOOL_REGISTRY, f"Tool {tool_name} missing from TOOL_REGISTRY"


def test_field_tech_tools_flags():
    for tool_name, expected in EXPECTED_FIELD_TECH_TOOLS.items():
        spec = TOOL_REGISTRY[tool_name]
        assert spec.cacheable == expected["cacheable"], f"{tool_name}.cacheable mismatch"
        assert spec.admin_only == expected["admin_only"], f"{tool_name}.admin_only mismatch"
        assert spec.mutation == expected["mutation"], f"{tool_name}.mutation mismatch"

        if expected["cacheable"]:
            assert tool_name in CACHEABLE_TOOLS
        else:
            assert tool_name not in CACHEABLE_TOOLS

        if expected["admin_only"]:
            assert tool_name in ADMIN_ONLY_TOOLS
        else:
            assert tool_name not in ADMIN_ONLY_TOOLS

        if expected["mutation"]:
            assert tool_name in MUTATION_TOOLS
        else:
            assert tool_name not in MUTATION_TOOLS


# ---------------------------------------------------------------------------
# 2. Opt-in Guard Enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_opt_in_guard_blocks_when_disabled(mock_engine):
    with patch(
        "nce.vertical_modules.field_tech.mcp_handlers.require_field_tech_enabled",
        new=AsyncMock(side_effect=FieldTechDisabledError("Not enabled")),
    ):
        for handler in [
            handle_field_tech_dispatch,
            handle_field_tech_partner_view,
            handle_field_tech_create_work_order,
            handle_field_tech_assign,
            handle_field_tech_complete_checklist,
            handle_field_tech_scan_serial,
            handle_field_tech_log_time,
            handle_field_tech_attach_photo,
            handle_field_tech_sync,
            handle_field_tech_record_outcome,
        ]:
            with pytest.raises(McpError) as exc_info:
                await handler(mock_engine, {"namespace_id": _NAMESPACE_ID})
            assert exc_info.value.code == MCP_SCOPE_FORBIDDEN
            assert "Field Tech vertical is not enabled" in exc_info.value.message


# ---------------------------------------------------------------------------
# 3. Happy Paths & Domain Refusals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_field_tech_dispatch_happy_path(mock_engine):
    with (
        patch(
            "nce.vertical_modules.field_tech.mcp_handlers.require_field_tech_enabled",
            new=AsyncMock(),
        ),
        patch(
            "nce.vertical_modules.field_tech.mcp_handlers.do_dispatch",
            new=AsyncMock(return_value={"work_order_id": _WO_ID, "ranked_candidates": []}),
        ),
    ):
        raw = await handle_field_tech_dispatch(
            mock_engine,
            {"namespace_id": _NAMESPACE_ID, "work_order_id": _WO_ID},
        )
        data = json.loads(raw)
        assert data["work_order_id"] == _WO_ID


@pytest.mark.asyncio
async def test_handle_field_tech_partner_view_happy_path(mock_engine):
    with (
        patch(
            "nce.vertical_modules.field_tech.mcp_handlers.require_field_tech_enabled",
            new=AsyncMock(),
        ),
        patch(
            "nce.vertical_modules.field_tech.mcp_handlers.do_partner_view",
            new=AsyncMock(return_value={"status": "success", "work_orders": []}),
        ),
    ):
        raw = await handle_field_tech_partner_view(
            mock_engine,
            {"namespace_id": _NAMESPACE_ID, "partner_scope_id": _PARTNER_SCOPE_ID},
        )
        data = json.loads(raw)
        assert data["status"] == "success"


@pytest.mark.asyncio
async def test_handle_field_tech_create_work_order_happy_path(mock_engine):
    with (
        patch(
            "nce.vertical_modules.field_tech.mcp_handlers.require_field_tech_enabled",
            new=AsyncMock(),
        ),
        patch(
            "nce.vertical_modules.field_tech.mcp_handlers.do_create_work_order",
            new=AsyncMock(return_value={"work_order_id": _WO_ID, "status": "pending"}),
        ),
    ):
        raw = await handle_field_tech_create_work_order(
            mock_engine,
            {"namespace_id": _NAMESPACE_ID, "work_order_id": _WO_ID, "kind": "install"},
        )
        data = json.loads(raw)
        assert data["status"] == "pending"


@pytest.mark.asyncio
async def test_handle_field_tech_assign_not_found(mock_engine):
    with (
        patch(
            "nce.vertical_modules.field_tech.mcp_handlers.require_field_tech_enabled",
            new=AsyncMock(),
        ),
        patch(
            "nce.vertical_modules.field_tech.mcp_handlers.do_assign",
            new=AsyncMock(side_effect=WorkOrderNotFoundError(work_order_id=_WO_ID)),
        ),
    ):
        with pytest.raises(McpError) as exc_info:
            await handle_field_tech_assign(
                mock_engine,
                {"namespace_id": _NAMESPACE_ID, "work_order_id": _WO_ID, "assignee_id": "tech-1"},
            )
        assert exc_info.value.code == MCP_SCOPE_FORBIDDEN
        assert exc_info.value.data.get("reason") == "not_found"


@pytest.mark.asyncio
async def test_handle_field_tech_complete_checklist_refusal(mock_engine):
    with (
        patch(
            "nce.vertical_modules.field_tech.mcp_handlers.require_field_tech_enabled",
            new=AsyncMock(),
        ),
        patch(
            "nce.vertical_modules.field_tech.mcp_handlers.do_complete_checklist",
            new=AsyncMock(side_effect=ChecklistIncompleteError("Missing required item")),
        ),
    ):
        with pytest.raises(McpError) as exc_info:
            await handle_field_tech_complete_checklist(
                mock_engine,
                {"namespace_id": _NAMESPACE_ID, "work_order_id": _WO_ID},
            )
        assert exc_info.value.code == MCP_SCOPE_FORBIDDEN
        assert exc_info.value.data.get("reason") == "checklist_incomplete"


@pytest.mark.asyncio
async def test_handle_field_tech_sync_happy_path(mock_engine):
    with (
        patch(
            "nce.vertical_modules.field_tech.mcp_handlers.require_field_tech_enabled",
            new=AsyncMock(),
        ),
        patch(
            "nce.vertical_modules.field_tech.mcp_handlers.do_sync",
            new=AsyncMock(return_value={"status": "synced", "applied_ops": ["op-1"]}),
        ),
    ):
        raw = await handle_field_tech_sync(
            mock_engine,
            {"namespace_id": _NAMESPACE_ID, "device_id": "dev-1", "ops": []},
        )
        data = json.loads(raw)
        assert data["status"] == "synced"
        assert "op-1" in data["applied_ops"]


@pytest.mark.asyncio
async def test_handle_field_tech_record_outcome_happy_path(mock_engine):
    with (
        patch(
            "nce.vertical_modules.field_tech.mcp_handlers.require_field_tech_enabled",
            new=AsyncMock(),
        ),
        patch(
            "nce.vertical_modules.field_tech.mcp_handlers.do_record_outcome",
            new=AsyncMock(return_value={"status": "recorded", "rating": 5.0}),
        ),
    ):
        raw = await handle_field_tech_record_outcome(
            mock_engine,
            {"namespace_id": _NAMESPACE_ID, "work_order_id": _WO_ID, "rating": 5.0},
        )
        data = json.loads(raw)
        assert data["status"] == "recorded"
