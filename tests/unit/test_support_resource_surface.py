"""
tests/unit/test_support_resource_surface.py
===========================================
Unit tests for Wave D-5:
  1. C12 Resource Surface registration for TICKET (service_tickets) and
     TICKET_ACTION (support_ticket_actions).
  2. Removal of TICKET from exemptions catalog.
  3. C12 tool registry generation for support tickets and ticket actions.
  4. Custom verbs:
     - MCP tools: support_log_ticket_action, support_ticket_timeline
     - MCP handlers: handle_support_log_ticket_action, handle_support_ticket_timeline
     - REST routes: POST /api/support/tickets/{id}/actions, GET /api/support/tickets/{id}/timeline
  5. Core logic in nce/vertical_modules/support/tickets.py:
     - do_log_ticket_action (append-only log + service_tickets.events update)
     - do_get_ticket_timeline (chronological ordering + latest_outcome)
     - Enum validations and error handling (TicketNotFoundError)
  6. Migration 087 and schema validation for support_ticket_actions.

Pure unit tests — mock connections and fixtures, no live Postgres/Redis required.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from nce import admin_state
from nce.admin_app import build_admin_routes
from nce.admin_handlers import support as support_admin_mod
from nce.event_log import EXPECTED_TENANT_RLS_TABLES
from nce.mcp_errors import MCP_SCOPE_FORBIDDEN, McpError
from nce.mcp_stdio_tools import TOOLS
from nce.resource_surface import (
    RESOURCE_SURFACE_EXEMPTIONS,
    get_all_resource_specs,
    load_all_engine_resources,
)
from nce.tool_registry import (
    ADMIN_ONLY_TOOLS,
    CACHEABLE_TOOLS,
    MUTATION_TOOLS,
    TOOL_REGISTRY,
)
from nce.vertical_modules.support._guard import SupportDisabledError
from nce.vertical_modules.support.mcp_handlers import (
    handle_support_log_ticket_action,
    handle_support_ticket_timeline,
)
from nce.vertical_modules.support.tickets import (
    EVENT_TYPE_TICKET_ACTION_LOGGED,
    TicketNotFoundError,
    do_get_ticket_timeline,
    do_log_ticket_action,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_FILE = REPO_ROOT / "nce" / "migrations" / "091_support_ticket_actions.sql"
SCHEMA_FILE = REPO_ROOT / "nce" / "schema.sql"

_NAMESPACE_ID = "00000000-0000-4000-8000-000000000001"
_TICKET_ID = "11111111-1111-4111-8111-111111111111"
_ACTION_ID = "22222222-2222-4222-8222-222222222222"


# ===========================================================================
# 1. C12 Resource Surface & Exemptions
# ===========================================================================


def test_ticket_and_ticket_action_registered_in_all_resource_specs():
    """TICKET and TICKET_ACTION specs must be registered in resource surface."""
    load_all_engine_resources()
    specs = get_all_resource_specs()
    node_types = {spec.node_type for spec in specs}
    assert "TICKET" in node_types, "TICKET missing from get_all_resource_specs()"
    assert "TICKET_ACTION" in node_types, "TICKET_ACTION missing from get_all_resource_specs()"

    ticket_spec = next(s for s in specs if s.node_type == "TICKET")
    assert ticket_spec.entity == "tickets"
    assert ticket_spec.table_name == "service_tickets"
    assert ticket_spec.engine == "support"
    assert ticket_spec.rest_slug == "tickets"

    action_spec = next(s for s in specs if s.node_type == "TICKET_ACTION")
    assert action_spec.entity == "ticket-actions"
    assert action_spec.table_name == "support_ticket_actions"
    assert action_spec.engine == "support"
    assert action_spec.rest_slug == "ticket-actions"


def test_ticket_not_in_exempt_resources():
    """TICKET must be retired from exemptions (Wave D-5 requirement)."""
    assert "TICKET" not in RESOURCE_SURFACE_EXEMPTIONS, (
        "TICKET should not be in RESOURCE_SURFACE_EXEMPTIONS"
    )
    assert "TICKET_ACTION" not in RESOURCE_SURFACE_EXEMPTIONS, (
        "TICKET_ACTION should not be in RESOURCE_SURFACE_EXEMPTIONS"
    )


def test_c12_generated_support_tools_in_registry():
    """C12 generated tools for tickets and ticket-actions must exist in TOOL_REGISTRY.

    archive removed 2026-09-20 (archive wave): neither service_tickets nor
    support_ticket_actions has an is_archived column; see
    ARCHIVE_COLUMN_SWEEP.md.
    """
    expected_generated_tools = {
        "support_list_tickets",
        "support_get_tickets",
        "support_upsert_tickets",
        "support_list_ticket_actions",
        "support_get_ticket_actions",
        "support_upsert_ticket_actions",
    }
    for tool_name in expected_generated_tools:
        assert tool_name in TOOL_REGISTRY, f"C12 tool {tool_name} missing from TOOL_REGISTRY"
    for removed_tool in ("support_archive_tickets", "support_archive_ticket_actions"):
        assert removed_tool not in TOOL_REGISTRY, (
            f"{removed_tool} should not exist -- archive is excluded for this spec"
        )


# ===========================================================================
# 2. Tool Registry & Stdio Definitions for Custom Verbs
# ===========================================================================


def test_custom_verbs_in_tool_registry():
    """Custom verbs support_log_ticket_action and support_ticket_timeline in registry."""
    assert "support_log_ticket_action" in TOOL_REGISTRY
    assert "support_ticket_timeline" in TOOL_REGISTRY

    log_spec = TOOL_REGISTRY["support_log_ticket_action"]
    assert log_spec.mutation is True
    assert log_spec.admin_only is True
    assert log_spec.cacheable is False

    timeline_spec = TOOL_REGISTRY["support_ticket_timeline"]
    assert timeline_spec.mutation is False
    assert timeline_spec.admin_only is False
    assert timeline_spec.cacheable is True

    assert "support_log_ticket_action" in MUTATION_TOOLS
    assert "support_log_ticket_action" in ADMIN_ONLY_TOOLS
    assert "support_ticket_timeline" in CACHEABLE_TOOLS


def test_custom_verbs_in_mcp_tools():
    """Custom verbs must appear in MCP_TOOLS with valid schema definitions."""
    tools_by_name = {t.name: t for t in TOOLS}
    assert "support_log_ticket_action" in tools_by_name
    assert "support_ticket_timeline" in tools_by_name

    log_tool = tools_by_name["support_log_ticket_action"]
    assert set(log_tool.inputSchema["required"]) == {
        "namespace_id",
        "ticket_id",
        "action_type",
        "action_summary",
        "outcome",
    }
    action_type_prop = log_tool.inputSchema["properties"]["action_type"]
    assert "diagnostic" in action_type_prop["enum"]
    assert "hardware_replacement" in action_type_prop["enum"]

    outcome_prop = log_tool.inputSchema["properties"]["outcome"]
    assert "resolved" in outcome_prop["enum"]
    assert "failed" in outcome_prop["enum"]

    timeline_tool = tools_by_name["support_ticket_timeline"]
    assert set(timeline_tool.inputSchema["required"]) == {"namespace_id", "ticket_id"}


# ===========================================================================
# 3. MCP Handlers
# ===========================================================================


@pytest.mark.asyncio
async def test_handle_support_log_ticket_action_success():
    """MCP handler invokes do_log_ticket_action and returns JSON."""
    engine = MagicMock()
    engine.pg_pool = MagicMock()

    args = {
        "namespace_id": _NAMESPACE_ID,
        "ticket_id": _TICKET_ID,
        "action_type": "diagnostic",
        "action_summary": "Tested power supply rail voltage",
        "outcome": "resolved",
    }
    mock_res = {
        "action": {"id": _ACTION_ID, "action_type": "diagnostic"},
        "ticket_id": _TICKET_ID,
        "status": "logged",
    }

    with (
        patch(
            "nce.vertical_modules.support.mcp_handlers._check_support_enabled",
            AsyncMock(return_value=_NAMESPACE_ID),
        ),
        patch(
            "nce.vertical_modules.support.mcp_handlers.do_log_ticket_action",
            AsyncMock(return_value=mock_res),
        ),
    ):
        raw = await handle_support_log_ticket_action(engine, args)
        data = json.loads(raw)
        assert data["ok"] is True
        assert data["status"] == "logged"
        assert data["ticket_id"] == _TICKET_ID


@pytest.mark.asyncio
async def test_handle_support_log_ticket_action_disabled_guard():
    """MCP handler enforces opt-in guard and returns McpError(-32005)."""
    engine = MagicMock()
    engine.pg_pool = MagicMock()

    args = {
        "namespace_id": _NAMESPACE_ID,
        "ticket_id": _TICKET_ID,
        "action_type": "configuration",
        "action_summary": "Check cables",
        "outcome": "resolved",
    }

    with patch(
        "nce.vertical_modules.support.mcp_handlers.require_support_enabled",
        AsyncMock(side_effect=SupportDisabledError("support not active")),
    ):
        with pytest.raises(McpError) as exc_info:
            await handle_support_log_ticket_action(engine, args)
        assert exc_info.value.code == MCP_SCOPE_FORBIDDEN


@pytest.mark.asyncio
async def test_handle_support_log_ticket_action_not_found():
    """TicketNotFoundError maps to McpError with ticket_not_found reason."""
    engine = MagicMock()
    engine.pg_pool = MagicMock()

    args = {
        "namespace_id": _NAMESPACE_ID,
        "ticket_id": _TICKET_ID,
        "action_type": "configuration",
        "action_summary": "Notes",
        "outcome": "resolved",
    }

    with (
        patch(
            "nce.vertical_modules.support.mcp_handlers._check_support_enabled",
            AsyncMock(return_value=_NAMESPACE_ID),
        ),
        patch(
            "nce.vertical_modules.support.mcp_handlers.do_log_ticket_action",
            AsyncMock(side_effect=TicketNotFoundError(ticket_id=_TICKET_ID)),
        ),
    ):
        with pytest.raises(McpError) as exc_info:
            await handle_support_log_ticket_action(engine, args)
        assert exc_info.value.code == MCP_SCOPE_FORBIDDEN
        assert exc_info.value.data.get("reason") == "ticket_not_found"


@pytest.mark.asyncio
async def test_handle_support_ticket_timeline_success():
    """handle_support_ticket_timeline retrieves timeline payload."""
    engine = MagicMock()
    engine.pg_pool = MagicMock()

    args = {"namespace_id": _NAMESPACE_ID, "ticket_id": _TICKET_ID}
    mock_timeline = {
        "ticket_id": _TICKET_ID,
        "ticket_summary": "System outage",
        "ticket_status": "OPEN",
        "created_at": "2026-09-18T12:00:00+00:00",
        "actions": [{"id": _ACTION_ID, "action_type": "NOTE", "action_summary": "Investigating"}],
        "total_actions": 1,
        "latest_outcome": None,
    }

    with (
        patch(
            "nce.vertical_modules.support.mcp_handlers._check_support_enabled",
            AsyncMock(return_value=_NAMESPACE_ID),
        ),
        patch(
            "nce.vertical_modules.support.mcp_handlers.do_get_ticket_timeline",
            AsyncMock(return_value=mock_timeline),
        ),
    ):
        raw = await handle_support_ticket_timeline(engine, args)
        data = json.loads(raw)
        assert data["ok"] is True
        assert data["total_actions"] == 1
        assert data["ticket_id"] == _TICKET_ID


# ===========================================================================
# 4. REST Routes
# ===========================================================================


def test_routes_mounted_in_admin_app():
    """Routes /api/support/tickets/{id}/actions and /timeline must be mounted."""
    routes = build_admin_routes()
    action_route = next((r for r in routes if r.path == "/api/support/tickets/{id}/actions"), None)
    assert action_route is not None, "Missing POST /api/support/tickets/{id}/actions"
    assert "POST" in action_route.methods

    timeline_route = next(
        (r for r in routes if r.path == "/api/support/tickets/{id}/timeline"), None
    )
    assert timeline_route is not None, "Missing GET /api/support/tickets/{id}/timeline"
    assert "GET" in timeline_route.methods


@pytest.mark.asyncio
async def test_api_support_tickets_log_action_success():
    """POST /api/support/tickets/{id}/actions returns 200 with result."""
    mock_req = MagicMock()
    mock_req.path_params = {"id": _TICKET_ID}
    mock_req.json = AsyncMock(
        return_value={
            "namespace_id": _NAMESPACE_ID,
            "action_type": "configuration",
            "action_summary": "Updated IP config",
            "outcome": "resolved",
        }
    )

    engine = MagicMock()
    engine.pg_pool = MagicMock()
    mock_res = {"action": {"id": _ACTION_ID}, "ticket_id": _TICKET_ID, "status": "logged"}

    with (
        patch.object(admin_state, "engine", engine),
        patch("nce.admin_handlers.support.require_support_enabled", AsyncMock()),
        patch("nce.admin_handlers.support.do_log_ticket_action", AsyncMock(return_value=mock_res)),
        patch("nce.admin_handlers.support.bump_mcp_cache_generation", AsyncMock()) as mock_bump,
    ):
        resp = await support_admin_mod.api_support_tickets_log_action(mock_req)
        assert resp.status_code == 200
        data = json.loads(resp.body.decode("utf-8"))
        assert data["ok"] is True
        assert data["status"] == "logged"
        mock_bump.assert_awaited_once()


@pytest.mark.asyncio
async def test_api_support_tickets_timeline_success():
    """GET /api/support/tickets/{id}/timeline returns 200 with timeline."""
    mock_req = MagicMock()
    mock_req.path_params = {"id": _TICKET_ID}
    mock_req.query_params = {"namespace_id": _NAMESPACE_ID, "limit": "50"}

    engine = MagicMock()
    engine.pg_pool = MagicMock()
    mock_res = {"ticket_id": _TICKET_ID, "actions": [], "total_actions": 0}

    with (
        patch.object(admin_state, "engine", engine),
        patch("nce.admin_handlers.support.require_support_enabled", AsyncMock()),
        patch(
            "nce.admin_handlers.support.do_get_ticket_timeline", AsyncMock(return_value=mock_res)
        ),
    ):
        resp = await support_admin_mod.api_support_tickets_timeline(mock_req)
        assert resp.status_code == 200
        data = json.loads(resp.body.decode("utf-8"))
        assert data["ok"] is True
        assert data["ticket_id"] == _TICKET_ID


# ===========================================================================
# 5. Core Ticket Actions & Timeline Logic
# ===========================================================================


@pytest.mark.asyncio
async def test_do_log_ticket_action_inserts_and_appends_event():
    """do_log_ticket_action inserts action and updates ticket events JSONB."""
    pool = MagicMock()
    conn = AsyncMock()

    mock_ticket_row = {
        "id": UUID(_TICKET_ID),
        "summary": "Network down in Room 302",
        "status": "OPEN",
    }
    mock_action_row = {
        "id": UUID(_ACTION_ID),
        "namespace_id": UUID(_NAMESPACE_ID),
        "ticket_id": UUID(_TICKET_ID),
        "action_type": "hardware_replacement",
        "action_summary": "Replaced bad patch cable",
        "action_details": "Replaced Cat6 with Cat6a shielded",
        "outcome": "resolved",
        "outcome_notes": "Link came up immediately",
        "performed_by": "Tech Sindre",
        "performed_at": datetime.datetime.now(datetime.timezone.utc),
        "change_origin": "agent",
        "created_at": datetime.datetime.now(datetime.timezone.utc),
        "updated_at": datetime.datetime.now(datetime.timezone.utc),
    }

    conn.fetchrow.side_effect = [mock_ticket_row, mock_action_row]
    conn.execute = AsyncMock()

    with patch("nce.vertical_modules.support.tickets.scoped_pg_session") as mock_session:
        mock_session.return_value.__aenter__.return_value = conn

        params = {
            "namespace_id": _NAMESPACE_ID,
            "ticket_id": _TICKET_ID,
            "action_type": "hardware_replacement",
            "action_summary": "Replaced bad patch cable",
            "action_details": "Replaced Cat6 with Cat6a shielded",
            "outcome": "resolved",
            "outcome_notes": "Link came up immediately",
            "performed_by": "Tech Sindre",
            "change_origin": "agent",
        }

        result = await do_log_ticket_action(pool, params)
        assert result["status"] == "logged"
        assert result["ticket_id"] == _TICKET_ID
        assert result["action"]["action_type"] == "hardware_replacement"

        # Verify SQL operations
        assert conn.fetchrow.await_count == 2
        assert conn.execute.await_count == 1
        # Execute query should update service_tickets events
        exec_args = conn.execute.await_args[0]
        assert "UPDATE service_tickets" in exec_args[0]
        # JSON payload contains event
        event_payload = json.loads(exec_args[3])
        assert event_payload[0]["event_type"] == EVENT_TYPE_TICKET_ACTION_LOGGED
        assert event_payload[0]["action_type"] == "hardware_replacement"


@pytest.mark.asyncio
async def test_do_log_ticket_action_ticket_not_found():
    """do_log_ticket_action raises TicketNotFoundError when ticket missing."""
    pool = MagicMock()
    conn = AsyncMock()
    conn.fetchrow.return_value = None  # ticket not found

    with patch("nce.vertical_modules.support.tickets.scoped_pg_session") as mock_session:
        mock_session.return_value.__aenter__.return_value = conn

        params = {
            "namespace_id": _NAMESPACE_ID,
            "ticket_id": _TICKET_ID,
            "action_type": "diagnostic",
            "action_summary": "Notes",
            "outcome": "resolved",
            "performed_by": "tech-1",
        }
        with pytest.raises(TicketNotFoundError) as exc_info:
            await do_log_ticket_action(pool, params)
        assert exc_info.value.ticket_id == _TICKET_ID


@pytest.mark.asyncio
async def test_do_log_ticket_action_enum_validation():
    """Invalid action_type or outcome raises ValueError."""
    pool = MagicMock()

    # Invalid action_type
    with pytest.raises(ValueError, match="Invalid action_type"):
        await do_log_ticket_action(
            pool,
            {
                "namespace_id": _NAMESPACE_ID,
                "ticket_id": _TICKET_ID,
                "action_type": "NOT_A_VALID_TYPE",
                "action_summary": "Invalid action",
                "outcome": "resolved",
            },
        )

    # Invalid outcome
    with pytest.raises(ValueError, match="Invalid outcome"):
        await do_log_ticket_action(
            pool,
            {
                "namespace_id": _NAMESPACE_ID,
                "ticket_id": _TICKET_ID,
                "action_type": "diagnostic",
                "action_summary": "Valid summary",
                "outcome": "SUPER_GREAT",
            },
        )


@pytest.mark.asyncio
async def test_do_get_ticket_timeline_chronological_ordering():
    """Timeline fetches ticket and orders actions chronologically."""
    pool = MagicMock()
    conn = AsyncMock()

    now = datetime.datetime.now(datetime.timezone.utc)
    mock_ticket_row = {
        "id": UUID(_TICKET_ID),
        "summary": "Audio humming",
        "status": "IN_PROGRESS",
        "created_at": now - datetime.timedelta(hours=2),
        "first_response_at": now - datetime.timedelta(hours=1),
        "resolved_at": None,
    }
    mock_action_1 = {
        "id": UUID(_ACTION_ID),
        "action_type": "diagnostic",
        "action_summary": "Checked ground loop",
        "outcome": "inconclusive",
        "performed_at": now - datetime.timedelta(hours=1),
        "created_at": now - datetime.timedelta(hours=1),
    }
    mock_action_2 = {
        "id": uuid4(),
        "action_type": "hardware_replacement",
        "action_summary": "Installed DI box isolator",
        "outcome": "resolved",
        "performed_at": now,
        "created_at": now,
    }

    conn.fetchrow.return_value = mock_ticket_row
    conn.fetch.return_value = [mock_action_1, mock_action_2]

    with patch("nce.vertical_modules.support.tickets.scoped_pg_session") as mock_session:
        mock_session.return_value.__aenter__.return_value = conn

        result = await do_get_ticket_timeline(
            pool,
            {
                "namespace_id": _NAMESPACE_ID,
                "ticket_id": _TICKET_ID,
            },
        )

        assert result["ticket_id"] == _TICKET_ID
        assert result["ticket_summary"] == "Audio humming"
        assert result["ticket_status"] == "IN_PROGRESS"
        assert result["total_actions"] == 2
        assert len(result["actions"]) == 2
        assert result["latest_outcome"] == "resolved"


# ===========================================================================
# 6. Schema and Migration 091 Validation
# ===========================================================================


def test_migration_091_structure_and_security():
    """Migration 091 must declare support_ticket_actions with RLS and constraints."""
    assert MIGRATION_FILE.is_file(), f"Migration file missing: {MIGRATION_FILE}"
    content = MIGRATION_FILE.read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS support_ticket_actions" in content
    assert "ticket_id" in content
    assert "action_type" in content
    assert "action_summary" in content
    assert "outcome" in content
    assert "performed_by" in content
    assert "performed_at" in content
    assert "change_origin" in content

    # Check constraints
    assert "support_ticket_actions_action_type_check" in content
    assert "support_ticket_actions_outcome_check" in content
    assert "support_ticket_actions_change_origin_check" in content

    # Forced RLS
    assert "ALTER TABLE support_ticket_actions ENABLE ROW LEVEL SECURITY" in content
    assert "ALTER TABLE support_ticket_actions FORCE ROW LEVEL SECURITY" in content
    assert "CREATE POLICY tenant_isolation_policy ON support_ticket_actions" in content
    assert "get_nce_namespace()" in content


def test_support_ticket_actions_in_schema_sql():
    """support_ticket_actions DDL and policies must be present in nce/schema.sql."""
    assert SCHEMA_FILE.is_file(), f"Schema file missing: {SCHEMA_FILE}"
    content = SCHEMA_FILE.read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS support_ticket_actions" in content
    assert "ALTER TABLE support_ticket_actions FORCE ROW LEVEL SECURITY" in content
    assert "CREATE POLICY tenant_isolation_policy ON support_ticket_actions" in content


def test_support_ticket_actions_in_expected_tenant_rls_tables():
    """support_ticket_actions must be registered in EXPECTED_TENANT_RLS_TABLES."""
    assert "support_ticket_actions" in EXPECTED_TENANT_RLS_TABLES
    assert EXPECTED_TENANT_RLS_TABLES["support_ticket_actions"] == "namespace_id"
