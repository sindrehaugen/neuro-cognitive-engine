"""tests/test_c4_donewhen.py

Done-when acceptance test for Batch 020 / M0.W20 c4-subscriber-dispatch (C4 §9.6).

Proves:
1. A→B dispatch: an event published by producer A is dispatched to subscriber B
   exactly once — the handler side-effect fires once and the event is marked
   in ``processed_outbox_events``.
2. Idempotent redelivery: a duplicate delivery of the same ``event_id`` is a
   silent no-op — the handler side-effect does NOT fire a second time.
3. Cert-expiry → allocation-invalidation without polling: an HR cert-expiry event
   (A) fires a Resources allocation-invalidation handler (B) via the subscriber
   registry + the relay drain.  No polling is involved — the handler fires off the
   drained event, not a scheduled scan.

All tests are ``@pytest.mark.integration`` — they require a live Postgres
instance reachable via ``NCE_INTEGRATION_PG_DSN`` / ``PG_DSN`` / ``DATABASE_URL``
(see ``conftest.py``).

Design notes
------------
- Handlers are registered via ``subscribe`` **unwrapped**.  ``deliver_one`` writes
  the ``processed_outbox_events`` row before invoking any handler, so wrapping a
  subscriber in ``make_idempotent_handler`` makes it observe its own dedup row and
  skip the business logic on every delivery.  Tests 2 and 4 still exercise the
  wrapper directly, outside the relay, where its SAVEPOINT semantics apply.
- The test restores ``OUTBOX_HANDLERS`` to its pre-test state after each case to
  avoid leaking registrations across the suite.
- No polling loop is added here or in ``dispatch.py`` — the "no polling" assertion
  is structural: the allocation handler fires because the relay drains the outbox
  row, not because any periodic scan queries for cert-expiry state.

Test-isolation strategy
-----------------------
``run_outbox_relay_once`` (called by ``dispatch_once``) drains ALL unpublished
rows from ``outbox_events``, regardless of event_type.  To prevent leftover rows
from other tests (e.g. ``account.upserted`` emitted by
``tests/test_emit_on_graph_write.py``) from triggering this file's handlers, every
test that calls ``dispatch_once`` does two things before publishing:

1. **Unique node_type per test** — a UUID-suffixed slug (e.g.
   ``"c4t1_acct_<hex>"``).  Because no other test emits this exact event_type,
   the registered handler can only be triggered by this test's own outbox row.

2. **Pre-clean unpublished rows for the test's event_type** — before publishing,
   any stale unpublished row with the same event_type is forcibly marked published.
   This makes the drain deterministic even if a previous failed run of this same
   test left behind an unpublished row.

These two measures together guarantee order-independence without weakening the
production assertions.  Tests 2 and 4 call the idempotent wrapper directly
(not via ``dispatch_once``) so they are not affected by relay drain scope and
need no special isolation beyond their unique UUIDs.
"""

from __future__ import annotations

import uuid
from typing import Any

import asyncpg  # type: ignore[import-untyped]
import pytest

from nce import outbox_relay
from nce.events.bus import publish, subscribe
from nce.events.dispatch import dispatch_once, make_idempotent_handler

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _save_and_clear_handlers() -> dict[str, list[outbox_relay.OutboxHandler]]:
    """Snapshot + clear OUTBOX_HANDLERS; return snapshot for restoration.

    Delegates to the relay's own helpers: the registry values are subscriber
    *lists*, so a plain ``dict()`` copy would alias them and a later subscribe
    would mutate the snapshot it is supposed to be restoring.
    """
    snapshot = outbox_relay.snapshot_handlers()
    outbox_relay.OUTBOX_HANDLERS.clear()
    return snapshot


def _restore_handlers(snapshot: dict[str, list[outbox_relay.OutboxHandler]]) -> None:
    outbox_relay.restore_handlers(snapshot)


async def _pre_clean_outbox(
    pool: asyncpg.Pool,
    event_type: str,
) -> None:
    """Mark all unpublished outbox_events rows for *event_type* as published.

    This prevents stale rows from a prior failed run of this test (which would
    have the same event_type) from being picked up by the relay drain.  It does
    NOT delete rows — it only sets ``published_at`` so the relay's
    ``WHERE published_at IS NULL`` filter skips them.
    """
    async with pool.acquire(timeout=10.0) as conn:
        async with conn.transaction():
            await conn.execute(
                """
                UPDATE outbox_events
                SET published_at = now()
                WHERE event_type = $1
                  AND published_at IS NULL
                """,
                event_type,
            )


# ---------------------------------------------------------------------------
# Test 1 — A→B dispatch: event published by A fires B's handler exactly once
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_to_b_dispatch_fires_handler_exactly_once(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """An event published by producer A is dispatched to subscriber B exactly once.

    Uses a UUID-suffixed node_type to ensure the event_type is unique to this
    test run, preventing leftover outbox rows from other tests from triggering
    the handler a second time when the relay drains the shared outbox table.

    Flow:
      1. Producer A publishes a test-unique event into ``outbox_events``
         inside a transaction (the event is not yet delivered).
      2. Subscriber B registers an idempotent handler for that event_type.
      3. ``dispatch_once`` drains the outbox — the relay delivers the event to B's
         handler and marks it published.
      4. The handler side-effect has fired exactly once.
      5. ``processed_outbox_events`` contains exactly one row for this event.
    """
    # UUID suffix makes node_type unique to this test run — no other test emits
    # this exact event_type, so the handler cannot be triggered by alien rows.
    node_type = f"c4t1_acct_{uuid.uuid4().hex[:12]}"
    op = "upserted"
    event_type = f"{node_type}.{op}"
    aggregate_id = f"test-acct-{uuid.uuid4().hex[:8]}"

    # Pre-clean: mark any stale unpublished rows for this event_type (e.g. from
    # a prior failed run) so the relay drain sees exactly the one row we publish.
    await _pre_clean_outbox(pg_pool, event_type)

    # --- Step 1: Producer A publishes the event ---
    async with pg_pool.acquire(timeout=10.0) as conn:
        async with conn.transaction():
            await publish(
                conn,
                namespace_id=namespace_id,
                node_type=node_type,
                op=op,
                aggregate_id=aggregate_id,
                payload={"node_type": node_type, "op": op, "id": aggregate_id},
            )

    # Retrieve the outbox row id so we can check processed_outbox_events later.
    async with pg_pool.acquire(timeout=10.0) as conn:
        outbox_id: uuid.UUID = await conn.fetchval(
            "SELECT id FROM outbox_events WHERE aggregate_id = $1 AND namespace_id = $2",
            aggregate_id,
            namespace_id,
        )
    assert outbox_id is not None, "publish() must insert a row into outbox_events"

    # --- Step 2: Subscriber B registers an idempotent handler ---
    call_count = 0

    async def _b_handler(
        conn: asyncpg.Connection,
        event: dict[str, Any],
    ) -> outbox_relay.PostCommitAction | None:
        nonlocal call_count
        call_count += 1
        return None

    snapshot = _save_and_clear_handlers()
    try:
        # Registered UNWRAPPED: deliver_one inserts the dedup row before calling
        # the handler, so a make_idempotent_handler wrapper here would observe its
        # own row and skip the business logic entirely.
        subscribe({"node_type": node_type, "op": op}, _b_handler)

        # --- Step 3: dispatch_once drains the outbox ---
        # OUTBOX_HANDLERS contains ONLY our test's handler (snapshot cleared it).
        # Any alien unpublished rows would raise OutboxDeliveryError (no handler)
        # and be DLQ'd without touching call_count.  The unique node_type + pre-clean
        # ensure there are no such alien rows with our event_type.
        delivered = await dispatch_once(pg_pool)

        # --- Step 4: handler fired exactly once ---
        assert delivered >= 1, "dispatch_once must report at least one delivered event"
        assert call_count == 1, f"Handler B must fire exactly once; call_count={call_count}"

        # --- Step 5: processed_outbox_events has exactly one row ---
        async with pg_pool.acquire(timeout=10.0) as conn:
            proc_count = await conn.fetchval(
                "SELECT COUNT(*) FROM processed_outbox_events WHERE event_id = $1",
                outbox_id,
            )
        assert proc_count == 1, (
            f"processed_outbox_events must contain exactly one row for event_id={outbox_id}; "
            f"got {proc_count}"
        )
    finally:
        _restore_handlers(snapshot)


# ---------------------------------------------------------------------------
# Test 2 — Idempotent redelivery: duplicate delivery is a no-op
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_duplicate_delivery_is_idempotent_noop(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """A redelivered event whose event_id is already in processed_outbox_events is a no-op.

    The relay marks the outbox row published after the first delivery.  To simulate
    a second delivery without re-running the relay (which would skip already-published
    rows), we call ``make_idempotent_handler`` directly, emulating the situation where
    the relay delivers the same event_id twice (e.g. crash-before-mark-published).

    The idempotent wrapper must:
    - Return ``None`` immediately on the second call (no business logic re-executed).
    - Leave ``processed_outbox_events`` with exactly one row for this event_id.
    """
    node_type = "contact"
    op = "updated"
    aggregate_id = f"test-contact-{uuid.uuid4().hex[:8]}"
    event_id = uuid.uuid4()

    # Pre-insert a namespace-scoped row into processed_outbox_events as if the
    # first delivery already ran.
    async with pg_pool.acquire(timeout=10.0) as conn:
        await conn.execute(
            """
            INSERT INTO processed_outbox_events (event_id, namespace_id)
            VALUES ($1, $2::uuid)
            ON CONFLICT (event_id) DO NOTHING
            """,
            event_id,
            str(namespace_id),
        )

    # Build a fake event dict matching the shape the relay passes to handlers.
    fake_event: dict[str, Any] = {
        "id": event_id,
        "namespace_id": namespace_id,
        "aggregate_type": node_type,
        "aggregate_id": aggregate_id,
        "event_type": f"{node_type}.{op}",
        "payload": {"node_type": node_type, "op": op, "id": aggregate_id},
        "attempt_count": 0,
    }

    call_count = 0

    async def _b_handler(
        conn: asyncpg.Connection,
        event: dict[str, Any],
    ) -> outbox_relay.PostCommitAction | None:
        nonlocal call_count
        call_count += 1
        return None

    idempotent = make_idempotent_handler(_b_handler)

    async with pg_pool.acquire(timeout=10.0) as conn:
        result = await idempotent(conn, fake_event)

    assert result is None, "Duplicate delivery must return None (no-op)"
    assert call_count == 0, (
        f"Handler business logic must NOT run on duplicate delivery; call_count={call_count}"
    )

    # processed_outbox_events must still contain exactly one row (no duplicate insert).
    async with pg_pool.acquire(timeout=10.0) as conn:
        proc_count = await conn.fetchval(
            "SELECT COUNT(*) FROM processed_outbox_events WHERE event_id = $1",
            event_id,
        )
    assert proc_count == 1, (
        f"processed_outbox_events must still contain exactly one row; got {proc_count}"
    )


# ---------------------------------------------------------------------------
# Test 3 — Cert-expiry → allocation-invalidation without polling (C4 done-when)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cert_expiry_fires_allocation_invalidation_without_polling(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """HR cert-expiry event (A) fires a Resources allocation-invalidation handler (B).

    This is the canonical C4 done-when scenario from §9.6 / 99-shared-core-foundation:
    a write in the HR engine (cert-expiry) reliably and idempotently triggers a
    Resources engine side-effect (allocation-invalidation) WITHOUT polling.

    No-polling proof: the allocation handler fires because the relay drains the
    outbox row inserted by the cert-expiry publish call.  There is no periodic scan
    that queries for expired certificates — the handler wires directly to the event.

    Flow:
      1. HR engine (A) publishes ``hr_cert.expired`` into outbox_events.
      2. Resources engine (B) registers an idempotent ``hr_cert.expired`` handler
         that records that an allocation-invalidation was triggered.
      3. ``dispatch_once`` drains the outbox — no polling loop is invoked.
      4. Allocation-invalidation handler fired exactly once.
      5. Redelivery of the same event (same event_id) is a no-op.
    """
    # UUID suffix makes node_type unique to this test run — isolates the drain.
    node_type = f"c4t3_hrcert_{uuid.uuid4().hex[:10]}"
    op = "expired"
    event_type = f"{node_type}.{op}"
    cert_aggregate_id = f"cert-{uuid.uuid4().hex[:8]}"
    allocation_invalidations: list[dict[str, Any]] = []

    # Pre-clean: mark any stale unpublished rows for this event_type so the
    # relay drain only sees the one row we publish below.
    await _pre_clean_outbox(pg_pool, event_type)

    # --- Step 1: HR engine (A) publishes cert-expiry event ---
    async with pg_pool.acquire(timeout=10.0) as conn:
        async with conn.transaction():
            await publish(
                conn,
                namespace_id=namespace_id,
                node_type=node_type,
                op=op,
                aggregate_id=cert_aggregate_id,
                payload={
                    "node_type": node_type,
                    "op": op,
                    "id": cert_aggregate_id,
                    "namespace": str(namespace_id),
                    "reason": "cert_expiry",
                },
            )

    # Retrieve the outbox row id.
    async with pg_pool.acquire(timeout=10.0) as conn:
        outbox_id: uuid.UUID = await conn.fetchval(
            "SELECT id FROM outbox_events WHERE aggregate_id = $1 AND namespace_id = $2",
            cert_aggregate_id,
            namespace_id,
        )
    assert outbox_id is not None, "HR cert-expiry event must be inserted into outbox_events"

    # --- Step 2: Resources engine (B) registers handler ---
    async def _invalidate_allocations(
        conn: asyncpg.Connection,
        event: dict[str, Any],
    ) -> outbox_relay.PostCommitAction | None:
        """Simulate allocation-invalidation triggered by cert-expiry.

        In production this would UPDATE relevant allocation rows; here we record
        the call to prove the handler fired exactly once without polling.
        """
        import json as _json

        raw_payload = event.get("payload") or {}
        payload: dict[str, Any] = (
            _json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
        )
        allocation_invalidations.append(
            {
                "cert_id": payload.get("id") or event.get("aggregate_id"),
                "reason": payload.get("reason"),
            }
        )
        return None

    snapshot = _save_and_clear_handlers()
    try:
        # Registered UNWRAPPED — see the note in test 1.
        subscribe({"node_type": node_type, "op": op}, _invalidate_allocations)

        # --- Step 3: dispatch_once drains without any polling loop ---
        delivered = await dispatch_once(pg_pool)

        # --- Step 4: allocation-invalidation fired exactly once ---
        assert delivered >= 1, "dispatch_once must report at least one delivered event"
        assert len(allocation_invalidations) == 1, (
            f"Allocation-invalidation handler must fire exactly once; "
            f"fired {len(allocation_invalidations)} time(s)"
        )
        assert allocation_invalidations[0]["cert_id"] == cert_aggregate_id
        assert allocation_invalidations[0]["reason"] == "cert_expiry"

        # --- Step 5: redelivery (same event_id) is a no-op ---
        # Build a fake event matching the relay's shape and call deliver_one
        # directly (the relay skips already-published rows, so this is how the
        # at-least-once redelivery path is exercised).  deliver_one owns the
        # dedup guard, so the subscriber must not fire again.
        fake_event: dict[str, Any] = {
            "id": outbox_id,
            "namespace_id": namespace_id,
            "aggregate_type": node_type,
            "aggregate_id": cert_aggregate_id,
            "event_type": f"{node_type}.{op}",
            "payload": {
                "node_type": node_type,
                "op": op,
                "id": cert_aggregate_id,
                "namespace": str(namespace_id),
                "reason": "cert_expiry",
            },
            "attempt_count": 0,
        }

        assert outbox_relay.OUTBOX_HANDLERS[f"{node_type}.{op}"] == [_invalidate_allocations], (
            "the subscriber must be registered unwrapped, exactly once"
        )
        async with pg_pool.acquire(timeout=10.0) as conn:
            async with conn.transaction():
                result = await outbox_relay.deliver_one(conn, fake_event)

        assert isinstance(result, outbox_relay._AlreadyProcessed), (
            "Redelivery of cert-expiry event must short-circuit on the dedup row"
        )
        assert len(allocation_invalidations) == 1, (
            f"Allocation-invalidation must NOT fire again on redelivery; "
            f"fired {len(allocation_invalidations)} time(s) total"
        )

        # processed_outbox_events has exactly one row.
        async with pg_pool.acquire(timeout=10.0) as conn:
            proc_count = await conn.fetchval(
                "SELECT COUNT(*) FROM processed_outbox_events WHERE event_id = $1",
                outbox_id,
            )
        assert proc_count == 1, (
            f"processed_outbox_events must contain exactly one row for the cert-expiry event; "
            f"got {proc_count}"
        )
    finally:
        _restore_handlers(snapshot)


# ---------------------------------------------------------------------------
# Test 4 — Failure → redeliver: dedup mark NOT committed when business_fn raises
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_handler_failure_does_not_commit_dedup_mark(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """If business_fn raises, the dedup mark must NOT be committed.

    This validates the SAVEPOINT fix for the at-least-once violation:

    - First delivery: handler raises RuntimeError.
      * processed_outbox_events must NOT contain the event_id (savepoint rolled back).
    - Second delivery (simulated by calling the wrapper directly again):
      * handler succeeds.
      * processed_outbox_events now contains exactly one row.
      * call_count == 2 (business_fn ran on attempt 1 (raised) + attempt 2 (succeeded)).

    The no-double-fire invariant is also re-proved: a genuine third delivery
    after the mark is committed returns None and does NOT increment call_count.
    """
    node_type = "widget"
    op = "broken"
    aggregate_id = f"widget-{uuid.uuid4().hex[:8]}"
    event_id = uuid.uuid4()

    fake_event: dict[str, Any] = {
        "id": event_id,
        "namespace_id": namespace_id,
        "aggregate_type": node_type,
        "aggregate_id": aggregate_id,
        "event_type": f"{node_type}.{op}",
        "payload": {"node_type": node_type, "op": op, "id": aggregate_id},
        "attempt_count": 0,
    }

    call_count = 0
    should_raise = True  # flip to False on second delivery

    async def _flaky_handler(
        conn: asyncpg.Connection,
        event: dict[str, Any],
    ) -> outbox_relay.PostCommitAction | None:
        nonlocal call_count, should_raise
        call_count += 1
        if should_raise:
            raise RuntimeError("simulated transient handler failure")
        return None

    idempotent = make_idempotent_handler(_flaky_handler)

    # --- First delivery: handler raises; dedup mark must NOT be committed ---
    async with pg_pool.acquire(timeout=10.0) as conn:
        # Wrap in an outer transaction to mirror the relay's own transaction.
        async with conn.transaction():
            try:
                await idempotent(conn, fake_event)
            except RuntimeError:
                pass  # expected — relay would catch this and call mark_failed

    # dedup mark must NOT be present (savepoint was rolled back with outer tx)
    async with pg_pool.acquire(timeout=10.0) as conn:
        proc_count_after_fail = await conn.fetchval(
            "SELECT COUNT(*) FROM processed_outbox_events WHERE event_id = $1",
            event_id,
        )
    assert proc_count_after_fail == 0, (
        "Dedup mark must NOT be committed when business_fn raises; "
        f"got {proc_count_after_fail} row(s) in processed_outbox_events"
    )
    assert call_count == 1, f"Handler must have been called once (then raised); got {call_count}"

    # --- Second delivery: handler succeeds; dedup mark IS committed ---
    should_raise = False
    async with pg_pool.acquire(timeout=10.0) as conn:
        async with conn.transaction():
            result = await idempotent(conn, fake_event)

    assert result is None  # handler returned None
    assert call_count == 2, (
        f"Handler must re-fire on second delivery (at-least-once); call_count={call_count}"
    )

    async with pg_pool.acquire(timeout=10.0) as conn:
        proc_count_after_success = await conn.fetchval(
            "SELECT COUNT(*) FROM processed_outbox_events WHERE event_id = $1",
            event_id,
        )
    assert proc_count_after_success == 1, (
        "Dedup mark must be committed after successful delivery; "
        f"got {proc_count_after_success} row(s)"
    )

    # --- Third delivery (genuine duplicate): no double-fire ---
    async with pg_pool.acquire(timeout=10.0) as conn:
        async with conn.transaction():
            result2 = await idempotent(conn, fake_event)

    assert result2 is None, "Genuine duplicate must return None (no double-fire)"
    assert call_count == 2, (
        f"Handler must NOT fire again on genuine duplicate; call_count={call_count}"
    )
