"""
tests/unit/test_customer_portal_mcp_auth_ratchet.py
===================================================
Security boundary & ratchet tests for Customer Portal internal MCP tools.

Mandates (Charter §13 / Wave T-6):
  1. All 9 customer_portal_* MCP tools are admin_only=True in TOOL_REGISTRY.
  2. InputSchema for all 9 tools requires customer_scope_id.
  3. Invoking any MCP handler without customer_scope_id fails closed (ValueError).
  4. Nil-UUID sentinel (00000000-0000-0000-0000-000000000000) is rejected.
  5. Operator assertion emits WORM audit event `customer_scope_impersonated`.
  6. evaluate_customer_scope_access fails closed on missing/nil scope and IDOR.
  7. enforce_customer_scope eradicates caller-controlled self-comparison tautologies.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from nce.auth import resolve_customer_scope
from nce.mcp_errors import MCP_INVALID_PARAMS, McpError
from nce.mcp_stdio_tools import TOOLS
from nce.tool_registry import ADMIN_ONLY_TOOLS
from nce.vertical_modules.customer_portal.auth import (
    NIL_UUID,
    enforce_customer_scope,
    evaluate_customer_scope_access,
)
from nce.vertical_modules.customer_portal.mcp_handlers import (
    _resolve_portal_mcp_scope,
    handle_customer_portal_advisor_answer,
    handle_customer_portal_asset_register,
    handle_customer_portal_list_documents,
    handle_customer_portal_list_invoices,
    handle_customer_portal_raise_service_request,
    handle_customer_portal_register_expansion_interest,
    handle_customer_portal_room_overview,
    handle_customer_portal_room_tracker,
    handle_customer_portal_sla_status,
)

_NINE_CUSTOMER_PORTAL_TOOLS: frozenset[str] = frozenset(
    {
        "customer_portal_room_tracker",
        "customer_portal_room_overview",
        "customer_portal_asset_register",
        "customer_portal_list_documents",
        "customer_portal_sla_status",
        "customer_portal_list_invoices",
        "customer_portal_advisor_answer",
        "customer_portal_raise_service_request",
        "customer_portal_register_expansion_interest",
    }
)

_MCP_HANDLERS = [
    handle_customer_portal_room_tracker,
    handle_customer_portal_room_overview,
    handle_customer_portal_asset_register,
    handle_customer_portal_list_documents,
    handle_customer_portal_sla_status,
    handle_customer_portal_list_invoices,
    handle_customer_portal_advisor_answer,
    handle_customer_portal_raise_service_request,
    handle_customer_portal_register_expansion_interest,
]


class DummyCallerContext:
    def __init__(self, external_scope_id: Any = None):
        self.external_scope_id = external_scope_id


class DummyRequest:
    def __init__(
        self,
        headers: dict[str, str] | None = None,
        state_attrs: dict[str, Any] | None = None,
    ):
        self.headers = headers or {}
        self.scope: dict[str, Any] = {}
        self.state = MagicMock()
        self.query_params: dict[str, str] = {}
        if state_attrs:
            for k, v in state_attrs.items():
                setattr(self.state, k, v)


# ---------------------------------------------------------------------------
# 1. Tool Registry & Schema Ratchets
# ---------------------------------------------------------------------------


def test_customer_portal_tools_in_admin_only_registry() -> None:
    """All 9 customer_portal_* tools must be registered as admin_only=True."""
    assert _NINE_CUSTOMER_PORTAL_TOOLS <= ADMIN_ONLY_TOOLS, (
        f"Missing admin_only tools: {_NINE_CUSTOMER_PORTAL_TOOLS - ADMIN_ONLY_TOOLS}"
    )


def test_customer_portal_mcp_schema_requires_customer_scope_id() -> None:
    """MCP inputSchema for all 9 tools must explicitly list customer_scope_id in required."""
    tools_by_name = {tool.name: tool for tool in TOOLS if tool.name in _NINE_CUSTOMER_PORTAL_TOOLS}
    assert len(tools_by_name) == 9

    for tool_name, tool in tools_by_name.items():
        req = tool.inputSchema.get("required", [])
        assert "namespace_id" in req, f"{tool_name} missing namespace_id in required"
        assert "customer_scope_id" in req, f"{tool_name} missing customer_scope_id in required"


# ---------------------------------------------------------------------------
# 2. MCP Handler Input Validation & Scope Enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("handler", _MCP_HANDLERS)
async def test_mcp_handler_missing_scope_rejected(handler: Any) -> None:
    """Calling any MCP handler without customer_scope_id raises McpError(MCP_INVALID_PARAMS)."""
    params = {
        "namespace_id": str(uuid4()),
        "room_id": "room-1",
        "summary": "Service needed",
        "query": "Status query",
        "description": "Expansion info",
    }
    with pytest.raises(McpError) as exc_info:
        await handler(None, params)
    assert exc_info.value.code == MCP_INVALID_PARAMS


@pytest.mark.asyncio
@pytest.mark.parametrize("handler", _MCP_HANDLERS)
async def test_mcp_handler_nil_uuid_rejected(handler: Any) -> None:
    """Calling any MCP handler with nil-UUID sentinel raises McpError(MCP_INVALID_PARAMS)."""
    params = {
        "namespace_id": str(uuid4()),
        "customer_scope_id": "00000000-0000-0000-0000-000000000000",
        "room_id": "room-1",
        "summary": "Service needed",
        "query": "Status query",
        "description": "Expansion info",
    }
    with pytest.raises(McpError) as exc_info:
        await handler(None, params)
    assert exc_info.value.code == MCP_INVALID_PARAMS


@pytest.mark.asyncio
@pytest.mark.parametrize("handler", _MCP_HANDLERS)
async def test_mcp_handler_invalid_uuid_rejected(handler: Any) -> None:
    """Calling any MCP handler with malformed UUID string raises McpError(MCP_INVALID_PARAMS)."""
    params = {
        "namespace_id": str(uuid4()),
        "customer_scope_id": "not-a-valid-uuid",
        "room_id": "room-1",
        "summary": "Service needed",
        "query": "Status query",
        "description": "Expansion info",
    }
    with pytest.raises(McpError) as exc_info:
        await handler(None, params)
    assert exc_info.value.code == MCP_INVALID_PARAMS


@pytest.mark.asyncio
async def test_resolve_portal_mcp_scope_helper_directly() -> None:
    """Direct invocations of _resolve_portal_mcp_scope raise ValueError on invalid/missing input."""
    with pytest.raises(ValueError, match="customer_scope_id"):
        await _resolve_portal_mcp_scope(None, {"namespace_id": str(uuid4())})

    with pytest.raises(ValueError, match="nil UUID sentinel"):
        await _resolve_portal_mcp_scope(
            None,
            {
                "namespace_id": str(uuid4()),
                "customer_scope_id": "00000000-0000-0000-0000-000000000000",
            },
        )

    with pytest.raises(ValueError, match="Invalid customer_scope_id"):
        await _resolve_portal_mcp_scope(
            None,
            {"namespace_id": str(uuid4()), "customer_scope_id": "not-a-uuid"},
        )


# ---------------------------------------------------------------------------
# 3. Impersonation WORM Audit Event Emission
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_customer_scope_emits_audit_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """Asserting customer_scope_id emits WORM customer_scope_impersonated event."""
    target_scope = uuid4()
    ns_id = uuid4()

    fake_conn = AsyncMock()
    tx_ctx = AsyncMock()
    tx_ctx.__aenter__.return_value = fake_conn
    tx_ctx.__aexit__.return_value = False
    fake_conn.transaction = MagicMock(return_value=tx_ctx)

    acquire_ctx = AsyncMock()
    acquire_ctx.__aenter__.return_value = fake_conn
    acquire_ctx.__aexit__.return_value = False

    fake_pool = MagicMock()
    fake_pool.acquire = MagicMock(return_value=acquire_ctx)
    fake_engine = MagicMock()
    fake_engine.pg_pool = fake_pool

    appended_events: list[dict[str, Any]] = []

    async def _mock_append_event(conn: Any, **kwargs: Any) -> Any:
        appended_events.append(kwargs)
        mock_result = MagicMock()
        mock_result.event_id = uuid4()
        return mock_result

    from nce import event_log

    monkeypatch.setattr(event_log, "append_event", _mock_append_event)

    result = await resolve_customer_scope(
        None,
        {"customer_scope_id": str(target_scope), "namespace_id": str(ns_id)},
        namespace_id=ns_id,
        engine=fake_engine,
        reason="test_impersonation",
    )

    assert result == str(target_scope)
    assert len(appended_events) == 1
    ev = appended_events[0]
    assert ev["event_type"] == "customer_scope_impersonated"
    assert ev["agent_id"] == "internal_admin"
    assert ev["namespace_id"] == ns_id
    assert ev["params"]["customer_scope_id"] == str(target_scope)
    assert ev["params"]["caller_identity"] == "internal_admin"
    assert ev["params"]["reason"] == "test_impersonation"


@pytest.mark.asyncio
async def test_mcp_handler_invocation_audited(monkeypatch: pytest.MonkeyPatch) -> None:
    """All MCP handlers audit their scope assertion via resolve_customer_scope."""
    target_scope = uuid4()
    ns_id = uuid4()

    fake_conn = AsyncMock()
    tx_ctx = AsyncMock()
    tx_ctx.__aenter__.return_value = fake_conn
    tx_ctx.__aexit__.return_value = False
    fake_conn.transaction = MagicMock(return_value=tx_ctx)

    acquire_ctx = AsyncMock()
    acquire_ctx.__aenter__.return_value = fake_conn
    acquire_ctx.__aexit__.return_value = False

    fake_pool = MagicMock()
    fake_pool.acquire = MagicMock(return_value=acquire_ctx)
    fake_engine = MagicMock()
    fake_engine.pg_pool = fake_pool

    appended_events: list[dict[str, Any]] = []

    async def _mock_append_event(conn: Any, **kwargs: Any) -> Any:
        appended_events.append(kwargs)
        mock_result = MagicMock()
        mock_result.event_id = uuid4()
        return mock_result

    from nce import event_log

    monkeypatch.setattr(event_log, "append_event", _mock_append_event)

    params = {
        "namespace_id": str(ns_id),
        "customer_scope_id": str(target_scope),
        "room_id": "room-101",
    }
    raw_res = await handle_customer_portal_room_tracker(fake_engine, params)
    data = json.loads(raw_res)
    assert "room_id" in data

    assert len(appended_events) == 1
    ev = appended_events[0]
    assert ev["event_type"] == "customer_scope_impersonated"
    assert ev["params"]["customer_scope_id"] == str(target_scope)
    assert ev["params"]["reason"] == "mcp_customer_portal_operator"


# ---------------------------------------------------------------------------
# 4. Verified Principal Context Resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_customer_scope_from_verified_caller_context() -> None:
    """Verified principal context on caller_ctx takes precedence without auditing."""
    verified_scope = uuid4()
    spoofed_scope = uuid4()

    req = DummyRequest()
    req.state.caller_ctx = DummyCallerContext(external_scope_id=verified_scope)

    result = await resolve_customer_scope(req, str(spoofed_scope))
    assert result == str(verified_scope)


@pytest.mark.asyncio
async def test_resolve_customer_scope_from_request_state() -> None:
    """Direct customer_scope_id on request.state is returned as verified scope."""
    verified_scope = uuid4()

    req = DummyRequest(state_attrs={"customer_scope_id": verified_scope})
    req.state.caller_ctx = None

    result = await resolve_customer_scope(req, None)
    assert result == str(verified_scope)


# ---------------------------------------------------------------------------
# 5. Isolation Policy & Self-Comparison Hardening
# ---------------------------------------------------------------------------


def test_evaluate_customer_scope_access_semantics() -> None:
    """evaluate_customer_scope_access fails closed on unset, nil, and mismatched scopes."""
    scope_a = uuid4()
    scope_b = uuid4()

    assert not evaluate_customer_scope_access(None, scope_a)
    assert not evaluate_customer_scope_access(scope_a, None)
    assert not evaluate_customer_scope_access(NIL_UUID, scope_a)
    assert not evaluate_customer_scope_access(scope_a, NIL_UUID)
    assert not evaluate_customer_scope_access(scope_a, scope_b)
    assert evaluate_customer_scope_access(scope_a, scope_a)


def test_enforce_customer_scope_eradicates_tautological_self_comparison() -> None:
    """enforce_customer_scope requires authoritative scope and refuses IDOR."""
    scope_a = uuid4()
    scope_b = uuid4()

    # Deny-when-unset
    with pytest.raises(PermissionError, match="IDOR"):
        enforce_customer_scope({})

    with pytest.raises(PermissionError, match="IDOR"):
        enforce_customer_scope({"customer_scope_id": None})

    with pytest.raises(PermissionError, match="IDOR"):
        enforce_customer_scope({"customer_scope_id": str(NIL_UUID)})

    # IDOR cross-scope mismatch
    with pytest.raises(PermissionError, match="IDOR"):
        enforce_customer_scope(
            {
                "customer_scope_id": str(scope_a),
                "target_scope_id": str(scope_b),
            }
        )

    # Valid authoritative scope
    valid_res = enforce_customer_scope({"customer_scope_id": str(scope_a)})
    assert valid_res == str(scope_a)

    # Valid matching target scope
    valid_matching = enforce_customer_scope(
        {
            "customer_scope_id": str(scope_a),
            "target_scope_id": str(scope_a),
        }
    )
    assert valid_matching == str(scope_a)
