"""Batch 110: outbox-idempotency integration tests.

Verifies:
1. Crash between handler success and mark_published → event reprocessed exactly
   once observably (dedup row in processed_outbox_events prevents double effect).
2. Registering a second handler type requires zero relay-loop edits — only a new
   @outbox_handler("...") decorated function.
3. The module docstring / contract states at-least-once, not at-most-once.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import asyncpg
import pytest

from nce import outbox_relay
from nce.outbox_relay import (
    OUTBOX_HANDLERS,
    outbox_handler,
    run_outbox_relay_once,
)

# ---------------------------------------------------------------------------
# Helper: insert a fresh outbox row
# ---------------------------------------------------------------------------


async def _insert_outbox_event(
    conn: asyncpg.Connection,
    *,
    namespace_id: uuid.UUID,
    event_type: str = "memory.stored",
    aggregate_id: str = "agg-1",
) -> uuid.UUID:
    event_id = uuid.uuid4()
    await conn.execute(
        """
        INSERT INTO outbox_events
            (id, namespace_id, aggregate_type, aggregate_id, event_type, payload)
        VALUES ($1, $2, 'memory', $3, $4, $5::jsonb)
        """,
        event_id,
        namespace_id,
        aggregate_id,
        event_type,
        json.dumps({"memory_id": aggregate_id, "saga_id": str(uuid.uuid4())}),
    )
    return event_id


# ---------------------------------------------------------------------------
# 1. Module contract: at-least-once
# ---------------------------------------------------------------------------


def test_module_docstring_states_at_least_once():
    """The module-level docstring must advertise at-least-once delivery."""
    doc = outbox_relay.__doc__ or ""
    assert "at-least-once" in doc, "Module docstring must state at-least-once delivery contract"
    assert "at-most-once" not in doc, "Module docstring must NOT say at-most-once"


# ---------------------------------------------------------------------------
# 2. Decorator registry: registering a second handler requires zero relay-loop edits
# ---------------------------------------------------------------------------


def test_decorator_registers_handler():
    """@outbox_handler adds to OUTBOX_HANDLERS without any relay-loop changes."""
    test_event_type = f"test.event.{uuid.uuid4().hex}"
    assert test_event_type not in OUTBOX_HANDLERS

    @outbox_handler(test_event_type)
    async def _handle_test(conn: Any, event: dict) -> None:  # noqa: ARG001
        return None

    assert OUTBOX_HANDLERS[test_event_type] == [_handle_test]
    # Clean up to avoid polluting OUTBOX_HANDLERS for other tests
    del OUTBOX_HANDLERS[test_event_type]


def test_decorator_rejects_sync_handler():
    """@outbox_handler rejects synchronous functions (no Redis/sync I/O in transaction)."""
    with pytest.raises(TypeError, match="must be an async def"):

        @outbox_handler(f"test.sync.{uuid.uuid4().hex}")
        def _sync_handler(conn: Any, event: dict) -> None:  # noqa: ARG001
            pass


def test_memory_stored_already_registered():
    """The built-in 'memory.stored' handler is registered via @outbox_handler."""
    assert "memory.stored" in OUTBOX_HANDLERS
    assert OUTBOX_HANDLERS["memory.stored"] == [outbox_relay.handle_memory_stored]


# ---------------------------------------------------------------------------
# 3. Idempotency: dedup via processed_outbox_events
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dedup_row_inserted_on_delivery(pg_pool: asyncpg.Pool, namespace_id: uuid.UUID):
    """A successful delivery inserts a row into processed_outbox_events."""
    called: list[uuid.UUID] = []

    async def counting_handler(conn: asyncpg.Connection, event: dict) -> None:
        called.append(event["id"])
        return None

    async with pg_pool.acquire() as conn:
        await conn.execute("DELETE FROM outbox_events WHERE namespace_id = $1", namespace_id)
        await conn.execute(
            "DELETE FROM processed_outbox_events WHERE namespace_id = $1", namespace_id
        )
        event_id = await _insert_outbox_event(conn, namespace_id=namespace_id)

    # Patch the handler for memory.stored
    original = OUTBOX_HANDLERS.get("memory.stored")
    OUTBOX_HANDLERS["memory.stored"] = [counting_handler]
    try:
        delivered = await run_outbox_relay_once(pg_pool, batch_size=10)
    finally:
        if original is not None:
            OUTBOX_HANDLERS["memory.stored"] = list(original)
        else:
            del OUTBOX_HANDLERS["memory.stored"]

    assert delivered == 1
    assert called == [event_id]

    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT event_id, namespace_id FROM processed_outbox_events WHERE event_id = $1",
            event_id,
        )
    assert row is not None, "processed_outbox_events row must exist after delivery"
    assert row["namespace_id"] == namespace_id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_crash_replay_idempotent(pg_pool: asyncpg.Pool, namespace_id: uuid.UUID):
    """Crash-injection scenario: simulate a crash between handler success and mark_published.

    The relay processes the event once (handler runs, dedup row committed).
    On re-poll the same event is redelivered (published_at still NULL) but the
    dedup check skips the handler — observable effect (call counter) stays at 1.
    """
    call_count: list[int] = [0]

    async def tracking_handler(conn: asyncpg.Connection, event: dict) -> None:
        call_count[0] += 1
        return None

    # --- Setup: fresh outbox row, no prior dedup entries ---
    async with pg_pool.acquire() as conn:
        await conn.execute("DELETE FROM outbox_events WHERE namespace_id = $1", namespace_id)
        await conn.execute(
            "DELETE FROM processed_outbox_events WHERE namespace_id = $1", namespace_id
        )
        event_id = await _insert_outbox_event(conn, namespace_id=namespace_id)

    # --- Pass 1: handler runs and dedup row is inserted, but we simulate a crash
    # by manually inserting the dedup row and then NOT calling mark_published.
    # This is the "crash between handler success and mark_published" scenario.
    # We replicate it directly: insert the dedup row in isolation (as the
    # handler's transaction would have committed it), but leave published_at NULL.
    async with pg_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO processed_outbox_events (event_id, namespace_id)
            VALUES ($1, $2)
            ON CONFLICT (event_id) DO NOTHING
            """,
            event_id,
            namespace_id,
        )
        # published_at deliberately NOT set — simulates the crash before mark_published

    # --- Pass 2: relay re-polls the event (published_at IS NULL) ---
    # The handler must NOT be called again; dedup row already exists.
    original = OUTBOX_HANDLERS.get("memory.stored")
    OUTBOX_HANDLERS["memory.stored"] = [tracking_handler]
    try:
        await run_outbox_relay_once(pg_pool, batch_size=10)
    finally:
        if original is not None:
            OUTBOX_HANDLERS["memory.stored"] = list(original)
        else:
            del OUTBOX_HANDLERS["memory.stored"]

    # Handler must NOT have been called in pass 2 (dedup skipped it)
    assert call_count[0] == 0, (
        f"Handler called {call_count[0]} time(s) on crash-replay — dedup must prevent double effect"
    )

    # The relay marks it published (so it won't be re-polled again)
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT published_at FROM outbox_events WHERE id = $1", event_id)
    assert row is not None
    assert row["published_at"] is not None, "published_at must be set after dedup-replay pass"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_normal_delivery_then_replay_no_double_effect(
    pg_pool: asyncpg.Pool, namespace_id: uuid.UUID
):
    """Normal delivery followed by a second relay pass does not re-execute the handler.

    Verifies end-to-end: first pass delivers + dedup-inserts + marks published;
    second pass finds published_at IS NOT NULL so the event is not polled at all.
    """
    call_count: list[int] = [0]

    async def counting_handler(conn: asyncpg.Connection, event: dict) -> None:
        call_count[0] += 1
        return None

    async with pg_pool.acquire() as conn:
        await conn.execute("DELETE FROM outbox_events WHERE namespace_id = $1", namespace_id)
        await conn.execute(
            "DELETE FROM processed_outbox_events WHERE namespace_id = $1", namespace_id
        )
        await _insert_outbox_event(conn, namespace_id=namespace_id)

    original = OUTBOX_HANDLERS.get("memory.stored")
    OUTBOX_HANDLERS["memory.stored"] = [counting_handler]
    try:
        # First pass: normal delivery
        delivered1 = await run_outbox_relay_once(pg_pool, batch_size=10)
        # Second pass: event already published, not polled
        delivered2 = await run_outbox_relay_once(pg_pool, batch_size=10)
    finally:
        if original is not None:
            OUTBOX_HANDLERS["memory.stored"] = list(original)
        else:
            del OUTBOX_HANDLERS["memory.stored"]

    assert delivered1 == 1
    assert delivered2 == 0
    assert call_count[0] == 1, "Handler must be called exactly once across both passes"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_second_handler_type_zero_relay_edits(pg_pool: asyncpg.Pool, namespace_id: uuid.UUID):
    """Registering a second event type via @outbox_handler requires zero relay-loop edits.

    The relay dispatches 'audit.created' without any change to run_outbox_relay_once.
    """
    second_event_type = "audit.created"
    second_calls: list[uuid.UUID] = []

    @outbox_handler(second_event_type)
    async def handle_audit_created(conn: asyncpg.Connection, event: dict) -> None:
        second_calls.append(event["id"])
        return None

    try:
        async with pg_pool.acquire() as conn:
            await conn.execute("DELETE FROM outbox_events WHERE namespace_id = $1", namespace_id)
            await conn.execute(
                "DELETE FROM processed_outbox_events WHERE namespace_id = $1", namespace_id
            )
            event_id = await _insert_outbox_event(
                conn,
                namespace_id=namespace_id,
                event_type=second_event_type,
                aggregate_id="audit-1",
            )

        delivered = await run_outbox_relay_once(pg_pool, batch_size=10)

        assert delivered == 1
        assert second_calls == [event_id], (
            "Second handler type must be dispatched without relay-loop edits"
        )

        # Dedup row must exist
        async with pg_pool.acquire() as conn:
            dedup = await conn.fetchval(
                "SELECT event_id FROM processed_outbox_events WHERE event_id = $1", event_id
            )
        assert dedup == event_id

    finally:
        del OUTBOX_HANDLERS[second_event_type]
