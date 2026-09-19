"""
tests/unit/test_support_on_call_rota.py
=======================================
Wave D-7: Unit test suite for On-Call Rota and active responder routing:
  - Tool registry registration and contract flags (support_get_on_call: cacheable=True, mutation=False, admin_only=False)
  - MCP stdio tool definition and inputSchema
  - Resources engine core: do_get_on_call_allocations (role/demand_kind filter, time window, released status, contractor view)
  - Support engine core: do_get_on_call via engine.modules["resources"] (Charter §8 cross-engine routing)
  - Resources engine disabled handling (EngineDisabledError -> resources_available=False)
  - Support engine disabled guard (require_support_enabled -> SupportDisabledError)
  - MCP tool handler: handle_support_get_on_call
  - Admin REST handler: api_support_on_call (GET /api/support/on-call)
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from nce.admin_handlers import support as support_handlers
from nce.admin_handlers._shared import admin_state
from nce.engine_registry import EngineDisabledError
from nce.mcp_errors import MCP_SCOPE_FORBIDDEN, McpError
from nce.mcp_stdio_tools import TOOLS
from nce.tool_registry import TOOL_REGISTRY
from nce.vertical_modules.resources.allocations import do_get_on_call_allocations
from nce.vertical_modules.support._guard import SupportDisabledError
from nce.vertical_modules.support.mcp_handlers import handle_support_get_on_call
from nce.vertical_modules.support.on_call import do_get_on_call


class MockRequest:
    def __init__(
        self,
        query_params: dict[str, str] | None = None,
        path_params: dict[str, str] | None = None,
        json_body: Any = None,
    ):
        self.query_params = query_params or {}
        self.path_params = path_params or {}
        self._json_body = json_body

    async def json(self) -> Any:
        return self._json_body


@pytest.fixture
def sample_ns_id():
    return str(uuid4())


@pytest.fixture
def mock_engine():
    engine = MagicMock()
    engine.pg_pool = MagicMock()
    return engine


# ---------------------------------------------------------------------------
# 1. Tool Registry & MCP Stdio Tool Schema Tests
# ---------------------------------------------------------------------------


def test_tool_registry_on_call_flags():
    """Verify support_get_on_call is registered with correct contracts."""
    assert "support_get_on_call" in TOOL_REGISTRY
    spec = TOOL_REGISTRY["support_get_on_call"]
    assert spec.cacheable is True
    assert spec.admin_only is False
    assert spec.mutation is False


def test_mcp_stdio_tools_definition():
    """Verify support_get_on_call is in mcp_stdio_tools.TOOLS with valid schema."""
    tools_by_name = {t.name: t for t in TOOLS}
    assert "support_get_on_call" in tools_by_name
    tool = tools_by_name["support_get_on_call"]
    assert tool.inputSchema["required"] == ["namespace_id"]
    props = tool.inputSchema["properties"]
    assert "namespace_id" in props
    assert "at" in props
    assert "starts_at" in props
    assert "ends_at" in props
    assert "include_released" in props
    assert "contractor_view" in props


# ---------------------------------------------------------------------------
# 2. Resources Engine Allocations Core (do_get_on_call_allocations)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_get_on_call_allocations_sql_and_results(sample_ns_id):
    """Verify do_get_on_call_allocations generates query filtering by role=on_call."""
    alloc_id = uuid4()
    resource_id = uuid4()
    mock_row = {
        "id": alloc_id,
        "namespace_id": sample_ns_id,
        "resource_id": resource_id,
        "demand_kind": "on_call",
        "demand_ref_id": None,
        "starts_at": "2026-09-19T08:00:00+00:00",
        "ends_at": "2026-09-19T20:00:00+00:00",
        "status": "active",
        "attrs": '{"role": "on_call", "tier": "primary"}',
        "resource_name": "Dr. Jane Doe",
        "resource_kind": "employee",
        "resource_attrs": '{"billing_rate": 1500, "skills": ["network", "audio"], "email": "jane@example.com", "phone": "+4712345678"}',
    }

    mock_conn = MagicMock()
    mock_conn.fetch = AsyncMock(return_value=[mock_row])
    mock_conn.execute = AsyncMock()

    mock_pool = MagicMock()
    mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)

    mock_engine = MagicMock()
    mock_engine.pg_pool = mock_pool

    params = {
        "namespace_id": sample_ns_id,
        "at": "2026-09-19T12:00:00Z",
    }
    result = await do_get_on_call_allocations(mock_engine, params)

    assert result["count"] == 1
    assert len(result["on_call"]) == 1
    item = result["on_call"][0]
    assert item["id"] == str(alloc_id)
    assert item["resource_name"] == "Dr. Jane Doe"
    assert item["resource_kind"] == "employee"
    assert item["resource_attrs"]["email"] == "jane@example.com"
    assert item["resource_attrs"]["phone"] == "+4712345678"
    assert item["attrs"]["role"] == "on_call"

    # Verify query SQL checked role/demand_kind
    call_args = mock_conn.fetch.call_args
    sql = call_args[0][0]
    assert "(a.attrs->>'role' = 'on_call' OR a.demand_kind = 'on_call')" in sql
    assert "a.status <> 'released'" in sql


@pytest.mark.asyncio
async def test_do_get_on_call_allocations_contractor_view_redaction(sample_ns_id):
    """Verify contractor_view=True redacts sensitive internal rates/costs."""
    alloc_id = uuid4()
    resource_id = uuid4()
    mock_row = {
        "id": alloc_id,
        "namespace_id": sample_ns_id,
        "resource_id": resource_id,
        "demand_kind": "on_call",
        "demand_ref_id": None,
        "starts_at": "2026-09-19T08:00:00+00:00",
        "ends_at": "2026-09-19T20:00:00+00:00",
        "status": "active",
        "attrs": '{"role": "on_call", "hourly_cost": 950}',
        "resource_name": "Contractor John",
        "resource_email": "john@partner.com",
        "resource_phone": "+4798765432",
        "resource_type": "contractor",
        "resource_attrs": '{"internal_rate": 800, "margin": 0.25, "skills": ["cctv"]}',
    }

    mock_conn = MagicMock()
    mock_conn.fetch = AsyncMock(return_value=[mock_row])
    mock_conn.execute = AsyncMock()

    mock_pool = MagicMock()
    mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)

    mock_engine = MagicMock()
    mock_engine.pg_pool = mock_pool

    params = {
        "namespace_id": sample_ns_id,
        "contractor_view": True,
    }
    result = await do_get_on_call_allocations(mock_engine, params)
    item = result["on_call"][0]

    assert item["resource_name"] == "Contractor John"
    assert item.get("redaction") == "contractor_allow_list_enforced"
    assert "attrs" not in item
    assert "resource_attrs" not in item


# ---------------------------------------------------------------------------
# 3. Support Engine Core (do_get_on_call) & Charter §8 Cross-Engine Flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_get_on_call_cross_engine_modules(sample_ns_id):
    """Verify do_get_on_call dispatches via engine.modules['resources']."""
    mock_resources_module = MagicMock()
    mock_resources_module.do_get_on_call_allocations = AsyncMock(
        return_value={
            "namespace_id": sample_ns_id,
            "on_call": [{"id": str(uuid4()), "resource_name": "Alice"}],
            "count": 1,
            "query_time": "2026-09-19T12:00:00+00:00",
        }
    )

    mock_engine = MagicMock()
    mock_engine.modules = {"resources": mock_resources_module}

    params = {"namespace_id": sample_ns_id}
    res = await do_get_on_call(mock_engine, params)

    assert res["count"] == 1
    assert res["resources_available"] is True
    assert res["on_call"][0]["resource_name"] == "Alice"
    mock_resources_module.do_get_on_call_allocations.assert_awaited_once()


@pytest.mark.asyncio
async def test_do_get_on_call_resources_disabled_graceful(sample_ns_id):
    """When Resources Engine is disabled for namespace, returns empty list with resources_available=False."""
    mock_modules = MagicMock()
    mock_modules.for_namespace = None
    mock_modules.__getitem__.side_effect = EngineDisabledError("resources", sample_ns_id)

    mock_engine = MagicMock()
    mock_engine.modules = mock_modules

    params = {"namespace_id": sample_ns_id}
    res = await do_get_on_call(mock_engine, params)

    assert res["count"] == 0
    assert res["on_call"] == []
    assert res["resources_available"] is False


@pytest.mark.asyncio
async def test_do_get_on_call_fallback_hermetic(sample_ns_id):
    """When engine.modules is absent, hermetic fallback calls allocations core directly."""
    mock_engine = MagicMock()
    mock_engine.modules = None

    with patch(
        "nce.vertical_modules.resources.allocations.do_get_on_call_allocations",
        new_callable=AsyncMock,
        return_value={"namespace_id": sample_ns_id, "on_call": [], "count": 0},
    ) as mock_alloc:
        res = await do_get_on_call(mock_engine, {"namespace_id": sample_ns_id})
        assert res["resources_available"] is True
        mock_alloc.assert_awaited_once()


@pytest.mark.asyncio
async def test_do_get_on_call_invalid_inputs(sample_ns_id):
    """Verify ValueError on invalid UUID or timestamp."""
    with pytest.raises(ValueError, match="namespace_id is required"):
        await do_get_on_call(MagicMock(), {})

    with pytest.raises(ValueError, match="Invalid namespace_id UUID"):
        await do_get_on_call(MagicMock(), {"namespace_id": "not-a-uuid"})

    with pytest.raises(ValueError, match="Invalid 'at' ISO datetime"):
        await do_get_on_call(
            MagicMock(), {"namespace_id": sample_ns_id, "at": "not-a-valid-datetime"}
        )


# ---------------------------------------------------------------------------
# 4. MCP Handler (handle_support_get_on_call)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_support_get_on_call_success(mock_engine, sample_ns_id):
    """Verify handle_support_get_on_call formats valid MCP response JSON."""
    expected = {
        "namespace_id": sample_ns_id,
        "on_call": [{"id": str(uuid4()), "resource_name": "Bob"}],
        "count": 1,
        "query_time": "2026-09-19T12:00:00+00:00",
        "resources_available": True,
    }

    with (
        patch(
            "nce.vertical_modules.support.mcp_handlers._check_support_enabled",
            new_callable=AsyncMock,
            return_value=sample_ns_id,
        ),
        patch(
            "nce.vertical_modules.support.mcp_handlers.do_get_on_call",
            new_callable=AsyncMock,
            return_value=expected,
        ) as mock_core,
    ):
        raw = await handle_support_get_on_call(mock_engine, {"namespace_id": sample_ns_id})
        data = json.loads(raw)
        assert data["ok"] is True
        assert data["count"] == 1
        assert data["on_call"][0]["resource_name"] == "Bob"
        assert data["resources_available"] is True
        mock_core.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_support_get_on_call_disabled(mock_engine, sample_ns_id):
    """Verify disabled support raises McpError(MCP_SCOPE_FORBIDDEN)."""
    with patch(
        "nce.vertical_modules.support.mcp_handlers._check_support_enabled",
        side_effect=McpError(MCP_SCOPE_FORBIDDEN, "Support disabled"),
    ):
        with pytest.raises(McpError) as exc_info:
            await handle_support_get_on_call(mock_engine, {"namespace_id": sample_ns_id})
        assert exc_info.value.code == MCP_SCOPE_FORBIDDEN


@pytest.mark.asyncio
async def test_handle_support_get_on_call_invalid_params(mock_engine, sample_ns_id):
    """Verify invalid parameters raise McpError(-32602)."""
    with (
        patch(
            "nce.vertical_modules.support.mcp_handlers._check_support_enabled",
            new_callable=AsyncMock,
            return_value=sample_ns_id,
        ),
        patch(
            "nce.vertical_modules.support.mcp_handlers.do_get_on_call",
            side_effect=ValueError("Invalid 'at' ISO datetime"),
        ),
    ):
        with pytest.raises(McpError) as exc_info:
            await handle_support_get_on_call(
                mock_engine, {"namespace_id": sample_ns_id, "at": "invalid"}
            )
        assert exc_info.value.code == -32602


# ---------------------------------------------------------------------------
# 5. Admin REST Handler (api_support_on_call)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rest_api_support_on_call_success(mock_engine, sample_ns_id):
    """GET /api/support/on-call returns 200 with on-call records."""
    admin_state.engine = mock_engine
    expected_result = {
        "namespace_id": sample_ns_id,
        "on_call": [{"resource_name": "Charlie"}],
        "count": 1,
        "query_time": "2026-09-19T12:00:00+00:00",
        "resources_available": True,
    }
    req = MockRequest(
        query_params={
            "namespace_id": sample_ns_id,
            "at": "2026-09-19T12:00:00Z",
            "contractor_view": "true",
        }
    )

    with (
        patch(
            "nce.admin_handlers.support.require_support_enabled",
            new_callable=AsyncMock,
        ),
        patch(
            "nce.admin_handlers.support.do_get_on_call",
            new_callable=AsyncMock,
            return_value=expected_result,
        ) as mock_core,
    ):
        res = await support_handlers.api_support_on_call(req)
        assert res.status_code == 200
        body = json.loads(res.body.decode("utf-8"))
        assert body["ok"] is True
        assert body["count"] == 1
        assert body["on_call"][0]["resource_name"] == "Charlie"
        mock_core.assert_awaited_once()


@pytest.mark.asyncio
async def test_rest_api_support_on_call_missing_namespace(mock_engine):
    """GET /api/support/on-call without namespace_id returns 422."""
    admin_state.engine = mock_engine
    req = MockRequest(query_params={})
    res = await support_handlers.api_support_on_call(req)
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_rest_api_support_on_call_disabled(mock_engine, sample_ns_id):
    """GET /api/support/on-call with support disabled returns 409."""
    admin_state.engine = mock_engine
    req = MockRequest(query_params={"namespace_id": sample_ns_id})
    with patch(
        "nce.admin_handlers.support.require_support_enabled",
        side_effect=SupportDisabledError(sample_ns_id),
    ):
        res = await support_handlers.api_support_on_call(req)
        assert res.status_code == 409
        body = json.loads(res.body.decode("utf-8"))
        assert body["reason"] == "support_disabled"


@pytest.mark.asyncio
async def test_rest_api_support_on_call_engine_not_connected():
    """GET /api/support/on-call returns 503 when admin_state.engine is None."""
    admin_state.engine = None
    req = MockRequest(query_params={"namespace_id": str(uuid4())})
    res = await support_handlers.api_support_on_call(req)
    assert res.status_code == 503
