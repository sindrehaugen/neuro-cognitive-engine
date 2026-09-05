"""
tests/unit/test_support_surface.py
==================================
Unit tests for the Module 10 (Support Engine) MCP surface:
  - Tool registry registration and exact flags for all 6 tools
  - Opt-in guard enforcement (_check_support_enabled -> McpError(-32005))
  - Parameter validation and error wrapping (McpError(-32602))
  - Happy-path execution for all 6 MCP handlers
  - Business refusal mapping (TicketNotFoundError, TicketAlreadyResolvedError, InvalidTicketStatusError)

Pure unit tests — no live database or Redis required.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from nce.mcp_errors import MCP_SCOPE_FORBIDDEN, McpError
from nce.tool_registry import (
    ADMIN_ONLY_TOOLS,
    CACHEABLE_TOOLS,
    MUTATION_TOOLS,
    TOOL_REGISTRY,
)
from nce.vertical_modules.support._guard import SupportDisabledError
from nce.vertical_modules.support.mcp_handlers import (
    handle_support_health_score,
    handle_support_open_ticket,
    handle_support_query_ticket,
    handle_support_record_touchpoint,
    handle_support_resolve_ticket,
    handle_support_sla_clock,
    handle_support_triage_ticket,
    handle_support_troubleshoot,
)
from nce.vertical_modules.support.tickets import (
    InvalidTicketStatusError,
    TicketAlreadyResolvedError,
    TicketNotFoundError,
)

_NAMESPACE_ID = "00000000-0000-4000-8000-000000000001"
_TICKET_ID = str(uuid4())


@pytest.fixture
def mock_engine():
    engine = MagicMock()
    engine.pg_pool = MagicMock()
    return engine


# ---------------------------------------------------------------------------
# 1. Tool Registry & Flag Invariants
# ---------------------------------------------------------------------------


def test_all_support_tools_registered():
    expected_tools = {
        "support_query_ticket",
        "support_open_ticket",
        "support_sla_clock",
        "support_health_score",
        "support_troubleshoot",
        "support_resolve_ticket",
        "support_triage_ticket",
        "support_record_touchpoint",
    }
    for tool_name in expected_tools:
        assert tool_name in TOOL_REGISTRY, f"Tool {tool_name} missing from TOOL_REGISTRY"


def test_support_tools_flags():
    expected_specs = {
        "support_query_ticket": {
            "cacheable": True,
            "admin_only": False,
            "mutation": False,
        },
        "support_open_ticket": {
            "cacheable": False,
            "admin_only": True,
            "mutation": True,
        },
        "support_sla_clock": {
            "cacheable": True,
            "admin_only": False,
            "mutation": False,
        },
        "support_health_score": {
            "cacheable": True,
            "admin_only": False,
            "mutation": False,
        },
        "support_troubleshoot": {
            "cacheable": True,
            "admin_only": False,
            "mutation": False,
        },
        "support_resolve_ticket": {
            "cacheable": False,
            "admin_only": True,
            "mutation": True,
        },
        "support_triage_ticket": {
            "cacheable": True,
            "admin_only": False,
            "mutation": False,
        },
        "support_record_touchpoint": {
            "cacheable": False,
            "admin_only": False,
            "mutation": True,
        },
    }

    for tool_name, expected in expected_specs.items():
        spec = TOOL_REGISTRY[tool_name]
        assert spec.cacheable == expected["cacheable"], (
            f"{tool_name} cacheable: expected {expected['cacheable']}, got {spec.cacheable}"
        )
        assert spec.admin_only == expected["admin_only"], (
            f"{tool_name} admin_only: expected {expected['admin_only']}, got {spec.admin_only}"
        )
        assert spec.mutation == expected["mutation"], (
            f"{tool_name} mutation: expected {expected['mutation']}, got {spec.mutation}"
        )

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
# 2. Opt-In Gate Enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "handler, args",
    [
        (handle_support_query_ticket, {"namespace_id": _NAMESPACE_ID}),
        (
            handle_support_open_ticket,
            {"namespace_id": _NAMESPACE_ID, "summary": "Test"},
        ),
        (
            handle_support_sla_clock,
            {"namespace_id": _NAMESPACE_ID, "ticket_id": _TICKET_ID},
        ),
        (
            handle_support_health_score,
            {"namespace_id": _NAMESPACE_ID, "customer_id": "CUST-1"},
        ),
        (
            handle_support_troubleshoot,
            {"namespace_id": _NAMESPACE_ID, "symptom_text": "display flicker"},
        ),
        (
            handle_support_resolve_ticket,
            {
                "namespace_id": _NAMESPACE_ID,
                "ticket_id": _TICKET_ID,
                "resolution_text": "Replaced cable",
            },
        ),
        (
            handle_support_triage_ticket,
            {"namespace_id": _NAMESPACE_ID, "ticket_id": _TICKET_ID},
        ),
        (
            handle_support_record_touchpoint,
            {
                "namespace_id": _NAMESPACE_ID,
                "customer_id": "CUST-1",
                "question_id": "q1",
                "answer": "Great service",
            },
        ),
    ],
)
async def test_opt_in_gate_raises_scope_forbidden(mock_engine, handler, args):
    with patch(
        "nce.vertical_modules.support.mcp_handlers.require_support_enabled",
        new=AsyncMock(side_effect=SupportDisabledError("support vertical not enabled")),
    ):
        with pytest.raises(McpError) as exc_info:
            await handler(mock_engine, args)

        assert exc_info.value.code == MCP_SCOPE_FORBIDDEN
        assert "Support vertical is not enabled" in exc_info.value.message
        assert exc_info.value.data.get("reason") == "support_disabled"


# ---------------------------------------------------------------------------
# 3. Missing Parameters / Validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_namespace_id_raises_invalid_params(mock_engine):
    with pytest.raises(McpError) as exc_info:
        await handle_support_query_ticket(mock_engine, {})
    assert exc_info.value.code == -32602


# ---------------------------------------------------------------------------
# 4. Handler Execution Happy Paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_support_query_ticket_success(mock_engine):
    mock_data = {
        "ticket": {"id": _TICKET_ID, "summary": "Touch panel dead"},
        "sla_clock": {"breached": False},
    }
    with (
        patch(
            "nce.vertical_modules.support.mcp_handlers.require_support_enabled",
            new=AsyncMock(),
        ),
        patch(
            "nce.vertical_modules.support.mcp_handlers.do_query_ticket",
            new=AsyncMock(return_value=mock_data),
        ) as mock_core,
    ):
        raw_res = await handle_support_query_ticket(
            mock_engine, {"namespace_id": _NAMESPACE_ID, "ticket_id": _TICKET_ID}
        )
        res = json.loads(raw_res)
        assert res["ok"] is True
        assert res["ticket"]["id"] == _TICKET_ID
        mock_core.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_support_open_ticket_success(mock_engine):
    mock_data = {
        "ticket": {
            "id": _TICKET_ID,
            "summary": "Audio humming",
            "priority": "high",
        },
        "sla_clock": {"sla_profile": "standard"},
    }
    with (
        patch(
            "nce.vertical_modules.support.mcp_handlers.require_support_enabled",
            new=AsyncMock(),
        ),
        patch(
            "nce.vertical_modules.support.mcp_handlers.do_open_ticket",
            new=AsyncMock(return_value=mock_data),
        ) as mock_core,
    ):
        raw_res = await handle_support_open_ticket(
            mock_engine,
            {
                "namespace_id": _NAMESPACE_ID,
                "summary": "Audio humming",
                "priority": "high",
            },
        )
        res = json.loads(raw_res)
        assert res["ok"] is True
        assert res["ticket"]["summary"] == "Audio humming"
        mock_core.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_support_sla_clock_success(mock_engine):
    mock_data = {
        "ticket_id": _TICKET_ID,
        "breached": False,
        "first_response_countdown_seconds": 1800.0,
    }
    with (
        patch(
            "nce.vertical_modules.support.mcp_handlers.require_support_enabled",
            new=AsyncMock(),
        ),
        patch(
            "nce.vertical_modules.support.mcp_handlers.do_sla_clock",
            new=AsyncMock(return_value=mock_data),
        ) as mock_core,
    ):
        raw_res = await handle_support_sla_clock(
            mock_engine, {"namespace_id": _NAMESPACE_ID, "ticket_id": _TICKET_ID}
        )
        res = json.loads(raw_res)
        assert res["ok"] is True
        assert res["first_response_countdown_seconds"] == 1800.0
        mock_core.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_support_health_score_success(mock_engine):
    mock_data = {
        "customer_id": "CUST-42",
        "score": 92.5,
        "churn_risk": "low",
    }
    with (
        patch(
            "nce.vertical_modules.support.mcp_handlers.require_support_enabled",
            new=AsyncMock(),
        ),
        patch(
            "nce.vertical_modules.support.mcp_handlers.do_health_score",
            new=AsyncMock(return_value=mock_data),
        ) as mock_core,
    ):
        raw_res = await handle_support_health_score(
            mock_engine, {"namespace_id": _NAMESPACE_ID, "customer_id": "CUST-42"}
        )
        res = json.loads(raw_res)
        assert res["ok"] is True
        assert res["score"] == 92.5
        mock_core.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_support_troubleshoot_success(mock_engine):
    mock_data = {
        "cited_ticket_ids": ["TICK-101"],
        "proposed_resolution": "Power cycle Crestron NVX encoder",
        "confidence": 0.88,
        "zero_history": False,
    }
    with (
        patch(
            "nce.vertical_modules.support.mcp_handlers.require_support_enabled",
            new=AsyncMock(),
        ),
        patch(
            "nce.vertical_modules.support.mcp_handlers.do_troubleshoot",
            new=AsyncMock(return_value=mock_data),
        ) as mock_core,
    ):
        raw_res = await handle_support_troubleshoot(
            mock_engine,
            {
                "namespace_id": _NAMESPACE_ID,
                "symptom_text": "NVX encoder black screen",
            },
        )
        res = json.loads(raw_res)
        assert res["ok"] is True
        assert res["cited_ticket_ids"] == ["TICK-101"]
        mock_core.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_support_resolve_ticket_success(mock_engine):
    mock_data = {
        "ticket": {"id": _TICKET_ID, "status": "resolved"},
        "resolution_id": "res-uuid-1",
    }
    with (
        patch(
            "nce.vertical_modules.support.mcp_handlers.require_support_enabled",
            new=AsyncMock(),
        ),
        patch(
            "nce.vertical_modules.support.mcp_handlers.do_resolve_ticket",
            new=AsyncMock(return_value=mock_data),
        ) as mock_core,
    ):
        raw_res = await handle_support_resolve_ticket(
            mock_engine,
            {
                "namespace_id": _NAMESPACE_ID,
                "ticket_id": _TICKET_ID,
                "resolution_text": "Fixed patch cable",
            },
        )
        res = json.loads(raw_res)
        assert res["ok"] is True
        assert res["ticket"]["status"] == "resolved"
        mock_core.assert_awaited_once()


# ---------------------------------------------------------------------------
# 5. Domain Refusals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ticket_not_found_mapped_to_mcp_error(mock_engine):
    with (
        patch(
            "nce.vertical_modules.support.mcp_handlers.require_support_enabled",
            new=AsyncMock(),
        ),
        patch(
            "nce.vertical_modules.support.mcp_handlers.do_query_ticket",
            new=AsyncMock(side_effect=TicketNotFoundError(ticket_id=_TICKET_ID)),
        ),
    ):
        with pytest.raises(McpError) as exc_info:
            await handle_support_query_ticket(
                mock_engine, {"namespace_id": _NAMESPACE_ID, "ticket_id": _TICKET_ID}
            )
        assert exc_info.value.code == MCP_SCOPE_FORBIDDEN
        assert exc_info.value.data.get("reason") == "ticket_not_found"
        assert exc_info.value.data.get("ticket_id") == _TICKET_ID


@pytest.mark.asyncio
async def test_ticket_already_resolved_mapped_to_mcp_error(mock_engine):
    with (
        patch(
            "nce.vertical_modules.support.mcp_handlers.require_support_enabled",
            new=AsyncMock(),
        ),
        patch(
            "nce.vertical_modules.support.mcp_handlers.do_resolve_ticket",
            new=AsyncMock(
                side_effect=TicketAlreadyResolvedError(ticket_id=_TICKET_ID, status="resolved")
            ),
        ),
    ):
        with pytest.raises(McpError) as exc_info:
            await handle_support_resolve_ticket(
                mock_engine,
                {
                    "namespace_id": _NAMESPACE_ID,
                    "ticket_id": _TICKET_ID,
                    "resolution_text": "Re-resolve attempt",
                },
            )
        assert exc_info.value.code == MCP_SCOPE_FORBIDDEN
        assert exc_info.value.data.get("reason") == "ticket_already_resolved"


@pytest.mark.asyncio
async def test_invalid_ticket_status_mapped_to_mcp_error(mock_engine):
    with (
        patch(
            "nce.vertical_modules.support.mcp_handlers.require_support_enabled",
            new=AsyncMock(),
        ),
        patch(
            "nce.vertical_modules.support.mcp_handlers.do_resolve_ticket",
            new=AsyncMock(
                side_effect=InvalidTicketStatusError(ticket_id=_TICKET_ID, status="cancelled")
            ),
        ),
    ):
        with pytest.raises(McpError) as exc_info:
            await handle_support_resolve_ticket(
                mock_engine,
                {
                    "namespace_id": _NAMESPACE_ID,
                    "ticket_id": _TICKET_ID,
                    "resolution_text": "Resolve cancelled",
                },
            )
        assert exc_info.value.code == MCP_SCOPE_FORBIDDEN
        assert exc_info.value.data.get("reason") == "invalid_ticket_status"


@pytest.mark.asyncio
async def test_handle_support_triage_ticket_success(mock_engine):
    mock_data = {
        "recommended_priority": "high",
        "suggested_skill": "audio_specialist",
        "suggested_route": "tier_1_remote_triage",
    }
    with (
        patch(
            "nce.vertical_modules.support.mcp_handlers.require_support_enabled",
            new=AsyncMock(),
        ),
        patch(
            "nce.vertical_modules.support.mcp_handlers.do_triage_ticket",
            new=AsyncMock(return_value=mock_data),
        ) as mock_core,
    ):
        raw_res = await handle_support_triage_ticket(
            mock_engine,
            {
                "namespace_id": _NAMESPACE_ID,
                "ticket_id": _TICKET_ID,
            },
        )
        res = json.loads(raw_res)
        assert res["ok"] is True
        assert res["recommended_priority"] == "high"
        mock_core.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_support_record_touchpoint_success(mock_engine):
    mock_data = {
        "customer_id": "CUST-42",
        "score": 88.0,
        "churn_risk": "low",
    }
    with (
        patch(
            "nce.vertical_modules.support.mcp_handlers.require_support_enabled",
            new=AsyncMock(),
        ),
        patch(
            "nce.vertical_modules.support.mcp_handlers.do_record_touchpoint",
            new=AsyncMock(return_value=mock_data),
        ) as mock_core,
    ):
        raw_res = await handle_support_record_touchpoint(
            mock_engine,
            {
                "namespace_id": _NAMESPACE_ID,
                "customer_id": "CUST-42",
                "question_id": "q_nps",
                "answer": "All systems operating normally",
                "score": 9.0,
            },
        )
        res = json.loads(raw_res)
        assert res["ok"] is True
        assert res["score"] == 88.0
        mock_core.assert_awaited_once()


@pytest.mark.asyncio
async def test_ticket_not_found_mapped_to_mcp_error_triage(mock_engine):
    with (
        patch(
            "nce.vertical_modules.support.mcp_handlers.require_support_enabled",
            new=AsyncMock(),
        ),
        patch(
            "nce.vertical_modules.support.mcp_handlers.do_triage_ticket",
            new=AsyncMock(side_effect=TicketNotFoundError(ticket_id=_TICKET_ID)),
        ),
    ):
        with pytest.raises(McpError) as exc_info:
            await handle_support_triage_ticket(
                mock_engine, {"namespace_id": _NAMESPACE_ID, "ticket_id": _TICKET_ID}
            )
        assert exc_info.value.code == MCP_SCOPE_FORBIDDEN
        assert exc_info.value.data.get("reason") == "ticket_not_found"
        assert exc_info.value.data.get("ticket_id") == _TICKET_ID
