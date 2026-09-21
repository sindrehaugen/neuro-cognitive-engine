"""Hermetic unit tests for Project Engine REST reads promoted to cacheable tools (Wave PJ-4).

Verifies:
1. Tool registration in TOOL_REGISTRY with correct flags:
   cacheable=True, admin_only=False, mutation=False for:
   - project_my_day
   - project_capacity
   - project_detect_scope_creep
   project_status_report is covered separately below: 2026-09-21 found it
   writes (DELETE + INSERT kg_nodes) and was mis-declared mutation=False;
   now cacheable=False, mutation=True (FILED_mutation_false_writers_
   2026-09-21.md).
2. MCP Stdio tool declarations in TOOLS with matching input schemas.
3. Handler invocation, JSON serialization, and error contract handling.
4. Input validation (missing namespace_id, missing project_id, invalid params).
5. Parity with admin REST routes in admin_app / admin_handlers.
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nce.mcp_errors import McpError
from nce.mcp_stdio_tools import TOOLS
from nce.tool_registry import CACHEABLE_TOOLS, TOOL_REGISTRY
from nce.vertical_modules.project import mcp_handlers as project_mcp_handlers


class TestProjectRestReadsSurface:
    """Verification of Wave PJ-4 promoted cacheable tools."""

    @pytest.mark.parametrize(
        "tool_name",
        [
            "project_my_day",
            "project_capacity",
            "project_detect_scope_creep",
        ],
    )
    def test_tool_registry_flags(self, tool_name: str) -> None:
        assert tool_name in TOOL_REGISTRY, f"{tool_name} not registered in TOOL_REGISTRY"
        spec = TOOL_REGISTRY[tool_name]
        assert spec.cacheable is True, f"{tool_name} must be cacheable=True"
        assert spec.admin_only is False, f"{tool_name} must be admin_only=False"
        assert spec.mutation is False, f"{tool_name} must be mutation=False"
        assert tool_name in CACHEABLE_TOOLS, f"{tool_name} must be in CACHEABLE_TOOLS set"

    def test_project_status_report_tool_registry_flags(self) -> None:
        """project_status_report writes (DELETE + INSERT kg_nodes) and was
        mis-declared mutation=False; fixed 2026-09-21. cacheable=False
        follows -- mutation=True makes a cache write unreachable (see
        FILED_mutation_false_writers_2026-09-21.md)."""
        assert "project_status_report" in TOOL_REGISTRY
        spec = TOOL_REGISTRY["project_status_report"]
        assert spec.cacheable is False
        assert spec.admin_only is False
        assert spec.mutation is True
        assert "project_status_report" not in CACHEABLE_TOOLS

    @pytest.mark.parametrize(
        "tool_name",
        [
            "project_my_day",
            "project_capacity",
            "project_detect_scope_creep",
            "project_status_report",
        ],
    )
    def test_mcp_stdio_tool_schema_exists(self, tool_name: str) -> None:
        tool_def = next((t for t in TOOLS if t.name == tool_name), None)
        assert tool_def is not None, f"{tool_name} not found in TOOLS"
        schema = tool_def.inputSchema
        assert schema.get("type") == "object"
        properties = schema.get("properties", {})
        required = schema.get("required", [])

        assert "namespace_id" in properties
        assert "namespace_id" in required

        if tool_name in ("project_detect_scope_creep", "project_status_report"):
            assert "project_id" in properties
            assert "project_id" in required

        if tool_name == "project_my_day":
            assert "employee_id" in properties
            assert "reference_date" in properties

        if tool_name == "project_capacity":
            assert "start_date" in properties
            assert "end_date" in properties
            assert "window" in properties

        if tool_name == "project_status_report":
            assert "estimated_cost_nok" in properties
            assert "estimated_revenue_nok" in properties

    @pytest.mark.asyncio
    async def test_handle_project_my_day_success(self) -> None:
        engine = MagicMock()
        ns_id = str(uuid.uuid4())
        expected_result = {"ok": True, "tasks": [{"task_label": "TASK:1", "priority": 15.0}]}

        with patch(
            "nce.vertical_modules.project.mcp_handlers.do_my_day",
            new_callable=AsyncMock,
            return_value=expected_result,
        ) as mock_core:
            args = {"namespace_id": ns_id, "employee_id": "EMP:001", "reference_date": "2026-09-16"}
            res_str = await project_mcp_handlers.handle_project_my_day(engine, args)
            res = json.loads(res_str)
            assert res == expected_result
            mock_core.assert_awaited_once_with(engine, args)

    @pytest.mark.asyncio
    async def test_handle_project_my_day_missing_namespace(self) -> None:
        engine = MagicMock()
        with pytest.raises(McpError) as exc_info:
            await project_mcp_handlers.handle_project_my_day(engine, {"employee_id": "EMP:001"})
        assert exc_info.value.code == -32602
        assert exc_info.value.message == "Invalid parameters"

    @pytest.mark.asyncio
    async def test_handle_project_capacity_success(self) -> None:
        engine = MagicMock()
        ns_id = str(uuid.uuid4())
        expected_result = {"ok": True, "teams": {"Team Alpha": 4.5}}

        with patch(
            "nce.vertical_modules.project.mcp_handlers.do_capacity",
            new_callable=AsyncMock,
            return_value=expected_result,
        ) as mock_core:
            args = {"namespace_id": ns_id, "start_date": "2026-09-01", "end_date": "2026-09-30"}
            res_str = await project_mcp_handlers.handle_project_capacity(engine, args)
            res = json.loads(res_str)
            assert res == expected_result
            mock_core.assert_awaited_once_with(engine, args)

    @pytest.mark.asyncio
    async def test_handle_project_capacity_missing_namespace(self) -> None:
        engine = MagicMock()
        with pytest.raises(McpError) as exc_info:
            await project_mcp_handlers.handle_project_capacity(engine, {"start_date": "2026-09-01"})
        assert exc_info.value.code == -32602
        assert exc_info.value.message == "Invalid parameters"

    @pytest.mark.asyncio
    async def test_handle_project_detect_scope_creep_success(self) -> None:
        engine = MagicMock()
        ns_id = str(uuid.uuid4())
        expected_result = {
            "ok": True,
            "change_orders": [{"label": "CO:1", "value": 12000.0}],
            "delta_signed_vs_current": 12000.0,
            "signed_total_nok": 50000.0,
            "current_total_nok": 62000.0,
            "sales_available": True,
        }

        with patch(
            "nce.vertical_modules.project.mcp_handlers.do_detect_scope_creep",
            new_callable=AsyncMock,
            return_value=expected_result,
        ) as mock_core:
            args = {"namespace_id": ns_id, "project_id": "PROJECT:Q100"}
            res_str = await project_mcp_handlers.handle_project_detect_scope_creep(engine, args)
            res = json.loads(res_str)
            assert res == expected_result
            mock_core.assert_awaited_once_with(engine, args)

    @pytest.mark.asyncio
    async def test_handle_project_detect_scope_creep_missing_project_id(self) -> None:
        engine = MagicMock()
        ns_id = str(uuid.uuid4())
        with pytest.raises(McpError) as exc_info:
            await project_mcp_handlers.handle_project_detect_scope_creep(
                engine, {"namespace_id": ns_id}
            )
        assert exc_info.value.code == -32602
        assert exc_info.value.message == "Invalid parameters"

    @pytest.mark.asyncio
    async def test_handle_project_status_report_success(self) -> None:
        engine = MagicMock()
        ns_id = str(uuid.uuid4())
        expected_result = {
            "ok": True,
            "project_id": "PROJECT:Q100",
            "narrative": "Project progressing on track.",
            "margin_trinity": {"sold": 0.25, "current": 0.22, "forecast": 0.23},
        }

        with patch(
            "nce.vertical_modules.project.mcp_handlers.do_status_report",
            new_callable=AsyncMock,
            return_value=expected_result,
        ) as mock_core:
            args = {
                "namespace_id": ns_id,
                "project_id": "PROJECT:Q100",
                "estimated_cost_nok": 45000.0,
                "estimated_revenue_nok": 60000.0,
            }
            res_str = await project_mcp_handlers.handle_project_status_report(engine, args)
            res = json.loads(res_str)
            assert res == expected_result
            mock_core.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_handle_project_status_report_invalid_float(self) -> None:
        engine = MagicMock()
        ns_id = str(uuid.uuid4())
        with pytest.raises(McpError) as exc_info:
            await project_mcp_handlers.handle_project_status_report(
                engine,
                {
                    "namespace_id": ns_id,
                    "project_id": "PROJECT:Q100",
                    "estimated_cost_nok": "not-a-float",
                },
            )
        assert exc_info.value.code == -32602
        assert exc_info.value.message == "Invalid parameters"

    @pytest.mark.asyncio
    async def test_handle_project_status_report_missing_project_id(self) -> None:
        engine = MagicMock()
        ns_id = str(uuid.uuid4())
        with pytest.raises(McpError) as exc_info:
            await project_mcp_handlers.handle_project_status_report(engine, {"namespace_id": ns_id})
        assert exc_info.value.code == -32602
        assert exc_info.value.message == "Invalid parameters"

    def test_rest_routes_exist_in_admin_app(self) -> None:
        from nce.admin_app import build_admin_routes

        routes = build_admin_routes()
        paths = {r.path for r in routes if hasattr(r, "path")}

        assert "/api/project/my-day" in paths
        assert "/api/project/capacity" in paths
        assert "/api/project/{id}/scope-creep" in paths
        assert "/api/project/{id}/status-report" in paths
