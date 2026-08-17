"""Unit tests for nce.snapshot_mcp_handlers (streaming export, row serialization)."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from nce.orchestrator import NCEEngine
from nce.snapshot_mcp_handlers import (
    _MAX_EXPORT_ROWS,
    _STREAM_PROGRESS_INTERVAL,
    _serialize_memory_row,
    stream_snapshot_export,
)


async def _collect(gen):
    return [line async for line in gen]


def _parse_ndjson_line(line: str) -> dict:
    return json.loads(line.strip())


async def _empty_fetch(_size: int) -> list:
    return []


def _memory_row_dict() -> dict:
    return {"memory_id": uuid4(), "metadata": "{}"}


def _fetch_from_batches(*batches: list) -> AsyncMock:
    """Return an async fetch that yields each batch in order, then []."""
    queue = list(batches)

    async def fetch(_size: int) -> list:
        if queue:
            return queue.pop(0)
        return []

    return fetch


def _mock_pool_with_conn(
    total: int,
    *,
    fetchmany=None,
) -> tuple[MagicMock, MagicMock, MagicMock]:
    """Pool + conn + cursor mocks for stream_snapshot_export happy paths."""
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"total": total})

    cursor = MagicMock()
    cursor.fetch = fetchmany if fetchmany is not None else _empty_fetch
    conn.cursor = AsyncMock(return_value=cursor)

    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=None)
    conn.transaction = MagicMock(return_value=tx)

    acq = MagicMock()
    acq.__aenter__ = AsyncMock(return_value=conn)
    acq.__aexit__ = AsyncMock(return_value=None)

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acq)
    return pool, conn, cursor


@pytest.fixture
def mock_engine_with_pool() -> NCEEngine:
    """Minimal engine with pg_pool set (unused on invalid-UUID path)."""
    engine = NCEEngine()
    engine.pg_pool = MagicMock()
    return engine


# ---------------------------------------------------------------------------
# stream_snapshot_export — invalid namespace_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_snapshot_export_invalid_uuid_yields_single_error(
    mock_engine_with_pool: NCEEngine,
) -> None:
    lines = await _collect(stream_snapshot_export(mock_engine_with_pool, "not-a-valid-uuid"))

    assert len(lines) == 1
    payload = _parse_ndjson_line(lines[0])
    assert payload["type"] == "error"
    assert "invalid namespace_id" in payload["message"].lower()
    mock_engine_with_pool.pg_pool.acquire.assert_not_called()


# ---------------------------------------------------------------------------
# stream_snapshot_export — exception sanitization
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_snapshot_export_exception_message_sanitized(
    mock_engine_with_pool: NCEEngine,
) -> None:
    mock_engine_with_pool.pg_pool.acquire.side_effect = RuntimeError("secret db failure")

    lines = await _collect(stream_snapshot_export(mock_engine_with_pool, str(uuid4())))

    error_lines = [
        _parse_ndjson_line(line)
        for line in lines
        if _parse_ndjson_line(line).get("type") == "error"
    ]
    assert len(error_lines) == 1

    message = error_lines[0]["message"]
    assert message == "Export failed after 0 records"
    assert "secret" not in message
    assert "RuntimeError" not in message
    assert "Error" not in message


# ---------------------------------------------------------------------------
# stream_snapshot_export — BATCH 2 row limits and cursor SQL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_snapshot_export_over_max_rows_yields_error(
    mock_engine_with_pool: NCEEngine,
) -> None:
    pool, conn, _ = _mock_pool_with_conn(_MAX_EXPORT_ROWS + 1)
    mock_engine_with_pool.pg_pool = pool

    lines = await _collect(stream_snapshot_export(mock_engine_with_pool, str(uuid4())))
    payloads = [_parse_ndjson_line(line) for line in lines]

    assert payloads[0]["type"] == "metadata"
    assert payloads[-1]["type"] == "error"
    assert "exceeds the maximum" in payloads[-1]["message"].lower()

    conn.cursor.assert_not_called()


@pytest.mark.asyncio
async def test_stream_snapshot_export_at_max_rows_boundary_proceeds(
    mock_engine_with_pool: NCEEngine,
) -> None:
    pool, conn, _ = _mock_pool_with_conn(_MAX_EXPORT_ROWS)
    mock_engine_with_pool.pg_pool = pool

    lines = await _collect(stream_snapshot_export(mock_engine_with_pool, str(uuid4())))
    payloads = [_parse_ndjson_line(line) for line in lines]

    assert [p["type"] for p in payloads] == ["metadata", "complete"]
    assert payloads[-1]["total_expected"] == _MAX_EXPORT_ROWS
    assert payloads[-1]["exported"] == 0
    assert not any(p["type"] == "error" for p in payloads)

    conn.cursor.assert_called_once()


@pytest.mark.asyncio
async def test_stream_snapshot_export_zero_rows_metadata_and_complete_only(
    mock_engine_with_pool: NCEEngine,
) -> None:
    pool, _, _ = _mock_pool_with_conn(0)
    mock_engine_with_pool.pg_pool = pool

    lines = await _collect(stream_snapshot_export(mock_engine_with_pool, str(uuid4())))
    payloads = [_parse_ndjson_line(line) for line in lines]

    assert [p["type"] for p in payloads] == ["metadata", "complete"]
    assert payloads[-1]["total_expected"] == 0
    assert payloads[-1]["exported"] == 0
    assert not any(p["type"] in ("memory", "progress", "error") for p in payloads)


@pytest.mark.asyncio
async def test_stream_snapshot_export_cursor_sql_stable_order_by(
    mock_engine_with_pool: NCEEngine,
) -> None:
    pool, conn, _ = _mock_pool_with_conn(0)
    mock_engine_with_pool.pg_pool = pool

    await _collect(stream_snapshot_export(mock_engine_with_pool, str(uuid4())))

    conn.cursor.assert_called_once()
    sql = conn.cursor.call_args[0][0]
    assert "ORDER BY m.valid_from ASC, m.id ASC" in sql


# ---------------------------------------------------------------------------
# stream_snapshot_export — BATCH 3 fetchmany timeout and progress
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_snapshot_export_fetchmany_timeout_yields_error(
    mock_engine_with_pool: NCEEngine,
) -> None:
    pool, _, _ = _mock_pool_with_conn(1)
    mock_engine_with_pool.pg_pool = pool

    with patch(
        "nce.snapshot_mcp_handlers.asyncio.wait_for",
        side_effect=asyncio.TimeoutError,
    ):
        lines = await _collect(stream_snapshot_export(mock_engine_with_pool, str(uuid4())))

    payloads = [_parse_ndjson_line(line) for line in lines]
    assert payloads[0]["type"] == "metadata"
    assert payloads[-1]["type"] == "error"
    assert payloads[-1]["message"] == "Export timed out"
    assert not any(p["type"] == "complete" for p in payloads)


@pytest.mark.asyncio
async def test_stream_snapshot_export_fetchmany_within_timeout_streams_rows(
    mock_engine_with_pool: NCEEngine,
) -> None:
    row = _memory_row_dict()
    pool, _, _ = _mock_pool_with_conn(1, fetchmany=_fetch_from_batches([row], []))
    mock_engine_with_pool.pg_pool = pool

    lines = await _collect(stream_snapshot_export(mock_engine_with_pool, str(uuid4())))
    payloads = [_parse_ndjson_line(line) for line in lines]

    assert payloads[0]["type"] == "metadata"
    memory_lines = [p for p in payloads if p["type"] == "memory"]
    assert len(memory_lines) == 1
    assert memory_lines[0]["memory"]["memory_id"] == str(row["memory_id"])
    assert payloads[-1]["type"] == "complete"
    assert payloads[-1]["exported"] == 1
    assert payloads[-1]["total_expected"] == 1


@pytest.mark.asyncio
async def test_stream_snapshot_export_progress_at_every_1000th_row(
    mock_engine_with_pool: NCEEngine,
) -> None:
    assert _STREAM_PROGRESS_INTERVAL == 1000
    rows = [_memory_row_dict() for _ in range(2500)]
    pool, _, _ = _mock_pool_with_conn(2500, fetchmany=_fetch_from_batches(rows))
    mock_engine_with_pool.pg_pool = pool

    lines = await _collect(stream_snapshot_export(mock_engine_with_pool, str(uuid4())))
    payloads = [_parse_ndjson_line(line) for line in lines]

    progress = [p for p in payloads if p["type"] == "progress"]
    assert [p["exported"] for p in progress] == [1000, 2000]
    assert all(p["total_expected"] == 2500 for p in progress)
    assert payloads[-1]["type"] == "complete"
    assert payloads[-1]["exported"] == 2500


@pytest.mark.asyncio
async def test_stream_snapshot_export_no_progress_when_under_interval(
    mock_engine_with_pool: NCEEngine,
) -> None:
    rows = [_memory_row_dict() for _ in range(500)]
    pool, _, _ = _mock_pool_with_conn(500, fetchmany=_fetch_from_batches(rows))
    mock_engine_with_pool.pg_pool = pool

    lines = await _collect(stream_snapshot_export(mock_engine_with_pool, str(uuid4())))
    payloads = [_parse_ndjson_line(line) for line in lines]

    assert not any(p["type"] == "progress" for p in payloads)
    assert sum(1 for p in payloads if p["type"] == "memory") == 500
    assert payloads[-1]["type"] == "complete"
    assert payloads[-1]["exported"] == 500


# ---------------------------------------------------------------------------
# _serialize_memory_row — metadata parsing
# ---------------------------------------------------------------------------


def test_serialize_memory_row_malformed_metadata_returns_empty_dict() -> None:
    row = {"memory_id": str(uuid4()), "metadata": "{not valid json"}
    result = _serialize_memory_row(row)
    assert result["metadata"] == {}


def test_serialize_memory_row_valid_json_metadata_parses() -> None:
    row = {"memory_id": str(uuid4()), "metadata": '{"foo": 1}'}
    result = _serialize_memory_row(row)
    assert result["metadata"] == {"foo": 1}


# ---------------------------------------------------------------------------
# restore_namespace & handle_import_snapshot
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restore_namespace_invalid_uuid(mock_engine_with_pool: NCEEngine) -> None:
    from unittest.mock import MagicMock

    from nce.snapshot_mcp_handlers import restore_namespace

    mock_engine_with_pool.mongo_client = MagicMock()
    res = await restore_namespace(mock_engine_with_pool, "invalid-uuid", "")
    assert res["status"] == "error"
    assert "Invalid target_namespace_id" in res["message"]


@pytest.mark.asyncio
async def test_restore_namespace_no_mongo() -> None:
    from nce.snapshot_mcp_handlers import restore_namespace

    engine = NCEEngine()
    engine.mongo_client = None
    res = await restore_namespace(engine, str(uuid4()), "")
    assert res["status"] == "error"
    assert "MongoDB client not connected" in res["message"]


@pytest.mark.asyncio
async def test_restore_namespace_happy_path() -> None:
    from unittest.mock import AsyncMock, MagicMock

    from nce.snapshot_mcp_handlers import restore_namespace

    engine = NCEEngine()
    engine.mongo_client = MagicMock()
    db = MagicMock()
    episodes = AsyncMock()
    db.episodes = episodes
    engine.mongo_client.memory_archive = db

    # Setup fake MongoDB doc
    fake_doc = {"raw_data": "Ingested content", "metadata": {"foo": "bar"}}
    episodes.find_one.return_value = fake_doc

    # Mock engine.store_memory
    engine.store_memory = AsyncMock(return_value={"quarantined": False, "payload_ref": "some_ref"})

    target_ns = str(uuid4())
    snapshot_lines = (
        '{"type": "metadata", "namespace_id": "original-ns"}\n'
        '{"type": "memory", "memory": {"payload_ref": "507f1f77bcf86cd799439011", "agent_id": "test-agent", "memory_type": "episodic", "assertion_type": "fact", "salience": 0.8, "metadata": {"foo": "bar"}}}\n'
    )

    res = await restore_namespace(engine, target_ns, snapshot_lines)
    assert res["status"] == "ok"
    assert res["imported"] == 1
    assert not res["errors"]

    engine.store_memory.assert_called_once()
    req = engine.store_memory.call_args[0][0]
    assert str(req.namespace_id) == target_ns
    assert req.content == "Ingested content"
    assert req.metadata["foo"] == "bar"
    assert req.metadata["salience"] == 0.8
    assert req.metadata["bypass_quarantine"] is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_snapshot_import_export_integration(pg_pool, make_namespace, monkeypatch) -> None:
    # 1. Connect a real NCEEngine
    engine = NCEEngine()
    await engine.connect()

    try:
        # 2. Create namespaces A and B
        ns_a = await make_namespace()
        ns_b = await make_namespace()

        # 3. Store a memory in namespace A via the engine
        from nce.models import AssertionType, MemoryType, StoreMemoryRequest

        payload = StoreMemoryRequest(
            namespace_id=ns_a,
            agent_id="test-agent",
            content="This is integration test content for snapshot export and import.",
            summary="Integration test summary",
            heavy_payload="Heavy payload content for integration test",
            memory_type=MemoryType.episodic,
            assertion_type=AssertionType.fact,
            metadata={"user_id": "user-a", "session_id": "sess-a"},
            check_contradictions=False,
        )
        res_store = await engine.store_memory(payload)
        payload_ref = res_store["payload_ref"]
        assert payload_ref

        # Verify pg count in A is 1
        async with engine.pg_pool.acquire() as conn:
            cnt_a = await conn.fetchval(
                "SELECT count(*) FROM memories WHERE namespace_id = $1", ns_a
            )
            assert cnt_a == 1

        # 4. Export from namespace A
        lines = []
        async for line in stream_snapshot_export(engine, str(ns_a)):
            lines.append(line)
        snapshot_data = "".join(lines)

        # Verify the exported data contains a memory line with the correct payload_ref
        assert "507f1f" not in snapshot_data
        assert payload_ref in snapshot_data
        assert "test-agent" in snapshot_data

        # 5. Import into namespace B
        from nce.snapshot_mcp_handlers import restore_namespace

        res_import = await restore_namespace(engine, str(ns_b), snapshot_data)
        assert res_import["status"] == "ok"
        assert res_import["imported"] == 1
        assert not res_import["errors"]

        # 6. Verify row counts and types in namespace B match namespace A
        async with engine.pg_pool.acquire() as conn:
            cnt_b = await conn.fetchval(
                "SELECT count(*) FROM memories WHERE namespace_id = $1", ns_b
            )
            assert cnt_b == 1

            # Assert that the type matches
            type_b = await conn.fetchval(
                "SELECT memory_type FROM memories WHERE namespace_id = $1", ns_b
            )
            assert type_b == "episodic"

    finally:
        await engine.disconnect()
