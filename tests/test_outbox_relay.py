"""TASK-11: Outbox relay delivery and failure-tracking tests."""

import json
import uuid

import pytest

from nce import outbox_relay


@pytest.mark.integration
@pytest.mark.asyncio
async def test_outbox_relay_marks_published(pg_pool, namespace_id, monkeypatch):
    called = []

    async def fake_handler(conn, event):
        called.append(event["id"])

    monkeypatch.setitem(outbox_relay.OUTBOX_HANDLERS, "memory.stored", [fake_handler])

    async with pg_pool.acquire(timeout=10.0) as conn:
        await conn.execute("DELETE FROM outbox_events")
        event_id = uuid.uuid4()
        await conn.execute(
            "INSERT INTO outbox_events (id, namespace_id, aggregate_type, aggregate_id, "
            "event_type, payload) VALUES ($1, $2, 'memory', $3, 'memory.stored', $4::jsonb)",
            event_id,
            namespace_id,
            "mem-1",
            json.dumps({"saga_id": str(uuid.uuid4()), "memory_id": "mem-1"}),
        )

    delivered = await outbox_relay.run_outbox_relay_once(pg_pool, batch_size=10)

    assert delivered == 1
    assert called == [event_id]

    async with pg_pool.acquire(timeout=10.0) as conn:
        published_at = await conn.fetchval(
            "SELECT published_at FROM outbox_events WHERE id = $1", event_id
        )
    assert published_at is not None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_outbox_relay_failed_handler_increments_attempt_count(
    pg_pool, namespace_id, monkeypatch
):
    async def failing_handler(conn, event):
        raise RuntimeError("simulated failure")

    monkeypatch.setitem(outbox_relay.OUTBOX_HANDLERS, "memory.stored", [failing_handler])

    async with pg_pool.acquire(timeout=10.0) as conn:
        await conn.execute("DELETE FROM outbox_events")
        event_id = uuid.uuid4()
        await conn.execute(
            "INSERT INTO outbox_events (id, namespace_id, aggregate_type, aggregate_id, "
            "event_type, payload) VALUES ($1, $2, 'memory', $3, 'memory.stored', $4::jsonb)",
            event_id,
            namespace_id,
            "mem-2",
            json.dumps({"saga_id": str(uuid.uuid4()), "memory_id": "mem-2"}),
        )

    await outbox_relay.run_outbox_relay_once(pg_pool, batch_size=10)

    async with pg_pool.acquire(timeout=10.0) as conn:
        row = await conn.fetchrow(
            "SELECT attempt_count, published_at, error_message FROM outbox_events WHERE id = $1",
            event_id,
        )
    assert row["attempt_count"] == 1
    assert row["published_at"] is None
    assert "simulated failure" in row["error_message"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_outbox_relay_exhausted_event_moves_to_dlq(pg_pool, namespace_id, monkeypatch):
    async def failing_handler(conn, event):
        raise RuntimeError("always fails")

    monkeypatch.setitem(outbox_relay.OUTBOX_HANDLERS, "memory.stored", [failing_handler])
    monkeypatch.setattr(outbox_relay, "MAX_OUTBOX_ATTEMPTS", 1)

    async with pg_pool.acquire(timeout=10.0) as conn:
        await conn.execute("DELETE FROM outbox_events")
        event_id = uuid.uuid4()
        await conn.execute(
            "INSERT INTO outbox_events (id, namespace_id, aggregate_type, aggregate_id, "
            "event_type, payload) VALUES ($1, $2, 'memory', $3, 'memory.stored', $4::jsonb)",
            event_id,
            namespace_id,
            "mem-3",
            json.dumps({"saga_id": str(uuid.uuid4()), "memory_id": "mem-3"}),
        )

    await outbox_relay.run_outbox_relay_once(pg_pool, batch_size=10)

    async with pg_pool.acquire(timeout=10.0) as conn:
        dlq_row = await conn.fetchrow(
            "SELECT task_name, job_id FROM dead_letter_queue WHERE job_id = $1",
            str(event_id),
        )
    assert dlq_row is not None
    assert dlq_row["task_name"] == "outbox:memory.stored"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_outbox_relay_failure_alerts(pg_pool, namespace_id, monkeypatch):
    from unittest.mock import AsyncMock, patch

    outbox_relay._ALERT_THROTTLE_CACHE.clear()

    async def failing_handler(conn, event):
        raise RuntimeError("simulated delivery error")

    monkeypatch.setitem(outbox_relay.OUTBOX_HANDLERS, "memory.stored", [failing_handler])

    async with pg_pool.acquire(timeout=10.0) as conn:
        await conn.execute("DELETE FROM outbox_events")
        event_id = uuid.uuid4()
        await conn.execute(
            "INSERT INTO outbox_events (id, namespace_id, aggregate_type, aggregate_id, "
            "event_type, payload) VALUES ($1, $2, 'memory', $3, 'memory.stored', $4::jsonb)",
            event_id,
            namespace_id,
            "mem-fail",
            json.dumps({"saga_id": str(uuid.uuid4()), "memory_id": "mem-fail"}),
        )

    with patch(
        "nce.notifications.NotificationDispatcher.dispatch_alert", new_callable=AsyncMock
    ) as mock_dispatch:
        await outbox_relay.run_outbox_relay_once(pg_pool, batch_size=10)

        # Assert delivery failure alert was dispatched
        mock_dispatch.assert_awaited_once()
        args, kwargs = mock_dispatch.call_args
        assert "Outbox Delivery Failed: memory.stored" in args[0]
        assert "simulated delivery error" in args[1]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_outbox_relay_exhaustion_alerts(pg_pool, namespace_id, monkeypatch):
    from unittest.mock import AsyncMock, patch

    outbox_relay._ALERT_THROTTLE_CACHE.clear()

    async def failing_handler(conn, event):
        raise RuntimeError("exhaustion failure")

    monkeypatch.setitem(outbox_relay.OUTBOX_HANDLERS, "memory.stored", [failing_handler])
    monkeypatch.setattr(outbox_relay, "MAX_OUTBOX_ATTEMPTS", 1)

    async with pg_pool.acquire(timeout=10.0) as conn:
        await conn.execute("DELETE FROM outbox_events")
        event_id = uuid.uuid4()
        await conn.execute(
            "INSERT INTO outbox_events (id, namespace_id, aggregate_type, aggregate_id, "
            "event_type, payload) VALUES ($1, $2, 'memory', $3, 'memory.stored', $4::jsonb)",
            event_id,
            namespace_id,
            "mem-exhaust",
            json.dumps({"saga_id": str(uuid.uuid4()), "memory_id": "mem-exhaust"}),
        )

    with patch(
        "nce.notifications.NotificationDispatcher.dispatch_alert", new_callable=AsyncMock
    ) as mock_dispatch:
        await outbox_relay.run_outbox_relay_once(pg_pool, batch_size=10)

        assert mock_dispatch.call_count == 2
        calls = [
            mock_dispatch.call_args_list[0][0],
            mock_dispatch.call_args_list[1][0],
        ]
        titles = [c[0] for c in calls]
        assert "Outbox Delivery Failed: memory.stored" in titles
        assert "Outbox Event Dead-Lettered: outbox:memory.stored" in titles
