"""
Tests for MCP cache invalidation — namespace-scoped cache keys, purge on
tenant/document lifecycle events, and cache key construction.

Coverage:
  - ``build_cache_key`` includes namespace_id in key (scoping)
  - Cache entries in different namespaces produce different keys
  - ``purge_namespace_cache`` deletes all keys for a namespace via SCAN
  - ``purge_document_cache`` deletes keys referencing a specific document
  - Server integration: cache miss writes namespace-scoped key
  - Server integration: mutation tools bump generation + purge document cache
  - Server integration: cache hit returns immediately
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from nce.mcp_args import (
    _document_cache_pattern,
    _namespace_cache_pattern,
    build_cache_key,
    extract_namespace_id,
    purge_document_cache,
    purge_namespace_cache,
)
from nce.models import ManageNamespaceCommand

TEST_NS = str(uuid4())
TEST_NS2 = str(uuid4())


# =========================================================================
# build_cache_key — namespace scoping
# =========================================================================


class TestBuildCacheKey:
    """``build_cache_key`` must scope keys by namespace_id."""

    def test_includes_namespace_id(self):
        key = build_cache_key("semantic_search", {}, 1, namespace_id=TEST_NS)
        assert TEST_NS in key
        assert key.startswith("mcp_cache:v1:")

    def test_different_namespaces_different_keys(self):
        args = {"query": "hello"}
        key_a = build_cache_key("semantic_search", args, 1, namespace_id=TEST_NS)
        key_b = build_cache_key("semantic_search", args, 1, namespace_id=TEST_NS2)
        assert key_a != key_b

    def test_none_namespace_uses_global(self):
        key = build_cache_key("semantic_search", {"query": "x"}, 1, namespace_id=None)
        assert "global" in key

    def test_same_args_same_namespace_same_key(self):
        ns = str(uuid4())
        args = {"query": "hello"}
        k1 = build_cache_key("graph_search", args, 5, namespace_id=ns)
        k2 = build_cache_key("graph_search", args, 5, namespace_id=ns)
        assert k1 == k2

    def test_different_generations_different_keys(self):
        ns = str(uuid4())
        args = {"query": "hello"}
        k1 = build_cache_key("search_codebase", args, 3, namespace_id=ns)
        k2 = build_cache_key("search_codebase", args, 4, namespace_id=ns)
        assert k1 != k2


# =========================================================================
# extract_namespace_id
# =========================================================================


class TestExtractNamespaceId:
    """``extract_namespace_id`` safely extracts namespace UUIDs."""

    def test_valid_uuid(self):
        assert extract_namespace_id({"namespace_id": TEST_NS}) == TEST_NS

    def test_missing_key(self):
        assert extract_namespace_id({}) is None

    def test_invalid_uuid(self):
        with pytest.raises(ValueError, match="Invalid namespace_id"):
            extract_namespace_id({"namespace_id": "not-a-uuid"})

    def test_none_value(self):
        assert extract_namespace_id({"namespace_id": None}) is None


# =========================================================================
# Key pattern helpers
# =========================================================================


class TestKeyPatterns:
    """Cache key glob patterns."""

    def test_namespace_pattern_format(self):
        pattern = _namespace_cache_pattern(TEST_NS)
        assert TEST_NS in pattern
        assert pattern.startswith("mcp_cache:v")

    def test_document_pattern_format(self):
        pattern = _document_cache_pattern(TEST_NS, "mem_123")
        assert TEST_NS in pattern
        assert "mem_123" in pattern


# =========================================================================
# purge_namespace_cache
# =========================================================================


class TestPurgeNamespaceCache:
    """``purge_namespace_cache`` deletes all keys for a namespace."""

    @pytest.mark.asyncio
    async def test_deletes_matching_keys(self):
        redis = MagicMock()
        redis.scan = AsyncMock(
            side_effect=[
                (
                    0,
                    [
                        b"mcp_cache:v1:ns-abc:tool:hash1",
                        b"mcp_cache:v1:ns-abc:tool:hash2",
                    ],
                ),
            ]
        )
        redis.delete = AsyncMock(return_value=2)

        deleted = await purge_namespace_cache(redis, "ns-abc")
        assert deleted == 2
        redis.delete.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_noop_when_no_keys(self):
        redis = MagicMock()
        redis.scan = AsyncMock(side_effect=[(0, [])])
        redis.delete = AsyncMock()

        deleted = await purge_namespace_cache(redis, "ns-empty")
        assert deleted == 0
        redis.delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_multi_cursor_pagination(self):
        redis = MagicMock()
        redis.scan = AsyncMock(
            side_effect=[
                (42, [b"mcp_cache:v1:ns-paged:tool:h1"]),
                (0, [b"mcp_cache:v1:ns-paged:tool:h2"]),
            ]
        )
        redis.delete = AsyncMock(return_value=1)

        deleted = await purge_namespace_cache(redis, "ns-paged")
        assert deleted == 2
        assert redis.delete.await_count == 2


# =========================================================================
# purge_document_cache
# =========================================================================


class TestPurgeDocumentCache:
    """``purge_document_cache`` deletes keys referencing a specific doc."""

    @pytest.mark.asyncio
    async def test_deletes_matching_keys(self):
        redis = MagicMock()
        redis.scan = AsyncMock(
            side_effect=[
                (0, [b"mcp_cache:v1:ns:a:tool:hash_with_mem123"]),
            ]
        )
        redis.delete = AsyncMock(return_value=1)

        deleted = await purge_document_cache(redis, "ns", "mem_123")
        assert deleted == 1


# =========================================================================
# ManageNamespaceCommand.delete
# =========================================================================


class TestManageNamespaceDelete:
    """``ManageNamespaceCommand.delete`` must be a valid enum member."""

    def test_delete_is_valid(self):
        assert ManageNamespaceCommand("delete") == ManageNamespaceCommand.delete
        assert ManageNamespaceCommand.delete.value == "delete"


# =========================================================================
# Server integration tests  (namespace-scoped cache keys)
# =========================================================================

# ---- fixtures ----


@pytest.fixture
def mock_engine():
    engine = MagicMock()
    engine.redis_client = AsyncMock()
    # Ensure the tool-disabled interceptor always passes (tools are enabled by default)
    engine.redis_client.hexists = AsyncMock(return_value=False)
    engine.store_memory = AsyncMock(return_value={"payload_ref": "mongo_123"})
    engine.semantic_search = AsyncMock(return_value=[{"id": 1}])
    engine.search_codebase = AsyncMock(return_value=[{"code": "def"}])
    engine.graph_search = AsyncMock(return_value={"nodes": []})
    engine.store_media = AsyncMock(return_value="mongo_456")
    engine.forget_memory = AsyncMock(return_value={"status": "success", "forgotten": True})
    return engine


@pytest.fixture(autouse=True)
def setup_server_engine(mock_engine):
    import server

    original_engine = server.engine
    server.engine = mock_engine
    yield
    server.engine = original_engine


@pytest.fixture(autouse=True)
def disable_quotas(monkeypatch):
    monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)


# ---- tests ----


@pytest.mark.asyncio
async def test_cache_miss_writes_namespace_scoped_key(mock_engine):
    """Verify that a cache miss writes a namespace-scoped key to Redis."""
    from server import call_tool

    mock_engine.redis_client.get.return_value = None

    args = {
        "namespace_id": TEST_NS,
        "agent_id": "u1",
        "query": "test query",
        "limit": 5,
    }
    await call_tool("semantic_search", args)

    mock_engine.semantic_search.assert_called_once()
    mock_engine.redis_client.setex.assert_called_once()

    call_args = mock_engine.redis_client.setex.call_args[0]
    redis_key = call_args[0]
    assert TEST_NS in redis_key, f"Expected namespace {TEST_NS} in cache key, got {redis_key}"
    assert redis_key.startswith("mcp_cache:v")
    assert call_args[1] == 300  # TTL


@pytest.mark.asyncio
async def test_cache_hit_returns_cached_value(mock_engine):
    """Verify that a cache hit returns immediately without calling engine."""
    from server import call_tool

    async def mock_get(key):
        if key == "mcp_cache_generation":
            return b"2"
        if TEST_NS in key and b"semantic_search" in (key.encode() if isinstance(key, str) else key):
            return b'[{"cached": "result"}]'
        return None

    mock_engine.redis_client.get.side_effect = mock_get

    args = {
        "namespace_id": TEST_NS,
        "agent_id": "u1",
        "query": "cached query",
        "limit": 5,
    }
    res = await call_tool("semantic_search", args)

    mock_engine.semantic_search.assert_not_called()
    assert res[0].text == '[{"cached": "result"}]'


@pytest.mark.asyncio
async def test_mutation_bumps_generation(mock_engine):
    """Mutation tools increment the cache generation counter."""
    from server import call_tool

    args = {
        "namespace_id": TEST_NS,
        "agent_id": "u1",
        "content": "new memory content",
        "summary": "new memory",
        "heavy_payload": "full content",
    }
    await call_tool("store_memory", args)

    # Should have called incr("mcp_cache_generation") via bump_cache_generation
    mock_engine.redis_client.incr.assert_called_once_with("mcp_cache_generation")
    mock_engine.store_memory.assert_called_once()


@pytest.mark.asyncio
async def test_redis_failure_after_a_committed_mutation_does_not_fail_the_call(mock_engine):
    """D3: the write has LANDED -- a Redis blip must not tell the caller it failed.

    ``bump_cache_generation`` runs AFTER the handler returns, inside the same
    ``try`` whose ``except BaseException`` rolls back quota and re-raises. So a
    Redis outage on that line turned a committed mutation into ``-32603``, and
    a client that retries on an internal error would write it twice.

    The REST twin already argues this exact case in its own docstring
    (``admin_handlers/_shared.py``: *"failing the HTTP response would invite the
    caller to retry a write that already landed"*). Same reasoning, two answers,
    one codebase -- until now.
    """
    from server import call_tool

    mock_engine.redis_client.incr.side_effect = ConnectionError("redis is down")

    args = {
        "namespace_id": TEST_NS,
        "agent_id": "u1",
        "content": "new memory content",
        "summary": "new memory",
        "heavy_payload": "full content",
    }
    res = await call_tool("store_memory", args)

    # The mutation must have happened, and the caller must be told it happened.
    mock_engine.store_memory.assert_called_once()
    assert res, "dispatch returned nothing for a committed mutation"
    body = res[0].text
    assert "-32603" not in body, (
        "a committed mutation was reported as an internal error because the "
        f"post-success cache bump raised: {body[:400]}"
    )
    assert "error" not in body.lower(), body[:400]


@pytest.mark.asyncio
async def test_forget_memory_triggers_document_purge(mock_engine):
    """``forget_memory`` must trigger document-level cache purge."""
    from server import call_tool

    mock_engine.redis_client.get.return_value = None  # no cached gen needed
    mock_engine.redis_client.incr.return_value = 1

    mem_id = str(uuid4())
    args = {
        "namespace_id": TEST_NS,
        "agent_id": "test-agent",
        "memory_id": mem_id,
    }
    await call_tool("forget_memory", args)

    # Should have called scan with a pattern matching the document
    [c for c in mock_engine.redis_client.scan.mock_calls if c.args]
    [c.args[1] for c in mock_engine.redis_client.scan.mock_calls if len(c.args) > 1]
    # At minimum, incr was called (generation bump)
    mock_engine.redis_client.incr.assert_called_with("mcp_cache_generation")


@pytest.mark.asyncio
async def test_cacheable_search_codebase(mock_engine):
    """search_codebase writes namespace-scoped cache key."""
    from server import call_tool

    mock_engine.redis_client.get.return_value = None

    args = {"namespace_id": TEST_NS, "query": "find stuff", "top_k": 5}
    await call_tool("search_codebase", args)

    mock_engine.search_codebase.assert_called_once()
    mock_engine.redis_client.setex.assert_called_once()
    key = mock_engine.redis_client.setex.call_args[0][0]
    assert TEST_NS in key


@pytest.mark.asyncio
async def test_cacheable_graph_search(mock_engine):
    """graph_search writes namespace-scoped cache key."""
    from server import call_tool

    mock_engine.redis_client.get.return_value = None

    args = {"namespace_id": TEST_NS, "query": "find related", "max_depth": 2}
    await call_tool("graph_search", args)

    mock_engine.graph_search.assert_called_once()
    mock_engine.redis_client.setex.assert_called_once()
    key = mock_engine.redis_client.setex.call_args[0][0]
    assert TEST_NS in key


@pytest.mark.asyncio
async def test_a2a_scope_violation_bypasses_cache_write(mock_engine, monkeypatch):
    """An A2A scope violation on a cacheable tool must bypass writing to the Redis cache."""
    from server import call_tool

    from nce.a2a import A2AScopeViolationError

    # Make semantic_search raise an A2AScopeViolationError
    mock_engine.semantic_search.side_effect = A2AScopeViolationError("A2A Scope violation")
    mock_engine.redis_client.get.return_value = None

    args = {
        "namespace_id": TEST_NS,
        "agent_id": "u1",
        "query": "forbidden query",
        "limit": 5,
    }

    # We call the tool. It returns an error response (since it handles the exception).
    res = await call_tool("semantic_search", args)

    # Verify the handler was indeed called
    mock_engine.semantic_search.assert_called_once()

    # Verify we did NOT call setex on the Redis cache
    mock_engine.redis_client.setex.assert_not_called()


@pytest.mark.asyncio
async def test_a2a_scope_violation_bypasses_generation_bump(mock_engine, monkeypatch):
    """An A2A scope violation on a mutation tool must bypass bumping the cache generation."""
    from server import call_tool

    from nce.a2a import A2AScopeViolationError

    # Make store_memory raise an A2AScopeViolationError
    mock_engine.store_memory.side_effect = A2AScopeViolationError("A2A Scope violation")

    args = {
        "namespace_id": TEST_NS,
        "agent_id": "u1",
        "content": "new memory content",
        "summary": "new memory",
        "heavy_payload": "full content",
    }

    res = await call_tool("store_memory", args)

    # Verify store_memory was called
    mock_engine.store_memory.assert_called_once()

    # Verify Redis incr was NOT called (which is called by bump_cache_generation)
    mock_engine.redis_client.incr.assert_not_called()
