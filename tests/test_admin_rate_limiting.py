import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nce.auth import _IN_MEMORY_LIMITS, RateLimitError, admin_rate_limit


@pytest.mark.asyncio
async def test_rate_limit_error_properties():
    """Verify custom RateLimitError properties and message format."""
    err = RateLimitError("tenant:1234", limit=10, period=60)
    assert err.key == "tenant:1234"
    assert err.limit == 10
    assert err.period == 60
    assert "max 10 requests per 60s for key suffix 'tenant:1234'" in str(err)


class MockEngine:
    def __init__(self, redis_client=None):
        self.redis_client = redis_client


@pytest.mark.asyncio
async def test_admin_rate_limit_within_limit():
    """Verify that requests below the limit pass through successfully using RAM fallback (no Redis)."""
    engine = MockEngine()  # No Redis client

    # Reset in-memory limit tracking
    _IN_MEMORY_LIMITS.clear()

    @admin_rate_limit(limit=3, period=10)
    async def sample_tool(engine_inst, arguments):
        return "success"

    # 3 allowed requests
    for _ in range(3):
        res = await sample_tool(engine, {"val": "test"})
        assert res == "success"


@pytest.mark.asyncio
async def test_admin_rate_limit_exceeded_local_fallback():
    """Verify rate limit is triggered on local RAM fallback when limit exceeded."""
    engine = MockEngine()
    _IN_MEMORY_LIMITS.clear()

    @admin_rate_limit(limit=3, period=10)
    async def sample_tool(engine_inst, arguments):
        return "success"

    # 3 allowed requests
    for _ in range(3):
        await sample_tool(engine, {})

    # 4th request must raise RateLimitError
    with pytest.raises(RateLimitError) as exc_info:
        await sample_tool(engine, {})

    assert exc_info.value.limit == 3
    assert exc_info.value.period == 10
    assert exc_info.value.key == "tool:sample_tool"


@pytest.mark.asyncio
async def test_admin_rate_limit_key_resolution():
    """Verify key suffix resolution checks namespace_id, then admin_identity, then tool name."""
    engine = MockEngine()
    _IN_MEMORY_LIMITS.clear()

    ns_calls = []
    id_calls = []
    tool_calls = []

    @admin_rate_limit(limit=10, period=60)
    async def sample_tool(engine_inst, arguments, admin_identity=None):
        return "ok"

    # 1. Namespace ID
    with patch(
        "nce.auth._check_in_memory_rate_limit",
        side_effect=lambda k, lim, p: ns_calls.append(k) or True,
    ):
        await sample_tool(engine, {"namespace_id": "ns-123"})
    assert any("tenant:ns-123" in k for k in ns_calls)

    # 2. Admin Identity
    with patch(
        "nce.auth._check_in_memory_rate_limit",
        side_effect=lambda k, lim, p: id_calls.append(k) or True,
    ):
        await sample_tool(engine, {"admin_identity": "support-agent"})
    assert any("identity:support-agent" in k for k in id_calls)

    # 3. Tool Name fallback
    with patch(
        "nce.auth._check_in_memory_rate_limit",
        side_effect=lambda k, lim, p: tool_calls.append(k) or True,
    ):
        await sample_tool(engine, {})
    assert any("tool:sample_tool" in k for k in tool_calls)


@pytest.mark.asyncio
async def test_admin_rate_limit_redis_success():
    """Verify sliding-window rate limiting uses Redis Lua script when available."""
    mock_redis = MagicMock()
    mock_redis.eval = AsyncMock(return_value=1)

    engine = MockEngine(redis_client=mock_redis)

    @admin_rate_limit(limit=3, period=60)
    async def sample_tool(engine_inst, arguments):
        return "redis_ok"

    res = await sample_tool(engine, {})
    assert res == "redis_ok"

    mock_redis.eval.assert_called_once()


@pytest.mark.asyncio
async def test_admin_rate_limit_redis_exceeded():
    """Verify Redis Lua script triggers RateLimitError when limit exceeded."""
    mock_redis = MagicMock()
    mock_redis.eval = AsyncMock(return_value=0)

    engine = MockEngine(redis_client=mock_redis)

    @admin_rate_limit(limit=3, period=60)
    async def sample_tool(engine_inst, arguments):
        return "redis_ok"

    with pytest.raises(RateLimitError):
        await sample_tool(engine, {})

    mock_redis.eval.assert_called_once()


@pytest.mark.asyncio
async def test_admin_rate_limit_redis_failure_falls_back_to_ram():
    """Verify that if Redis Lua eval fails, we fall back to local in-memory limit enforcement."""
    mock_redis = MagicMock()
    mock_redis.eval = AsyncMock(side_effect=Exception("Redis connection refused"))

    engine = MockEngine(redis_client=mock_redis)
    _IN_MEMORY_LIMITS.clear()

    @admin_rate_limit(limit=2, period=10)
    async def sample_tool(engine_inst, arguments):
        return "fallback_ok"

    # Should fall back to in-memory, allowing first 2 but blocking 3rd
    assert await sample_tool(engine, {}) == "fallback_ok"
    assert await sample_tool(engine, {}) == "fallback_ok"

    with pytest.raises(RateLimitError):
        await sample_tool(engine, {})


@pytest.mark.asyncio
async def test_server_call_tool_translates_rate_limit_error():
    """Verify server.py call_tool intercepts RateLimitError and converts it to ValueError with -32029."""
    from server import call_tool

    # Inject mock tool into server context or mock dispatch
    engine = MockEngine()
    engine.pg_pool = MagicMock()

    mock_reservation = AsyncMock()
    mock_reservation.rollback = AsyncMock()

    with patch("server.engine", engine):
        with patch("nce.quotas.consume_for_tool", AsyncMock(return_value=mock_reservation)):
            with patch(
                "nce.admin_mcp_handlers.handle_get_health",
                side_effect=RateLimitError("test", 1, 60),
            ):
                # call_tool now returns JSON-RPC 2.0 error responses as TextContent
                # instead of raising ValueError.
                result = await call_tool(
                    "get_health",
                    {"admin_api_key": os.environ["NCE_ADMIN_API_KEY"]},
                )

            assert len(result) == 1
            payload = json.loads(result[0].text)
            assert payload["jsonrpc"] == "2.0"
            assert payload["error"]["code"] == -32029
            assert "Rate limit exceeded" in payload["error"]["message"]
