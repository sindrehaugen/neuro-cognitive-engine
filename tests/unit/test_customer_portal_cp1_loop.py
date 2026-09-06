"""tests/unit/test_customer_portal_cp1_loop.py
=============================================
Unit tests and acceptance gates for Wave CP-1:
  "Close the loop: Customer request becomes a ticket via W-1 Engine Registry"

Mandate per MLv1.5-B Charter §6 & Updates §13:
1. Production consumer of W-1 Engine Registry: `engine.modules["support"]` and `engine.modules["sales"]`.
2. Must run against a REAL `NCEEngine`, not a stub with `.support` or `.sales` assigned.
3. Handle `EngineDisabledError` explicitly and degrade visibly.
4. Eliminate `hasattr(engine, "support")` and `hasattr(engine, "sales")` seams from `customer_portal/actions.py`.
5. Verify IDOR `PermissionError -> MCP_INVALID_REQUEST (-32600)` and `ResourcesError -> MCP_INVALID_PARAMS (-32602)`.
"""

from __future__ import annotations

import ast
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from nce.engine_registry import EngineRegistry
from nce.mcp_errors import (
    MCP_INVALID_PARAMS,
    MCP_INVALID_REQUEST,
    McpError,
    mcp_handler,
)
from nce.orchestrator import NCEEngine
from nce.vertical_modules.customer_portal.actions import (
    do_raise_service_request,
    do_register_expansion_interest,
)
from nce.vertical_modules.customer_portal.mcp_handlers import (
    handle_customer_portal_raise_service_request,
)
from nce.vertical_modules.resources._guard import ResourceValidationError

_NS_ID = "00000000-0000-4000-8000-000000000001"
_CUST_SCOPE = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
_ACTIONS_PY = Path("nce/vertical_modules/customer_portal/actions.py")


@pytest.fixture(autouse=True)
def _patch_scoped_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pass-through fixture for scoped_pg_session in support.tickets during unit tests."""

    @asynccontextmanager
    async def _fake_scoped(pool: Any, ns: Any) -> Any:
        ctx = pool.acquire()
        if hasattr(ctx, "__aenter__"):
            conn = await ctx.__aenter__()
            try:
                yield conn
            finally:
                if hasattr(ctx, "__aexit__"):
                    await ctx.__aexit__(None, None, None)
        else:
            yield ctx

    monkeypatch.setattr(
        "nce.vertical_modules.support.tickets.scoped_pg_session",
        _fake_scoped,
    )


def _make_mock_pool(conn: AsyncMock) -> MagicMock:
    pool = MagicMock()
    acquire_ctx = AsyncMock()
    acquire_ctx.__aenter__.return_value = conn
    acquire_ctx.__aexit__.return_value = None
    pool.acquire.return_value = acquire_ctx
    return pool


# ---------------------------------------------------------------------------
# 1. Real NCEEngine architectural contract
# ---------------------------------------------------------------------------


def test_real_nce_engine_has_modules_registry_and_no_engine_attributes() -> None:
    """A real NCEEngine has engine.modules (EngineRegistry) and NO .support/.sales attributes."""
    engine = NCEEngine()
    assert isinstance(engine.modules, EngineRegistry)
    assert "support" in engine.modules
    assert "sales" in engine.modules
    # Crucial: prove that NCEEngine does NOT have engine.support or engine.sales
    assert not hasattr(engine, "support")
    assert not hasattr(engine, "sales")


# ---------------------------------------------------------------------------
# 2. Support Hand-off & Ticket Creation via Real NCEEngine
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_customer_portal_service_request_creates_support_ticket() -> None:
    """Inbound service request hands off to Support via engine.modules['support'].do_open_ticket."""
    engine = NCEEngine()
    conn = AsyncMock()

    ticket_id = str(uuid.uuid4())
    ticket_row = {
        "id": uuid.UUID(ticket_id),
        "namespace_id": uuid.UUID(_NS_ID),
        "summary": "Audio cutting out in Room 101",
        "status": "open",
        "priority": "high",
        "events": [],
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    sla_row = {
        "id": uuid.uuid4(),
        "ticket_id": uuid.UUID(ticket_id),
        "namespace_id": uuid.UUID(_NS_ID),
        "sla_profile": "standard",
        "breached": False,
        "breach_type": None,
        "paused_intervals": [],
        "updated_at": datetime.now(timezone.utc),
    }
    conn.fetchrow.side_effect = [ticket_row, sla_row]
    engine.pg_pool = _make_mock_pool(conn)

    params = {
        "namespace_id": _NS_ID,
        "customer_scope_id": _CUST_SCOPE,
        "request_id": f"req-test-{uuid.uuid4().hex[:6]}",
        "room_id": "room-101",
        "summary": "Audio cutting out in Room 101",
        "priority": "high",
        "contract_b_covered": True,
    }

    result = await do_raise_service_request(engine, params)

    assert result["request_id"] == params["request_id"]
    assert result["ticket_id"] == ticket_id
    assert result["ticket_status"] == "open"
    assert "support_degraded" not in result
    assert "degradation_reason" not in result


# ---------------------------------------------------------------------------
# 3. Visible Degradation on EngineDisabledError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_customer_portal_service_request_degrades_when_support_disabled() -> None:
    """When Support is disabled for a namespace, do_raise_service_request degrades visibly."""
    engine = NCEEngine()
    engine.modules.disable_for_namespace(_NS_ID, "support")

    params = {
        "namespace_id": _NS_ID,
        "customer_scope_id": _CUST_SCOPE,
        "request_id": f"req-disabled-{uuid.uuid4().hex[:6]}",
        "room_id": "room-101",
        "summary": "Display flickering",
        "contract_b_covered": True,
    }

    result = await do_raise_service_request(engine, params)

    assert result["request_id"] == params["request_id"]
    assert result["support_degraded"] is True
    assert result["degradation_reason"] == "support_engine_disabled"
    assert "ticket_id" not in result


# ---------------------------------------------------------------------------
# 4. Sales Expansion Interest Hand-off & Degradation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_customer_portal_expansion_interest_routes_to_sales() -> None:
    """Inbound expansion interest hooks into Sales engine via engine.modules['sales']."""
    engine = NCEEngine()

    params = {
        "namespace_id": _NS_ID,
        "customer_scope_id": _CUST_SCOPE,
        "room_id": "room-boardroom",
        "category": "video_conferencing",
        "description": "Interested in 4K dual camera setup",
    }

    result = await do_register_expansion_interest(engine, params)

    assert result["status"] == "recorded"
    assert result["sales_lead_routed"] is True
    assert "sales_degraded" not in result


@pytest.mark.asyncio
async def test_customer_portal_expansion_interest_degrades_when_sales_disabled() -> None:
    """When Sales is disabled for a namespace, do_register_expansion_interest degrades visibly."""
    engine = NCEEngine()
    engine.modules.disable_for_namespace(_NS_ID, "sales")

    params = {
        "namespace_id": _NS_ID,
        "customer_scope_id": _CUST_SCOPE,
        "room_id": "room-boardroom",
        "category": "video_conferencing",
        "description": "Interested in 4K dual camera setup",
    }

    result = await do_register_expansion_interest(engine, params)

    assert result["status"] == "recorded"
    assert result["sales_degraded"] is True
    assert result["degradation_reason"] == "sales_engine_disabled"
    assert not result.get("sales_lead_routed", False)


# ---------------------------------------------------------------------------
# 5. AST Seam Ratchet: Zero hasattr probes in customer_portal/actions.py
# ---------------------------------------------------------------------------


def test_ast_ratchet_zero_hasattr_engine_probes() -> None:
    """AST validation: customer_portal/actions.py must contain zero hasattr(..., 'support'|'sales')."""
    tree = ast.parse(_ACTIONS_PY.read_bytes().decode("utf-8"))
    prohibited = {"support", "sales"}
    violations = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        fname = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if fname != "hasattr":
            continue
        if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
            if node.args[1].value in prohibited:
                violations.append((node.lineno, node.args[1].value))

    assert not violations, f"Found prohibited hasattr probes in {_ACTIONS_PY}: {violations}"


# ---------------------------------------------------------------------------
# 6. Anti-Stub Positive Control
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_anti_stub_ignores_hasattr_support() -> None:
    """A legacy mock with .support assigned is ignored when engine.modules is absent."""

    class LegacyStub:
        def __init__(self) -> None:
            self.support = MagicMock()

    stub = LegacyStub()
    params = {
        "namespace_id": _NS_ID,
        "customer_scope_id": _CUST_SCOPE,
        "request_id": "req-anti-stub",
        "room_id": "room-1",
        "summary": "Touchpanel disconnected",
        "contract_b_covered": True,
    }

    result = await do_raise_service_request(stub, params)
    # The action must NOT have called stub.support
    assert stub.support.call_count == 0
    assert result["support_degraded"] is True
    assert result["degradation_reason"] == "engine_modules_unavailable"


# ---------------------------------------------------------------------------
# 7. MCP Error Mapping: PermissionError -> -32600 & ResourcesError -> -32602
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_handler_permission_error_maps_to_invalid_request() -> None:
    """PermissionError (such as IDOR) maps to MCP_INVALID_REQUEST (-32600), NOT -32603."""
    engine = NCEEngine()
    idor_params = {
        "namespace_id": _NS_ID,
        "customer_scope_id": _CUST_SCOPE,
        "target_scope_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        "request_id": "req-idor-fail",
        "summary": "IDOR attempt",
    }

    with pytest.raises(McpError) as exc_info:
        await handle_customer_portal_raise_service_request(engine, idor_params)

    assert exc_info.value.code == MCP_INVALID_REQUEST
    assert "Permission denied" in exc_info.value.message
    assert exc_info.value.data.get("reason") == "permission_denied"


@pytest.mark.asyncio
async def test_mcp_handler_resources_error_maps_to_invalid_params() -> None:
    """ResourcesError (such as ResourceValidationError) maps to MCP_INVALID_PARAMS (-32602), NOT -32603."""

    @mcp_handler
    async def sample_handler(engine: Any, params: dict[str, Any]) -> str:
        raise ResourceValidationError("window with 'starts_at' and 'ends_at' is required.")

    with pytest.raises(McpError) as exc_info:
        await sample_handler(None, {})

    assert exc_info.value.code == MCP_INVALID_PARAMS
    assert "Invalid parameters" in exc_info.value.message


@pytest.mark.asyncio
async def test_missing_method_on_support_module_raises_loudly() -> None:
    """If support_module lacks do_open_ticket, it raises AttributeError rather than silently dropping."""
    engine = NCEEngine()
    empty_support_module = object()

    mock_scoped_registry = {"support": empty_support_module}
    mock_registry = MagicMock()
    mock_registry.for_namespace.return_value = mock_scoped_registry

    engine.modules = mock_registry

    params = {
        "namespace_id": _NS_ID,
        "customer_scope_id": _CUST_SCOPE,
        "request_id": f"req-loud-{uuid.uuid4().hex[:6]}",
        "summary": "Should be loud if method missing",
    }

    with pytest.raises(AttributeError):
        await do_raise_service_request(engine, params)
