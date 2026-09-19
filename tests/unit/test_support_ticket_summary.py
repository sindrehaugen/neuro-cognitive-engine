"""
tests/unit/test_support_ticket_summary.py
=========================================
Wave D-6: Comprehensive unit test suite for Support Ticket Summary & Links:
  - Tool registry registration and contract flags:
      * support_summarise_ticket: cacheable=True, mutation=False, admin_only=False
      * support_get_ticket_links: cacheable=True, mutation=False, admin_only=False
      * support_link_ticket: cacheable=False, mutation=True, admin_only=True
  - MCP stdio tool schemas in TOOLS list
  - Core domain functions:
      * do_summarise_ticket: C9a grounded prose and citations via kg_nodes & ground()
      * do_get_ticket_links: resolves direct room/asset and kg_edges (FL, agreement, asset, WO)
      * do_link_ticket: establishes boundary edge in kg_edges, updates ticket, logs audit event
  - Support disabled guard enforcement across all layers
  - TicketNotFoundError handling (404 / McpError)
  - Admin REST handlers (api_support_ticket_summary, api_support_tickets_links, api_support_tickets_link)
"""

from __future__ import annotations

import datetime
import json
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
    handle_support_get_ticket_links,
    handle_support_link_ticket,
    handle_support_summarise_ticket,
)
from nce.vertical_modules.support.summary import (
    do_get_ticket_links,
    do_link_ticket,
    do_summarise_ticket,
)
from nce.vertical_modules.support.tickets import TicketNotFoundError


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
def sample_ticket_id():
    return str(uuid4())


@pytest.fixture
def mock_engine():
    engine = MagicMock()
    engine.pg_pool = MagicMock()
    return engine


# ===========================================================================
# 1. Tool Registry & Contract Flags
# ===========================================================================


def test_tool_registry_registration():
    """Verify Wave D-6 tools are registered with precise contract flags."""
    assert "support_summarise_ticket" in TOOL_REGISTRY
    spec_summary = TOOL_REGISTRY["support_summarise_ticket"]
    assert spec_summary.cacheable is True
    assert spec_summary.mutation is False
    assert spec_summary.admin_only is False

    assert "support_get_ticket_links" in TOOL_REGISTRY
    spec_links = TOOL_REGISTRY["support_get_ticket_links"]
    assert spec_links.cacheable is True
    assert spec_links.mutation is False
    assert spec_links.admin_only is False

    assert "support_link_ticket" in TOOL_REGISTRY
    spec_link = TOOL_REGISTRY["support_link_ticket"]
    assert spec_link.cacheable is False
    assert spec_link.mutation is True
    assert spec_link.admin_only is True


def test_mcp_stdio_tool_definitions():
    """Verify MCP stdio tool schemas are present and properly declared."""
    tools_by_name = {t.name: t for t in TOOLS}
    assert "support_summarise_ticket" in tools_by_name
    t_sum = tools_by_name["support_summarise_ticket"]
    assert "namespace_id" in t_sum.inputSchema["required"]
    assert "ticket_id" in t_sum.inputSchema["required"]

    assert "support_get_ticket_links" in tools_by_name
    t_links = tools_by_name["support_get_ticket_links"]
    assert "namespace_id" in t_links.inputSchema["required"]
    assert "ticket_id" in t_links.inputSchema["required"]

    assert "support_link_ticket" in tools_by_name
    t_link = tools_by_name["support_link_ticket"]
    assert "namespace_id" in t_link.inputSchema["required"]
    assert "ticket_id" in t_link.inputSchema["required"]
    assert "target_type" in t_link.inputSchema["required"]
    assert "target_id" in t_link.inputSchema["required"]


# ===========================================================================
# 2. Core: do_summarise_ticket (C9a retrieval-grounded generation)
# ===========================================================================


@pytest.mark.asyncio
async def test_do_summarise_ticket_success(sample_ns_id, sample_ticket_id):
    """do_summarise_ticket gathers facts, upserts kg_nodes, and grounds prose."""
    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock()
    mock_conn.fetch = AsyncMock()
    mock_conn.fetchval = AsyncMock(return_value=uuid4())

    # 1. Ticket row
    mock_conn.fetchrow.side_effect = [
        # ticket_row
        {
            "id": sample_ticket_id,
            "namespace_id": sample_ns_id,
            "source": "nce",
            "source_id": None,
            "asset_id": None,
            "room_id": "ROOM-101",
            "customer_id": "CUST-42",
            "status": "open",
            "priority": "high",
            "summary": "Audio DSP clipping during video conference",
            "description": "Customer reported severe clipping on line 2.",
            "sla_profile": "gold",
            "first_response_at": None,
            "resolved_at": None,
            "ai_diagnosis": {},
            "events": [],
            "change_origin": "agent",
            "created_at": datetime.datetime.now(datetime.timezone.utc),
            "updated_at": datetime.datetime.now(datetime.timezone.utc),
        },
        # sla_row
        {
            "sla_profile": "gold",
            "breached": False,
            "breach_type": None,
            "first_response_due": None,
            "resolution_due": None,
        },
    ]

    # 2. Action rows and Edge rows
    mock_conn.fetch.side_effect = [
        # action_rows
        [
            {
                "id": uuid4(),
                "action_type": "diagnostic",
                "action_summary": "Checked DSP input levels and firmware version",
                "outcome": "improved",
                "performed_by": "Tech Sindre",
                "performed_at": datetime.datetime.now(datetime.timezone.utc),
            }
        ],
        # edge_rows
        [
            {
                "subject_label": f"TICKET:{sample_ticket_id}",
                "predicate": "about",
                "object_label": "FUNCTIONAL_LOCATION:ROOM-101",
                "confidence": 1.0,
            }
        ],
    ]

    mock_pool = MagicMock()
    with patch("nce.vertical_modules.support.summary.scoped_pg_session") as mock_scoped:
        mock_scoped.return_value.__aenter__.return_value = mock_conn
        with patch(
            "nce.vertical_modules.support.summary.require_support_enabled", new_callable=AsyncMock
        ):
            with patch("nce.vertical_modules.support.summary.assert_owner", new_callable=AsyncMock):
                with patch(
                    "nce.vertical_modules.support.summary.ground",
                    new_callable=AsyncMock,
                    return_value={
                        "prose": "Support Ticket Summary: Ticket Audio DSP clipping. Priority: high. SLA Status: in-compliance.",
                        "citations": [{"node_id": "n1", "fact": "Audio DSP clipping"}],
                        "dropped": [],
                    },
                ) as mock_ground:
                    res = await do_summarise_ticket(
                        mock_pool,
                        {"namespace_id": sample_ns_id, "ticket_id": sample_ticket_id},
                    )

    assert res["ok"] is True
    assert res["ticket_id"] == sample_ticket_id
    assert "Support Ticket Summary" in res["prose"]
    assert len(res["citations"]) == 1
    assert res["action_count"] == 1
    assert res["link_count"] == 1
    assert mock_ground.await_count == 1


@pytest.mark.asyncio
async def test_do_summarise_ticket_not_found(sample_ns_id, sample_ticket_id):
    """do_summarise_ticket raises TicketNotFoundError when ticket does not exist."""
    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(return_value=None)

    mock_pool = MagicMock()
    with patch("nce.vertical_modules.support.summary.scoped_pg_session") as mock_scoped:
        mock_scoped.return_value.__aenter__.return_value = mock_conn
        with patch(
            "nce.vertical_modules.support.summary.require_support_enabled", new_callable=AsyncMock
        ):
            with pytest.raises(TicketNotFoundError):
                await do_summarise_ticket(
                    mock_pool,
                    {"namespace_id": sample_ns_id, "ticket_id": sample_ticket_id},
                )


# ===========================================================================
# 3. Core: do_get_ticket_links
# ===========================================================================


@pytest.mark.asyncio
async def test_do_get_ticket_links_success(sample_ns_id, sample_ticket_id):
    """do_get_ticket_links categorizes direct attributes and kg_edges."""
    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(
        return_value={
            "id": sample_ticket_id,
            "room_id": "ROOM-BOARDROOM-A",
            "asset_id": "00000000-0000-0000-0000-000000000005",
            "customer_id": "CUST-99",
        }
    )
    mock_conn.fetch = AsyncMock(
        return_value=[
            {
                "subject_label": f"TICKET:{sample_ticket_id}",
                "predicate": "about",
                "object_label": "AGREEMENT:AGR-2026-001",
                "confidence": 1.0,
                "change_origin": "agent",
                "created_at": datetime.datetime.now(datetime.timezone.utc),
            },
            {
                "subject_label": f"TICKET:{sample_ticket_id}",
                "predicate": "dispatched_as",
                "object_label": "WORK_ORDER:WO-882",
                "confidence": 1.0,
                "change_origin": "agent",
                "created_at": datetime.datetime.now(datetime.timezone.utc),
            },
        ]
    )

    mock_pool = MagicMock()
    with patch("nce.vertical_modules.support.summary.scoped_pg_session") as mock_scoped:
        mock_scoped.return_value.__aenter__.return_value = mock_conn
        with patch(
            "nce.vertical_modules.support.summary.require_support_enabled", new_callable=AsyncMock
        ):
            res = await do_get_ticket_links(
                mock_pool,
                {"namespace_id": sample_ns_id, "ticket_id": sample_ticket_id},
            )

    assert res["ok"] is True
    assert res["ticket_id"] == sample_ticket_id
    assert len(res["functional_locations"]) == 1
    assert res["functional_locations"][0]["target_id"] == "ROOM-BOARDROOM-A"
    assert len(res["assets"]) == 1
    assert len(res["agreements"]) == 1
    assert res["agreements"][0]["target_id"] == "AGR-2026-001"
    assert len(res["work_orders"]) == 1
    assert res["work_orders"][0]["target_id"] == "WO-882"
    assert res["count"] == 4


# ===========================================================================
# 4. Core: do_link_ticket
# ===========================================================================


@pytest.mark.asyncio
async def test_do_link_ticket_success(sample_ns_id, sample_ticket_id):
    """do_link_ticket creates kg_edges boundary edge, updates ticket, logs event."""
    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(
        return_value={"id": sample_ticket_id, "room_id": None, "asset_id": None}
    )
    mock_conn.execute = AsyncMock()

    mock_pool = MagicMock()
    with patch("nce.vertical_modules.support.summary.scoped_pg_session") as mock_scoped:
        mock_scoped.return_value.__aenter__.return_value = mock_conn
        with patch(
            "nce.vertical_modules.support.summary.require_support_enabled", new_callable=AsyncMock
        ):
            with patch(
                "nce.vertical_modules.support.summary.assert_owner", new_callable=AsyncMock
            ) as mock_owner:
                res = await do_link_ticket(
                    mock_pool,
                    {
                        "namespace_id": sample_ns_id,
                        "ticket_id": sample_ticket_id,
                        "target_type": "functional_location",
                        "target_id": "FL-ROOM-202",
                        "relation": "about",
                    },
                )

    assert res["ok"] is True
    assert res["ticket_id"] == sample_ticket_id
    assert res["target_type"] == "functional_location"
    assert res["target_id"] == "FL-ROOM-202"
    assert res["status"] == "linked"
    assert mock_owner.await_count == 1
    # Check that update for room_id and events both executed
    assert mock_conn.execute.await_count >= 2


@pytest.mark.asyncio
async def test_do_link_ticket_invalid_target_type(sample_ns_id, sample_ticket_id):
    """do_link_ticket rejects invalid target type."""
    mock_pool = MagicMock()
    with pytest.raises(ValueError, match="invalid target_type"):
        await do_link_ticket(
            mock_pool,
            {
                "namespace_id": sample_ns_id,
                "ticket_id": sample_ticket_id,
                "target_type": "unsupported_type",
                "target_id": "123",
            },
        )


# ===========================================================================
# 5. MCP Handlers
# ===========================================================================


@pytest.mark.asyncio
async def test_handle_support_summarise_ticket(mock_engine, sample_ns_id, sample_ticket_id):
    """handle_support_summarise_ticket executes cleanly under mock."""
    with patch(
        "nce.vertical_modules.support.mcp_handlers.require_support_enabled", new_callable=AsyncMock
    ):
        with patch(
            "nce.vertical_modules.support.mcp_handlers.do_summarise_ticket",
            new_callable=AsyncMock,
            return_value={"ticket_id": sample_ticket_id, "prose": "Ground summary"},
        ):
            raw = await handle_support_summarise_ticket(
                mock_engine,
                {"namespace_id": sample_ns_id, "ticket_id": sample_ticket_id},
            )
            data = json.loads(raw)
            assert data["ok"] is True
            assert data["ticket_id"] == sample_ticket_id


@pytest.mark.asyncio
async def test_handle_support_get_ticket_links(mock_engine, sample_ns_id, sample_ticket_id):
    """handle_support_get_ticket_links executes cleanly under mock."""
    with patch(
        "nce.vertical_modules.support.mcp_handlers.require_support_enabled", new_callable=AsyncMock
    ):
        with patch(
            "nce.vertical_modules.support.mcp_handlers.do_get_ticket_links",
            new_callable=AsyncMock,
            return_value={"ticket_id": sample_ticket_id, "functional_locations": []},
        ):
            raw = await handle_support_get_ticket_links(
                mock_engine,
                {"namespace_id": sample_ns_id, "ticket_id": sample_ticket_id},
            )
            data = json.loads(raw)
            assert data["ok"] is True
            assert data["ticket_id"] == sample_ticket_id


@pytest.mark.asyncio
async def test_handle_support_link_ticket(mock_engine, sample_ns_id, sample_ticket_id):
    """handle_support_link_ticket executes cleanly under mock."""
    with patch(
        "nce.vertical_modules.support.mcp_handlers.require_support_enabled", new_callable=AsyncMock
    ):
        with patch(
            "nce.vertical_modules.support.mcp_handlers.do_link_ticket",
            new_callable=AsyncMock,
            return_value={"ticket_id": sample_ticket_id, "status": "linked"},
        ):
            raw = await handle_support_link_ticket(
                mock_engine,
                {
                    "namespace_id": sample_ns_id,
                    "ticket_id": sample_ticket_id,
                    "target_type": "agreement",
                    "target_id": "AGR-10",
                },
            )
            data = json.loads(raw)
            assert data["ok"] is True
            assert data["status"] == "linked"


@pytest.mark.asyncio
async def test_handle_support_disabled(mock_engine, sample_ns_id, sample_ticket_id):
    """MCP handlers raise MCP_SCOPE_FORBIDDEN when Support engine is disabled."""
    with patch(
        "nce.vertical_modules.support.mcp_handlers.require_support_enabled",
        new_callable=AsyncMock,
        side_effect=SupportDisabledError("support engine disabled"),
    ):
        with pytest.raises(McpError) as exc_info:
            await handle_support_summarise_ticket(
                mock_engine,
                {"namespace_id": sample_ns_id, "ticket_id": sample_ticket_id},
            )
        assert exc_info.value.code == MCP_SCOPE_FORBIDDEN


# ===========================================================================
# 6. Admin REST Handlers
# ===========================================================================


@pytest.mark.asyncio
async def test_api_support_ticket_summary(mock_engine, sample_ns_id, sample_ticket_id):
    """GET /api/support/tickets/{id}/summary returns 200 with JSON payload."""
    admin_state.engine = mock_engine
    req = MockRequest(
        query_params={"namespace_id": sample_ns_id},
        path_params={"id": sample_ticket_id},
    )

    with patch("nce.admin_handlers.support.require_support_enabled", new_callable=AsyncMock):
        with patch(
            "nce.admin_handlers.support.do_summarise_ticket",
            new_callable=AsyncMock,
            return_value={"ticket_id": sample_ticket_id, "prose": "Summary narrative"},
        ):
            resp = await support_handlers.api_support_ticket_summary(req)
            assert resp.status_code == 200
            payload = json.loads(resp.body)
            assert payload["ok"] is True
            assert payload["ticket_id"] == sample_ticket_id


@pytest.mark.asyncio
async def test_api_support_tickets_links(mock_engine, sample_ns_id, sample_ticket_id):
    """GET /api/support/tickets/{id}/links returns 200 with JSON payload."""
    admin_state.engine = mock_engine
    req = MockRequest(
        query_params={"namespace_id": sample_ns_id},
        path_params={"id": sample_ticket_id},
    )

    with patch("nce.admin_handlers.support.require_support_enabled", new_callable=AsyncMock):
        with patch(
            "nce.admin_handlers.support.do_get_ticket_links",
            new_callable=AsyncMock,
            return_value={"ticket_id": sample_ticket_id, "functional_locations": []},
        ):
            resp = await support_handlers.api_support_tickets_links(req)
            assert resp.status_code == 200
            payload = json.loads(resp.body)
            assert payload["ok"] is True


@pytest.mark.asyncio
async def test_api_support_tickets_link(mock_engine, sample_ns_id, sample_ticket_id):
    """POST /api/support/tickets/{id}/links returns 200 and bumps MCP cache."""
    admin_state.engine = mock_engine
    req = MockRequest(
        path_params={"id": sample_ticket_id},
        json_body={
            "namespace_id": sample_ns_id,
            "target_type": "agreement",
            "target_id": "AGR-100",
        },
    )

    with patch("nce.admin_handlers.support.require_support_enabled", new_callable=AsyncMock):
        with patch(
            "nce.admin_handlers.support.do_link_ticket",
            new_callable=AsyncMock,
            return_value={"ticket_id": sample_ticket_id, "status": "linked"},
        ):
            with patch(
                "nce.admin_handlers.support.bump_mcp_cache_generation", new_callable=AsyncMock
            ) as mock_bump:
                resp = await support_handlers.api_support_tickets_link(req)
                assert resp.status_code == 200
                payload = json.loads(resp.body)
                assert payload["ok"] is True
                assert mock_bump.await_count == 1
