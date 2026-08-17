import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nce.orchestrators.graph import GraphOrchestrator
from nce.reembedding_migration import (
    PostgresAspectReembeddingStore,
    neighbor_overlap_fraction,
)
from nce.tasks import process_code_indexing

NS = "00000000-0000-4000-8000-000000000001"


class AsyncIteratorMock:
    def __init__(self, items: list[dict]) -> None:
        self._items = list(items)
        self._index = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._index >= len(self._items):
            raise StopAsyncIteration
        item = self._items[self._index]
        self._index += 1
        return item


@pytest.mark.asyncio
async def test_postgres_aspect_reembedding_store_pending_and_load() -> None:
    pool_mock = MagicMock()
    conn_mock = AsyncMock()
    pool_mock.acquire.return_value.__aenter__.return_value = conn_mock

    # Mock pop_pending_ids query return
    conn_mock.fetch.return_value = [{"id": uuid.uuid4()}]

    store = PostgresAspectReembeddingStore(pool_mock, aspect="code_intent")
    pending = await store.pop_pending_ids(limit=5)
    assert len(pending) == 1
    conn_mock.fetch.assert_called_once()
    assert "LEFT JOIN embedding_aspects ea" in conn_mock.fetch.call_args[0][0]

    # Mock load_row queries
    memory_id = str(uuid.uuid4())
    conn_mock.fetchrow.return_value = {
        "id": memory_id,
        "payload_ref": "0123456789abcdef01234567",
        "name": "test_func",
        "filepath": "test.py",
        "embedding_vector": [0.1] * 768,
        "namespace_id": uuid.UUID(NS),
    }

    mongo_client = MagicMock()
    mongo_db = MagicMock()
    mongo_client.memory_archive = mongo_db
    collection_mock = MagicMock()
    mongo_db.code_files = collection_mock
    collection_mock.find_one = AsyncMock(return_value={"raw_code": "def test_func():\n    pass"})

    store.mongo_client = mongo_client
    row = await store.load_row(memory_id)
    assert row is not None
    assert row.memory_id == memory_id
    assert row.canonical_text == "def test_func():\n    pass"

    # Test load_row with nl_intent
    store_nl = PostgresAspectReembeddingStore(pool_mock, aspect="nl_intent")
    row_nl = await store_nl.load_row(memory_id)
    assert row_nl is not None
    assert row_nl.canonical_text == "test_func"


@pytest.mark.asyncio
async def test_postgres_aspect_reembedding_store_write() -> None:
    pool_mock = MagicMock()
    conn_mock = AsyncMock()
    pool_mock.acquire.return_value.__aenter__.return_value = conn_mock

    # Mock fetchval returning namespace_id
    conn_mock.fetchval.return_value = uuid.UUID(NS)

    store = PostgresAspectReembeddingStore(pool_mock, aspect="code_intent")

    # Mock scoped_pg_session
    session_conn = AsyncMock()
    scoped_session_mock = MagicMock()
    scoped_session_mock.__aenter__ = AsyncMock(return_value=session_conn)
    scoped_session_mock.__aexit__ = AsyncMock(return_value=False)

    with patch("nce.db_utils.scoped_pg_session", return_value=scoped_session_mock):
        await store.write_embedding_v2(
            memory_id=str(uuid.uuid4()),
            embedding=[0.2] * 768,
            model_id="v2-test",
        )
        session_conn.execute.assert_called_once()
        sql = session_conn.execute.call_args[0][0]
        assert "INSERT INTO embedding_aspects" in sql
        assert "code_intent" in session_conn.execute.call_args[0][2]


@pytest.mark.asyncio
async def test_graph_orchestrator_search_codebase_aspect() -> None:
    embed = AsyncMock(return_value=[0.1] * 768)
    graph_orch = GraphOrchestrator(
        pg_pool=MagicMock(),
        mongo_client=MagicMock(),
        graph_traverser=MagicMock(),
        embed_fn=embed,
    )

    ids = [uuid.uuid4()]
    fused = [{"id": ids[0], "score": 0.5}]
    mock_conn = AsyncMock()

    sql_calls = []

    async def _capture_fetch(sql, *params):
        sql_calls.append((sql, params))
        if "WITH vector_candidates" in sql:
            return fused
        return [
            {
                "id": ids[0],
                "payload_ref": "code/ref.py",
                "language": "python",
                "filepath": "ref.py",
                "assertion_type": None,
                "metadata": None,
                "content_fts": "def main",
                "name": "main",
                "node_type": "function",
                "start_line": 1,
                "end_line": 10,
            }
        ]

    mock_conn.fetch = AsyncMock(side_effect=_capture_fetch)

    # Scoped session mock
    @asynccontextmanager
    async def _fake_scoped(_pool, _namespace_id):
        yield mock_conn

    # Check search with code_intent aspect
    with patch("nce.orchestrators.graph.scoped_pg_session", _fake_scoped):
        with patch(
            "nce.orchestrators.graph.fetch_code_files_raw_by_ref",
            new_callable=AsyncMock,
            return_value={},
        ):
            with patch("nce.orchestrators.graph.normalize_payload_ref", side_effect=lambda x: x):
                results = await graph_orch.search_codebase(
                    "test query", namespace_id=NS, aspect="code_intent"
                )

    assert len(results) == 1
    assert len(sql_calls) >= 1
    # Check that SQL joins and filters by aspect
    fused_sql = sql_calls[0][0]
    assert "JOIN embedding_aspects ea ON m.id = ea.memory_id AND ea.aspect =" in fused_sql
    assert "ea.embedding <=> $1::vector" in fused_sql


@pytest.mark.asyncio
async def test_process_code_indexing_stores_aspects() -> None:
    # Set up mock engine
    engine_mock = MagicMock()
    engine_mock.connect = AsyncMock()
    engine_mock.disconnect = AsyncMock()
    pg_pool_mock = MagicMock()
    engine_mock.pg_pool = pg_pool_mock

    # Mock connection and transaction
    conn_mock = AsyncMock()
    conn_mock.transaction = MagicMock()
    conn_mock.transaction.return_value.__aenter__ = AsyncMock(return_value=conn_mock)
    conn_mock.transaction.return_value.__aexit__ = AsyncMock(return_value=False)

    engine_mock.scoped_session.return_value.__aenter__ = AsyncMock(return_value=conn_mock)
    engine_mock.scoped_session.return_value.__aexit__ = AsyncMock(return_value=False)

    mongo_client = MagicMock()
    engine_mock.mongo_client = mongo_client
    mongo_db = MagicMock()
    mongo_client.memory_archive = mongo_db
    collection_mock = MagicMock()
    mongo_db.code_files = collection_mock
    collection_mock.insert_one = AsyncMock(
        return_value=MagicMock(inserted_id="0123456789abcdef01234567")
    )

    redis_client = AsyncMock()
    engine_mock.redis_client = redis_client

    # Mock embeddings to return fixed vectors
    mock_vectors = [[0.1] * 768] * 3  # primary, code, nl

    with patch("nce.tasks.NCEEngine", return_value=engine_mock):
        with patch(
            "nce.tasks._embeddings.embed_batch", new_callable=AsyncMock, return_value=mock_vectors
        ):
            with patch("nce.tasks._get_redis", return_value=MagicMock()):
                res = process_code_indexing(
                    filepath="foo.py",
                    raw_code="def bar():\n    pass",
                    language="python",
                    namespace_id=NS,
                )

    assert res == {"status": "success", "chunks": 1}
    # Check SQL executes
    execute_calls = [args[0] for args, _ in conn_mock.execute.call_args_list]
    insert_aspect_calls = [sql for sql in execute_calls if "INSERT INTO embedding_aspects" in sql]
    assert len(insert_aspect_calls) == 2
    assert any("code_intent" in sql for sql in insert_aspect_calls)
    assert any("nl_intent" in sql for sql in insert_aspect_calls)


def test_jaccard_overlap_passes() -> None:
    old = ["a", "b", "c", "d"]
    new = ["b", "c", "d", "e"]
    overlap = neighbor_overlap_fraction(old, new)
    assert overlap == 0.6  # intersection 3 / union 5 = 0.6
    assert neighbor_overlap_fraction([], []) == 1.0
    assert neighbor_overlap_fraction(["a"], []) == 0.0
