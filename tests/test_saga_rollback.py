"""
Saga rollback tests for NCEEngine.store_memory().

Verifies the phase-aware universal rollback across Mongo, Postgres, and the
Knowledge Graph.  Each test simulates a failure at a specific saga stage and
asserts that all artefacts from earlier stages are cleanly removed.

NOTE: graph_extract and pii_process are imported *inside* store_memory() with
  from nce.graph_extractor import extract as graph_extract
  from nce.pii import process as pii_process
so patches must target the source modules, not nce.orchestrator.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_payload(**overrides):
    from nce.models import AssertionType, MemoryType, StoreMemoryRequest

    defaults = dict(
        namespace_id="00000000-0000-4000-8000-000000000001",
        agent_id="test-agent",
        content="Hello world",
        summary="Test summary for saga rollback",
        heavy_payload="Heavy payload content",
        memory_type=MemoryType.episodic,
        assertion_type=AssertionType.fact,
        metadata={"user_id": "user-1", "session_id": "sess-1"},
        check_contradictions=False,
    )
    defaults.update(overrides)
    return StoreMemoryRequest(**defaults)


class _FakeInsertResult:
    def __init__(self, inserted_id: str):
        self.inserted_id = inserted_id


def _make_mongo_mock():
    collection = AsyncMock()
    collection.insert_one = AsyncMock(return_value=_FakeInsertResult("507f1f77bcf86cd799439011"))
    collection.delete_one = AsyncMock()
    db = MagicMock()
    type(db).episodes = PropertyMock(return_value=collection)
    mongo_client = MagicMock()
    type(mongo_client).memory_archive = PropertyMock(return_value=db)
    return mongo_client, collection


class _FakeConn:
    """Minimal asyncpg connection double."""

    def __init__(self, fetchrow_result=None, fetchval_result=None, fetch_result=None):
        self.fetchrow = AsyncMock(return_value=fetchrow_result)
        self.fetchval = AsyncMock(return_value=fetchval_result)
        self._fetch_result = fetch_result or []
        self.execute = AsyncMock()
        self.executemany = AsyncMock()

    async def fetch(self, query: str = "", *args):
        # Return node rows for kg_nodes RETURNING, model rows otherwise
        if "kg_nodes" in query and "RETURNING" in query:
            labels = args[0] if args else []
            return [{"id": f"node-{i}", "label": label} for i, label in enumerate(labels)]
        return self._fetch_result

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    def transaction(self):
        return self


def _make_pg_mock():
    conn = _FakeConn(
        fetchrow_result={"id": "saga-uuid-123", "metadata": "{}"},
        fetchval_result="mem-uuid-0001",
        fetch_result=[{"id": "model-1"}],
    )
    pool = MagicMock()
    pool.acquire = MagicMock()
    pool.acquire.return_value = conn
    return pool, conn


def _pii_mock(sanitized="sanitized", redacted=False, entities_found=0, vault_entries=None):
    m = MagicMock()
    m.sanitized_text = sanitized
    m.redacted = redacted
    m.entities_found = entities_found
    m.vault_entries = vault_entries or []
    return m


# ---------------------------------------------------------------------------
# Patch targets — graph_extract/pii_process/append_event are imported inside
# store_memory(), so we patch the SOURCE modules, not orchestrator.
# ---------------------------------------------------------------------------
_P_EMBED = "nce.orchestrator._embeddings.embed_batch"
_P_GRAPH = "nce.graph_extractor.extract"
_P_PII = "nce.pii.process"
_P_EVENT = "nce.event_log.append_event"


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def engine():
    from nce.orchestrator import NCEEngine

    eng = NCEEngine()
    eng.mongo_client, eng._mongo_collection = _make_mongo_mock()
    eng.pg_pool, eng._pg_conn = _make_pg_mock()
    eng.redis_client = AsyncMock()
    eng.redis_client.setex = AsyncMock()

    # scoped_session must be a proper async context manager,
    # not a bare async generator.
    @asynccontextmanager
    async def _scoped(_ns):
        yield eng._pg_conn

    eng.scoped_session = _scoped

    return eng


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rollback_mongo_when_pg_transaction_fails(engine):
    """Failure during PG transaction -> Mongo rolled back, PG auto-rolled back."""
    payload = _make_payload()
    engine._pg_conn.fetchval = AsyncMock(side_effect=Exception("PG insert exploded"))

    with patch(_P_EMBED, return_value=[[0.1] * 768, [0.2] * 768]):
        with patch(_P_GRAPH, return_value=([], [])):
            with patch(_P_PII, return_value=_pii_mock()):
                with pytest.raises(Exception, match="PG insert exploded"):
                    await engine.store_memory(payload)

    engine._mongo_collection.delete_one.assert_called_once()
    calls = [c[0][0] for c in engine._pg_conn.execute.call_args_list]
    pg_sql = " ".join(calls)
    assert "DELETE FROM kg_edges" in pg_sql or "DELETE FROM memories" in pg_sql


@pytest.mark.asyncio
async def test_rollback_all_stores_when_post_pg_failure(engine):
    """After PG commit, Redis cache failures are advisory and do not roll back the saga."""
    payload = _make_payload(
        metadata={"user_id": "user-1", "session_id": "sess-1"},
    )
    engine.redis_client.setex = AsyncMock(side_effect=Exception("Redis exploded"))

    from nce.models import KGEdge, KGNode

    entities = [KGNode(label="Alice", entity_type="Person", source_text="Alice")]
    triplets = [
        KGEdge(subject_label="Alice", predicate="knows", object_label="Bob", confidence=0.9)
    ]
    vault = [
        {"token": "tok1", "encrypted_value": "enc1", "entity_type": "EMAIL"},
        {"token": "tok2", "encrypted_value": "enc2", "entity_type": "PHONE"},
    ]

    with patch(_P_EMBED, return_value=[[0.1] * 768, [0.2] * 768]):
        with patch(_P_GRAPH, return_value=(entities, triplets)):
            with patch(
                _P_PII,
                return_value=_pii_mock(
                    redacted=True,
                    entities_found=2,
                    vault_entries=vault,
                ),
            ):
                with patch(_P_EVENT, return_value=None):
                    result = await engine.store_memory(payload)

    assert result["payload_ref"] is not None
    engine._mongo_collection.delete_one.assert_not_called()


@pytest.mark.asyncio
async def test_no_rollback_when_failure_before_mongo(engine):
    """Failure before Mongo insert -> nothing to roll back."""
    payload = _make_payload()

    with patch(_P_GRAPH, return_value=([], [])):
        with patch(_P_PII, side_effect=Exception("PII processing exploded")):
            with pytest.raises(Exception, match="PII processing exploded"):
                await engine.store_memory(payload)

    engine._mongo_collection.delete_one.assert_not_called()
    pg_delete_calls = [
        c for c in engine._pg_conn.execute.call_args_list if "DELETE" in str(c[0][0])
    ]
    assert len(pg_delete_calls) == 0


@pytest.mark.asyncio
async def test_rollback_does_not_mask_original_exception(engine):
    """Mongo rollback failure must NOT mask the original saga exception."""
    payload = _make_payload()
    engine._mongo_collection.delete_one = AsyncMock(
        side_effect=Exception("Mongo delete also failed")
    )
    engine._pg_conn.fetchval = AsyncMock(side_effect=Exception("Original PG failure"))

    with patch(_P_EMBED, return_value=[[0.1] * 768]):
        with patch(_P_GRAPH, return_value=([], [])):
            with patch(_P_PII, return_value=_pii_mock()):
                with pytest.raises(Exception) as exc_info:
                    await engine.store_memory(payload)

    assert "Original PG failure" in str(exc_info.value)


@pytest.mark.asyncio
async def test_rollback_mongo_when_embedding_fails(engine):
    """Failure during embedding -> Mongo rolled back, PG safety cleanup."""
    payload = _make_payload()

    with patch(_P_EMBED, side_effect=Exception("Embedding service unavailable")):
        with patch(_P_GRAPH, return_value=([], [])):
            with patch(_P_PII, return_value=_pii_mock()):
                with pytest.raises(Exception, match="Embedding service unavailable"):
                    await engine.store_memory(payload)

    engine._mongo_collection.delete_one.assert_called_once()
    pg_delete_calls = [
        c for c in engine._pg_conn.execute.call_args_list if "DELETE" in str(c[0][0])
    ]
    assert len(pg_delete_calls) >= 1
