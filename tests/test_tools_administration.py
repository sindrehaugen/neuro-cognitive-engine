"""Tests for NCE Tools Dynamic Administration and Dynamic Interception."""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.requests import Request

from nce.a2a import A2AScopeViolationError
from nce.a2a_server import NamespaceContext, _dispatch_skill
from nce.admin_handlers.tools import api_admin_tools, api_admin_tools_toggle
from nce.mcp_stdio_dispatch import execute_call_tool


class MockRedis:
    def __init__(self, data=None):
        self.data = data or {}
        self.hkeys = AsyncMock(
            return_value=[k.encode() if isinstance(k, str) else k for k in self.data.keys()]
        )
        self.hexists = AsyncMock(side_effect=lambda h, k: k in self.data)
        self.hset = AsyncMock(side_effect=self._hset)
        self.hdel = AsyncMock(side_effect=self._hdel)

    def _hset(self, name, key, val):
        self.data[key] = val
        return 1

    def _hdel(self, name, key):
        if key in self.data:
            del self.data[key]
            return 1
        return 0


@pytest.fixture
def mock_engine():
    engine = MagicMock()
    engine.redis_client = None
    engine.pg_pool = MagicMock()
    engine.semantic_search = AsyncMock(return_value=[])
    return engine


@pytest.mark.asyncio
async def test_api_admin_tools_list_all_enabled(mock_engine):
    """Verify listing tools and skills when Redis has no disabled items."""
    redis = MockRedis()
    mock_engine.redis_client = redis

    with patch("nce.admin_state.engine", mock_engine):
        request = Request({"type": "http", "method": "GET", "path": "/api/admin/tools"})
        response = await api_admin_tools(request)

        assert response.status_code == 200
        data = json.loads(response.body.decode())

        # Verify both categories exist
        assert "mcp_tools" in data
        assert "a2a_skills" in data

        # Verify standard elements exist
        mcp_names = {t["name"] for t in data["mcp_tools"]}
        assert "store_memory" in mcp_names
        assert "search_codebase" in mcp_names

        a2a_names = {t["name"] for t in data["a2a_skills"]}
        assert "recall_relevant_context" in a2a_names

        # Verify all default to enabled: True
        for t in data["mcp_tools"]:
            assert t["enabled"] is True
            assert "impact" in t
            assert "description" in t
        for s in data["a2a_skills"]:
            assert s["enabled"] is True
            assert "impact" in s
            assert "description" in s


@pytest.mark.asyncio
async def test_api_admin_tools_list_with_disabled_items(mock_engine):
    """Verify tools marked disabled in Redis are correctly flagged in the response."""
    redis = MockRedis({"store_memory": "1", "recall_relevant_context": "1"})
    mock_engine.redis_client = redis

    with patch("nce.admin_state.engine", mock_engine):
        request = Request({"type": "http", "method": "GET", "path": "/api/admin/tools"})
        response = await api_admin_tools(request)

        assert response.status_code == 200
        data = json.loads(response.body.decode())

        # Check store_memory is disabled
        store_mem = next(t for t in data["mcp_tools"] if t["name"] == "store_memory")
        assert store_mem["enabled"] is False

        # Check search_codebase remains enabled
        search_code = next(t for t in data["mcp_tools"] if t["name"] == "search_codebase")
        assert search_code["enabled"] is True

        # Check recall_relevant_context is disabled
        recall_ctx = next(s for s in data["a2a_skills"] if s["name"] == "recall_relevant_context")
        assert recall_ctx["enabled"] is False


@pytest.mark.asyncio
async def test_api_admin_tools_list_redis_fail_safe(mock_engine):
    """Verify list tools API defaults to all-enabled if Redis lookup throws an exception."""
    redis = MagicMock()
    redis.hkeys = AsyncMock(side_effect=RuntimeError("Redis connection refused"))
    mock_engine.redis_client = redis

    with patch("nce.admin_state.engine", mock_engine):
        request = Request({"type": "http", "method": "GET", "path": "/api/admin/tools"})
        response = await api_admin_tools(request)

        assert response.status_code == 200
        data = json.loads(response.body.decode())
        # Should gracefully fallback to all enabled
        assert all(t["enabled"] is True for t in data["mcp_tools"])
        assert all(s["enabled"] is True for s in data["a2a_skills"])


@pytest.mark.asyncio
async def test_api_admin_tools_toggle_success(mock_engine):
    """Verify toggling tool dynamic state updates Redis hset/hdel correctly."""
    redis = MockRedis()
    mock_engine.redis_client = redis

    async def mock_receive():
        return {
            "type": "http.request",
            "body": b'{"tool_name": "store_memory", "tool_type": "mcp", "enabled": false}',
        }

    with patch("nce.admin_state.engine", mock_engine):
        # Disable tool
        request = Request(
            {"type": "http", "method": "POST", "path": "/api/admin/tools/toggle"},
            receive=mock_receive,
        )
        response = await api_admin_tools_toggle(request)
        assert response.status_code == 200
        data = json.loads(response.body.decode())
        assert data["ok"] is True
        assert "store_memory" in redis.data

        # Enable tool back
        async def mock_receive_enable():
            return {
                "type": "http.request",
                "body": b'{"tool_name": "store_memory", "tool_type": "mcp", "enabled": true}',
            }

        request_enable = Request(
            {"type": "http", "method": "POST", "path": "/api/admin/tools/toggle"},
            receive=mock_receive_enable,
        )
        response_enable = await api_admin_tools_toggle(request_enable)
        assert response_enable.status_code == 200
        assert "store_memory" not in redis.data


@pytest.mark.asyncio
async def test_api_admin_tools_toggle_invalid_requests(mock_engine):
    """Verify validation boundary checks for toggles."""
    redis = MockRedis()
    mock_engine.redis_client = redis

    with patch("nce.admin_state.engine", mock_engine):
        # Invalid tool_type
        async def mock_receive_invalid_type():
            return {
                "type": "http.request",
                "body": b'{"tool_name": "store_memory", "tool_type": "invalid", "enabled": false}',
            }

        request = Request({"type": "http", "method": "POST"}, receive=mock_receive_invalid_type)
        response = await api_admin_tools_toggle(request)
        assert response.status_code == 422
        assert "tool_type must be either" in json.loads(response.body.decode())["error"]

        # Missing body params
        async def mock_receive_missing():
            return {"type": "http.request", "body": b'{"tool_name": "store_memory"}'}

        request_missing = Request({"type": "http", "method": "POST"}, receive=mock_receive_missing)
        response_missing = await api_admin_tools_toggle(request_missing)
        assert response_missing.status_code == 400


@pytest.mark.asyncio
async def test_api_admin_tools_toggle_redis_down(mock_engine):
    """Verify toggle returns 500 error if Redis update fails."""
    redis = MagicMock()
    redis.hset = AsyncMock(side_effect=RuntimeError("Redis connection timeout"))
    mock_engine.redis_client = redis

    async def mock_receive():
        return {
            "type": "http.request",
            "body": b'{"tool_name": "store_memory", "tool_type": "mcp", "enabled": false}',
        }

    with patch("nce.admin_state.engine", mock_engine):
        request = Request({"type": "http", "method": "POST"}, receive=mock_receive)
        response = await api_admin_tools_toggle(request)
        assert response.status_code == 500
        assert "Redis synchronization failed" in json.loads(response.body.decode())["error"]


@pytest.mark.asyncio
async def test_stdio_mcp_dispatch_interception(mock_engine):
    """Verify disabled stdio tools are intercepted and rejected with RPC code -32005."""
    from nce.tool_governance import GOVERNANCE

    redis = MockRedis({"store_memory": "1"})
    mock_engine.redis_client = redis

    # Batch 100: force a live governance refresh so the seeded ``hkeys`` snapshot
    # is read (conftest pre-seeds an INITIALIZED-EMPTY allow-snapshot otherwise).
    GOVERNANCE._snapshot = None
    GOVERNANCE._fetched_at = None

    # Try executing a call to the disabled tool
    results = await execute_call_tool(mock_engine, "store_memory", {})

    # Check that it returns the customized JSON-RPC error response structure
    # Results is expected to be a list containing TextContent representing the error envelope
    assert len(results) == 1
    content = results[0]
    assert hasattr(content, "text")

    data = json.loads(content.text)
    assert data["error"]["code"] == -32005
    assert "disabled by the administrator" in data["error"]["data"]["detail"]


@pytest.mark.asyncio
async def test_stdio_mcp_dispatch_fail_safe(mock_engine):
    """Batch 100: a Redis blip during stdio dispatch must NOT block while the
    last-known-good snapshot is fresh (within STALE_OK) — availability preserved
    without silently un-revoking. The conftest seeds an INITIALIZED-EMPTY
    snapshot, so dispatch proceeds and ``hkeys`` is never even consulted.
    """
    redis = MagicMock()
    redis.hkeys = AsyncMock(side_effect=RuntimeError("Redis down"))
    mock_engine.redis_client = redis

    # We patch enforce_mcp_tool_auth so it doesn't fail on missing keys
    with patch("nce.mcp_stdio_dispatch.enforce_mcp_tool_auth") as mock_auth:
        # Mocking the actual tool execution to succeed or not raise dynamic auth exception
        with patch("nce.mcp_stdio_tools.TOOLS") as mock_tools:
            # Create a mock tool that matches store_memory
            tool = MagicMock()
            tool.name = "store_memory"
            mock_tools.__iter__.return_value = [tool]

            with patch("nce.observability.instrument_tool_call"):
                # Dispatch proceeds past the governance gate (fresh allow-snapshot)
                # and reaches auth enforcement.
                await execute_call_tool(mock_engine, "store_memory", {})
                mock_auth.assert_called_once()
                # Fresh snapshot served without a Redis call — no silent un-revoke risk.
                redis.hkeys.assert_not_awaited()


@pytest.mark.asyncio
async def test_a2a_skill_server_interception(mock_engine):
    """Verify disabled A2A skills are intercepted at the server level, raising A2AScopeViolationError."""
    from nce.tool_governance import GOVERNANCE

    redis = MockRedis({"recall_relevant_context": "1"})
    mock_engine.redis_client = redis

    # Batch 100: force a live governance refresh so the seeded ``hkeys`` snapshot
    # is read (conftest pre-seeds an INITIALIZED-EMPTY allow-snapshot otherwise).
    GOVERNANCE._snapshot = None
    GOVERNANCE._fetched_at = None

    caller_ctx = NamespaceContext(namespace_id=uuid.uuid4(), agent_id="agent-b")
    params = {"query": "hello", "namespace_id": str(uuid.uuid4())}

    with patch("nce.a2a_server._engine", mock_engine):
        with pytest.raises(A2AScopeViolationError) as exc:
            await _dispatch_skill("recall_relevant_context", params, caller_ctx)
        assert "disabled by the administrator" in str(exc.value)


@pytest.mark.asyncio
async def test_a2a_skill_server_fail_safe(mock_engine):
    """Batch 100: a Redis blip during A2A dispatch must NOT block while the
    last-known-good snapshot is fresh (within STALE_OK). The conftest seeds an
    INITIALIZED-EMPTY snapshot, so the skill runs and ``hkeys`` is not consulted.
    """
    redis = MagicMock()
    redis.hkeys = AsyncMock(side_effect=RuntimeError("Redis down"))
    mock_engine.redis_client = redis

    caller_ctx = NamespaceContext(namespace_id=uuid.uuid4(), agent_id="agent-b")
    params = {"query": "hello", "namespace_id": str(uuid.uuid4()), "limit": 5}

    with patch("nce.a2a_server._engine", mock_engine):
        # Skill executes — governance gate served the fresh allow-snapshot.
        await _dispatch_skill("recall_relevant_context", params, caller_ctx)
        mock_engine.semantic_search.assert_called_once()
        redis.hkeys.assert_not_awaited()
