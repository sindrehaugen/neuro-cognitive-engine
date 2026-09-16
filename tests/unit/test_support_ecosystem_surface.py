"""
tests/unit/test_support_ecosystem_surface.py
============================================
Wave SU-3: Unit test suite for Support vertical module ecosystem surface completion:
  - MCP tools: support_failure_pattern, support_upsell_signal, support_at_risk_aggregate
  - Tool registry flags (mutation, admin_only, cacheable)
  - Stdio Tool schemas in mcp_stdio_tools.py
  - REST endpoints:
      POST /api/support/tickets/{id}/failure-pattern (api_support_tickets_failure_pattern)
      POST /api/support/tickets/{id}/upsell-signal (api_support_tickets_upsell_signal)
      GET  /api/support/at-risk-aggregate (api_support_at_risk_aggregate)
  - Cache invalidation via bump_mcp_cache_generation on mutating routes
  - Refusals and status mapping (404, 409, 422, 503)
  - Pruning of internal-cores.json allowlist
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from nce.admin_handlers import support as support_handlers
from nce.admin_handlers._shared import admin_state
from nce.mcp_errors import MCP_SCOPE_FORBIDDEN, McpError
from nce.mcp_stdio_tools import TOOLS
from nce.tool_registry import TOOL_REGISTRY
from nce.vertical_modules.support._guard import SupportDisabledError
from nce.vertical_modules.support.mcp_handlers import (
    handle_support_at_risk_aggregate,
    handle_support_failure_pattern,
    handle_support_upsell_signal,
)
from nce.vertical_modules.support.tickets import TicketNotFoundError

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_INTERNAL_CORES_PATH = _REPO_ROOT / "nce" / "config_data" / "internal-cores.json"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_engine():
    engine = MagicMock()
    engine.pg_pool = MagicMock()
    return engine


@pytest.fixture
def sample_ns_id():
    return str(uuid4())


@pytest.fixture
def sample_ticket_id():
    return str(uuid4())


# ---------------------------------------------------------------------------
# 1. Tool Registry & Schema Declarations (Wave SU-3)
# ---------------------------------------------------------------------------


def test_tool_registry_registration_and_flags():
    """Verify all 3 tools are registered in TOOL_REGISTRY with correct contracts."""
    assert "support_failure_pattern" in TOOL_REGISTRY
    spec_fp = TOOL_REGISTRY["support_failure_pattern"]
    assert spec_fp.mutation is True
    assert spec_fp.admin_only is True
    assert spec_fp.cacheable is False

    assert "support_upsell_signal" in TOOL_REGISTRY
    spec_us = TOOL_REGISTRY["support_upsell_signal"]
    assert spec_us.mutation is True
    assert spec_us.admin_only is True
    assert spec_us.cacheable is False

    assert "support_at_risk_aggregate" in TOOL_REGISTRY
    spec_ara = TOOL_REGISTRY["support_at_risk_aggregate"]
    assert spec_ara.mutation is False
    assert spec_ara.admin_only is False
    assert spec_ara.cacheable is True


def test_mcp_stdio_tools_definitions():
    """Verify schemas are defined with required attributes in mcp_stdio_tools.TOOLS."""
    tools_by_name = {t.name: t for t in TOOLS}

    # 1. support_failure_pattern
    assert "support_failure_pattern" in tools_by_name
    t_fp = tools_by_name["support_failure_pattern"]
    assert set(t_fp.inputSchema["required"]) == {"namespace_id", "ticket_id", "product_sku"}
    assert "product_sku" in t_fp.inputSchema["properties"]

    # 2. support_upsell_signal
    assert "support_upsell_signal" in tools_by_name
    t_us = tools_by_name["support_upsell_signal"]
    assert set(t_us.inputSchema["required"]) == {"namespace_id", "ticket_id", "target_id"}
    assert "target_type" in t_us.inputSchema["properties"]

    # 3. support_at_risk_aggregate
    assert "support_at_risk_aggregate" in tools_by_name
    t_ara = tools_by_name["support_at_risk_aggregate"]
    assert set(t_ara.inputSchema["required"]) == {"namespace_id"}
    assert "lookback_days" in t_ara.inputSchema["properties"]


def test_internal_cores_allowlist_pruned():
    """Verify the 3 surfaced cores are no longer in internal-cores.json."""
    with open(_INTERNAL_CORES_PATH, encoding="utf-8") as f:
        cores = json.load(f)

    assert "nce/vertical_modules/support/ecosystem.py::do_record_failure_pattern" not in cores
    assert "nce/vertical_modules/support/ecosystem.py::do_record_upsell_signal" not in cores
    assert "nce/vertical_modules/support/ecosystem.py::do_support_at_risk_aggregate" not in cores


# ---------------------------------------------------------------------------
# 2. MCP Handlers Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_support_failure_pattern_success(mock_engine, sample_ns_id, sample_ticket_id):
    expected_result = {
        "ok": True,
        "ticket_id": sample_ticket_id,
        "product_sku": "SKU-TEST-01",
        "edge": f"TICKET:{sample_ticket_id} -[failure_pattern]-> PRODUCT_SKU:SKU-TEST-01",
    }
    with (
        patch(
            "nce.vertical_modules.support.mcp_handlers.require_support_enabled",
            new_callable=AsyncMock,
        ),
        patch(
            "nce.vertical_modules.support.mcp_handlers.do_record_failure_pattern",
            new_callable=AsyncMock,
            return_value=expected_result,
        ) as mock_core,
    ):
        raw = await handle_support_failure_pattern(
            mock_engine,
            {
                "namespace_id": sample_ns_id,
                "ticket_id": sample_ticket_id,
                "product_sku": "SKU-TEST-01",
                "confidence": 0.95,
            },
        )
        data = json.loads(raw)
        assert data["ok"] is True
        assert data["product_sku"] == "SKU-TEST-01"
        mock_core.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_support_failure_pattern_ticket_not_found(
    mock_engine, sample_ns_id, sample_ticket_id
):
    with (
        patch(
            "nce.vertical_modules.support.mcp_handlers.require_support_enabled",
            new_callable=AsyncMock,
        ),
        patch(
            "nce.vertical_modules.support.mcp_handlers.do_record_failure_pattern",
            side_effect=TicketNotFoundError(ticket_id=sample_ticket_id),
        ),
    ):
        with pytest.raises(McpError) as exc_info:
            await handle_support_failure_pattern(
                mock_engine,
                {
                    "namespace_id": sample_ns_id,
                    "ticket_id": sample_ticket_id,
                    "product_sku": "SKU-TEST-01",
                },
            )
        assert exc_info.value.code == MCP_SCOPE_FORBIDDEN
        assert exc_info.value.data.get("reason") == "ticket_not_found"


@pytest.mark.asyncio
async def test_handle_support_failure_pattern_disabled(mock_engine, sample_ns_id, sample_ticket_id):
    with patch(
        "nce.vertical_modules.support.mcp_handlers.require_support_enabled",
        side_effect=SupportDisabledError(sample_ns_id),
    ):
        with pytest.raises(McpError) as exc_info:
            await handle_support_failure_pattern(
                mock_engine,
                {
                    "namespace_id": sample_ns_id,
                    "ticket_id": sample_ticket_id,
                    "product_sku": "SKU-TEST-01",
                },
            )
        assert exc_info.value.code == MCP_SCOPE_FORBIDDEN
        assert exc_info.value.data.get("reason") == "support_disabled"


@pytest.mark.asyncio
async def test_handle_support_upsell_signal_success(mock_engine, sample_ns_id, sample_ticket_id):
    target_id = str(uuid4())
    expected_result = {
        "ok": True,
        "ticket_id": sample_ticket_id,
        "target": f"OPPORTUNITY:{target_id}",
        "edge": f"TICKET:{sample_ticket_id} -[upsell_opportunity]-> OPPORTUNITY:{target_id}",
    }
    with (
        patch(
            "nce.vertical_modules.support.mcp_handlers.require_support_enabled",
            new_callable=AsyncMock,
        ),
        patch(
            "nce.vertical_modules.support.mcp_handlers.do_record_upsell_signal",
            new_callable=AsyncMock,
            return_value=expected_result,
        ) as mock_core,
    ):
        raw = await handle_support_upsell_signal(
            mock_engine,
            {
                "namespace_id": sample_ns_id,
                "ticket_id": sample_ticket_id,
                "target_type": "OPPORTUNITY",
                "target_id": target_id,
            },
        )
        data = json.loads(raw)
        assert data["ok"] is True
        assert data["target"] == f"OPPORTUNITY:{target_id}"
        mock_core.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_support_upsell_signal_validation_error(
    mock_engine, sample_ns_id, sample_ticket_id
):
    with (
        patch(
            "nce.vertical_modules.support.mcp_handlers.require_support_enabled",
            new_callable=AsyncMock,
        ),
        patch(
            "nce.vertical_modules.support.mcp_handlers.do_record_upsell_signal",
            side_effect=ValueError("target_id is required"),
        ),
    ):
        with pytest.raises(McpError) as exc_info:
            await handle_support_upsell_signal(
                mock_engine,
                {
                    "namespace_id": sample_ns_id,
                    "ticket_id": sample_ticket_id,
                    "target_id": "",
                },
            )
        assert exc_info.value.code == -32602


@pytest.mark.asyncio
async def test_handle_support_at_risk_aggregate_success(mock_engine, sample_ns_id):
    expected_result = {
        "ok": True,
        "namespace_id": sample_ns_id,
        "operations_slice": {
            "sla_at_risk_count": 1,
            "churn_risk_count": 0,
            "proactive_tickets_count": 2,
        },
    }
    with (
        patch(
            "nce.vertical_modules.support.mcp_handlers.require_support_enabled",
            new_callable=AsyncMock,
        ),
        patch(
            "nce.vertical_modules.support.mcp_handlers.do_support_at_risk_aggregate",
            new_callable=AsyncMock,
            return_value=expected_result,
        ) as mock_core,
    ):
        raw = await handle_support_at_risk_aggregate(
            mock_engine,
            {"namespace_id": sample_ns_id, "lookback_days": 7},
        )
        data = json.loads(raw)
        assert data["ok"] is True
        assert data["operations_slice"]["proactive_tickets_count"] == 2
        mock_core.assert_awaited_once()


# ---------------------------------------------------------------------------
# 3. REST Handlers Tests
# ---------------------------------------------------------------------------


class MockRequest:
    def __init__(
        self,
        json_body: dict[str, Any] | None = None,
        query_params: dict[str, Any] | None = None,
        path_params: dict[str, Any] | None = None,
    ):
        self._json = json_body or {}
        self.query_params = query_params or {}
        self.path_params = path_params or {}

    async def json(self):
        return self._json


@pytest.mark.asyncio
async def test_rest_failure_pattern_success(mock_engine, sample_ns_id, sample_ticket_id):
    admin_state.engine = mock_engine
    expected_result = {
        "ok": True,
        "ticket_id": sample_ticket_id,
        "product_sku": "SKU-001",
        "edge": f"TICKET:{sample_ticket_id} -[failure_pattern]-> PRODUCT_SKU:SKU-001",
    }
    req = MockRequest(
        json_body={
            "namespace_id": sample_ns_id,
            "product_sku": "SKU-001",
            "confidence": 0.9,
        },
        path_params={"id": sample_ticket_id},
    )

    with (
        patch(
            "nce.admin_handlers.support.require_support_enabled",
            new_callable=AsyncMock,
        ),
        patch(
            "nce.admin_handlers.support.do_record_failure_pattern",
            new_callable=AsyncMock,
            return_value=expected_result,
        ) as mock_core,
        patch(
            "nce.admin_handlers.support.bump_mcp_cache_generation",
            new_callable=AsyncMock,
        ) as mock_bump,
    ):
        res = await support_handlers.api_support_tickets_failure_pattern(req)
        assert res.status_code == 200
        body = json.loads(res.body.decode("utf-8"))
        assert body["ok"] is True
        assert body["product_sku"] == "SKU-001"
        mock_core.assert_awaited_once()
        mock_bump.assert_awaited_once_with(mock_engine, route="api_support_tickets_failure_pattern")


@pytest.mark.asyncio
async def test_rest_failure_pattern_missing_sku(mock_engine, sample_ns_id, sample_ticket_id):
    admin_state.engine = mock_engine
    req = MockRequest(
        json_body={"namespace_id": sample_ns_id, "product_sku": ""},
        path_params={"id": sample_ticket_id},
    )
    with patch(
        "nce.admin_handlers.support.require_support_enabled",
        new_callable=AsyncMock,
    ):
        res = await support_handlers.api_support_tickets_failure_pattern(req)
        assert res.status_code == 422


@pytest.mark.asyncio
async def test_rest_upsell_signal_success(mock_engine, sample_ns_id, sample_ticket_id):
    admin_state.engine = mock_engine
    target_id = str(uuid4())
    expected_result = {
        "ok": True,
        "ticket_id": sample_ticket_id,
        "target": f"QUOTE:{target_id}",
        "edge": f"TICKET:{sample_ticket_id} -[upsell_opportunity]-> QUOTE:{target_id}",
    }
    req = MockRequest(
        json_body={
            "namespace_id": sample_ns_id,
            "target_type": "QUOTE",
            "target_id": target_id,
            "signal_reason": "Customer needs expansion chassis.",
        },
        path_params={"id": sample_ticket_id},
    )

    with (
        patch(
            "nce.admin_handlers.support.require_support_enabled",
            new_callable=AsyncMock,
        ),
        patch(
            "nce.admin_handlers.support.do_record_upsell_signal",
            new_callable=AsyncMock,
            return_value=expected_result,
        ) as mock_core,
        patch(
            "nce.admin_handlers.support.bump_mcp_cache_generation",
            new_callable=AsyncMock,
        ) as mock_bump,
    ):
        res = await support_handlers.api_support_tickets_upsell_signal(req)
        assert res.status_code == 200
        body = json.loads(res.body.decode("utf-8"))
        assert body["ok"] is True
        assert body["target"] == f"QUOTE:{target_id}"
        mock_core.assert_awaited_once()
        mock_bump.assert_awaited_once_with(mock_engine, route="api_support_tickets_upsell_signal")


@pytest.mark.asyncio
async def test_rest_at_risk_aggregate_success(mock_engine, sample_ns_id):
    admin_state.engine = mock_engine
    expected_result = {
        "ok": True,
        "namespace_id": sample_ns_id,
        "operations_slice": {
            "sla_at_risk_count": 0,
            "churn_risk_count": 1,
            "proactive_tickets_count": 0,
        },
    }
    req = MockRequest(query_params={"namespace_id": sample_ns_id, "lookback_days": "14"})

    with (
        patch(
            "nce.admin_handlers.support.require_support_enabled",
            new_callable=AsyncMock,
        ),
        patch(
            "nce.admin_handlers.support.do_support_at_risk_aggregate",
            new_callable=AsyncMock,
            return_value=expected_result,
        ) as mock_core,
    ):
        res = await support_handlers.api_support_at_risk_aggregate(req)
        assert res.status_code == 200
        body = json.loads(res.body.decode("utf-8"))
        assert body["ok"] is True
        assert body["operations_slice"]["churn_risk_count"] == 1
        mock_core.assert_awaited_once()


@pytest.mark.asyncio
async def test_rest_engine_not_connected():
    admin_state.engine = None
    req = MockRequest()
    res1 = await support_handlers.api_support_tickets_failure_pattern(req)
    assert res1.status_code == 503
    res2 = await support_handlers.api_support_tickets_upsell_signal(req)
    assert res2.status_code == 503
    res3 = await support_handlers.api_support_at_risk_aggregate(req)
    assert res3.status_code == 503
