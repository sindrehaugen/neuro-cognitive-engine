"""tests/test_event_bus_interface.py

Acceptance tests for nce.events (C4 §9.6 — subscribe/publish interface).

Unit test  : subscribe() registers the handler in the relay's OUTBOX_HANDLERS
             registry so it is resolvable for its node_type/op selector, and a
             second subscriber for the same selector is appended, not swapped in.
Integration: publish() inserts a row into outbox_events with the correct columns
             (requires Postgres — tagged @pytest.mark.integration).
"""

from __future__ import annotations

import json
import uuid

import pytest

from nce import outbox_relay
from nce.events import publish, subscribe

# ---------------------------------------------------------------------------
# Unit — pure registry logic (no DB)
# ---------------------------------------------------------------------------


def test_subscribe_registers_handler_in_relay_registry(monkeypatch):
    """subscribe() appends the handler to OUTBOX_HANDLERS under the expected key."""
    # Snapshot the registry so we don't pollute other tests.  snapshot_handlers()
    # copies each subscriber list — a plain dict() would alias them.
    original = outbox_relay.snapshot_handlers()
    try:

        async def _my_handler(conn, event):
            return None

        subscribe({"node_type": "device", "op": "synced"}, _my_handler)

        assert outbox_relay.OUTBOX_HANDLERS.get("device.synced") == [_my_handler]
    finally:
        outbox_relay.restore_handlers(original)


def test_subscribe_key_format():
    """The selector key combines node_type and op with a dot separator."""
    original = outbox_relay.snapshot_handlers()
    try:

        async def _handler(conn, event):
            return None

        subscribe({"node_type": "interface", "op": "created"}, _handler)
        assert "interface.created" in outbox_relay.OUTBOX_HANDLERS
    finally:
        outbox_relay.restore_handlers(original)


def test_subscribe_fans_out_to_multiple_handlers():
    """A second subscriber for the same selector must not evict the first."""
    original = outbox_relay.snapshot_handlers()
    try:

        async def _first(conn, event):
            return None

        async def _second(conn, event):
            return None

        subscribe({"node_type": "device", "op": "shipped"}, _first)
        subscribe({"node_type": "device", "op": "shipped"}, _second)

        assert outbox_relay.OUTBOX_HANDLERS["device.shipped"] == [_first, _second]
    finally:
        outbox_relay.restore_handlers(original)


# ---------------------------------------------------------------------------
# Integration — publish() writes a row to outbox_events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_drops_unconsumed_event_and_increments_counter(monkeypatch):
    """publish() drops unconsumed selector and increments Prometheus counter without executing SQL."""
    from unittest.mock import AsyncMock, MagicMock

    from nce.observability import OUTBOX_UNCONSUMED_TOTAL

    mock_conn = MagicMock()
    mock_conn.execute = AsyncMock()

    mock_counter = MagicMock()
    monkeypatch.setattr(OUTBOX_UNCONSUMED_TOTAL, "labels", mock_counter)

    monkeypatch.setitem(outbox_relay.OUTBOX_HANDLERS, "unconsumed.event", [])

    await publish(
        mock_conn,
        namespace_id=uuid.uuid4(),
        node_type="unconsumed",
        op="event",
        aggregate_id="test-id",
        payload={"key": "val"},
    )

    mock_conn.execute.assert_not_called()
    mock_counter.assert_called_once_with(selector="unconsumed.event")
    mock_counter.return_value.inc.assert_called_once()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_publish_lands_row_in_outbox_events(pg_pool, namespace_id):
    """publish() inserts a correctly shaped row when consumer is registered."""
    aggregate_id = f"dev-{uuid.uuid4()}"
    payload = {"interface": "GigabitEthernet0/1", "status": "up"}

    original = outbox_relay.snapshot_handlers()
    try:

        async def _dummy_handler(conn, event):
            return None

        subscribe({"node_type": "device", "op": "interface_updated"}, _dummy_handler)

        async with pg_pool.acquire(timeout=10.0) as conn:
            async with conn.transaction():
                await publish(
                    conn,
                    namespace_id=namespace_id,
                    node_type="device",
                    op="interface_updated",
                    aggregate_id=aggregate_id,
                    payload=payload,
                )

        async with pg_pool.acquire(timeout=10.0) as conn:
            row = await conn.fetchrow(
                """
                SELECT namespace_id, aggregate_type, aggregate_id, event_type,
                       payload, published_at
                FROM outbox_events
                WHERE aggregate_id = $1
                """,
                aggregate_id,
            )

        assert row is not None, "publish() must insert a row into outbox_events"
        assert row["namespace_id"] == namespace_id
        assert row["aggregate_type"] == "device"
        assert row["aggregate_id"] == aggregate_id
        assert row["event_type"] == "device.interface_updated"
        stored = row["payload"]
        if isinstance(stored, str):
            stored = json.loads(stored)
        assert stored["interface"] == "GigabitEthernet0/1"
        assert row["published_at"] is None, "relay has not run yet — row is unpublished"
    finally:
        outbox_relay.restore_handlers(original)
