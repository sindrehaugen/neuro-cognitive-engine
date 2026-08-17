"""C4 outbox fan-out — many subscribers per selector (M0.W20a).

Before this wave ``OUTBOX_HANDLERS`` was ``dict[str, OutboxHandler]``: a second
``subscribe`` for the same ``"{node_type}.{op}"`` selector silently replaced the
first, last-writer-wins, with no warning.  These tests pin the replacement
contract:

Unit (no DB)
    - ``register_handler`` appends rather than replaces, and is idempotent by
      handler equality so a double bootstrap cannot double-fire.
    - ``snapshot_handlers``/``restore_handlers`` copy the subscriber lists
      instead of aliasing them.

Integration (live Postgres)
    - Two subscribers on one selector both fire, exactly once each, in
      registration order, through a real ``run_outbox_relay_once`` pass.
    - Both subscribers' post-commit actions fire, after the commit.
    - Failure semantics: one raising subscriber fails the WHOLE event — no
      dedup row, no ``published_at``, ``attempt_count`` incremented, and the
      sibling's DB writes rolled back with it.
    - An emptied subscriber list is treated exactly like a missing one:
      ``OutboxDeliveryError`` → straight to the DLQ.
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
    OutboxDeliveryError,
    register_handler,
    run_outbox_relay_once,
)

# ---------------------------------------------------------------------------
# Unit — registration semantics (no DB)
# ---------------------------------------------------------------------------


async def _noop_handler(conn: Any, event: dict) -> None:  # noqa: ARG001
    return None


async def _other_handler(conn: Any, event: dict) -> None:  # noqa: ARG001
    return None


def test_register_handler_appends_second_subscriber() -> None:
    """A second subscriber for the same selector must not evict the first."""
    key = f"fanout.unit.{uuid.uuid4().hex}"
    snapshot = outbox_relay.snapshot_handlers()
    try:
        assert register_handler(key, _noop_handler) is True
        assert register_handler(key, _other_handler) is True
        assert OUTBOX_HANDLERS[key] == [_noop_handler, _other_handler]
    finally:
        outbox_relay.restore_handlers(snapshot)


def test_register_handler_is_idempotent_for_same_object() -> None:
    """Re-registering the same handler object is a no-op, not a second subscription.

    This is what keeps a double bootstrap (e.g. two composition roots in one
    process) from firing every side effect twice inside a single transaction.
    """
    key = f"fanout.unit.{uuid.uuid4().hex}"
    snapshot = outbox_relay.snapshot_handlers()
    try:
        assert register_handler(key, _noop_handler) is True
        assert register_handler(key, _noop_handler) is False
        assert OUTBOX_HANDLERS[key] == [_noop_handler]
    finally:
        outbox_relay.restore_handlers(snapshot)


def test_register_handler_dedups_bound_methods() -> None:
    """Bound methods dedup by equality — ``obj.m is obj.m`` is False in Python."""

    class _Subscriber:
        async def handle(self, conn: Any, event: dict) -> None:  # noqa: ARG002
            return None

    sub = _Subscriber()
    key = f"fanout.unit.{uuid.uuid4().hex}"
    snapshot = outbox_relay.snapshot_handlers()
    try:
        assert sub.handle is not sub.handle  # the trap this guards against
        assert register_handler(key, sub.handle) is True
        assert register_handler(key, sub.handle) is False
        assert len(OUTBOX_HANDLERS[key]) == 1
    finally:
        outbox_relay.restore_handlers(snapshot)


def test_register_handler_rejects_sync_callable() -> None:
    """The coroutine check now also guards the ``subscribe`` path."""
    key = f"fanout.unit.{uuid.uuid4().hex}"
    snapshot = outbox_relay.snapshot_handlers()
    try:

        def _sync(conn: Any, event: dict) -> None:  # noqa: ARG001
            return None

        with pytest.raises(TypeError, match="must be an async def"):
            register_handler(key, _sync)  # type: ignore[arg-type]
        assert OUTBOX_HANDLERS.get(key) in (None, [])
    finally:
        outbox_relay.restore_handlers(snapshot)


def test_snapshot_handlers_does_not_alias_subscriber_lists() -> None:
    """A snapshot must survive a later subscribe on an already-populated key."""
    key = f"fanout.unit.{uuid.uuid4().hex}"
    outer = outbox_relay.snapshot_handlers()
    try:
        register_handler(key, _noop_handler)
        snapshot = outbox_relay.snapshot_handlers()

        register_handler(key, _other_handler)
        assert snapshot[key] == [_noop_handler], "snapshot aliased the live list"

        outbox_relay.restore_handlers(snapshot)
        assert OUTBOX_HANDLERS[key] == [_noop_handler]
    finally:
        outbox_relay.restore_handlers(outer)


# ---------------------------------------------------------------------------
# Integration helpers
# ---------------------------------------------------------------------------


async def _drain_stale_outbox(pool: asyncpg.Pool) -> None:
    """Mark every currently-unpublished outbox row published.

    ``run_outbox_relay_once`` drains ALL unpublished rows regardless of
    event_type, so a single orphaned row left by another test (or by a
    concurrent branch's dev-DB pollution) can abort the whole relay
    transaction and poison every test in this file.  Rows are not deleted —
    only their ``published_at`` is set, so the relay's ``WHERE published_at IS
    NULL`` filter skips them.
    """
    async with pool.acquire(timeout=10.0) as conn:
        await conn.execute(
            "UPDATE outbox_events SET published_at = now() WHERE published_at IS NULL"
        )


async def _insert_event(
    pool: asyncpg.Pool,
    *,
    namespace_id: uuid.UUID,
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> uuid.UUID:
    event_id = uuid.uuid4()
    async with pool.acquire(timeout=10.0) as conn:
        await conn.execute(
            """
            INSERT INTO outbox_events
                (id, namespace_id, aggregate_type, aggregate_id, event_type, payload)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb)
            """,
            event_id,
            namespace_id,
            event_type.split(".")[0],
            f"agg-{event_id.hex[:8]}",
            event_type,
            json.dumps(payload or {}),
        )
    return event_id


# ---------------------------------------------------------------------------
# Integration — fan-out through the real relay
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_two_subscribers_both_fire_once_in_registration_order(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """Both subscribers on one selector run, once each, in registration order."""
    event_type = f"fanout_a_{uuid.uuid4().hex[:10]}.created"
    calls: list[str] = []

    async def _first(conn: asyncpg.Connection, event: dict) -> None:  # noqa: ARG001
        calls.append("first")
        return None

    async def _second(conn: asyncpg.Connection, event: dict) -> None:  # noqa: ARG001
        calls.append("second")
        return None

    await _drain_stale_outbox(pg_pool)
    event_id = await _insert_event(pg_pool, namespace_id=namespace_id, event_type=event_type)

    snapshot = outbox_relay.snapshot_handlers()
    try:
        register_handler(event_type, _first)
        register_handler(event_type, _second)

        delivered = await run_outbox_relay_once(pg_pool, batch_size=10)
    finally:
        outbox_relay.restore_handlers(snapshot)

    assert delivered == 1
    assert calls == ["first", "second"], (
        "both subscribers must fire exactly once, in registration order; "
        "a single-slot registry would show only one"
    )

    async with pg_pool.acquire(timeout=10.0) as conn:
        published_at = await conn.fetchval(
            "SELECT published_at FROM outbox_events WHERE id = $1", event_id
        )
        dedup_rows = await conn.fetchval(
            "SELECT COUNT(*) FROM processed_outbox_events WHERE event_id = $1", event_id
        )
    assert published_at is not None
    assert dedup_rows == 1, "one dedup row per EVENT, not per subscriber"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_post_commit_actions_of_all_subscribers_fire(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """Every subscriber's post-commit action is collected and fired."""
    event_type = f"fanout_b_{uuid.uuid4().hex[:10]}.created"
    fired: list[str] = []

    async def _first(conn: asyncpg.Connection, event: dict):  # noqa: ARG001
        return lambda: fired.append("first")

    async def _second(conn: asyncpg.Connection, event: dict):  # noqa: ARG001
        return lambda: fired.append("second")

    await _drain_stale_outbox(pg_pool)
    await _insert_event(pg_pool, namespace_id=namespace_id, event_type=event_type)

    snapshot = outbox_relay.snapshot_handlers()
    try:
        register_handler(event_type, _first)
        register_handler(event_type, _second)
        delivered = await run_outbox_relay_once(pg_pool, batch_size=10)
    finally:
        outbox_relay.restore_handlers(snapshot)

    assert delivered == 1
    assert fired == ["first", "second"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_one_raising_subscriber_fails_the_whole_event(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """Chosen failure semantics: no partial application, no silent drop.

    ``processed_outbox_events`` is keyed by ``event_id`` alone, so "subscriber A
    applied, subscriber B did not" is not representable.  A raising subscriber
    therefore rolls the whole event back: the sibling's write disappears, no
    dedup row is committed, ``published_at`` stays NULL and ``attempt_count``
    is incremented so the event is retried and eventually dead-lettered.
    """
    event_type = f"fanout_c_{uuid.uuid4().hex[:10]}.created"
    marker = f"fanout-marker-{uuid.uuid4().hex[:8]}"

    async def _writer(conn: asyncpg.Connection, event: dict) -> None:  # noqa: ARG001
        await conn.execute(
            """
            INSERT INTO outbox_events
                (namespace_id, aggregate_type, aggregate_id, event_type, payload, published_at)
            VALUES ($1, 'fanout', $2, 'fanout.sibling_write', '{}'::jsonb, now())
            """,
            namespace_id,
            marker,
        )
        return None

    async def _raiser(conn: asyncpg.Connection, event: dict) -> None:  # noqa: ARG001
        raise RuntimeError("simulated subscriber failure")

    await _drain_stale_outbox(pg_pool)
    event_id = await _insert_event(pg_pool, namespace_id=namespace_id, event_type=event_type)

    snapshot = outbox_relay.snapshot_handlers()
    try:
        register_handler(event_type, _writer)
        register_handler(event_type, _raiser)
        delivered = await run_outbox_relay_once(pg_pool, batch_size=10)
    finally:
        outbox_relay.restore_handlers(snapshot)

    assert delivered == 0

    async with pg_pool.acquire(timeout=10.0) as conn:
        row = await conn.fetchrow(
            "SELECT attempt_count, published_at, error_message FROM outbox_events WHERE id = $1",
            event_id,
        )
        dedup_rows = await conn.fetchval(
            "SELECT COUNT(*) FROM processed_outbox_events WHERE event_id = $1", event_id
        )
        sibling_rows = await conn.fetchval(
            "SELECT COUNT(*) FROM outbox_events WHERE aggregate_id = $1", marker
        )

    assert row["attempt_count"] == 1, "the event must be retried, not silently consumed"
    assert row["published_at"] is None
    assert "simulated subscriber failure" in row["error_message"]
    assert dedup_rows == 0, "no dedup row — otherwise the surviving subscriber is lost forever"
    assert sibling_rows == 0, "the sibling subscriber's write must roll back with the event"

    # --- and the retry is REAL: the next pass re-runs the subscribers ---
    retried: list[str] = []

    async def _recovered(conn: asyncpg.Connection, event: dict) -> None:  # noqa: ARG001
        retried.append("ran")
        return None

    snapshot = outbox_relay.snapshot_handlers()
    try:
        register_handler(event_type, _recovered)
        delivered2 = await run_outbox_relay_once(pg_pool, batch_size=10)
    finally:
        outbox_relay.restore_handlers(snapshot)

    assert delivered2 == 1
    assert retried == ["ran"], (
        "redelivery must actually re-invoke the subscriber; a dedup row committed "
        "during the failed pass would silently skip it and mark the event published"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_empty_subscriber_list_is_treated_as_no_subscriber(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """An emptied list must raise OutboxDeliveryError, exactly like a missing key.

    Keeping the exception type matters: ``run_outbox_relay_once`` fast-fails that
    type straight to the DLQ instead of burning five retries on a config error.
    """
    event_type = f"fanout_d_{uuid.uuid4().hex[:10]}.created"
    snapshot = outbox_relay.snapshot_handlers()
    try:
        OUTBOX_HANDLERS[event_type] = []
        fake_event = {
            "id": uuid.uuid4(),
            "namespace_id": namespace_id,
            "event_type": event_type,
            "attempt_count": 0,
        }
        async with pg_pool.acquire(timeout=10.0) as conn:
            with pytest.raises(OutboxDeliveryError, match="No outbox handler registered"):
                await outbox_relay.deliver_one(conn, fake_event)

            # The raise must precede the dedup INSERT, or a config error would
            # permanently suppress the event even after the wiring is fixed.
            dedup_rows = await conn.fetchval(
                "SELECT COUNT(*) FROM processed_outbox_events WHERE event_id = $1",
                fake_event["id"],
            )
        assert dedup_rows == 0
    finally:
        outbox_relay.restore_handlers(snapshot)
