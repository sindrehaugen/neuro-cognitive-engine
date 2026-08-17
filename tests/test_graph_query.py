from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from bson import ObjectId

from nce.graph_query import GraphEdge, GraphNode, GraphRAGTraverser


@pytest.fixture
def mock_pg_pool():
    pool = MagicMock()
    conn = AsyncMock()
    tx = AsyncMock()
    tx.__aenter__.return_value = None
    tx.__aexit__.return_value = False
    conn.transaction = MagicMock(return_value=tx)
    acq = AsyncMock()
    acq.__aenter__.return_value = conn
    acq.__aexit__.return_value = None
    pool.acquire = MagicMock(return_value=acq)
    return pool, conn


class _AsyncCursor:
    """Minimal async iterator wrapper for MongoDB cursor mocking."""

    def __init__(self, docs: list[dict]):
        self._docs = docs

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._docs:
            raise StopAsyncIteration
        return self._docs.pop(0)


@pytest.fixture
def mock_mongo_client():
    client = MagicMock()
    db = MagicMock()
    client.memory_archive = db
    # Batch hydration uses find(), not find_one() — return an empty cursor by default
    db.episodes.find = MagicMock(return_value=_AsyncCursor([]))
    db.code_files.find = MagicMock(return_value=_AsyncCursor([]))
    return client, db


@pytest.fixture
def traverser(mock_pg_pool, mock_mongo_client):
    pool, conn = mock_pg_pool
    client, db = mock_mongo_client

    async def dummy_embed(query: str):
        return [0.1, 0.2, 0.3]

    return GraphRAGTraverser(pg_pool=pool, mongo_client=client, embedding_fn=dummy_embed)


@pytest.mark.asyncio
async def test_find_anchor(traverser, mock_pg_pool):
    _, conn = mock_pg_pool
    conn.fetch.return_value = [
        {"label": "Redis", "entity_type": "TOOL", "payload_ref": "123", "distance": 0.1}
    ]

    anchors = await traverser._find_anchor("query", top_k=1, _allow_global_sweep=True)

    assert len(anchors) == 1
    assert anchors[0].label == "Redis"
    assert anchors[0].distance == 0.1
    conn.fetch.assert_called_once()


@pytest.mark.asyncio
async def test_bfs(traverser, mock_pg_pool):
    _, conn = mock_pg_pool

    # New BFS makes 2 fetch calls:
    #   1st: recursive CTE → returns visited labels
    #   2nd: batch edge query → returns edges
    conn.fetch.side_effect = [
        # First call: recursive CTE returns visited labels
        [{"label": "Redis", "depth": 0}, {"label": "Data", "depth": 1}],
        # Second call: batch edge fetch
        [
            {
                "subject_label": "Redis",
                "predicate": "caches",
                "object_label": "Data",
                "payload_ref": "456",
                "decayed_confidence": 0.9,
            }
        ],
    ]

    visited, edges = await traverser._bfs("Redis", max_depth=1, _allow_global_sweep=True)

    assert "Redis" in visited
    assert "Data" in visited
    assert len(edges) == 1
    assert edges[0].subject == "Redis"
    assert edges[0].obj == "Data"


@pytest.mark.asyncio
async def test_hydrate_sources(traverser, mock_mongo_client):
    _, db = mock_mongo_client

    # Batch hydration uses find() with $in — return an async cursor
    doc_id = ObjectId("5f3b3e3e3e3e3e3e3e3e3e3e")
    db.episodes.find = MagicMock(
        return_value=_AsyncCursor(
            [
                {"_id": doc_id, "raw_data": "Test memory", "type": "chat"},
            ]
        )
    )

    sources = await traverser._hydrate_sources({"5f3b3e3e3e3e3e3e3e3e3e3e"})

    assert len(sources) == 1
    assert sources[0]["excerpt"] == "Test memory"
    assert sources[0]["collection"] == "episodes"


@pytest.mark.asyncio
async def test_search_full_pipeline(traverser, mock_pg_pool, mock_mongo_client):
    pool, conn = mock_pg_pool
    _, db = mock_mongo_client

    # Setup step 1: find anchor
    async def mock_find_anchor(*args, **kwargs):
        return [GraphNode(label="Anchor", entity_type="CONCEPT", payload_ref="abc", distance=0.0)]

    traverser._find_anchor = mock_find_anchor

    # Setup step 2: bfs
    async def mock_bfs(*args, **kwargs):
        edges = [
            GraphEdge(
                subject="Anchor",
                predicate="is",
                obj="Target",
                confidence=1.0,
                payload_ref="def",
            )
        ]
        return {"Anchor", "Target"}, edges

    traverser._bfs = mock_bfs

    # Setup step 3: node metadata query
    conn.fetch.return_value = [
        {"label": "Anchor", "entity_type": "CONCEPT", "payload_ref": "abc"},
        {"label": "Target", "entity_type": "CONCEPT", "payload_ref": "xyz"},
    ]

    # Setup step 4: hydrate
    async def mock_hydrate(*args, **kwargs):
        return [{"excerpt": "data"}]

    traverser._hydrate_sources = mock_hydrate

    subgraph = await traverser.search("query", max_depth=1, _allow_global_sweep=True)

    assert subgraph.anchor == "Anchor"
    assert len(subgraph.nodes) == 2
    assert len(subgraph.edges) == 1
    assert len(subgraph.sources) == 1
    assert subgraph.edge_total == 1
    assert subgraph.has_more_edges is False
    assert subgraph.max_edges_per_node == 512


@pytest.mark.asyncio
async def test_search_edge_pagination(traverser, mock_pg_pool, mock_mongo_client):
    """Deduplicated edges are sliced by edge_offset / edge_limit; metadata reflects totals."""
    _, conn = mock_pg_pool

    async def mock_find_anchor(*args, **kwargs):
        return [GraphNode(label="A", entity_type="CONCEPT", payload_ref="p1", distance=0.0)]

    async def mock_bfs(*args, **kwargs):
        edges = [
            GraphEdge(subject="A", predicate="r", obj="B", confidence=1.0, payload_ref=None),
            GraphEdge(subject="A", predicate="r2", obj="C", confidence=0.9, payload_ref=None),
            GraphEdge(subject="B", predicate="r3", obj="D", confidence=0.8, payload_ref=None),
        ]
        return {"A", "B", "C", "D"}, edges

    traverser._find_anchor = mock_find_anchor
    traverser._bfs = mock_bfs
    conn.fetch.return_value = [
        {"label": "A", "entity_type": "CONCEPT", "payload_ref": "p1"},
        {"label": "B", "entity_type": "CONCEPT", "payload_ref": None},
        {"label": "C", "entity_type": "CONCEPT", "payload_ref": None},
        {"label": "D", "entity_type": "CONCEPT", "payload_ref": None},
    ]
    traverser._hydrate_sources = AsyncMock(return_value=[])

    subgraph = await traverser.search(
        "q",
        max_depth=1,
        _allow_global_sweep=True,
        edge_limit=1,
        edge_offset=1,
    )
    assert subgraph.edge_total == 3
    assert len(subgraph.edges) == 1
    assert subgraph.edges[0].obj == "C"
    assert subgraph.has_more_edges is True
    assert subgraph.edge_offset == 1
    assert subgraph.edge_limit == 1
    labels = {n.label for n in subgraph.nodes}
    assert labels == {"A", "C"}


@pytest.mark.asyncio
async def test_get_subgraph_alias(traverser, mock_pg_pool, mock_mongo_client):
    _, conn = mock_pg_pool

    async def mock_find_anchor(*args, **kwargs):
        return [GraphNode(label="A", entity_type="X", payload_ref=None, distance=0.0)]

    async def mock_bfs(*args, **kwargs):
        return {"A"}, []

    traverser._find_anchor = mock_find_anchor
    traverser._bfs = mock_bfs
    conn.fetch.return_value = [{"label": "A", "entity_type": "X", "payload_ref": None}]
    traverser._hydrate_sources = AsyncMock(return_value=[])

    sg = await traverser.get_subgraph("q", max_depth=1, _allow_global_sweep=True)
    assert sg.anchor == "A"
    assert sg.edges == []


# ---------------------------------------------------------------------------
# Time-travel signature verification — tampered event_log detection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_time_travel_anchor_detects_tampered_event(
    traverser,
    mock_pg_pool,
    monkeypatch,
):
    """A tampered event_log row must cause DataIntegrityError in _find_anchor."""
    from nce.event_log import DataIntegrityError

    _, conn = mock_pg_pool

    # The CTE query returns rows with event_ids.
    cte_rows = [
        {
            "label": "Redis",
            "entity_type": "TOOL",
            "payload_ref": "abc123abc123abc123abc123",
            "distance": 0.1,
            "event_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        }
    ]
    # The verification query fetches full event_log rows.
    verification_rows = [
        {
            "id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "signature": b"\x00" * 32,
            "signature_key_id": "sk-deadbeef",
            "namespace_id": "11111111-2222-3333-4444-555555555555",
            "agent_id": "test-agent",
            "event_type": "store_memory",
            "event_seq": 1,
            "occurred_at": "2025-01-01T00:00:00+00:00",
            "parent_event_id": None,
            "params": '{"entities": []}',
        }
    ]

    call_count = [0]

    async def fetch_side_effect(query, *args):
        call_count[0] += 1
        if "SELECT * FROM event_log WHERE id = ANY" in query:
            return verification_rows
        return cte_rows

    conn.fetch = fetch_side_effect

    # Make verify_event_signature raise DataIntegrityError for the tampered row.
    async def mock_verify(conn_arg, record):
        raise DataIntegrityError(
            f"Event signature mismatch for event_id={record['id']}. Tampering detected."
        )

    monkeypatch.setattr("nce.event_log.verify_event_signature", mock_verify)

    with pytest.raises(DataIntegrityError, match="Tampering detected"):
        await traverser._find_anchor(
            "query",
            namespace_id="11111111-2222-3333-4444-555555555555",
            top_k=1,
            as_of=datetime(2025, 1, 2, tzinfo=timezone.utc),
        )


@pytest.mark.asyncio
async def test_time_travel_bfs_detects_tampered_event(
    traverser,
    mock_pg_pool,
    monkeypatch,
):
    """A tampered event_log row must cause DataIntegrityError in _bfs."""
    from nce.event_log import DataIntegrityError

    _, conn = mock_pg_pool

    cte_rows = [
        {
            "subject_label": "Redis",
            "predicate": "caches",
            "object_label": "Data",
            "payload_ref": "abc123abc123abc123abc123",
            "decayed_confidence": 0.9,
            "event_id": "bbbbbbbb-cccc-dddd-eeee-ffffffffffff",
        }
    ]
    verification_rows = [
        {
            "id": "bbbbbbbb-cccc-dddd-eeee-ffffffffffff",
            "signature": b"\x00" * 32,
            "signature_key_id": "sk-cafebabe",
            "namespace_id": "11111111-2222-3333-4444-555555555555",
            "agent_id": "test-agent",
            "event_type": "store_memory",
            "event_seq": 1,
            "occurred_at": "2025-01-01T00:00:00+00:00",
            "parent_event_id": None,
            "params": '{"triplets": []}',
        }
    ]

    call_count = [0]

    async def fetch_side_effect(query, *args):
        call_count[0] += 1
        # First call: recursive CTE returns visited labels
        if "RECURSIVE" in query:
            return [{"label": "Redis", "depth": 0}, {"label": "Data", "depth": 1}]
        # Second call: batch edge fetch returns tampered edges
        if "SELECT * FROM event_log WHERE id = ANY" in query:
            return verification_rows
        return cte_rows

    conn.fetch = fetch_side_effect

    async def mock_verify(conn_arg, record):
        raise DataIntegrityError("Tampering detected in BFS.")

    monkeypatch.setattr("nce.event_log.verify_event_signature", mock_verify)

    with pytest.raises(DataIntegrityError, match="Tampering detected"):
        await traverser._bfs(
            "Redis",
            max_depth=1,
            namespace_id="11111111-2222-3333-4444-555555555555",
            as_of=datetime(2025, 1, 2, tzinfo=timezone.utc),
        )


@pytest.mark.asyncio
async def test_time_travel_passes_with_valid_signatures(
    traverser,
    mock_pg_pool,
    monkeypatch,
):
    """Time-travel with valid signatures must complete without error."""
    _, conn = mock_pg_pool

    cte_rows = [
        {
            "label": "Redis",
            "entity_type": "TOOL",
            "payload_ref": "abc123abc123abc123abc123",
            "distance": 0.1,
            "event_id": "cccccccc-dddd-eeee-ffff-000000000000",
        }
    ]
    verification_rows = [
        {
            "id": "cccccccc-dddd-eeee-ffff-000000000000",
            "signature": b"\x01" * 32,
            "signature_key_id": "sk-validkey",
            "namespace_id": "11111111-2222-3333-4444-555555555555",
            "agent_id": "test-agent",
            "event_type": "store_memory",
            "event_seq": 1,
            "occurred_at": "2025-01-01T00:00:00+00:00",
            "parent_event_id": None,
            "params": '{"entities": []}',
        }
    ]

    call_count = [0]

    async def fetch_side_effect(query, *args):
        call_count[0] += 1
        if "SELECT * FROM event_log WHERE id = ANY" in query:
            return verification_rows
        return cte_rows

    conn.fetch = fetch_side_effect

    # verify_event_signature passes (does nothing on success)
    async def mock_verify_pass(conn_arg, record):
        return  # success — no exception

    monkeypatch.setattr("nce.event_log.verify_event_signature", mock_verify_pass)

    # Must not raise
    anchors = await traverser._find_anchor(
        "query",
        namespace_id="11111111-2222-3333-4444-555555555555",
        top_k=1,
        as_of=datetime(2025, 1, 2, tzinfo=timezone.utc),
    )
    assert len(anchors) == 1
    assert anchors[0].label == "Redis"


# ---------------------------------------------------------------------------
# _allow_global_sweep guard — accidental None namespace_id rejection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_anchor_rejects_none_namespace_without_flag(
    traverser,
    mock_pg_pool,
):
    """_find_anchor must raise ValueError when namespace_id=None and _allow_global_sweep=False."""
    with pytest.raises(ValueError, match="namespace_id is required"):
        await traverser._find_anchor("query", namespace_id=None, top_k=1)


@pytest.mark.asyncio
async def test_find_anchor_allows_none_namespace_with_flag(
    traverser,
    mock_pg_pool,
):
    """_find_anchor must succeed when namespace_id=None with _allow_global_sweep=True."""
    _, conn = mock_pg_pool
    conn.fetch.return_value = [
        {"label": "Redis", "entity_type": "TOOL", "payload_ref": "123", "distance": 0.1}
    ]
    anchors = await traverser._find_anchor(
        "query", namespace_id=None, top_k=1, _allow_global_sweep=True
    )
    assert len(anchors) == 1


@pytest.mark.asyncio
async def test_bfs_rejects_none_namespace_without_flag(
    traverser,
    mock_pg_pool,
):
    """_bfs must raise ValueError when namespace_id=None and _allow_global_sweep=False."""
    with pytest.raises(ValueError, match="namespace_id is required"):
        await traverser._bfs("Redis", max_depth=1, namespace_id=None)


@pytest.mark.asyncio
async def test_bfs_allows_none_namespace_with_flag(
    traverser,
    mock_pg_pool,
):
    """_bfs must succeed when namespace_id=None with _allow_global_sweep=True."""
    _, conn = mock_pg_pool
    # New BFS makes 2 fetch calls: recursive CTE (labels) then batch edges
    conn.fetch.side_effect = [
        [{"label": "Redis", "depth": 0}],  # recursive CTE → start label
        [],  # batch edge fetch → no edges
    ]
    visited, edges = await traverser._bfs(
        "Redis", max_depth=1, namespace_id=None, _allow_global_sweep=True
    )
    assert "Redis" in visited
    assert len(edges) == 0


@pytest.mark.asyncio
async def test_search_rejects_none_namespace_without_flag(
    traverser,
    mock_pg_pool,
    mock_mongo_client,
):
    """search must raise ValueError when namespace_id=None and _allow_global_sweep=False."""
    with pytest.raises(ValueError, match="namespace_id is required"):
        await traverser.search("query", namespace_id=None)


@pytest.mark.asyncio
async def test_search_allows_none_namespace_with_flag(
    traverser,
    mock_pg_pool,
    mock_mongo_client,
):
    """search must succeed when namespace_id=None with _allow_global_sweep=True."""
    _, conn = mock_pg_pool
    conn.fetch.return_value = [
        {"label": "Redis", "entity_type": "TOOL", "payload_ref": "123", "distance": 0.1}
    ]

    # Patch BFS + hydrate to avoid needing more mock DB rows
    async def mock_bfs(*args, **kwargs):
        return {"Redis"}, []

    traverser._bfs = mock_bfs

    async def mock_hydrate(*args, **kwargs):
        return []

    traverser._hydrate_sources = mock_hydrate

    subgraph = await traverser.search("query", namespace_id=None, _allow_global_sweep=True)
    assert subgraph.anchor == "Redis"
