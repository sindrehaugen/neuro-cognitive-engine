"""tests/test_emit_on_graph_write.py

Acceptance test for Wave 19 — c4-emit-on-graph-write (C4 §9.6).

Verifies:
1. A single kg_nodes upsert via ``_upsert_kg_node`` emits exactly one
   ``(node_type, op, id, namespace)`` event into ``outbox_events``.
2. Duplicate delivery of the same ``event_id`` is deduped via
   ``processed_outbox_events`` (idempotent at-least-once).
3. The outbox emit is atomic with the kg_nodes write: if the transaction
   rolls back (e.g. emit raises), neither row is visible — both commit or
   both roll back (transactional-outbox semantics).

Also covers Wave M0.W20b — c4-payload-contract:
4. ``emit_graph_write``'s payload stays exactly the 4-key
   ``(node_type, op, id, namespace)`` shape (regression guard — 22
   ``op="upserted"`` sites and ``memory.stored`` depend on it).
5. ``emit_status_change`` (new, additive) carries ``project_id``/``status``
   on top of the same 4 keys, and both round-trip intact through
   ``publish`` → JSONB → read-back.

Most tests are ``@pytest.mark.integration`` and require a live Postgres
instance reachable via ``NCE_INTEGRATION_PG_DSN`` / ``PG_DSN`` / ``DATABASE_URL``.
The payload-shape unit tests (4 and 5 above) patch ``publish`` out, so they
need no database.
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg  # type: ignore[import-untyped]
import pytest

from nce.events.emit import emit_graph_write, emit_status_change, mark_graph_write_processed

# ---------------------------------------------------------------------------
# Unit — pure-logic tests (no DB)
# ---------------------------------------------------------------------------


def test_emit_graph_write_is_coroutine():
    """emit_graph_write must be an async function."""
    import inspect

    conn = MagicMock()
    coro = emit_graph_write(
        conn,
        namespace_id=uuid.uuid4(),
        node_type="account",
        op="upserted",
        node_id="Contoso Ltd",
    )
    assert inspect.iscoroutine(coro), "emit_graph_write must return a coroutine"
    coro.close()  # prevent ResourceWarning


def test_emit_graph_write_payload_is_exactly_four_keys():
    """Regression guard (M0.W20b): emit_graph_write's payload must stay 4-key.

    ``memory.stored`` and the 22 ``op="upserted"`` call sites depend on this
    exact shape.  Adding ``emit_status_change`` alongside it must not widen
    this payload by even one key.
    """
    import asyncio

    ns_id = uuid.uuid4()
    captured: dict = {}

    async def _fake_publish(conn, *, namespace_id, node_type, op, aggregate_id, payload):
        captured["payload"] = payload

    with patch("nce.events.emit.publish", new=_fake_publish):
        asyncio.run(
            emit_graph_write(
                MagicMock(),
                namespace_id=ns_id,
                node_type="account",
                op="upserted",
                node_id="Contoso Ltd",
            )
        )

    assert captured["payload"] == {
        "node_type": "account",
        "op": "upserted",
        "id": "Contoso Ltd",
        "namespace": str(ns_id),
    }
    assert set(captured["payload"].keys()) == {"node_type", "op", "id", "namespace"}


def test_emit_status_change_is_coroutine():
    """emit_status_change must be an async function."""
    import inspect

    conn = MagicMock()
    coro = emit_status_change(
        conn,
        namespace_id=uuid.uuid4(),
        node_type="BOM_LINE",
        op="status_changed",
        node_id="BOM_LINE:Q1:AMP01",
        project_id="PROJECT:Q1",
        status="DELIVERED",
    )
    assert inspect.iscoroutine(coro), "emit_status_change must return a coroutine"
    coro.close()  # prevent ResourceWarning


def test_emit_status_change_builds_six_key_payload_round_trip():
    """emit_status_change's payload carries the base 4 keys PLUS project_id/status.

    This is the payload-contract acceptance test for M0.W20b: Module 7's
    handlers require ``project_id`` and ``status`` on top of the base
    ``(node_type, op, id, namespace)`` contract, and both must round-trip
    intact through the payload dict handed to ``publish``.
    """
    import asyncio

    ns_id = uuid.uuid4()
    captured: dict = {}

    async def _fake_publish(conn, *, namespace_id, node_type, op, aggregate_id, payload):
        captured["payload"] = payload
        captured["namespace_id"] = namespace_id
        captured["node_type"] = node_type
        captured["op"] = op
        captured["aggregate_id"] = aggregate_id

    with patch("nce.events.emit.publish", new=_fake_publish):
        asyncio.run(
            emit_status_change(
                MagicMock(),
                namespace_id=ns_id,
                node_type="BOM_LINE",
                op="status_changed",
                node_id="BOM_LINE:Q1:AMP01",
                project_id="PROJECT:Q1",
                status="DELIVERED",
            )
        )

    assert captured["payload"] == {
        "node_type": "BOM_LINE",
        "op": "status_changed",
        "id": "BOM_LINE:Q1:AMP01",
        "namespace": str(ns_id),
        "project_id": "PROJECT:Q1",
        "status": "DELIVERED",
    }
    # publish() plumbing args unaffected — same call shape as emit_graph_write.
    assert captured["namespace_id"] == ns_id
    assert captured["node_type"] == "BOM_LINE"
    assert captured["op"] == "status_changed"
    assert captured["aggregate_id"] == "BOM_LINE:Q1:AMP01"


def test_mark_graph_write_processed_is_coroutine():
    """mark_graph_write_processed must be an async function."""
    import inspect

    conn = MagicMock()
    coro = mark_graph_write_processed(
        conn,
        event_id=uuid.uuid4(),
        namespace_id=uuid.uuid4(),
    )
    assert inspect.iscoroutine(coro), "mark_graph_write_processed must return a coroutine"
    coro.close()


# ---------------------------------------------------------------------------
# Integration — requires Postgres
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_kg_upsert_emits_exactly_one_graph_write_event(pg_pool, namespace_id):
    """A kg_node upsert + post-commit emit lands exactly one row in outbox_events.

    Exercises the same pattern that ``_upsert_kg_node`` uses: upsert a kg_nodes
    row then call ``emit_graph_write`` inside the same transaction.  The event
    payload must carry (node_type, op, id, namespace) as documented in C4 §9.6.

    Note: the test inserts into ``kg_nodes`` using the base columns present in
    every schema version.  Columns added by later D365 migrations (metadata,
    d365_source_id) are intentionally omitted to keep this test schema-agnostic.
    """
    label = f"test-account-{uuid.uuid4().hex[:8]}"
    entity_type = "account"

    async with pg_pool.acquire(timeout=10.0) as conn:
        async with conn.transaction():
            # Upsert the kg_node (base columns, schema-agnostic).
            await conn.execute(
                """
                INSERT INTO kg_nodes (label, entity_type, namespace_id)
                VALUES ($1, $2, $3::uuid)
                ON CONFLICT (label, namespace_id) DO UPDATE
                    SET entity_type = EXCLUDED.entity_type,
                        updated_at = NOW()
                """,
                label,
                entity_type,
                str(namespace_id),
            )
            # Post-commit emit — same call the engine makes.
            await emit_graph_write(
                conn,
                namespace_id=namespace_id,
                node_type=entity_type,
                op="upserted",
                node_id=label,
            )

    # The transaction committed; the outbox row must be present.
    async with pg_pool.acquire(timeout=10.0) as conn:
        rows = await conn.fetch(
            """
            SELECT id, namespace_id, aggregate_type, aggregate_id, event_type, payload
            FROM outbox_events
            WHERE aggregate_id = $1
              AND namespace_id = $2
            ORDER BY created_at
            """,
            label,
            namespace_id,
        )

    assert len(rows) == 1, (
        f"Expected exactly 1 outbox_events row for aggregate_id={label!r}, got {len(rows)}"
    )

    row = rows[0]
    assert row["namespace_id"] == namespace_id
    assert row["aggregate_type"] == entity_type
    assert row["aggregate_id"] == label
    assert row["event_type"] == f"{entity_type}.upserted"

    payload = row["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    assert payload["node_type"] == entity_type
    assert payload["op"] == "upserted"
    assert payload["id"] == label
    assert payload["namespace"] == str(namespace_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_duplicate_delivery_is_deduped_via_processed_outbox_events(pg_pool, namespace_id):
    """Duplicate relay delivery of the same event_id is recorded once.

    Simulates at-least-once delivery: two calls to ``mark_graph_write_processed``
    with the same event_id — the first succeeds (True), the second is a no-op
    (False).  The ``processed_outbox_events`` table contains exactly one row.
    """
    event_id = uuid.uuid4()

    async with pg_pool.acquire(timeout=10.0) as conn:
        first = await mark_graph_write_processed(conn, event_id=event_id, namespace_id=namespace_id)
        second = await mark_graph_write_processed(
            conn, event_id=event_id, namespace_id=namespace_id
        )

    assert first is True, "First delivery must return True (newly recorded)"
    assert second is False, "Duplicate delivery must return False (already seen)"

    async with pg_pool.acquire(timeout=10.0) as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM processed_outbox_events WHERE event_id = $1",
            event_id,
        )
    assert count == 1, (
        f"processed_outbox_events must contain exactly one row for event_id={event_id}, got {count}"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_emit_graph_write_lands_in_outbox_events(pg_pool, namespace_id):
    """emit_graph_write inserts a correctly shaped row into outbox_events.

    Direct call to emit_graph_write (not via the sync engine) — verifies the
    helper independently of DataverseSyncEngine.
    """
    node_id = f"direct-test-{uuid.uuid4().hex[:8]}"
    node_type = "contact"

    async with pg_pool.acquire(timeout=10.0) as conn:
        async with conn.transaction():
            await emit_graph_write(
                conn,
                namespace_id=namespace_id,
                node_type=node_type,
                op="upserted",
                node_id=node_id,
            )

    async with pg_pool.acquire(timeout=10.0) as conn:
        row = await conn.fetchrow(
            """
            SELECT namespace_id, aggregate_type, aggregate_id, event_type, payload
            FROM outbox_events
            WHERE aggregate_id = $1 AND namespace_id = $2
            """,
            node_id,
            namespace_id,
        )

    assert row is not None, "emit_graph_write must insert a row into outbox_events"
    assert row["namespace_id"] == namespace_id
    assert row["aggregate_type"] == node_type
    assert row["event_type"] == f"{node_type}.upserted"

    payload = row["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    assert payload["node_type"] == node_type
    assert payload["op"] == "upserted"
    assert payload["id"] == node_id
    assert payload["namespace"] == str(namespace_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_emit_status_change_lands_in_outbox_events_with_project_and_status(
    pg_pool, namespace_id
):
    """emit_status_change inserts a row whose payload carries project_id + status.

    Same DB round-trip shape as ``test_emit_graph_write_lands_in_outbox_events``,
    but for the new typed helper: proves the extra two keys survive the
    ``publish`` → JSONB → read-back round trip, not just an in-memory dict
    build.
    """
    node_id = f"direct-status-test-{uuid.uuid4().hex[:8]}"
    node_type = "BOM_LINE"
    project_id = f"PROJECT:{uuid.uuid4().hex[:8].upper()}"
    status = "DELIVERED"

    async with pg_pool.acquire(timeout=10.0) as conn:
        async with conn.transaction():
            await emit_status_change(
                conn,
                namespace_id=namespace_id,
                node_type=node_type,
                op="status_changed",
                node_id=node_id,
                project_id=project_id,
                status=status,
            )

    async with pg_pool.acquire(timeout=10.0) as conn:
        row = await conn.fetchrow(
            """
            SELECT namespace_id, aggregate_type, aggregate_id, event_type, payload
            FROM outbox_events
            WHERE aggregate_id = $1 AND namespace_id = $2
            """,
            node_id,
            namespace_id,
        )

    assert row is not None, "emit_status_change must insert a row into outbox_events"
    assert row["namespace_id"] == namespace_id
    assert row["aggregate_type"] == node_type
    assert row["event_type"] == f"{node_type}.status_changed"

    payload = row["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    assert payload == {
        "node_type": node_type,
        "op": "status_changed",
        "id": node_id,
        "namespace": str(namespace_id),
        "project_id": project_id,
        "status": status,
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_kg_upsert_and_emit_are_atomic(pg_pool, namespace_id):
    """When emit_graph_write raises, the kg_nodes row is also absent (full rollback).

    This asserts transactional-outbox semantics: the kg_nodes write and the
    outbox INSERT are in the *same* transaction.  If the emit step fails (simulated
    by patching ``publish`` to raise), the transaction aborts and *neither* the
    kg_node nor the outbox row must be visible afterwards.

    A best-effort / swallowed-exception design would let the kg_node commit
    while the outbox row is missing — that would be the bug this test catches.
    """
    label = f"atomic-test-{uuid.uuid4().hex[:8]}"
    entity_type = "account"

    with patch(
        "nce.events.emit.publish",
        new_callable=AsyncMock,
        side_effect=RuntimeError("simulated outbox failure"),
    ):
        with pytest.raises(RuntimeError, match="simulated outbox failure"):
            async with pg_pool.acquire(timeout=10.0) as conn:
                async with conn.transaction():
                    await conn.execute(
                        """
                        INSERT INTO kg_nodes (label, entity_type, namespace_id)
                        VALUES ($1, $2, $3::uuid)
                        ON CONFLICT (label, namespace_id) DO UPDATE
                            SET entity_type = EXCLUDED.entity_type,
                                updated_at = NOW()
                        """,
                        label,
                        entity_type,
                        str(namespace_id),
                    )
                    # This raises — the whole transaction must roll back.
                    await emit_graph_write(
                        conn,
                        namespace_id=namespace_id,
                        node_type=entity_type,
                        op="upserted",
                        node_id=label,
                    )

    # Neither the kg_node nor an outbox row should be present after the rollback.
    async with pg_pool.acquire(timeout=10.0) as conn:
        kg_count = await conn.fetchval(
            "SELECT COUNT(*) FROM kg_nodes WHERE label = $1 AND namespace_id = $2",
            label,
            namespace_id,
        )
        outbox_count = await conn.fetchval(
            "SELECT COUNT(*) FROM outbox_events WHERE aggregate_id = $1 AND namespace_id = $2",
            label,
            namespace_id,
        )

    assert kg_count == 0, (
        f"kg_nodes row must NOT exist after emit failure (got {kg_count}). "
        "This indicates the emit was swallowed and the transaction poisoned silently."
    )
    assert outbox_count == 0, (
        f"outbox_events row must NOT exist after rollback (got {outbox_count})."
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_kg_upsert_rolls_back_on_real_pg_error_in_outbox_insert(pg_pool, namespace_id):
    """A *real* Postgres error on the outbox INSERT rolls back the kg_nodes write.

    Unlike ``test_kg_upsert_and_emit_are_atomic`` (which mocks ``publish`` to
    raise a Python ``RuntimeError``), this test drives the **fully unpatched**
    ``emit_graph_write`` → ``publish`` path and forces Postgres itself to abort
    the transaction.

    Failure injection: ``outbox_events.namespace_id`` is ``UUID NOT NULL``.  We
    write a valid kg_nodes row first, then call ``emit_graph_write`` with
    ``namespace_id=None``.  ``publish`` binds ``None`` to the NOT NULL
    ``namespace_id`` column, so Postgres raises ``NotNullViolationError`` *inside*
    the open transaction.  Because the kg-write and the outbox INSERT share one
    transaction, the kg_nodes row must roll back with it — proving atomicity
    under a genuine PG constraint failure, not just a mocked exception.
    """
    label = f"pgfail-test-{uuid.uuid4().hex[:8]}"
    entity_type = "account"

    with pytest.raises(asyncpg.NotNullViolationError):
        async with pg_pool.acquire(timeout=10.0) as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO kg_nodes (label, entity_type, namespace_id)
                    VALUES ($1, $2, $3::uuid)
                    ON CONFLICT (label, namespace_id) DO UPDATE
                        SET entity_type = EXCLUDED.entity_type,
                            updated_at = NOW()
                    """,
                    label,
                    entity_type,
                    str(namespace_id),
                )
                # Real PG failure: NULL into outbox_events.namespace_id (NOT NULL).
                # No mock — Postgres raises NotNullViolationError and aborts the txn.
                await emit_graph_write(
                    conn,
                    namespace_id=None,
                    node_type=entity_type,
                    op="upserted",
                    node_id=label,
                )

    # The whole transaction aborted at the PG level; neither row may survive.
    async with pg_pool.acquire(timeout=10.0) as conn:
        kg_count = await conn.fetchval(
            "SELECT COUNT(*) FROM kg_nodes WHERE label = $1 AND namespace_id = $2",
            label,
            namespace_id,
        )
        outbox_count = await conn.fetchval(
            "SELECT COUNT(*) FROM outbox_events WHERE aggregate_id = $1 AND namespace_id = $2",
            label,
            namespace_id,
        )

    assert kg_count == 0, (
        f"kg_nodes row must roll back when the outbox INSERT raises a real PG error "
        f"(got {kg_count}). A non-atomic emit would leave this row committed."
    )
    assert outbox_count == 0, (
        f"outbox_events must contain no row after the NOT NULL violation (got {outbox_count})."
    )
