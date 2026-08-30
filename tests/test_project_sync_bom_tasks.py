"""Integration tests for project/tasks.py — Wave 6 (do_sync_bom_tasks).

Validates the Acceptance criteria from Batch_073_Module_7_Wave_6.md:

  1. A single C4 BOM_LINE.status_changed event triggers one task sync:
       - The right PROJECT_TASK node is created in kg_nodes.
       - The BOM_LINE -[generates]-> TASK edge is written with correct
         confidence.
       - do_sync_bom_tasks fires exactly once per event delivery.
  2. Status → TASK mapping: each BOM_LINE status maps to the correct
     task kind (PROCUREMENT / DELIVERY / INSTALLATION / TESTING / HANDOVER).
  3. Replay is idempotent: a re-delivered event (same event_id already in
     processed_outbox_events) produces zero new writes.
  4. Project writes zero BOM_LINE status or content: the BOM_LINE node's
     entity_type and all kg_edges FROM it with predicate 'has_status'
     remain unchanged after do_sync_bom_tasks runs.
  5. Status advancement closes superseded tasks: when the BOM_LINE status
     moves from PLANNED → ORDERED the PROCUREMENT generates-edge is removed
     (the task node stays as provenance).

Also covers Wave M0.W20b — c4-payload-contract (fail-loud, defect B):
  6. ``_handle_bom_line_status_changed`` raises ``EngineNotRegisteredError``
     (never logs-and-returns) when no engine is registered.
  7. That undeliverable event lands in ``dead_letter_queue`` with
     ``published_at`` left NULL — proven end-to-end through the real relay.
  8. The SAME handler raises ``IncompletePayloadError`` (never logs-and-
     returns) when the payload is missing a required field (e.g. ``status``)
     even though an engine IS registered -- the identical bug class as 6/7,
     caught in review as a second live instance in the same function.
  9. That undeliverable event ALSO lands in ``dead_letter_queue`` with
     ``published_at`` left NULL, proven end-to-end through the real relay.

Most tests are @pytest.mark.integration — require a live Postgres with
schema.sql + migrations applied.  Tests 6 and 8 above are pure-logic unit
tests (no DB) that each isolate one defect-B failure mode from the others
(missing engine vs. incomplete payload) and from defect A (the payload-
contract widening tested in ``tests/test_emit_on_graph_write.py``).

Design notes
-----------
- Tests seed project/BOM_LINE state directly (bypassing do_convert_signed_quote)
  so they don't depend on the Sales A2A seam.
- ``OUTBOX_HANDLERS`` is saved/restored per-test to avoid cross-test leakage.
- Unique quote_id per test avoids kg_nodes collision in a shared DB.
"""

from __future__ import annotations

import uuid
from typing import Any

import asyncpg  # type: ignore[import-untyped]
import pytest

from nce import outbox_relay
from nce.auth import set_namespace_context
from nce.entity_resolution.ownership_seed import seed_node_ownership_registry
from nce.events.bus import OutboxDeliveryError, publish, subscribe
from nce.events.dispatch import dispatch_once
from nce.vertical_modules.project.tasks import (
    _GENERATES_CONFIDENCE,
    _STATUS_TO_TASK_KIND,
    EngineNotRegisteredError,
    IncompletePayloadError,
    _task_label_for_kind,
    do_sync_bom_tasks,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_engine_stub(pg_pool: asyncpg.Pool) -> Any:  # type: ignore[type-arg]
    class _EngineStub:
        pass

    stub = _EngineStub()
    stub.pg_pool = pg_pool  # type: ignore[attr-defined]
    return stub


def _save_and_clear_handlers() -> dict[str, outbox_relay.OutboxHandler]:
    snapshot = dict(outbox_relay.OUTBOX_HANDLERS)
    outbox_relay.OUTBOX_HANDLERS.clear()
    return snapshot


def _restore_handlers(snapshot: dict[str, outbox_relay.OutboxHandler]) -> None:
    outbox_relay.OUTBOX_HANDLERS.clear()
    outbox_relay.OUTBOX_HANDLERS.update(snapshot)


def _bom_line_label(quote_id: str, line_ref: str) -> str:
    return f"BOM_LINE:{quote_id.upper()}:{line_ref.upper()}"


def _project_label(quote_id: str) -> str:
    return f"PROJECT:{quote_id.upper()}"


async def _seed_project_and_bom(
    pg_pool: asyncpg.Pool,
    ns_uuid: uuid.UUID,
    quote_id: str,
    line_refs: list[str],
) -> tuple[str, list[str]]:
    """Seed a minimal PROJECT + BOM_LINE nodes + contains edges.

    Returns (project_label, [bom_line_labels]).
    """
    project_lbl = _project_label(quote_id)
    bom_labels = [_bom_line_label(quote_id, r) for r in line_refs]

    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, ns_uuid)
            await seed_node_ownership_registry(conn, ns_uuid)

            # PROJECT_PROJECT node
            await conn.execute(
                """
                INSERT INTO kg_nodes (label, entity_type, namespace_id)
                VALUES ($1, 'PROJECT_PROJECT', $2::uuid)
                ON CONFLICT (label, namespace_id) DO NOTHING
                """,
                project_lbl,
                str(ns_uuid),
            )

            for bom_label in bom_labels:
                # BOM_LINE node (owned by another engine — insert directly)
                await conn.execute(
                    """
                    INSERT INTO kg_nodes (label, entity_type, namespace_id)
                    VALUES ($1, 'BOM_LINE', $2::uuid)
                    ON CONFLICT (label, namespace_id) DO NOTHING
                    """,
                    bom_label,
                    str(ns_uuid),
                )
                # PROJECT -[contains]-> BOM_LINE edge
                await conn.execute(
                    """
                    INSERT INTO kg_edges
                        (subject_label, predicate, object_label, confidence, namespace_id)
                    VALUES ($1, 'contains', $2, 1.0, $3::uuid)
                    ON CONFLICT (subject_label, predicate, object_label, namespace_id)
                    DO NOTHING
                    """,
                    project_lbl,
                    bom_label,
                    str(ns_uuid),
                )

    return project_lbl, bom_labels


async def _pre_clean_outbox(pg_pool: asyncpg.Pool, event_type: str) -> None:
    """Mark stale unpublished outbox rows for *event_type* as published."""
    async with pg_pool.acquire(timeout=10.0) as conn:
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


async def _count_task_nodes(
    pg_pool: asyncpg.Pool,
    ns_uuid: uuid.UUID,
    task_label: str,
) -> int:
    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, ns_uuid)
            return await conn.fetchval(
                """
                SELECT COUNT(*) FROM kg_nodes
                WHERE label        = $1
                  AND entity_type  = 'PROJECT_TASK'
                  AND namespace_id = $2::uuid
                """,
                task_label,
                str(ns_uuid),
            )


async def _count_generates_edges(
    pg_pool: asyncpg.Pool,
    ns_uuid: uuid.UUID,
    bom_label: str,
    task_label: str,
) -> int:
    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, ns_uuid)
            return await conn.fetchval(
                """
                SELECT COUNT(*) FROM kg_edges
                WHERE subject_label = $1
                  AND predicate      = 'generates'
                  AND object_label   = $2
                  AND namespace_id   = $3::uuid
                """,
                bom_label,
                task_label,
                str(ns_uuid),
            )


async def _count_bom_status_writes(
    pg_pool: asyncpg.Pool,
    ns_uuid: uuid.UUID,
    bom_label: str,
) -> int:
    """Return how many times the BOM_LINE entity_type was changed by us (should be 0)."""
    # We check entity_type is still 'BOM_LINE' — any update would have set updated_at.
    # As a proxy for "project wrote BOM status", we simply check the entity_type remains unchanged.
    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, ns_uuid)
            et = await conn.fetchval(
                """
                SELECT entity_type FROM kg_nodes
                WHERE label        = $1
                  AND namespace_id = $2::uuid
                """,
                bom_label,
                str(ns_uuid),
            )
    return 0 if et == "BOM_LINE" else 1  # 0 = not written by project; 1 = violation


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_planned_status_creates_procurement_task(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """PLANNED status creates a PROCUREMENT task and a generates edge."""
    quote_id = f"B073-T1-{uuid.uuid4().hex[:8]}"
    project_lbl, bom_labels = await _seed_project_and_bom(
        pg_pool, namespace_id, quote_id, ["AMP01"]
    )
    bom_label = bom_labels[0]
    engine = _make_engine_stub(pg_pool)

    result = await do_sync_bom_tasks(
        engine,
        {
            "namespace_id": namespace_id,
            "project_id": project_lbl,
            "bom_line_label": bom_label,
            "status": "PLANNED",
        },
    )

    assert result["ok"] is True, result
    expected_task = _task_label_for_kind(bom_label, "PROCUREMENT")
    assert expected_task in result["tasks_created"]

    # Verify kg_nodes
    count = await _count_task_nodes(pg_pool, namespace_id, expected_task)
    assert count == 1, f"Expected 1 PROJECT_TASK node, got {count}"

    # Verify generates edge
    edge_count = await _count_generates_edges(pg_pool, namespace_id, bom_label, expected_task)
    assert edge_count == 1, f"Expected 1 generates edge, got {edge_count}"

    # Verify confidence on the edge
    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, namespace_id)
            conf = await conn.fetchval(
                """
                SELECT confidence FROM kg_edges
                WHERE subject_label = $1
                  AND predicate      = 'generates'
                  AND object_label   = $2
                  AND namespace_id   = $3::uuid
                """,
                bom_label,
                expected_task,
                str(namespace_id),
            )
    assert conf is not None
    assert abs(float(conf) - _GENERATES_CONFIDENCE) < 1e-6, f"confidence mismatch: {conf}"


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,expected_kind",
    list(_STATUS_TO_TASK_KIND.items()),
)
async def test_status_maps_to_correct_task_kind(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
    status: str,
    expected_kind: str,
) -> None:
    """Each BOM_LINE status maps to the documented task kind."""
    quote_id = f"B073-T2-{uuid.uuid4().hex[:8]}-{status}"
    project_lbl, bom_labels = await _seed_project_and_bom(
        pg_pool, namespace_id, quote_id, ["LINE01"]
    )
    bom_label = bom_labels[0]
    engine = _make_engine_stub(pg_pool)

    result = await do_sync_bom_tasks(
        engine,
        {
            "namespace_id": namespace_id,
            "project_id": project_lbl,
            "bom_line_label": bom_label,
            "status": status,
        },
    )

    assert result["ok"] is True, result
    expected_task = _task_label_for_kind(bom_label, expected_kind)
    assert expected_task in result["tasks_created"], (
        f"status={status} should create kind={expected_kind} task; got {result['tasks_created']}"
    )

    count = await _count_task_nodes(pg_pool, namespace_id, expected_task)
    assert count == 1, f"Expected 1 PROJECT_TASK node for {expected_task}, got {count}"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_replay_is_idempotent_no_duplicate_tasks(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """Calling do_sync_bom_tasks twice with the same args produces no duplicate nodes or edges."""
    quote_id = f"B073-T3-{uuid.uuid4().hex[:8]}"
    project_lbl, bom_labels = await _seed_project_and_bom(
        pg_pool, namespace_id, quote_id, ["SPEAKER01"]
    )
    bom_label = bom_labels[0]
    engine = _make_engine_stub(pg_pool)
    params = {
        "namespace_id": namespace_id,
        "project_id": project_lbl,
        "bom_line_label": bom_label,
        "status": "DELIVERED",
    }

    # First call
    r1 = await do_sync_bom_tasks(engine, params)
    assert r1["ok"] is True, r1

    # Second call — idempotent
    r2 = await do_sync_bom_tasks(engine, params)
    assert r2["ok"] is True, r2

    expected_task = _task_label_for_kind(bom_label, "INSTALLATION")

    # Still exactly one task node (ON CONFLICT DO UPDATE)
    count = await _count_task_nodes(pg_pool, namespace_id, expected_task)
    assert count == 1, f"Expected exactly 1 PROJECT_TASK node, got {count}"

    # Still exactly one generates edge
    edge_count = await _count_generates_edges(pg_pool, namespace_id, bom_label, expected_task)
    assert edge_count == 1, f"Expected exactly 1 generates edge, got {edge_count}"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_project_writes_zero_bom_line_status_or_content(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """Project does NOT write BOM_LINE entity_type or status — §9.1 5-writer rule."""
    quote_id = f"B073-T4-{uuid.uuid4().hex[:8]}"
    project_lbl, bom_labels = await _seed_project_and_bom(
        pg_pool, namespace_id, quote_id, ["SWITCH01"]
    )
    bom_label = bom_labels[0]
    engine = _make_engine_stub(pg_pool)

    # Capture entity_type BEFORE sync
    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, namespace_id)
            et_before = await conn.fetchval(
                "SELECT entity_type FROM kg_nodes WHERE label=$1 AND namespace_id=$2::uuid",
                bom_label,
                str(namespace_id),
            )

    await do_sync_bom_tasks(
        engine,
        {
            "namespace_id": namespace_id,
            "project_id": project_lbl,
            "bom_line_label": bom_label,
            "status": "INSTALLED",
        },
    )

    # Capture entity_type AFTER sync
    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, namespace_id)
            et_after = await conn.fetchval(
                "SELECT entity_type FROM kg_nodes WHERE label=$1 AND namespace_id=$2::uuid",
                bom_label,
                str(namespace_id),
            )

    assert et_before == "BOM_LINE", f"BOM_LINE entity_type changed before sync: {et_before!r}"
    assert et_after == "BOM_LINE", (
        f"Project wrote BOM_LINE entity_type — invariant violated: {et_after!r}"
    )

    # No has_status edge written by project
    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, namespace_id)
            has_status_count = await conn.fetchval(
                """
                SELECT COUNT(*) FROM kg_edges
                WHERE subject_label = $1
                  AND predicate      = 'has_status'
                  AND namespace_id   = $2::uuid
                """,
                bom_label,
                str(namespace_id),
            )
    assert has_status_count == 0, (
        f"Project must not write has_status edges; found {has_status_count}"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_status_advancement_closes_superseded_task(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """Advancing from PLANNED → ORDERED removes the PROCUREMENT generates edge."""
    quote_id = f"B073-T5-{uuid.uuid4().hex[:8]}"
    project_lbl, bom_labels = await _seed_project_and_bom(
        pg_pool, namespace_id, quote_id, ["CABLE01"]
    )
    bom_label = bom_labels[0]
    engine = _make_engine_stub(pg_pool)

    # Step 1: PLANNED → creates PROCUREMENT task
    r_planned = await do_sync_bom_tasks(
        engine,
        {
            "namespace_id": namespace_id,
            "project_id": project_lbl,
            "bom_line_label": bom_label,
            "status": "PLANNED",
        },
    )
    assert r_planned["ok"] is True, r_planned
    procurement_task = _task_label_for_kind(bom_label, "PROCUREMENT")
    assert await _count_generates_edges(pg_pool, namespace_id, bom_label, procurement_task) == 1

    # Step 2: ORDERED → creates DELIVERY task, closes PROCUREMENT generates edge
    r_ordered = await do_sync_bom_tasks(
        engine,
        {
            "namespace_id": namespace_id,
            "project_id": project_lbl,
            "bom_line_label": bom_label,
            "status": "ORDERED",
        },
    )
    assert r_ordered["ok"] is True, r_ordered
    delivery_task = _task_label_for_kind(bom_label, "DELIVERY")
    assert delivery_task in r_ordered["tasks_created"]

    # PROCUREMENT generates edge removed (task closed)
    procurement_edge_count = await _count_generates_edges(
        pg_pool, namespace_id, bom_label, procurement_task
    )
    assert procurement_edge_count == 0, (
        f"PROCUREMENT generates edge should be removed after ORDERED; got {procurement_edge_count}"
    )

    # DELIVERY generates edge created
    delivery_edge_count = await _count_generates_edges(
        pg_pool, namespace_id, bom_label, delivery_task
    )
    assert delivery_edge_count == 1, (
        f"Expected 1 DELIVERY generates edge, got {delivery_edge_count}"
    )

    # PROCUREMENT task NODE still present (provenance retained)
    node_count = await _count_task_nodes(pg_pool, namespace_id, procurement_task)
    assert node_count == 1, f"PROCUREMENT task node should remain as provenance; got {node_count}"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_c4_bus_subscriber_fires_do_sync_bom_tasks_exactly_once(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """A single C4 BOM_LINE.status_changed event fires do_sync_bom_tasks exactly once.

    Uses the production subscribe + make_idempotent_handler + dispatch_once flow.
    A UUID-suffixed node_type ensures the event_type is unique to this test run.
    """
    from nce.vertical_modules.project.tasks import _ENGINE_REGISTRY, _handle_bom_line_status_changed

    quote_id = f"B073-T6-{uuid.uuid4().hex[:8]}"
    project_lbl, bom_labels = await _seed_project_and_bom(
        pg_pool, namespace_id, quote_id, ["RACK01"]
    )
    bom_label = bom_labels[0]
    engine = _make_engine_stub(pg_pool)

    # Register engine in module registry so the handler can call do_sync_bom_tasks
    _ENGINE_REGISTRY["engine"] = engine

    # Use a unique node_type suffix to isolate this test's outbox rows
    node_type_suffix = uuid.uuid4().hex[:12]
    node_type = f"BOM_LINE_{node_type_suffix}"
    op = "status_changed"
    event_type = f"{node_type}.{op}"

    call_count = 0

    async def _counting_handler(
        conn: asyncpg.Connection,  # type: ignore[type-arg]
        event: dict[str, Any],
    ) -> None:
        nonlocal call_count
        call_count += 1
        await _handle_bom_line_status_changed(conn, event)
        return None

    await _pre_clean_outbox(pg_pool, event_type)

    snapshot = _save_and_clear_handlers()
    try:
        # Register without make_idempotent_handler: the relay (deliver_one)
        # already provides at-least-once dedup via processed_outbox_events
        # before invoking registered handlers.
        subscribe(
            {"node_type": node_type, "op": op},
            _counting_handler,
        )

        # Publish the event with the BOM task payload
        async with pg_pool.acquire(timeout=10.0) as conn:
            async with conn.transaction():
                await publish(
                    conn,
                    namespace_id=namespace_id,
                    node_type=node_type,
                    op=op,
                    aggregate_id=bom_label,
                    payload={
                        "node_type": node_type,
                        "op": op,
                        "id": bom_label,
                        "namespace": str(namespace_id),
                        "project_id": project_lbl,
                        "bom_line_label": bom_label,
                        "status": "PLANNED",
                    },
                )

        delivered = await dispatch_once(pg_pool)
        assert delivered >= 1, "dispatch_once must report at least one delivered event"
        assert call_count == 1, f"Handler must fire exactly once; got call_count={call_count}"

        # Verify the task was created
        expected_task = _task_label_for_kind(bom_label, "PROCUREMENT")
        task_count = await _count_task_nodes(pg_pool, namespace_id, expected_task)
        assert task_count == 1, f"Expected 1 PROJECT_TASK node after C4 event, got {task_count}"

    finally:
        _restore_handlers(snapshot)
        _ENGINE_REGISTRY.pop("engine", None)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_c4_relay_replay_is_idempotent(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """A duplicate delivery (same event_id already processed) is a no-op.

    Simulates at-least-once redelivery by pre-inserting the event_id into
    processed_outbox_events before the handler runs.
    """
    from nce.events.dispatch import make_idempotent_handler as mid
    from nce.vertical_modules.project.tasks import _ENGINE_REGISTRY, _handle_bom_line_status_changed

    quote_id = f"B073-T7-{uuid.uuid4().hex[:8]}"
    project_lbl, bom_labels = await _seed_project_and_bom(
        pg_pool, namespace_id, quote_id, ["PROJECTOR01"]
    )
    bom_label = bom_labels[0]
    engine = _make_engine_stub(pg_pool)
    _ENGINE_REGISTRY["engine"] = engine

    event_id = uuid.uuid4()

    # Pre-insert into processed_outbox_events (simulates prior delivery)
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

    fake_event: dict[str, Any] = {
        "id": event_id,
        "namespace_id": namespace_id,
        "aggregate_type": "BOM_LINE",
        "aggregate_id": bom_label,
        "event_type": "BOM_LINE.status_changed",
        "payload": {
            "id": bom_label,
            "namespace": str(namespace_id),
            "project_id": project_lbl,
            "bom_line_label": bom_label,
            "status": "PLANNED",
        },
        "attempt_count": 0,
    }

    call_count = 0

    async def _counting_handler(
        conn: asyncpg.Connection,  # type: ignore[type-arg]
        event: dict[str, Any],
    ) -> None:
        nonlocal call_count
        call_count += 1
        await _handle_bom_line_status_changed(conn, event)
        return None

    idempotent = mid(_counting_handler)

    async with pg_pool.acquire(timeout=10.0) as conn:
        result = await idempotent(conn, fake_event)

    assert result is None, "Duplicate delivery must return None"
    assert call_count == 0, (
        f"Handler business logic must NOT run on duplicate delivery; call_count={call_count}"
    )

    # No TASK node should have been created
    expected_task = _task_label_for_kind(bom_label, "PROCUREMENT")
    task_count = await _count_task_nodes(pg_pool, namespace_id, expected_task)
    assert task_count == 0, f"No task must be created on replay; got {task_count}"

    _ENGINE_REGISTRY.pop("engine", None)


# ---------------------------------------------------------------------------
# M0.W20b — c4-payload-contract: fail-loud on no-engine (defect B)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handler_raises_engine_not_registered_error_when_no_engine() -> None:
    """No-engine must raise, never log-and-return None.

    Before this fix, ``_handle_bom_line_status_changed`` logged an error and
    returned ``None`` when no engine was registered.  To the relay
    (``outbox_relay.deliver_one`` / ``run_outbox_relay_once``) a plain
    ``None`` return is indistinguishable from success: the dedup row commits
    and the outbox row is marked published, permanently destroying the event
    with the automation never having run and no DLQ row or alert to show for
    it.

    It must now raise ``EngineNotRegisteredError`` — a subclass of
    ``OutboxDeliveryError`` — which the relay treats as a wiring defect: dead
    letter immediately, alert, keep the event replayable.
    """
    from nce.vertical_modules.project.tasks import _ENGINE_REGISTRY, _handle_bom_line_status_changed

    _ENGINE_REGISTRY.pop("engine", None)  # the defect under test: no engine registered

    ns_id = uuid.uuid4()
    event: dict[str, Any] = {
        "id": uuid.uuid4(),
        "namespace_id": ns_id,
        "event_type": "BOM_LINE.status_changed",
        "payload": {
            "id": "BOM_LINE:NOENGINE:AMP01",
            "namespace": str(ns_id),
            "project_id": "PROJECT:NOENGINE",
            "bom_line_label": "BOM_LINE:NOENGINE:AMP01",
            "status": "PLANNED",
        },
        "attempt_count": 0,
    }

    # The payload is complete (project_id + status both present), so the
    # ONLY reason this can fail is the missing engine — isolates defect B
    # from defect A (the payload-contract widening tested elsewhere).
    with pytest.raises(EngineNotRegisteredError):
        await _handle_bom_line_status_changed(None, event)  # type: ignore[arg-type]

    assert issubclass(EngineNotRegisteredError, OutboxDeliveryError), (
        "EngineNotRegisteredError must be an OutboxDeliveryError so the relay's "
        "immediate-DLQ (no-retry) branch in run_outbox_relay_once applies to it"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_no_engine_event_lands_in_dlq_not_marked_published(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """Behavioural proof: an undeliverable (no-engine) event lands in the DLQ, never 'published'.

    BEFORE M0.W20b: ``_handle_bom_line_status_changed`` returned ``None`` on
    no-engine.  Run through the real relay (``run_outbox_relay_once``), that
    return value would have been read as a successful delivery — the dedup
    row would commit, ``mark_published`` would run, ``delivered`` would count
    it, and the event would vanish: no ``dead_letter_queue`` row, no alert,
    no trace it was ever undeliverable.

    AFTER: the handler raises ``EngineNotRegisteredError``.  This test
    registers the REAL production handler (unwrapped, exactly as
    ``register_bom_task_subscriber`` would), publishes one complete event
    through it with no engine registered, drains via
    ``outbox_relay.run_outbox_relay_once``, and asserts:
      1. ``outbox_events.published_at`` stays NULL (never marked delivered).
      2. A ``dead_letter_queue`` row appears on the FIRST attempt (the relay's
         ``OutboxDeliveryError`` branch skips the normal 5-retry ramp).
      3. No ``PROJECT_TASK`` node was created — the automation never ran.
    """
    from nce.vertical_modules.project.tasks import _ENGINE_REGISTRY, _handle_bom_line_status_changed

    _ENGINE_REGISTRY.pop("engine", None)  # the defect under test: no engine registered

    quote_id = f"B20B-NOENGINE-{uuid.uuid4().hex[:8]}"
    project_lbl, bom_labels = await _seed_project_and_bom(
        pg_pool, namespace_id, quote_id, ["AMP01"]
    )
    bom_label = bom_labels[0]

    # Unique node_type isolates this test's outbox rows from concurrent tests
    # (including the live nce-cron container that drains the same dev DB).
    node_type_suffix = uuid.uuid4().hex[:12]
    node_type = f"BOM_LINE_{node_type_suffix}"
    op = "status_changed"
    event_type = f"{node_type}.{op}"

    await _pre_clean_outbox(pg_pool, event_type)

    snapshot = _save_and_clear_handlers()
    try:
        # Register the REAL handler unwrapped — exactly the production
        # registration shape (register_bom_task_subscriber calls subscribe()
        # with this same function object, unwrapped).
        subscribe({"node_type": node_type, "op": op}, _handle_bom_line_status_changed)

        async with pg_pool.acquire(timeout=10.0) as conn:
            async with conn.transaction():
                await publish(
                    conn,
                    namespace_id=namespace_id,
                    node_type=node_type,
                    op=op,
                    aggregate_id=bom_label,
                    payload={
                        "node_type": node_type,
                        "op": op,
                        "id": bom_label,
                        "namespace": str(namespace_id),
                        "project_id": project_lbl,
                        "bom_line_label": bom_label,
                        "status": "PLANNED",
                    },
                )

        await outbox_relay.run_outbox_relay_once(pg_pool, batch_size=10)

        async with pg_pool.acquire(timeout=10.0) as conn:
            row = await conn.fetchrow(
                """
                SELECT id, published_at, attempt_count
                FROM outbox_events
                WHERE aggregate_id = $1 AND namespace_id = $2 AND event_type = $3
                """,
                bom_label,
                namespace_id,
                event_type,
            )
        assert row is not None, "the outbox row itself must still exist"
        assert row["published_at"] is None, (
            "REGRESSION: an unhandleable event (no engine) must NOT be marked published — "
            "this is exactly the silent-data-loss defect M0.W20b fixes"
        )
        assert row["attempt_count"] == outbox_relay.MAX_OUTBOX_ATTEMPTS, (
            "OutboxDeliveryError must skip straight to the exhausted attempt_count, "
            "not burn through the normal per-attempt retry ramp"
        )

        async with pg_pool.acquire(timeout=10.0) as conn:
            dlq_row = await conn.fetchrow(
                "SELECT task_name, error_message FROM dead_letter_queue WHERE job_id = $1",
                str(row["id"]),
            )
        assert dlq_row is not None, (
            "an unhandleable event must land in dead_letter_queue instead of being lost silently"
        )
        assert dlq_row["task_name"] == f"outbox:{event_type}"
        assert "EngineNotRegisteredError" in dlq_row["error_message"]

        # The automation itself must never have run.
        expected_task = _task_label_for_kind(bom_label, "PROCUREMENT")
        task_count = await _count_task_nodes(pg_pool, namespace_id, expected_task)
        assert task_count == 0, "do_sync_bom_tasks must never have run when no engine is registered"

    finally:
        _restore_handlers(snapshot)
        _ENGINE_REGISTRY.pop("engine", None)


# ---------------------------------------------------------------------------
# M0.W20b re-review fix — fail-loud on incomplete payload (defect B, 2nd instance)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handler_raises_incomplete_payload_error_when_status_missing() -> None:
    """Incomplete payload must raise, never log-and-return None.

    Same bug class as the no-engine case, found by review as a SECOND live
    instance in the same function: before this fix,
    ``_handle_bom_line_status_changed`` logged a warning and returned
    ``None`` when the payload was missing ``status`` (or any of
    ``namespace``/``project_id``/``bom_line_label``).  This event matched the
    ``BOM_LINE.status_changed`` selector, so it IS for this handler -- it
    just cannot act on it.  A plain ``None`` return is indistinguishable
    from success to the relay: the dedup row commits and the outbox row is
    marked published, permanently destroying the event with no DLQ row and
    no indication of which field was missing.

    An engine IS registered here (unlike the no-engine test) so the ONLY
    possible failure is the missing ``status`` field -- isolating this
    failure mode from ``EngineNotRegisteredError``.  Must raise
    ``IncompletePayloadError``, a subclass of ``OutboxDeliveryError``, naming
    ``status`` in its message.
    """
    from nce.vertical_modules.project.tasks import _ENGINE_REGISTRY, _handle_bom_line_status_changed

    _ENGINE_REGISTRY["engine"] = object()  # any truthy value; must not be reached
    try:
        ns_id = uuid.uuid4()
        event: dict[str, Any] = {
            "id": uuid.uuid4(),
            "namespace_id": ns_id,
            "event_type": "BOM_LINE.status_changed",
            "aggregate_id": "BOM_LINE:NOSTATUS:AMP01",
            "payload": {
                "id": "BOM_LINE:NOSTATUS:AMP01",
                "namespace": str(ns_id),
                "project_id": "PROJECT:NOSTATUS",
                "bom_line_label": "BOM_LINE:NOSTATUS:AMP01",
                # "status" deliberately omitted.
            },
            "attempt_count": 0,
        }

        with pytest.raises(IncompletePayloadError) as exc_info:
            await _handle_bom_line_status_changed(None, event)  # type: ignore[arg-type]

        assert "status" in str(exc_info.value), (
            "IncompletePayloadError message must name the missing field so the DLQ row "
            "is diagnostic, not just a stack trace"
        )
        assert issubclass(IncompletePayloadError, OutboxDeliveryError), (
            "IncompletePayloadError must be an OutboxDeliveryError so the relay's "
            "immediate-DLQ (no-retry) branch in run_outbox_relay_once applies to it"
        )
    finally:
        _ENGINE_REGISTRY.pop("engine", None)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_incomplete_payload_event_lands_in_dlq_not_marked_published(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """Behavioural proof: an incomplete-payload event lands in the DLQ, never 'published'.

    Mirrors ``test_no_engine_event_lands_in_dlq_not_marked_published`` but
    isolates the OTHER defect-B failure mode: here an engine IS registered
    (so ``EngineNotRegisteredError`` cannot fire) and the published event's
    payload is simply missing ``status``.

    BEFORE this fix: the handler logged a warning and returned ``None`` --
    read by the real relay as a successful delivery.  AFTER: it raises
    ``IncompletePayloadError``.  Drains via the real
    ``outbox_relay.run_outbox_relay_once`` and asserts:
      1. ``outbox_events.published_at`` stays NULL (never marked delivered).
      2. A ``dead_letter_queue`` row appears on the FIRST attempt (the
         relay's ``OutboxDeliveryError`` branch skips the normal 5-retry
         ramp), naming the missing field.
      3. No ``PROJECT_TASK`` node was created -- the automation never ran.
    """
    from nce.vertical_modules.project.tasks import _ENGINE_REGISTRY, _handle_bom_line_status_changed

    quote_id = f"B20B-NOSTATUS-{uuid.uuid4().hex[:8]}"
    project_lbl, bom_labels = await _seed_project_and_bom(
        pg_pool, namespace_id, quote_id, ["AMP02"]
    )
    bom_label = bom_labels[0]
    engine = _make_engine_stub(pg_pool)
    _ENGINE_REGISTRY["engine"] = (
        engine  # registered -- isolates this from defect-B's no-engine path
    )

    node_type_suffix = uuid.uuid4().hex[:12]
    node_type = f"BOM_LINE_{node_type_suffix}"
    op = "status_changed"
    event_type = f"{node_type}.{op}"

    await _pre_clean_outbox(pg_pool, event_type)

    snapshot = _save_and_clear_handlers()
    try:
        subscribe({"node_type": node_type, "op": op}, _handle_bom_line_status_changed)

        async with pg_pool.acquire(timeout=10.0) as conn:
            async with conn.transaction():
                await publish(
                    conn,
                    namespace_id=namespace_id,
                    node_type=node_type,
                    op=op,
                    aggregate_id=bom_label,
                    payload={
                        "node_type": node_type,
                        "op": op,
                        "id": bom_label,
                        "namespace": str(namespace_id),
                        "project_id": project_lbl,
                        "bom_line_label": bom_label,
                        # "status" deliberately omitted -- the defect under test.
                    },
                )

        await outbox_relay.run_outbox_relay_once(pg_pool, batch_size=10)

        async with pg_pool.acquire(timeout=10.0) as conn:
            row = await conn.fetchrow(
                """
                SELECT id, published_at, attempt_count
                FROM outbox_events
                WHERE aggregate_id = $1 AND namespace_id = $2 AND event_type = $3
                """,
                bom_label,
                namespace_id,
                event_type,
            )
        assert row is not None, "the outbox row itself must still exist"
        assert row["published_at"] is None, (
            "REGRESSION: an incomplete-payload event must NOT be marked published -- "
            "this is the second instance of the silent-data-loss defect M0.W20b fixes"
        )
        assert row["attempt_count"] == outbox_relay.MAX_OUTBOX_ATTEMPTS, (
            "OutboxDeliveryError must skip straight to the exhausted attempt_count, "
            "not burn through the normal per-attempt retry ramp"
        )

        async with pg_pool.acquire(timeout=10.0) as conn:
            dlq_row = await conn.fetchrow(
                "SELECT task_name, error_message FROM dead_letter_queue WHERE job_id = $1",
                str(row["id"]),
            )
        assert dlq_row is not None, (
            "an incomplete-payload event must land in dead_letter_queue instead of being "
            "lost silently"
        )
        assert dlq_row["task_name"] == f"outbox:{event_type}"
        assert "IncompletePayloadError" in dlq_row["error_message"]
        assert "status" in dlq_row["error_message"], (
            "the DLQ error message must name the missing field, not just the exception class"
        )

        expected_task = _task_label_for_kind(bom_label, "PROCUREMENT")
        task_count = await _count_task_nodes(pg_pool, namespace_id, expected_task)
        assert task_count == 0, "do_sync_bom_tasks must never have run on an incomplete payload"

    finally:
        _restore_handlers(snapshot)
        _ENGINE_REGISTRY.pop("engine", None)
