"""
Tests for Phase 2.1 Re-embedding Worker (nce/reembedding_worker.py).

All async functions are driven via asyncio.run() to sidestep pytest-asyncio.
Embed calls are stubbed so tests run without a GPU / SentenceTransformer.
DB interactions use asyncpg AsyncMock — no live Postgres required.
"""

import asyncio
import json
import uuid
from datetime import datetime

try:
    from datetime import timezone
except ImportError:
    timezone.utc = timezone.utc
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nce import reembedding_worker as rw
from nce.reembedding_worker import (
    ReembeddingWorker,
    _fallback_text,
    _fetch_kg_nodes_batch,
    _fetch_memories_batch,
    _resolve_texts_from_mongo,
    _update_kg_nodes_batch,
    _update_memories_batch,
    current_model_uuid,
)

pytestmark = pytest.mark.heavy

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

_FAKE_VEC = [0.1] * 768


def _make_pool(conn: AsyncMock) -> MagicMock:
    """Wrap a fake connection in a context-manager pool."""
    pool = MagicMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire.return_value = ctx
    return pool


def _make_conn() -> AsyncMock:
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value="UPDATE 1")
    conn.executemany = AsyncMock(return_value=None)
    conn.fetchval = AsyncMock(return_value=uuid.uuid4())
    conn.fetch = AsyncMock(return_value=[])
    conn.transaction = MagicMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=None)
    ctx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction.return_value = ctx
    return conn


def _fake_memory_record(memory_type: str = "episodic") -> MagicMock:
    rec = MagicMock()
    rec.__getitem__ = lambda s, k: {
        "id": uuid.uuid4(),
        "created_at": datetime(2024, 1, 1, tzinfo=timezone.utc),
        "memory_type": memory_type,
        "payload_ref": "a" * 24,
        "name": "test_memory",
        "filepath": None,
    }[k]
    rec.get = lambda k, default=None: {
        "id": rec["id"],
        "created_at": rec["created_at"],
        "memory_type": memory_type,
        "payload_ref": "a" * 24,
        "name": "test_memory",
        "filepath": None,
    }.get(k, default)
    return rec


def _fake_kg_record() -> MagicMock:
    rec = MagicMock()
    _id = uuid.uuid4()
    rec.__getitem__ = lambda s, k: {"id": _id, "label": "TestEntity"}[k]
    rec.get = lambda k, d=None: {"id": _id, "label": "TestEntity"}.get(k, d)
    return rec


# --------------------------------------------------------------------------- #
# Unit: current_model_uuid() is deterministic
# --------------------------------------------------------------------------- #


def test_current_model_uuid_is_deterministic():
    a = current_model_uuid()
    b = current_model_uuid()
    assert a == b
    assert isinstance(a, uuid.UUID)


# --------------------------------------------------------------------------- #
# Unit: _fallback_text
# --------------------------------------------------------------------------- #


def test_fallback_text_uses_name_and_filepath():
    rec = MagicMock()
    rec.get = lambda k, d=None: {"name": "foo", "filepath": "bar/baz.py"}.get(k, d)
    text = _fallback_text(rec, 200)
    assert "foo" in text
    assert "bar/baz.py" in text


def test_fallback_text_clips_to_max_chars():
    rec = MagicMock()
    rec.get = lambda k, d=None: {"name": "x" * 300, "filepath": None}.get(k, d)
    text = _fallback_text(rec, 50)
    assert len(text) <= 50


def test_fallback_text_empty_when_no_fields():
    rec = MagicMock()
    rec.get = lambda k, d=None: None
    assert _fallback_text(rec, 100) == ""


# --------------------------------------------------------------------------- #
# Unit: _fetch_memories_batch — verifies SQL paths without live PG
# --------------------------------------------------------------------------- #


def test_fetch_memories_batch_initial_cursor():
    conn = _make_conn()
    conn.fetch = AsyncMock(return_value=[])

    asyncio.run(_fetch_memories_batch(conn, current_model_uuid(), 32, None, None))

    conn.fetch.assert_awaited_once()
    sql = conn.fetch.await_args.args[0].lower()
    assert "embedding_model_id" in sql
    assert "order" in sql
    # Initial cursor must NOT reference cursor position
    assert "cursor" not in sql


def test_fetch_memories_batch_with_cursor():
    conn = _make_conn()
    conn.fetch = AsyncMock(return_value=[])
    ts = datetime(2024, 6, 1, tzinfo=timezone.utc)
    cid = uuid.uuid4()

    asyncio.run(_fetch_memories_batch(conn, current_model_uuid(), 32, ts, cid))

    conn.fetch.assert_awaited_once()
    sql = conn.fetch.await_args.args[0].lower()
    assert "created_at" in sql  # composite keyset present


# --------------------------------------------------------------------------- #
# Unit: _fetch_kg_nodes_batch
# --------------------------------------------------------------------------- #


def test_fetch_kg_nodes_batch_initial():
    conn = _make_conn()
    conn.fetch = AsyncMock(return_value=[])

    asyncio.run(_fetch_kg_nodes_batch(conn, current_model_uuid(), 16, None))

    sql = conn.fetch.await_args.args[0].lower()
    assert "kg_nodes" in sql
    assert "order by id" in sql


def test_fetch_kg_nodes_batch_with_cursor():
    conn = _make_conn()
    conn.fetch = AsyncMock(return_value=[])
    cid = uuid.uuid4()

    asyncio.run(_fetch_kg_nodes_batch(conn, current_model_uuid(), 16, cid))

    sql = conn.fetch.await_args.args[0].lower()
    assert "id > $" in sql


# --------------------------------------------------------------------------- #
# Unit: _update_memories_batch — wraps in transaction, uses correct SQL
# --------------------------------------------------------------------------- #


def test_update_memories_batch_calls_executemany():
    conn = _make_conn()
    model_uuid = current_model_uuid()
    mem_id = uuid.uuid4()
    created_at = datetime(2024, 1, 1, tzinfo=timezone.utc)
    vec = _FAKE_VEC

    asyncio.run(_update_memories_batch(conn, [(mem_id, created_at, vec)], model_uuid))

    conn.executemany.assert_awaited_once()
    sql = conn.executemany.await_args.args[0].lower()
    assert "update memories" in sql
    assert "embedding_model_id" in sql

    rows = conn.executemany.await_args.args[1]
    assert len(rows) == 1
    # payload row: (json_vec, model_str, mem_id, created_at)
    assert rows[0][2] == mem_id
    assert rows[0][3] == created_at
    assert json.loads(rows[0][0]) == _FAKE_VEC


# --------------------------------------------------------------------------- #
# Unit: _update_kg_nodes_batch
# --------------------------------------------------------------------------- #


def test_update_kg_nodes_batch_calls_executemany():
    conn = _make_conn()
    node_id = uuid.uuid4()

    asyncio.run(_update_kg_nodes_batch(conn, [(node_id, _FAKE_VEC)], current_model_uuid()))

    conn.executemany.assert_awaited_once()
    sql = conn.executemany.await_args.args[0].lower()
    assert "update kg_nodes" in sql
    rows = conn.executemany.await_args.args[1]
    assert len(rows) == 1
    # payload row: (json_vec, model_str, node_id)
    assert rows[0][2] == node_id
    assert json.loads(rows[0][0]) == _FAKE_VEC


# --------------------------------------------------------------------------- #
# Unit: _resolve_texts_from_mongo — batch lookup, collection routing
# --------------------------------------------------------------------------- #


def test_resolve_texts_returns_episodic_raw_data():
    ref = "b" * 24

    rec = MagicMock()
    rec.get = lambda k, d=None: {
        "payload_ref": ref,
        "memory_type": "episodic",
        "namespace_id": "00000000-0000-0000-0000-000000000001",
    }.get(k, d)

    class _FakeId(str):
        """str subclass so str(doc["_id"]) == ref."""

    async def _fake_find(*_, **__):
        yield {"_id": _FakeId(ref), "raw_data": "hello world"}

    mongo_client = MagicMock()
    mongo_client.memory_archive.episodes.find = _fake_find

    # ObjectId is imported *locally* inside the function; patch it at its
    # source so that ObjectId(ref) just returns the string ref unchanged.
    with patch("bson.ObjectId", side_effect=lambda x: _FakeId(x)):
        result, failed = asyncio.run(
            _resolve_texts_from_mongo(mongo_client, [rec], max_text_chars=512)
        )

    assert result.get(ref) == "hello world"
    assert not failed


# --------------------------------------------------------------------------- #
# Integration: ReembeddingWorker.run_once — happy path, no rows
# --------------------------------------------------------------------------- #


def test_worker_run_once_no_stale_rows():
    """When there are no stale memories, the worker completes with 0 updates."""
    conn = _make_conn()
    run_uuid = uuid.uuid4()
    conn.fetchval = AsyncMock(return_value=run_uuid)
    # First fetch: no rows → terminates immediately
    conn.fetch = AsyncMock(return_value=[])

    pool = _make_pool(conn)

    with patch.object(rw, "_embeddings") as mock_emb:
        mock_emb.embed_batch = AsyncMock(return_value=[])
        result = asyncio.run(ReembeddingWorker(batch_size=8, batches_per_minute=600).run_once(pool))

    assert result["status"] == "completed"
    assert result["memories_done"] == 0
    assert result["kg_nodes_done"] == 0


# --------------------------------------------------------------------------- #
# Integration: ReembeddingWorker.run_once — processes one batch of memories
# --------------------------------------------------------------------------- #


def test_worker_processes_one_memory_batch():
    """Worker fetches one page of rows, embeds them, and marks run completed."""
    conn = _make_conn()
    run_uuid = uuid.uuid4()
    conn.fetchval = AsyncMock(return_value=run_uuid)

    fake_row = _fake_memory_record("episodic")
    # First fetch returns one row; second fetch returns nothing (end of cursor).
    conn.fetch = AsyncMock(side_effect=[[fake_row], [], []])

    pool = _make_pool(conn)

    with patch.object(rw, "_embeddings") as mock_emb:
        mock_emb.embed_batch = AsyncMock(return_value=[_FAKE_VEC])
        result = asyncio.run(
            ReembeddingWorker(
                batch_size=32,
                batches_per_minute=600,  # sleep ≈ 0.1 s
            ).run_once(pool, mongo_client=None)
        )

    assert result["status"] == "completed"
    # embed_batch called once (the batch with one row)
    mock_emb.embed_batch.assert_awaited_once()
    # executemany called once for the UPDATE
    conn.executemany.assert_awaited()


# --------------------------------------------------------------------------- #
# Integration: max_rows_per_run stops early
# --------------------------------------------------------------------------- #


def test_worker_respects_max_rows_per_run():
    conn = _make_conn()
    conn.fetchval = AsyncMock(return_value=uuid.uuid4())

    row = _fake_memory_record()
    # Return rows indefinitely — worker must stop at max_rows_per_run.
    conn.fetch = AsyncMock(return_value=[row])

    pool = _make_pool(conn)

    with patch.object(rw, "_embeddings") as mock_emb:
        mock_emb.embed_batch = AsyncMock(return_value=[_FAKE_VEC])
        result = asyncio.run(
            ReembeddingWorker(
                batch_size=1,
                batches_per_minute=600,
                max_rows_per_run=1,
            ).run_once(pool, mongo_client=None)
        )

    assert result["status"] == "completed"
    assert result["memories_done"] == 1
    # embed_batch must have been called exactly once
    mock_emb.embed_batch.assert_awaited_once()


# --------------------------------------------------------------------------- #
# Integration: embed failure propagates and run is marked 'failed'
# --------------------------------------------------------------------------- #


def test_worker_marks_run_failed_on_embed_error():
    conn = _make_conn()
    conn.fetchval = AsyncMock(return_value=uuid.uuid4())

    row = _fake_memory_record()
    conn.fetch = AsyncMock(return_value=[row])

    pool = _make_pool(conn)

    with patch.object(rw, "_embeddings") as mock_emb:
        mock_emb.embed_batch = AsyncMock(side_effect=RuntimeError("GPU OOM"))
        with pytest.raises(RuntimeError, match="GPU OOM"):
            asyncio.run(
                ReembeddingWorker(batch_size=1, batches_per_minute=600).run_once(
                    pool, mongo_client=None
                )
            )

    # The final UPDATE must set status='failed'
    final_execute_calls = conn.execute.await_args_list
    assert any("failed" in str(call) for call in final_execute_calls), (
        "Expected status='failed' in final UPDATE"
    )


# --------------------------------------------------------------------------- #
# Integration: kg_nodes phase runs when include_kg_nodes=True
# --------------------------------------------------------------------------- #


def test_worker_processes_kg_nodes_when_enabled():
    conn = _make_conn()
    conn.fetchval = AsyncMock(return_value=uuid.uuid4())

    kg_row = _fake_kg_record()
    # memories fetch: no rows → skip Phase A
    # kg_nodes fetch: one row, then empty
    conn.fetch = AsyncMock(side_effect=[[], [kg_row], []])

    pool = _make_pool(conn)

    with patch.object(rw, "_embeddings") as mock_emb:
        mock_emb.embed_batch = AsyncMock(return_value=[_FAKE_VEC])
        result = asyncio.run(
            ReembeddingWorker(
                batch_size=32,
                batches_per_minute=600,
                include_kg_nodes=True,
            ).run_once(pool, mongo_client=None)
        )

    assert result["status"] == "completed"
    assert result["kg_nodes_done"] == 1
    mock_emb.embed_batch.assert_awaited_once()


@pytest.mark.asyncio
async def test_concurrent_reembedding_workers_redis_lock_contention():
    """If two workers run _embed concurrently:
    1. One worker successfully acquires the lock.
    2. The other worker fails to acquire the lock, logs a warning, and still proceeds safely.
    """
    import nce.reembedding_worker as rw
    from nce.reembedding_worker import ReembeddingWorker

    worker1 = ReembeddingWorker()
    worker2 = ReembeddingWorker()

    # Mock redis client and acquire/release lock helpers
    fake_client = MagicMock()
    worker1._redis_client = fake_client
    worker2._redis_client = fake_client

    lock_acquisitions = []

    async def mock_acquire(client, key, ttl):
        # Allow first call to succeed, second to fail (return None)
        if not lock_acquisitions:
            token = "token-1"
            lock_acquisitions.append(token)
            return token
        return None

    async def mock_release(client, key, token):
        pass

    fake_pool = MagicMock()

    with (
        patch("nce.reembedding_worker.cfg.REDIS_URL", "redis://localhost"),
        patch("nce.reembedding_worker._acquire_redis_lock", side_effect=mock_acquire),
        patch("nce.reembedding_worker._release_redis_lock", side_effect=mock_release),
        patch.object(
            rw._embeddings, "embed_batch", new_callable=AsyncMock, return_value=[_FAKE_VEC]
        ) as mock_embed,
    ):
        # Trigger both concurrently
        res1, res2 = await asyncio.gather(
            worker1._embed(fake_pool, ["text1"]), worker2._embed(fake_pool, ["text2"])
        )

        assert res1 == [_FAKE_VEC]
        assert res2 == [_FAKE_VEC]
        # Verify both embedded the text
        assert mock_embed.call_count == 2


# --------------------------------------------------------------------------- #
# VRAM guard — (a) below watermark: embeds normally and emits the gauge
# --------------------------------------------------------------------------- #


def test_vram_gate_below_watermark_embeds_and_emits_gauge():
    """(a) VRAM below watermark → embeds normally; allocated gauge is set."""
    import sys
    import types

    # Build a minimal torch stub that reports 50 % utilisation (below 0.85 default).
    torch_stub = types.ModuleType("torch")
    cuda_stub = types.SimpleNamespace(
        is_available=lambda: True,
        memory_allocated=lambda: 500_000_000,
        memory_reserved=lambda: 600_000_000,
        max_memory_allocated=lambda: 550_000_000,
        empty_cache=lambda: None,
        reset_peak_memory_stats=lambda: None,
        get_device_properties=lambda idx: types.SimpleNamespace(total_memory=1_000_000_000),
    )
    torch_stub.cuda = cuda_stub

    worker = ReembeddingWorker(batches_per_minute=600)

    allocated_set: list[float] = []

    fake_gauge = MagicMock()
    fake_gauge.labels.return_value.set = MagicMock(side_effect=lambda v: allocated_set.append(v))

    with (
        patch.dict(sys.modules, {"torch": torch_stub}),
        patch("nce.reembedding_worker.REEMBEDDER_VRAM_ALLOCATED", fake_gauge),
        patch("nce.reembedding_worker.REEMBEDDER_VRAM_RESERVED", MagicMock()),
        patch("nce.reembedding_worker.REEMBEDDER_VRAM_PEAK", MagicMock()),
        patch("nce.reembedding_worker.cfg") as mock_cfg,
    ):
        mock_cfg.NCE_REEMBED_VRAM_HIGH_WATERMARK = 0.85
        mock_cfg.NCE_REEMBED_VRAM_MAX_PRESSURE_WAITS = 12
        mock_cfg.REDIS_URL = ""

        conn = _make_conn()
        pool = _make_pool(conn)

        with patch("nce.reembedding_worker._embeddings") as mock_emb:
            mock_emb.embed_batch = AsyncMock(return_value=[_FAKE_VEC])
            result = asyncio.run(worker._embed(pool, ["hello"]))

        assert result == [_FAKE_VEC], "Expected embed result to be returned"
        mock_emb.embed_batch.assert_awaited_once()
        # The allocated gauge must have been set with the stubbed value.
        assert any(v == 500_000_000 for v in allocated_set), (
            f"Expected allocated gauge to be emitted with 500_000_000; got {allocated_set}"
        )


# --------------------------------------------------------------------------- #
# VRAM guard — (b) saturated forever: VRAMPressureError; run_once exits cleanly
# --------------------------------------------------------------------------- #


def test_vram_gate_saturated_raises_pressure_error_and_run_once_exits_cleanly():
    """(b) VRAM always at 95 % → VRAMPressureError after MAX_PRESSURE_WAITS;
    run_once returns status='vram_paused', no exception escapes."""
    import sys
    import types

    torch_stub = types.ModuleType("torch")
    cuda_stub = types.SimpleNamespace(
        is_available=lambda: True,
        memory_allocated=lambda: 950_000_000,  # 95 % of 1 GB — always above watermark
        memory_reserved=lambda: 960_000_000,
        max_memory_allocated=lambda: 970_000_000,
        empty_cache=lambda: None,
        reset_peak_memory_stats=lambda: None,
        get_device_properties=lambda idx: types.SimpleNamespace(total_memory=1_000_000_000),
    )
    torch_stub.cuda = cuda_stub

    # Use only 1 wait cycle to keep the test fast.
    worker = ReembeddingWorker(batches_per_minute=600)

    conn = _make_conn()
    conn.fetchval = AsyncMock(return_value=uuid.uuid4())
    # Return one stale memory so _embed is actually invoked.
    fake_row = _fake_memory_record("episodic")
    conn.fetch = AsyncMock(return_value=[fake_row])
    pool = _make_pool(conn)

    sleep_calls: list[float] = []

    async def fast_sleep(t: float) -> None:
        sleep_calls.append(t)

    with (
        patch.dict(sys.modules, {"torch": torch_stub}),
        patch("nce.reembedding_worker.REEMBEDDER_VRAM_ALLOCATED", MagicMock()),
        patch("nce.reembedding_worker.REEMBEDDER_VRAM_RESERVED", MagicMock()),
        patch("nce.reembedding_worker.REEMBEDDER_VRAM_PEAK", MagicMock()),
        patch("nce.reembedding_worker.asyncio.sleep", side_effect=fast_sleep),
        patch("nce.reembedding_worker.cfg") as mock_cfg,
        patch("nce.reembedding_worker._embeddings") as mock_emb,
    ):
        mock_cfg.NCE_REEMBED_VRAM_HIGH_WATERMARK = 0.85
        mock_cfg.NCE_REEMBED_VRAM_MAX_PRESSURE_WAITS = 1  # only 1 wait
        mock_cfg.REDIS_URL = ""
        mock_cfg.REEMBED_BATCH_SIZE = 32
        mock_cfg.REEMBED_BATCHES_PER_MINUTE = 600
        mock_cfg.REEMBED_MAX_ROWS_PER_RUN = 0
        mock_cfg.REEMBED_INCLUDE_KG_NODES = False
        mock_cfg.REEMBED_MAX_TEXT_CHARS = 4096
        mock_cfg.REEMBED_CRON_INTERVAL_MINUTES = 60
        mock_emb.embed_batch = AsyncMock(return_value=[_FAKE_VEC])

        result = asyncio.run(worker.run_once(pool, mongo_client=None))

    # No exception must escape run_once.
    assert result["status"] == "vram_paused", f"Expected vram_paused, got {result['status']}"
    # embed_batch must NOT have been called (gate fired before it).
    mock_emb.embed_batch.assert_not_awaited()
    # The gate must have slept exactly max_pressure_waits times (1).
    assert len(sleep_calls) >= 1


# --------------------------------------------------------------------------- #
# VRAM guard — (c) CUDA absent: gate is a no-op
# --------------------------------------------------------------------------- #


def test_vram_gate_no_op_when_cuda_absent():
    """(c) torch.cuda.is_available() == False → gate returns immediately,
    embed proceeds normally."""
    import sys
    import types

    torch_stub = types.ModuleType("torch")
    cuda_stub = types.SimpleNamespace(
        is_available=lambda: False,  # CPU-only
        memory_allocated=lambda: 0,
        memory_reserved=lambda: 0,
        max_memory_allocated=lambda: 0,
        empty_cache=lambda: None,
        reset_peak_memory_stats=lambda: None,
        get_device_properties=lambda idx: types.SimpleNamespace(total_memory=0),
    )
    torch_stub.cuda = cuda_stub

    worker = ReembeddingWorker(batches_per_minute=600)

    gauge_set_calls: list[object] = []

    fake_gauge = MagicMock()
    fake_gauge.labels.return_value.set = MagicMock(side_effect=lambda v: gauge_set_calls.append(v))

    with (
        patch.dict(sys.modules, {"torch": torch_stub}),
        patch("nce.reembedding_worker.REEMBEDDER_VRAM_ALLOCATED", fake_gauge),
        patch("nce.reembedding_worker.REEMBEDDER_VRAM_RESERVED", MagicMock()),
        patch("nce.reembedding_worker.REEMBEDDER_VRAM_PEAK", MagicMock()),
        patch("nce.reembedding_worker.cfg") as mock_cfg,
    ):
        mock_cfg.NCE_REEMBED_VRAM_HIGH_WATERMARK = 0.85
        mock_cfg.NCE_REEMBED_VRAM_MAX_PRESSURE_WAITS = 12
        mock_cfg.REDIS_URL = ""

        conn = _make_conn()
        pool = _make_pool(conn)

        with patch("nce.reembedding_worker._embeddings") as mock_emb:
            mock_emb.embed_batch = AsyncMock(return_value=[_FAKE_VEC])
            result = asyncio.run(worker._embed(pool, ["hello"]))

        assert result == [_FAKE_VEC], "Expected embed result when CUDA absent"
        mock_emb.embed_batch.assert_awaited_once()
        # No gauge was emitted (gate exited early for CPU-only).
        assert len(gauge_set_calls) == 0, (
            f"Expected no gauge emissions for CPU-only, got {gauge_set_calls}"
        )
