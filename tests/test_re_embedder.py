import asyncio
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bson import ObjectId

from nce.re_embedder import run_re_embedding_worker

# --------------------------------------------------------------------------- #
# Helpers & Mocks
# --------------------------------------------------------------------------- #

_FAKE_VEC = [0.1] * 768


def _make_pool(conn: AsyncMock) -> MagicMock:
    pool = MagicMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire.return_value = ctx
    return pool


def _make_conn() -> AsyncMock:
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value="UPDATE 1")
    conn.fetchval = AsyncMock(return_value=uuid.uuid4())
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchrow = AsyncMock(return_value=None)
    conn.transaction = MagicMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=None)
    ctx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction.return_value = ctx
    return conn


class AsyncIteratorMock:
    def __init__(self, items):
        self.items = items
        self.cursor = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.cursor >= len(self.items):
            raise StopAsyncIteration
        item = self.items[self.cursor]
        self.cursor += 1
        return item


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_re_embedder_no_active_migrations():
    conn = _make_conn()
    pool = _make_pool(conn)
    mongo = MagicMock()

    # Stub loop termination via sleep raising exception
    with patch("asyncio.sleep", side_effect=asyncio.CancelledError) as mock_sleep:
        with pytest.raises(asyncio.CancelledError):
            await run_re_embedding_worker(pool, mongo)

        conn.fetchrow.assert_called_once()
        mock_sleep.assert_called_once_with(10)


@pytest.mark.asyncio
@patch("nce.embeddings.embed_batch", new_callable=AsyncMock)
async def test_re_embedder_processes_memories_batch_successfully(mock_embed):
    conn = _make_conn()
    pool = _make_pool(conn)

    # Active migration found
    migration_id = uuid.uuid4()
    target_model_id = uuid.uuid4()
    conn.fetchrow.return_value = {
        "id": migration_id,
        "target_model_id": target_model_id,
        "last_memory_id": None,
        "last_node_id": None,
        "model_name": "test-model",
    }

    # First fetch returns memory records
    mem_id = uuid.uuid4()
    oid_str = "a" * 24
    conn.fetch.return_value = [{"id": mem_id, "payload_ref": oid_str, "namespace_id": uuid.uuid4()}]

    # Mock MongoDB
    mongo = MagicMock()
    db = MagicMock()
    mongo.memory_archive = db

    # Mock find cursor returning doc using actual ObjectId
    db.episodes.find.return_value = AsyncIteratorMock(
        [{"_id": ObjectId(oid_str), "raw_data": "Can embed this text!"}]
    )

    # Mock Embed batch vector
    mock_embed.return_value = [_FAKE_VEC]

    # To break the infinite loop inside worker after one pass, we raise CancelledError on the second cycle
    conn.fetchrow.side_effect = [conn.fetchrow.return_value, asyncio.CancelledError()]

    with pytest.raises(asyncio.CancelledError):
        await run_re_embedding_worker(pool, mongo)

    # Asserts
    db.episodes.find.assert_called_once()
    find_args = db.episodes.find.call_args[0][0]
    # Verify we batched using $in bulk lookups
    assert "$in" in find_args["_id"]
    assert find_args["_id"]["$in"] == [ObjectId(oid_str)]

    mock_embed.assert_awaited_once_with(["Can embed this text!"])

    # Check that database inserts occurred
    conn.execute.assert_any_call(
        """
                                    INSERT INTO memory_embeddings (memory_id, model_id, embedding)
                                    VALUES ($1, $2, $3::vector)
                                    ON CONFLICT DO NOTHING
                                    """,
        mem_id,
        target_model_id,
        json.dumps(_FAKE_VEC),
    )


@pytest.mark.asyncio
@patch("nce.embeddings.embed_batch", new_callable=AsyncMock)
async def test_re_embedder_skips_invalid_payload_refs(mock_embed):
    conn = _make_conn()
    pool = _make_pool(conn)

    migration_id = uuid.uuid4()
    target_model_id = uuid.uuid4()
    conn.fetchrow.return_value = {
        "id": migration_id,
        "target_model_id": target_model_id,
        "last_memory_id": None,
        "last_node_id": None,
        "model_name": "test-model",
    }

    # One valid payload_ref, one invalid
    mem_id_1 = uuid.uuid4()
    mem_id_2 = uuid.uuid4()
    ns_id = uuid.uuid4()
    conn.fetch.return_value = [
        {"id": mem_id_1, "payload_ref": "invalid-non-hex", "namespace_id": ns_id},
        {"id": mem_id_2, "payload_ref": "b" * 24, "namespace_id": ns_id},
    ]

    mongo = MagicMock()
    db = MagicMock()
    mongo.memory_archive = db
    db.episodes.find.return_value = AsyncIteratorMock(
        [{"_id": ObjectId("b" * 24), "raw_data": "Valid doc text"}]
    )

    mock_embed.return_value = [_FAKE_VEC]

    # Terminate loop on second fetchrow
    conn.fetchrow.side_effect = [conn.fetchrow.return_value, asyncio.CancelledError()]

    with patch("logging.Logger.warning") as mock_warn:
        with pytest.raises(asyncio.CancelledError):
            await run_re_embedding_worker(pool, mongo)

        # Verify warnings logged for bad ObjectIds
        mock_warn.assert_called_once()

    # Verify bulk find called with only the valid ObjectId
    db.episodes.find.assert_called_once()
    find_args = db.episodes.find.call_args[0][0]
    assert find_args["_id"]["$in"] == [ObjectId("b" * 24)]

    # Verify only the valid memory was embedded and saved
    mock_embed.assert_awaited_once_with(["Valid doc text"])
    conn.execute.assert_any_call(
        """
                                    INSERT INTO memory_embeddings (memory_id, model_id, embedding)
                                    VALUES ($1, $2, $3::vector)
                                    ON CONFLICT DO NOTHING
                                    """,
        mem_id_2,
        target_model_id,
        json.dumps(_FAKE_VEC),
    )
