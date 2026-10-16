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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_outbox_relay_drains_catalogued_unconsumed_event(pg_pool, namespace_id):
    """Live integration test: A catalogued UNCONSUMED selector drains without DLQ or alerts.

    Asserts by querying:
    1. dead_letter_queue count unchanged across relay execution.
    2. outbox_events.published_at IS NOT NULL after relay pass 1.
    3. A second relay pass returns 0 (proves the row has truly drained and is not re-polled).
    """
    from nce.events.catalogue import EVENT_CATALOGUE

    # Pick an honestly-declared unconsumed selector from EVENT_CATALOGUE
    unconsumed = [
        sel
        for sel, contract in EVENT_CATALOGUE.items()
        if contract.status == "UNCONSUMED" and not contract.declared_consumers
    ]
    assert unconsumed, "EVENT_CATALOGUE must contain at least one UNCONSUMED selector"
    selector = unconsumed[0]  # e.g. "BOM_LINE.upserted"

    event_id = uuid.uuid4()

    async with pg_pool.acquire(timeout=10.0) as conn:
        await conn.execute("DELETE FROM outbox_events")
        initial_dlq_count = await conn.fetchval("SELECT count(*) FROM dead_letter_queue")
        await conn.execute(
            "INSERT INTO outbox_events (id, namespace_id, aggregate_type, aggregate_id, "
            "event_type, payload) VALUES ($1, $2, 'test_agg', $3, $4, $5::jsonb)",
            event_id,
            namespace_id,
            "agg-1",
            selector,
            json.dumps({"test_drain": True, "selector": selector}),
        )

    # Pass 1: Run relay once.
    res1 = await outbox_relay.run_outbox_relay_once(pg_pool, batch_size=10)
    assert res1.delivered == 0
    assert res1.drained_no_consumer == 1

    # Assert by querying the database directly:
    async with pg_pool.acquire(timeout=10.0) as conn:
        final_dlq_count = await conn.fetchval("SELECT count(*) FROM dead_letter_queue")
        row = await conn.fetchrow(
            "SELECT published_at, attempt_count, error_message FROM outbox_events WHERE id = $1",
            event_id,
        )
        dedup_exists = await conn.fetchval(
            "SELECT count(*) FROM processed_outbox_events WHERE event_id = $1",
            event_id,
        )

    assert final_dlq_count == initial_dlq_count, (
        f"DLQ count changed from {initial_dlq_count} to {final_dlq_count}; unconsumed events must not DLQ"
    )
    assert row is not None
    assert row["published_at"] is not None, "published_at must be populated after relay run"
    assert row["error_message"] is None, f"unexpected error_message: {row['error_message']}"
    assert dedup_exists == 1, "processed_outbox_events must contain dedup row for drained event"

    # Pass 2: Second relay pass returns 0 (verifying the row truly drained and does not re-poll)
    res2 = await outbox_relay.run_outbox_relay_once(pg_pool, batch_size=10)
    assert res2 == 0
    assert res2.delivered == 0
    assert res2.drained_no_consumer == 0


@pytest.mark.asyncio
async def test_deliver_one_and_dlq_bind_namespace_context(monkeypatch) -> None:
    """Verify deliver_one and move_to_dead_letter_if_exhausted bind tenant RLS context."""
    from contextlib import asynccontextmanager
    from datetime import datetime, timezone
    from unittest.mock import AsyncMock, MagicMock, patch

    ns_id = uuid.uuid4()
    event_id = uuid.uuid4()
    active_ns: list[str] = []
    handler_saw_ns: list[str] = []
    dlq_saw_ns: list[str] = []

    conn = MagicMock()

    @asynccontextmanager
    async def _tx():
        yield

    conn.transaction = _tx

    async def _execute(sql: str, *args):
        if "set_config('nce.namespace_id'" in sql:
            active_ns.append(str(args[0]))
        elif "INSERT INTO dead_letter_queue" in sql:
            dlq_saw_ns.append(active_ns[-1] if active_ns else "")
        return "OK"

    conn.execute = AsyncMock(side_effect=_execute)
    conn.fetchval = AsyncMock(return_value=event_id)
    conn.fetch = AsyncMock(return_value=[])

    async def _handler(_c, _ev):
        handler_saw_ns.append(active_ns[-1] if active_ns else "")
        return None

    monkeypatch.setitem(outbox_relay.OUTBOX_HANDLERS, "test.rls.event", [_handler])
    event = {
        "id": event_id,
        "namespace_id": ns_id,
        "aggregate_type": "test",
        "aggregate_id": "1",
        "event_type": "test.rls.event",
        "payload": {"k": "v"},
        "attempt_count": outbox_relay.MAX_OUTBOX_ATTEMPTS - 1,
        "created_at": datetime.now(timezone.utc),
    }

    actions = await outbox_relay.deliver_one(conn, event)
    assert actions == []
    assert handler_saw_ns == [str(ns_id)]

    active_ns.clear()
    with patch.object(outbox_relay, "_dispatch_throttled_alert", new_callable=AsyncMock):
        await outbox_relay.move_to_dead_letter_if_exhausted(conn, event, "boom")
    assert dlq_saw_ns == [str(ns_id)]


@pytest.mark.asyncio
async def test_notification_subscribers_and_reminders_bind_namespace_context() -> None:
    """Verify notification subscribers and reminders bind tenant RLS context before DB access."""
    from contextlib import asynccontextmanager
    from datetime import datetime, timezone
    from unittest.mock import AsyncMock, MagicMock

    from nce.vertical_modules.notifications.reminders import (
        create_reminder,
        fire_pending_reminders,
    )
    from nce.vertical_modules.notifications.subscribers import handle_ticket_sla_breached

    ns_id = uuid.uuid4()
    rem_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    active_ns: list[str] = []
    queries_saw_ns: list[tuple[str, str]] = []

    conn = MagicMock()

    @asynccontextmanager
    async def _tx():
        yield

    conn.transaction = _tx

    async def _execute(sql: str, *args):
        if "set_config('nce.namespace_id'" in sql:
            active_ns.append(str(args[0]))
        elif "UPDATE reminders" in sql:
            queries_saw_ns.append(("update_reminders", active_ns[-1] if active_ns else ""))
        return "OK"

    async def _fetch(sql: str, *args):
        if "notification_subscriptions" in sql:
            queries_saw_ns.append(
                ("notification_subscriptions", active_ns[-1] if active_ns else "")
            )
            return [{"principal_id": "alice"}]
        if "FROM reminders" in sql:
            queries_saw_ns.append(("select_reminders", active_ns[-1] if active_ns else ""))
            return [
                {
                    "id": rem_id,
                    "namespace_id": ns_id,
                    "principal_id": "alice",
                    "node_type": "TICKET",
                    "node_id": "T-1",
                    "title": "Check ticket",
                    "note": "",
                    "remind_at": now,
                    "status": "pending",
                    "fired_at": None,
                    "is_archived": False,
                    "created_at": now,
                    "updated_at": now,
                }
            ]
        return []

    async def _fetchrow(sql: str, *args):
        if "INSERT INTO notifications" in sql:
            queries_saw_ns.append(("insert_notifications", active_ns[-1] if active_ns else ""))
            return {"id": uuid.uuid4()}
        if "INSERT INTO reminders" in sql:
            queries_saw_ns.append(("insert_reminders", active_ns[-1] if active_ns else ""))
            return {"id": rem_id, "namespace_id": ns_id}
        return None

    conn.execute = AsyncMock(side_effect=_execute)
    conn.fetch = AsyncMock(side_effect=_fetch)
    conn.fetchrow = AsyncMock(side_effect=_fetchrow)

    await handle_ticket_sla_breached(
        conn,
        {
            "namespace_id": str(ns_id),
            "aggregate_id": "T-1",
            "payload": {"ticket_id": "T-1", "breach_type": "response"},
        },
    )
    active_ns.clear()
    await create_reminder(
        conn,
        namespace_id=ns_id,
        principal_id="alice",
        node_type="TICKET",
        node_id="T-1",
        title="Check ticket",
        remind_at=now,
    )
    active_ns.clear()
    fired = await fire_pending_reminders(conn, namespace_id=ns_id, now=now)
    assert len(fired) == 1
    assert queries_saw_ns == [
        ("notification_subscriptions", str(ns_id)),
        ("insert_notifications", str(ns_id)),
        ("insert_reminders", str(ns_id)),
        ("select_reminders", str(ns_id)),
        ("update_reminders", str(ns_id)),
    ]
