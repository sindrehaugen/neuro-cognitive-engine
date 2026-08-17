"""Unit tests for Batch 3 changes covering A2A, MCP Cache, and Robustness features.

Files covered:
- nce/a2a_server.py
- nce/mcp_stdio_dispatch.py
- nce/mcp_stdio_rpc.py
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp.types import TextContent

import nce.a2a_server as a2a_server
from nce.auth import NamespaceContext
from nce.config import cfg
from nce.mcp_stdio_dispatch import execute_call_tool
from nce.mcp_stdio_rpc import _try_cached_mcp_tool_response
from nce.tool_registry import TOOL_REGISTRY, ToolSpec


# ---------------------------------------------------------------------------
# 1. get_cognitive_state parameter forwarding
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_get_cognitive_state_parameter_forwarding() -> None:
    """Verify get_cognitive_state forwards parameter `n` as `limit=n` to recall_recent()."""
    mock_engine = MagicMock()
    mock_engine.recall_recent = AsyncMock(return_value=["memory1", "memory2"])
    mock_engine.redis_client = None

    with patch("nce.a2a_server._engine", mock_engine):
        caller_ctx = MagicMock(spec=NamespaceContext)
        caller_ctx.agent_id = "default-agent"
        # NamespaceContext is a pydantic model, so its fields are invisible to
        # spec= — set the contractor-scoping field the dispatcher now reads.
        caller_ctx.principal_kind = "employee"

        # Scenario A: Explicit n value
        params_with_n = {
            "namespace_id": "00000000-0000-4000-8000-000000000001",
            "agent_id": "test-agent",
            "n": 25,
            "user_id": "test-user",
        }

        result = await a2a_server._dispatch_skill("get_cognitive_state", params_with_n, caller_ctx)

        assert result["n_requested"] == 25
        assert result["context"] == ["memory1", "memory2"]

        mock_engine.recall_recent.assert_awaited_once_with(
            namespace_id="00000000-0000-4000-8000-000000000001",
            agent_id="test-agent",
            limit=25,
            user_id="test-user",
            session_id="test-agent",
        )

        # Scenario B: Default n value when not provided
        params_no_n = {
            "namespace_id": "00000000-0000-4000-8000-000000000001",
            "agent_id": "test-agent",
        }
        mock_engine.recall_recent.reset_mock()

        await a2a_server._dispatch_skill("get_cognitive_state", params_no_n, caller_ctx)

        mock_engine.recall_recent.assert_awaited_once_with(
            namespace_id="00000000-0000-4000-8000-000000000001",
            agent_id="test-agent",
            limit=10,  # Default fallback logic in get_cognitive_state
            user_id="default",
            session_id="test-agent",
        )


# ---------------------------------------------------------------------------
# 2. Bounded tasks store
# ---------------------------------------------------------------------------
def test_tasks_is_bounded_dict_and_evicts() -> None:
    """Verify _tasks uses BoundedDict and evicts oldest item when maxlen exceeded."""
    from nce.a2a_server import BoundedDict, _tasks

    assert isinstance(_tasks, BoundedDict)

    orig_maxlen = _tasks.maxlen
    try:
        # 1. Baseline FIFO eviction check
        _tasks.maxlen = 2
        _tasks.clear()

        _tasks["task1"] = {"id": "task1"}
        _tasks["task2"] = {"id": "task2"}
        assert len(_tasks) == 2
        assert "task1" in _tasks
        assert "task2" in _tasks

        # Adding third item should evict the oldest (task1)
        _tasks["task3"] = {"id": "task3"}
        assert len(_tasks) == 2
        assert "task1" not in _tasks
        assert "task2" in _tasks
        assert "task3" in _tasks

        # 2. Rapid invocation simulation (1000 insertions)
        _tasks.maxlen = 10
        _tasks.clear()
        for i in range(1000):
            _tasks[f"task_rapid_{i}"] = {"id": f"task_rapid_{i}"}
            # Verify capacity never exceeded during rapid ingestion
            assert len(_tasks) <= 10

        # Verify only the last 10 are retained
        assert len(_tasks) == 10
        for i in range(990):
            assert f"task_rapid_{i}" not in _tasks
        for i in range(990, 1000):
            assert f"task_rapid_{i}" in _tasks

        # 3. Update existing key test (should not evict or change size)
        assert "task_rapid_990" in _tasks
        _tasks["task_rapid_990"] = {"id": "task_rapid_990_updated"}
        assert len(_tasks) == 10
        assert _tasks["task_rapid_990"] == {"id": "task_rapid_990_updated"}
        assert "task_rapid_991" in _tasks

        # Verify order/FIFO after update: OrderedDict preserves insertion order.
        keys_list = list(_tasks.keys())
        assert keys_list[0] == "task_rapid_990"  # still the first one in insertion order

        # Now if we add another item task_rapid_1000, the oldest item (task_rapid_990) should be evicted
        _tasks["task_rapid_1000"] = {"id": "task_rapid_1000"}
        assert "task_rapid_990" not in _tasks
        assert len(_tasks) == 10
    finally:
        # Reset task store state to avoid side effects on other tests
        _tasks.maxlen = orig_maxlen
        _tasks.clear()


# ---------------------------------------------------------------------------
# 3. Runtime check for uninitialized engine
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_dispatch_skill_raises_runtime_error_if_engine_none() -> None:
    """Verify _dispatch_skill raises a RuntimeError if _engine is None."""
    with patch("nce.a2a_server._engine", None):
        caller_ctx = MagicMock(spec=NamespaceContext)
        with pytest.raises(RuntimeError, match="engine not initialized"):
            await a2a_server._dispatch_skill("recall_relevant_context", {}, caller_ctx)


# ---------------------------------------------------------------------------
# 4. Bounded concurrency in archive_session
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_archive_session_bounded_concurrency() -> None:
    """Verify archive_session performs concurrent writes with asyncio.Semaphore(4)
    and validates the concurrent resolution profiles.
    """
    start_times = []
    end_times = []

    active_calls = 0
    max_concurrent_calls = 0
    lock = asyncio.Lock()

    async def mock_store_memory(req: any) -> dict[str, str]:
        nonlocal active_calls, max_concurrent_calls

        # Capture start
        start = asyncio.get_running_loop().time()
        start_times.append(start)

        async with lock:
            active_calls += 1
            if active_calls > max_concurrent_calls:
                max_concurrent_calls = active_calls

        # Sleep to simulate async boundary and allow overlaps
        await asyncio.sleep(0.05)

        async with lock:
            active_calls -= 1

        # Capture end
        end = asyncio.get_running_loop().time()
        end_times.append(end)

        return {"payload_ref": f"mocked-ref-{req.content}"}

    mock_engine = MagicMock()
    mock_engine.store_memory = AsyncMock(side_effect=mock_store_memory)
    mock_engine.redis_client = None

    with patch("nce.a2a_server._engine", mock_engine):
        with patch("asyncio.Semaphore", wraps=asyncio.Semaphore) as mock_sem:
            caller_ctx = MagicMock(spec=NamespaceContext)
            caller_ctx.principal_kind = "employee"
            # 12 memories (three batches of 4)
            memories = [{"content": f"mem-{i}", "summary": f"sum-{i}"} for i in range(12)]

            params = {
                "namespace_id": "00000000-0000-4000-8000-000000000001",
                "agent_id": "test-agent",
                "memories": memories,
            }

            result = await a2a_server._dispatch_skill("archive_session", params, caller_ctx)

            # Assert semaphore limit of 4 was instantiated
            mock_sem.assert_called_with(4)

            # Verify store_memory was called 12 times
            assert mock_engine.store_memory.call_count == 12

            # Verify concurrency limit was active (<= 4) and reached exactly 4
            assert max_concurrent_calls == 4

            # Check overlap profiles by sorting start/end events and checking peak overlap
            events = [(t_start, 1) for t_start in start_times] + [
                (t_end, -1) for t_end in end_times
            ]
            events.sort(key=lambda x: (x[0], x[1]))

            current_overlap = 0
            peak_overlap = 0
            for time, change in events:
                current_overlap += change
                if current_overlap > peak_overlap:
                    peak_overlap = current_overlap

            assert peak_overlap == 4, f"Expected peak overlap of 4, got {peak_overlap}"

            # Verify response
            assert result["archived"] == 12
            assert len(result["refs"]) == 12
            assert result["refs"][0] == "mocked-ref-mem-0"


# ---------------------------------------------------------------------------
# 5. Rollback exception isolation
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_rollback_exception_isolation() -> None:
    """Verify that if q_res.rollback() fails, it does not mask the original exception."""
    mock_handler = AsyncMock(side_effect=ValueError("original_error"))
    tool_name = "test_rollback_tool"

    test_spec = ToolSpec(
        handler=mock_handler,
        admin_only=False,
        cacheable=False,
        mutation=True,
        migration=False,
    )

    with patch.dict(TOOL_REGISTRY, {tool_name: test_spec}):
        mock_engine = MagicMock()
        mock_engine.redis_client = None

        mock_reservation = MagicMock()
        mock_reservation.rollback = AsyncMock(side_effect=RuntimeError("rollback_error"))

        with (
            patch("nce.mcp_stdio_dispatch.enforce_mcp_tool_auth") as mock_auth,
            patch(
                "nce.mcp_stdio_dispatch._consume_quota_for_mcp_tool",
                AsyncMock(return_value=mock_reservation),
            ),
            patch(
                "nce.mcp_stdio_dispatch._try_cached_mcp_tool_response",
                AsyncMock(return_value=(None, None)),
            ),
        ):
            # Execute tool call
            res = await execute_call_tool(mock_engine, tool_name, {})

            # Validate that the ValueError("original_error") is returned in JSON-RPC error
            # rather than being masked by the RuntimeError("rollback_error")
            assert len(res) == 1
            assert isinstance(res[0], TextContent)

            body = json.loads(res[0].text)
            assert "error" in body
            assert body["error"]["code"] == -32602  # Invalid params (maps from ValueError)
            assert "original_error" in body["error"]["data"]["detail"]

            # Confirm auth bypass ran, and rollback was attempted
            mock_auth.assert_called_once()
            mock_reservation.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_rollback_exception_isolation_base_exception() -> None:
    """Verify that if a BaseException (e.g. asyncio.CancelledError) occurs in a tool handler,
    and the quota rollback also fails, the original BaseException is propagated without being masked.
    """

    class CustomBaseException(BaseException):
        pass

    mock_handler = AsyncMock(side_effect=CustomBaseException("original_base_exception"))
    tool_name = "test_rollback_base_tool"

    test_spec = ToolSpec(
        handler=mock_handler,
        admin_only=False,
        cacheable=False,
        mutation=True,
        migration=False,
    )

    with patch.dict(TOOL_REGISTRY, {tool_name: test_spec}):
        mock_engine = MagicMock()
        mock_engine.redis_client = None

        mock_reservation = MagicMock()
        mock_reservation.rollback = AsyncMock(side_effect=RuntimeError("rollback_failed"))

        with (
            patch("nce.mcp_stdio_dispatch.enforce_mcp_tool_auth") as mock_auth,
            patch(
                "nce.mcp_stdio_dispatch._consume_quota_for_mcp_tool",
                AsyncMock(return_value=mock_reservation),
            ),
            patch(
                "nce.mcp_stdio_dispatch._try_cached_mcp_tool_response",
                AsyncMock(return_value=(None, None)),
            ),
        ):
            # Execute tool call and verify it propagates CustomBaseException (the parent error)
            with pytest.raises(CustomBaseException, match="original_base_exception"):
                await execute_call_tool(mock_engine, tool_name, {})

            # Confirm auth bypass ran, and rollback was attempted
            mock_auth.assert_called_once()
            mock_reservation.rollback.assert_awaited_once()


# ---------------------------------------------------------------------------
# 6. Redis read exception resilience
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_redis_read_exception_resilience() -> None:
    """Verify if Redis raises various exceptions during _try_cached_mcp_tool_response,
    they are caught and it returns None, None (graceful cache miss fallback).
    """
    mock_engine = MagicMock()
    mock_redis = MagicMock()

    exceptions_to_test = [
        Exception("Redis connection lost"),
        ConnectionError("Socket closed on remote end"),
        TimeoutError("Connection timed out"),
    ]

    try:
        import redis.exceptions

        exceptions_to_test.append(redis.exceptions.ConnectionError("Redis connection error"))
        exceptions_to_test.append(redis.exceptions.TimeoutError("Redis timeout error"))
        exceptions_to_test.append(redis.exceptions.ResponseError("Redis response error"))
    except ImportError:
        pass

    # "semantic_search" is in CACHEABLE_TOOLS
    tool_name = "semantic_search"
    arguments = {"namespace_id": "00000000-0000-4000-8000-000000000001"}

    for exc in exceptions_to_test:
        mock_redis.get = AsyncMock(side_effect=exc)
        mock_engine.redis_client = mock_redis

        # Execute and verify resilient fallback
        res, cache_key = await _try_cached_mcp_tool_response(mock_engine, tool_name, arguments)

        assert res is None
        assert cache_key is None
        mock_redis.get.assert_awaited_with("mcp_cache_generation")


@pytest.mark.asyncio
async def test_redis_disabled_check_exception_resilience() -> None:
    """Batch 100: a Redis blip during a governance *refresh* must NOT block
    dispatch while the last-known-good snapshot is still within STALE_HARD.

    The old fail-open ``hexists`` check defaulted to enabled on any error. The
    new last-known-good cache serves the prior snapshot instead. Here the prior
    snapshot is empty (nothing disabled), so a non-disabled tool still executes —
    availability preserved without silently un-revoking anything.
    """
    import time as _time

    from nce.tool_governance import GOVERNANCE

    mock_engine = MagicMock()
    mock_redis = MagicMock()
    # Governance now reads ``hkeys``; make it raise to simulate the Redis blip.
    mock_redis.hkeys = AsyncMock(side_effect=Exception("Redis connection lost during toggle check"))
    mock_engine.redis_client = mock_redis

    tool_name = "test_resilient_tool"
    mock_handler = AsyncMock(return_value="success")
    test_spec = ToolSpec(
        handler=mock_handler,
        admin_only=False,
        cacheable=False,
        mutation=False,
        migration=False,
    )

    # INITIALIZED-EMPTY snapshot whose age forces a refresh (past STALE_OK) but
    # remains within STALE_HARD, so the refresh is attempted, fails, and the
    # last-known-good empty snapshot is still enforced (allow).
    GOVERNANCE._snapshot = frozenset()
    GOVERNANCE._fetched_at = _time.monotonic() - (cfg.NCE_TOOL_GOVERNANCE_STALE_OK_SEC + 1)

    from nce.quotas import null_reservation

    with patch.dict(TOOL_REGISTRY, {tool_name: test_spec}):
        with (
            patch("nce.mcp_stdio_dispatch.enforce_mcp_tool_auth"),
            patch(
                "nce.mcp_stdio_dispatch._consume_quota_for_mcp_tool",
                AsyncMock(return_value=null_reservation()),
            ),
        ):
            res = await execute_call_tool(mock_engine, tool_name, {})

            # Should have executed the tool and returned success
            assert len(res) == 1
            assert res[0].text == "success"
            # The refresh attempt consulted ``hkeys`` (and raised → served snapshot).
            mock_redis.hkeys.assert_awaited_once_with("nce:tools:disabled")
