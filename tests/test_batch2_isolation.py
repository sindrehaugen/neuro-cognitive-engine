"""Unit test suite covering Batch 2 persistence layer concurrency & isolation changes.

Tests:
1. Connection pool release during slow embeddings in `nce/semantic_search.py`.
2. Multi-tenant segregation on `kg_nodes` fetches in `nce/graph_query.py`.
3. GC loop error isolation (acquisition and transaction failures) in `nce/garbage_collector.py`.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from bson import ObjectId

from nce.embeddings import VECTOR_DIM
from nce.garbage_collector import _fetch_pg_refs
from nce.graph_query import GraphNode, GraphRAGTraverser
from nce.semantic_search import semantic_search

# Define constants for testing
NS = "00000000-0000-4000-8000-000000000001"
AGENT = "test-agent"


# ---------------------------------------------------------------------------
# Test 1: Connection Pool Release during slow embeddings
# ---------------------------------------------------------------------------


class MockTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return False


class TracePool:
    def __init__(self, trace_log, mock_conn):
        self.trace_log = trace_log
        self.mock_conn = mock_conn
        self.acquire_count = 0

    @asynccontextmanager
    async def acquire(self, timeout=None):
        self.acquire_count += 1
        self.trace_log.append(("acquire_start", self.acquire_count))
        try:
            yield self.mock_conn
        finally:
            self.trace_log.append(("acquire_release", self.acquire_count))


def _mongo_client_mock(episode_docs=None):
    docs = episode_docs or {}

    async def _find(query, projection=None):
        ids = query.get("_id", {}).get("$in", [])
        for oid in ids:
            doc = docs.get(str(oid))
            if doc is not None:
                yield doc

    episodes = MagicMock()
    episodes.find = MagicMock(side_effect=_find)

    client = MagicMock()
    client.memory_archive = MagicMock()
    client.memory_archive.episodes = episodes
    return client


@pytest.mark.asyncio
async def test_semantic_search_pool_release_during_slow_embedding() -> None:
    trace_log = []

    # Mock connection setup
    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(return_value={"metadata": {}})
    mock_conn.fetchval = AsyncMock(return_value="active-model-123")

    # Return one result row from fetch
    result_oid = str(ObjectId())
    result_mid = uuid.uuid4()
    mock_conn.fetch = AsyncMock(
        return_value=[{"payload_ref": result_oid, "memory_id": result_mid, "final_score": 0.95}]
    )
    mock_conn.transaction = MagicMock(return_value=MockTransaction())
    mock_conn.execute = AsyncMock()

    # Create tracing connection pool
    trace_pool = TracePool(trace_log, mock_conn)

    # Slow embedding mock
    async def mock_embedding_fn(query: str):
        trace_log.append(("embedding_call_start", query))
        await asyncio.sleep(0.05)
        trace_log.append(("embedding_call_end", query))
        return [0.1] * VECTOR_DIM

    # Mongo client mock
    mongo_client = _mongo_client_mock(
        {result_oid: {"_id": ObjectId(result_oid), "raw_data": "retrieved-memory-data"}}
    )

    # Run semantic search
    results = await semantic_search(
        pg_pool=trace_pool,
        mongo_client=mongo_client,
        embedding_fn=mock_embedding_fn,
        query="network database latency",
        namespace_id=NS,
        agent_id=AGENT,
        limit=5,
    )

    # Verify return values
    assert len(results) == 1
    assert results[0]["payload_ref"] == result_oid
    assert results[0]["memory_id"] == result_mid
    assert results[0]["raw_data"] == "retrieved-memory-data"

    # Verify acquire/release ordering
    # Expected order:
    # 1. acquire_start 1 (fetch config and active model ID)
    # 2. acquire_release 1
    # 3. embedding_call_start
    # 4. embedding_call_end
    # 5. acquire_start 2 (run actual query with vector and FTS)
    # 6. acquire_release 2

    assert len(trace_log) == 6
    assert trace_log[0] == ("acquire_start", 1)
    assert trace_log[1] == ("acquire_release", 1)
    assert trace_log[2] == ("embedding_call_start", "network database latency")
    assert trace_log[3] == ("embedding_call_end", "network database latency")
    assert trace_log[4] == ("acquire_start", 2)
    assert trace_log[5] == ("acquire_release", 2)


# ---------------------------------------------------------------------------
# Test 2: Multi-Tenant Segregation Filter on kg_nodes fetches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_graph_query_multi_tenant_segregation_on_kg_nodes_fetches() -> None:
    # Set up mock PG Pool and Connection to trace SQL queries
    mock_pool = MagicMock()
    mock_conn = AsyncMock()

    # Mock transaction block
    tx = AsyncMock()
    tx.__aenter__.return_value = None
    tx.__aexit__.return_value = False
    mock_conn.transaction = MagicMock(return_value=tx)

    acq = AsyncMock()
    acq.__aenter__.return_value = mock_conn
    acq.__aexit__.return_value = None
    mock_pool.acquire = MagicMock(return_value=acq)

    # Mock Mongo client
    mongo_client = MagicMock()
    mongo_client.memory_archive = MagicMock()

    # Dummy embedding
    async def dummy_embed(query: str):
        return [0.1, 0.2, 0.3]

    # Create GraphRAGTraverser
    traverser = GraphRAGTraverser(
        pg_pool=mock_pool, mongo_client=mongo_client, embedding_fn=dummy_embed
    )

    # Mock _find_anchor to return a dummy anchor node
    anchor_node = GraphNode(
        label="AnchorNode", entity_type="CONCEPT", payload_ref="ref123", distance=0.0
    )
    traverser._find_anchor = AsyncMock(return_value=[anchor_node])

    # Mock _bfs to return visited labels set and empty edges
    traverser._bfs = AsyncMock(return_value=({"AnchorNode"}, []))

    # Mock _hydrate_sources
    traverser._hydrate_sources = AsyncMock(return_value=[])

    # Record queries executed on fetch
    executed_queries = []

    async def mock_fetch(query_str, *args):
        executed_queries.append(query_str)
        return [{"label": "AnchorNode", "entity_type": "CONCEPT", "payload_ref": "ref123"}]

    mock_conn.fetch = mock_fetch

    # Call traverser.search() under a specific namespace ID
    target_namespace = str(uuid.uuid4())
    await traverser.search("latency spikes", namespace_id=target_namespace)

    # Verify that the query to fetch node metadata for visited labels was executed
    # and explicitly contains the namespace_id = $2::uuid filter.
    metadata_query_found = False
    for sql in executed_queries:
        if "kg_nodes" in sql:
            metadata_query_found = True
            # Verify explicit RLS filter condition
            assert "namespace_id = $2::uuid" in sql
            assert "label = ANY($1::text[])" in sql

    assert metadata_query_found, "Metadata query targeting kg_nodes was not executed."


# ---------------------------------------------------------------------------
# Test 3: GC Loop Error Isolation (Acquisition & Transaction Failures)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gc_loop_error_isolation_acquisition_failure() -> None:
    # We will pass three namespaces
    ns1 = uuid.uuid4()
    ns2 = uuid.uuid4()
    ns3 = uuid.uuid4()

    mock_conn = AsyncMock()
    tx = AsyncMock()
    tx.__aenter__.return_value = None
    tx.__aexit__.return_value = False
    mock_conn.transaction = MagicMock(return_value=tx)

    # Mock fetch for _fetch_pg_ref_batch keyset pagination.
    # It returns a page with payload_ref on the first check of a namespace, then empty list to end pagination.
    # We check the last_seen_id parameter. If it is all-zeros (start of page), we return a row. Otherwise, empty list.
    fetch_calls = []

    async def mock_fetch(query_str, last_seen_id, limit):
        fetch_calls.append((query_str, last_seen_id))
        if last_seen_id == uuid.UUID(int=0):
            # Return a valid row
            return [
                {"id": uuid.uuid4(), "payload_ref": f"ref_from_{last_seen_id}_{len(fetch_calls)}"}
            ]
        return []

    mock_conn.fetch = mock_fetch

    # Mock pool.acquire to raise exception on the FIRST namespace (ns1), but succeed on others
    acquire_count = 0

    @asynccontextmanager
    async def mock_acquire(timeout=None):
        nonlocal acquire_count
        acquire_count += 1
        if acquire_count == 1:
            raise RuntimeError("DB pool acquisition error for namespace 1 (simulated)")
        yield mock_conn

    mock_pool = MagicMock()
    mock_pool.acquire = mock_acquire

    # Call the GC ref builder
    # Should propagate the error for ns1 immediately
    with pytest.raises(RuntimeError, match="DB pool acquisition error for namespace 1"):
        await _fetch_pg_refs(mock_pool, [ns1, ns2, ns3])

    # Assertions
    # 1. Total pool acquire attempts should be 1 (fails on the first namespace)
    assert acquire_count == 1


@pytest.mark.asyncio
async def test_gc_loop_error_isolation_transaction_failure() -> None:
    # We will pass three namespaces
    ns1 = uuid.uuid4()
    ns2 = uuid.uuid4()
    ns3 = uuid.uuid4()

    mock_conn = AsyncMock()

    # Mock transaction to raise error on the first namespace, but succeed on the others
    transaction_count = 0

    class FailingTransaction:
        async def __aenter__(self):
            nonlocal transaction_count
            transaction_count += 1
            if transaction_count == 1:
                raise RuntimeError("DB transaction begin failure for namespace 1 (simulated)")
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            return False

    mock_conn.transaction = MagicMock(side_effect=lambda: FailingTransaction())

    # Mock fetch for successful namespaces
    async def mock_fetch(query_str, last_seen_id, limit):
        if last_seen_id == uuid.UUID(int=0):
            return [{"id": uuid.uuid4(), "payload_ref": f"ref_{transaction_count}"}]
        return []

    mock_conn.fetch = mock_fetch

    # Mock pool acquire to always return connection
    @asynccontextmanager
    async def mock_acquire(timeout=None):
        yield mock_conn

    mock_pool = MagicMock()
    mock_pool.acquire = mock_acquire

    # Call the GC ref builder
    # Should propagate the error for ns1 immediately
    with pytest.raises(RuntimeError, match="DB transaction begin failure for namespace 1"):
        await _fetch_pg_refs(mock_pool, [ns1, ns2, ns3])

    # Assertions
    # 1. Transaction should have been entered 1 time
    assert transaction_count == 1
