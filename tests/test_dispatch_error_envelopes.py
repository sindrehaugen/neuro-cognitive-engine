"""
tests/test_dispatch_error_envelopes.py

Phase 0 — characterization tests for execute_call_tool error envelopes.

These tests pin the exact JSON-RPC 2.0 error code + message produced by every
error branch in execute_call_tool BEFORE any refactoring touches the routing.
They must keep passing through Phase 2 (registry rewrite) as the behavioral
contract.

Patching strategy
-----------------
* instrument_tool_call  — lazy import inside function body; patch the module attr.
* null_reservation      — lazy import inside function body; patch the module attr.
* _try_cached_mcp_tool_response / _consume_quota_for_mcp_tool
                        — module-level imports from mcp_stdio_rpc into
                          mcp_stdio_dispatch; patch in dispatch namespace.
* enforce_mcp_tool_auth — module-level import from auth into dispatch; patch
                          in dispatch namespace.
* bump_cache_generation / purge_document_cache
                        — lazy imports inside the mutation branch; patch module.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

import nce.mcp_stdio_dispatch as dispatch_mod
from nce.auth import RateLimitError, ScopeError
from nce.mcp_errors import MCP_METHOD_NOT_FOUND, McpError
from nce.mcp_stdio_dispatch import execute_call_tool
from nce.mcp_stdio_rpc import MCP_QUOTA_EXCEEDED_PREFIX

# ---------------------------------------------------------------------------
# Shared infrastructure
# ---------------------------------------------------------------------------


def _parse(result: list) -> dict:
    """Parse the single TextContent response into a plain dict."""
    assert len(result) == 1
    return json.loads(result[0].text)


def _error_code(result: list) -> int:
    return _parse(result)["error"]["code"]


def _error_message(result: list) -> str:
    return _parse(result)["error"]["message"]


def _make_engine(*, disabled_tools: list[str] | None = None) -> MagicMock:
    """Minimal engine mock sufficient for dispatch-level tests.

    Batch 100: governance now reads ``hkeys("nce:tools:disabled")`` via the
    last-known-good cache (not the per-call ``hexists`` of the old fail-open
    check). ``disabled_tools`` seeds that hash.
    """
    engine = MagicMock()
    engine.redis_client = AsyncMock()
    engine.redis_client.hkeys = AsyncMock(
        return_value=[t.encode("utf-8") for t in (disabled_tools or [])]
    )
    engine.redis_client.get = AsyncMock(return_value=None)
    engine.redis_client.setex = AsyncMock()
    engine.pg_pool = MagicMock()
    return engine


@pytest.fixture(autouse=True)
def _patch_infra(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Patch all cross-cutting infrastructure so each test controls exactly one
    variable at a time.
    """

    # 1. instrument_tool_call — lazy import, patch on the observability module
    @asynccontextmanager
    async def _noop_instrument(_name: str):
        yield

    monkeypatch.setattr("nce.observability.instrument_tool_call", _noop_instrument)

    # 2. null_reservation — lazy import, patch on the quotas module
    _mock_res = AsyncMock()
    _mock_res.rollback = AsyncMock()
    monkeypatch.setattr("nce.quotas.null_reservation", lambda: _mock_res)

    # 3. _try_cached_mcp_tool_response — imported into dispatch namespace
    monkeypatch.setattr(
        dispatch_mod,
        "_try_cached_mcp_tool_response",
        AsyncMock(return_value=(None, None)),
    )

    # 4. _consume_quota_for_mcp_tool — imported into dispatch namespace
    _mock_quota = AsyncMock()
    _mock_quota.rollback = AsyncMock()
    monkeypatch.setattr(
        dispatch_mod,
        "_consume_quota_for_mcp_tool",
        AsyncMock(return_value=_mock_quota),
    )

    # 5. enforce_mcp_tool_auth — imported into dispatch namespace
    monkeypatch.setattr(dispatch_mod, "enforce_mcp_tool_auth", lambda _n, _a: None)

    # 6. bump_cache_generation / purge_document_cache
    monkeypatch.setattr(dispatch_mod, "bump_cache_generation", AsyncMock(return_value=1))
    monkeypatch.setattr(dispatch_mod, "purge_document_cache", AsyncMock(return_value=None))


# ---------------------------------------------------------------------------
# Case 1: engine is None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_engine_none_returns_internal_error() -> None:
    result = await execute_call_tool(None, "store_memory", {})
    assert _error_code(result) == -32603
    assert _error_message(result) == "Internal error"


# ---------------------------------------------------------------------------
# Case 2: Redis admin-disable toggle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_redis_toggle_disabled_returns_scope_forbidden() -> None:
    # Force a live governance refresh so the seeded ``hkeys`` snapshot is read
    # (the conftest pre-seeds an INITIALIZED-EMPTY allow-snapshot otherwise).
    from nce.tool_governance import GOVERNANCE

    GOVERNANCE._snapshot = None
    GOVERNANCE._fetched_at = None

    engine = _make_engine(disabled_tools=["store_memory"])
    result = await execute_call_tool(engine, "store_memory", {})
    assert _error_code(result) == -32005
    assert (
        "disabled" in _parse(result)["error"].get("data", {}).get("detail", "").lower()
        or _error_message(result) == "Scope forbidden"
    )


# ---------------------------------------------------------------------------
# Case 3: enforce_mcp_tool_auth raises ScopeError (inner catch → -32005)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scope_error_from_auth_returns_scope_forbidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _make_engine()
    exc = ScopeError(required_scope="memory:write", reason="missing_scope")
    monkeypatch.setattr(
        dispatch_mod, "enforce_mcp_tool_auth", lambda _n, _a: (_ for _ in ()).throw(exc)
    )

    result = await execute_call_tool(engine, "store_memory", {})
    assert _error_code(result) == -32005
    assert _error_message(result) == "Scope forbidden"


# ---------------------------------------------------------------------------
# Case 4: UnknownToolError (unknown tool name → McpError → -32601)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_tool_returns_method_not_found() -> None:
    engine = _make_engine()
    result = await execute_call_tool(engine, "does_not_exist_tool", {})
    assert _error_code(result) == MCP_METHOD_NOT_FOUND  # -32601
    assert "does_not_exist_tool" in _error_message(result)


# ---------------------------------------------------------------------------
# Case 5: McpError from a handler propagates code as-is
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_error_from_handler_propagates_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _make_engine()
    from nce import memory_mcp_handlers

    monkeypatch.setattr(
        memory_mcp_handlers,
        "handle_store_memory",
        AsyncMock(side_effect=McpError(-32099, "custom mcp error")),
    )
    result = await execute_call_tool(engine, "store_memory", {})
    assert _error_code(result) == -32099
    assert _error_message(result) == "custom mcp error"


# ---------------------------------------------------------------------------
# Case 6: ScopeError from a handler (propagated by @mcp_handler) → -32005
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scope_error_from_handler_returns_scope_forbidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _make_engine()
    from nce import memory_mcp_handlers

    monkeypatch.setattr(
        memory_mcp_handlers,
        "handle_store_memory",
        AsyncMock(side_effect=ScopeError(required_scope="memory:write", reason="forbidden")),
    )
    result = await execute_call_tool(engine, "store_memory", {})
    assert _error_code(result) == -32005
    assert _error_message(result) == "Scope forbidden"


# ---------------------------------------------------------------------------
# Case 7: RateLimitError from a handler → -32029
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limit_error_from_handler_returns_rate_limited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _make_engine()
    from nce import memory_mcp_handlers

    monkeypatch.setattr(
        memory_mcp_handlers,
        "handle_store_memory",
        AsyncMock(side_effect=RateLimitError("ns:store_memory", limit=100, period=60)),
    )
    result = await execute_call_tool(engine, "store_memory", {})
    assert _error_code(result) == -32029
    assert _error_message(result) == "Rate limit exceeded"


# ---------------------------------------------------------------------------
# Case 8: ValueError starting with MCP_QUOTA_EXCEEDED_PREFIX → -32013
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quota_exceeded_prefix_returns_quota_exceeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _make_engine()
    monkeypatch.setattr(
        dispatch_mod,
        "_consume_quota_for_mcp_tool",
        AsyncMock(side_effect=ValueError(f"{MCP_QUOTA_EXCEEDED_PREFIX}: hard limit reached")),
    )
    result = await execute_call_tool(engine, "semantic_search", {})
    assert _error_code(result) == -32013
    assert _error_message(result) == "Resource quota exceeded"


# ---------------------------------------------------------------------------
# Case 9: ValueError starting with "Rate limit exceeded" → -32029
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_value_error_rate_limit_string_returns_rate_limited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _make_engine()
    monkeypatch.setattr(
        dispatch_mod,
        "_consume_quota_for_mcp_tool",
        AsyncMock(side_effect=ValueError("Rate limit exceeded for namespace abc")),
    )
    result = await execute_call_tool(engine, "semantic_search", {})
    assert _error_code(result) == -32029
    assert _error_message(result) == "Rate limit exceeded"


# ---------------------------------------------------------------------------
# Case 11: Generic ValueError → -32602
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generic_value_error_returns_invalid_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _make_engine()
    monkeypatch.setattr(
        dispatch_mod,
        "_consume_quota_for_mcp_tool",
        AsyncMock(side_effect=ValueError("namespace_id is required")),
    )
    result = await execute_call_tool(engine, "semantic_search", {})
    assert _error_code(result) == -32602
    assert _error_message(result) == "Invalid params"


# ---------------------------------------------------------------------------
# Case 12: Cache hit — handler is never called, cached payload returned
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_hit_returns_cached_payload_without_calling_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp.types import TextContent

    engine = _make_engine()
    cached_payload = [TextContent(type="text", text='{"status": "cached"}')]

    monkeypatch.setattr(
        dispatch_mod,
        "_try_cached_mcp_tool_response",
        AsyncMock(return_value=(cached_payload, "cache-key-123")),
    )

    handler_called = False
    from nce import memory_mcp_handlers

    async def _handler(*_a: object, **_k: object) -> str:
        nonlocal handler_called
        handler_called = True
        return '{"status": "live"}'

    monkeypatch.setattr(memory_mcp_handlers, "handle_semantic_search", _handler)

    result = await execute_call_tool(engine, "semantic_search", {})
    assert not handler_called, "Handler must not be called on cache hit"
    assert json.loads(result[0].text)["status"] == "cached"


# ---------------------------------------------------------------------------
# Case 13: Mutation tool triggers bump_cache_generation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mutation_tool_bumps_cache_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _make_engine()
    bump_spy = AsyncMock(return_value=2)
    monkeypatch.setattr(dispatch_mod, "bump_cache_generation", bump_spy)

    from nce import memory_mcp_handlers

    monkeypatch.setattr(
        memory_mcp_handlers,
        "handle_store_memory",
        AsyncMock(return_value='{"status": "ok"}'),
    )

    result = await execute_call_tool(
        engine, "store_memory", {"namespace_id": "ns-1", "content": "test"}
    )
    bump_spy.assert_awaited_once_with(engine.redis_client)
    assert json.loads(result[0].text)["status"] == "ok"


# ---------------------------------------------------------------------------
# Case 14: QuotaExceededError raised directly → -32013
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quota_exceeded_error_returns_quota_exceeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nce.quotas import QuotaExceededError

    engine = _make_engine()
    monkeypatch.setattr(
        dispatch_mod,
        "_consume_quota_for_mcp_tool",
        AsyncMock(side_effect=QuotaExceededError("hard limit reached")),
    )
    result = await execute_call_tool(engine, "semantic_search", {})
    assert _error_code(result) == -32013
    assert _error_message(result) == "Resource quota exceeded"
