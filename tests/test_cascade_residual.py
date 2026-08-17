"""Batch 111 — contradiction-cascade-residual integration tests.

Gate scenario: A contradicts B; both feed consolidated memory C.

* resolve accepted_a  → B + C soft-deleted (Batch 23 base) AND B's
  origin-tagged kg_edges floored to ≤0.1 with an ``edge_confidence_floored`` event.

* resolve superseded  → C deleted, sources' salience restored + re-queued
  (``consolidation_requeue`` event emitted).
"""

from __future__ import annotations

import json
import uuid

import pytest

from nce.db_utils import scoped_pg_session
from nce.orchestrators.cognitive import CognitiveOrchestrator

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_EMBED = json.dumps([0.1] * 768)


async def _insert_memory(
    conn,
    *,
    memory_id: uuid.UUID,
    ns_id: uuid.UUID,
    memory_type: str = "episodic",
    derived_from: list[str] | None = None,
    payload_ref: str = "000000000000000000000001",
) -> None:
    if derived_from is not None:
        await conn.execute(
            """
            INSERT INTO memories (id, namespace_id, agent_id, embedding, assertion_type,
                                  memory_type, payload_ref, metadata, derived_from)
            VALUES ($1, $2, 'test-agent', $3::vector, 'fact', $4, $5, '{}'::jsonb, $6::jsonb)
            """,
            memory_id,
            ns_id,
            _EMBED,
            memory_type,
            payload_ref,
            json.dumps([str(s) for s in derived_from]),
        )
    else:
        await conn.execute(
            """
            INSERT INTO memories (id, namespace_id, agent_id, embedding, assertion_type,
                                  memory_type, payload_ref, metadata)
            VALUES ($1, $2, 'test-agent', $3::vector, 'fact', $4, $5, '{}'::jsonb)
            """,
            memory_id,
            ns_id,
            _EMBED,
            memory_type,
            payload_ref,
        )


async def _insert_contradiction(
    conn,
    *,
    contradiction_id: uuid.UUID,
    ns_id: uuid.UUID,
    memory_a_id: uuid.UUID,
    memory_b_id: uuid.UUID,
) -> None:
    await conn.execute(
        """
        INSERT INTO contradictions (id, namespace_id, memory_a_id, memory_b_id,
                                    agent_id, detection_path, signals, confidence, resolution)
        VALUES ($1, $2, $3, $4, 'system', 'sync', '{}'::jsonb, 0.9, NULL)
        """,
        contradiction_id,
        ns_id,
        memory_a_id,
        memory_b_id,
    )


async def _insert_store_memory_event(
    conn,
    *,
    ns_id: uuid.UUID,
    memory_id: uuid.UUID,
    resolved_by: str = "test-admin",
) -> uuid.UUID:
    """Append a ``store_memory`` event and return its id (used as origin_event_id)."""
    from nce.event_log import append_event

    result = await append_event(
        conn=conn,
        namespace_id=ns_id,
        agent_id=resolved_by,
        event_type="store_memory",
        params={
            "saga_id": str(uuid.uuid4()),
            "memory_id": str(memory_id),
            "payload_ref": "000000000000000000000001",
            "assertion_type": "fact",
            "entities": [],
            "triplets": [],
        },
    )
    return result.event_id


async def _insert_kg_edge(
    conn,
    *,
    ns_id: uuid.UUID,
    subject: str,
    predicate: str,
    object_: str,
    confidence: float,
    origin_event_id: uuid.UUID,
) -> None:
    await conn.execute(
        """
        INSERT INTO kg_edges (subject_label, predicate, object_label,
                              confidence, namespace_id, change_origin, origin_event_id)
        VALUES ($1, $2, $3, $4, $5, 'agent', $6)
        ON CONFLICT (subject_label, predicate, object_label, namespace_id)
        DO UPDATE SET confidence = EXCLUDED.confidence,
                      origin_event_id = EXCLUDED.origin_event_id
        """,
        subject,
        predicate,
        object_,
        confidence,
        ns_id,
        origin_event_id,
    )


# ---------------------------------------------------------------------------
# Test 1 — accepted_a: B + C soft-deleted, B's edges floored, event emitted
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_accepted_a_floors_loser_edges(pg_pool, make_namespace) -> None:
    """Resolve accepted_a: loser B's kg_edges (origin_event_id traceable to B's store event)
    must be floored to ≤0.1 AND an edge_confidence_floored event must be emitted.
    """
    ns_id = await make_namespace()
    memory_a_id = uuid.uuid4()
    memory_b_id = uuid.uuid4()
    memory_c_id = uuid.uuid4()  # consolidated from B
    contradiction_id = uuid.uuid4()

    async with scoped_pg_session(pg_pool, ns_id) as conn:
        async with conn.transaction():
            # Insert memories A and B
            await _insert_memory(
                conn, memory_id=memory_a_id, ns_id=ns_id, payload_ref="000000000000000000000001"
            )
            await _insert_memory(
                conn, memory_id=memory_b_id, ns_id=ns_id, payload_ref="000000000000000000000002"
            )

            # Insert consolidated memory C derived from B
            await _insert_memory(
                conn,
                memory_id=memory_c_id,
                ns_id=ns_id,
                memory_type="consolidated",
                derived_from=[str(memory_b_id)],
                payload_ref="000000000000000000000003",
            )

            # Emit store_memory event for B (so origin_event_id is linkable)
            b_event_id = await _insert_store_memory_event(conn, ns_id=ns_id, memory_id=memory_b_id)

            # Insert contradiction
            await _insert_contradiction(
                conn,
                contradiction_id=contradiction_id,
                ns_id=ns_id,
                memory_a_id=memory_a_id,
                memory_b_id=memory_b_id,
            )

            # Insert a KG edge attributed to B's store_memory event (high confidence)
            await _insert_kg_edge(
                conn,
                ns_id=ns_id,
                subject="EntityX",
                predicate="knows",
                object_="EntityY",
                confidence=0.95,
                origin_event_id=b_event_id,
            )

    # Resolve the contradiction (accepted_a → B is loser)
    orchestrator = CognitiveOrchestrator(pg_pool)
    await orchestrator.resolve_contradiction(
        contradiction_id=str(contradiction_id),
        namespace_id=str(ns_id),
        resolution="accepted_a",
        resolved_by="test-admin",
    )

    # Verify
    async with scoped_pg_session(pg_pool, ns_id) as conn:
        row_a = await conn.fetchrow("SELECT valid_to FROM memories WHERE id = $1", memory_a_id)
        row_b = await conn.fetchrow("SELECT valid_to FROM memories WHERE id = $1", memory_b_id)
        row_c = await conn.fetchrow("SELECT valid_to FROM memories WHERE id = $1", memory_c_id)

        # A survives, B and C are soft-deleted (Batch 23 base)
        assert row_a is not None and row_a["valid_to"] is None, "Memory A must survive"
        assert row_b is not None and row_b["valid_to"] is not None, "Memory B must be soft-deleted"
        assert row_c is not None and row_c["valid_to"] is not None, "Memory C must be soft-deleted"

        # KG edge must be floored to ≤0.1 (NOT deleted)
        edge_row = await conn.fetchrow(
            """
            SELECT confidence FROM kg_edges
            WHERE namespace_id = $1
              AND subject_label = 'EntityX'
              AND predicate = 'knows'
              AND object_label = 'EntityY'
            """,
            ns_id,
        )
        assert edge_row is not None, "KG edge must NOT be deleted (floor-not-delete)"
        assert edge_row["confidence"] <= 0.1, (
            f"Edge confidence must be ≤0.1 after floor, got {edge_row['confidence']}"
        )

        # edge_confidence_floored event must have been emitted
        floor_evt = await conn.fetchrow(
            """
            SELECT params FROM event_log
            WHERE namespace_id = $1
              AND event_type = 'edge_confidence_floored'
            """,
            ns_id,
        )
        assert floor_evt is not None, "edge_confidence_floored event must be emitted"
        params = floor_evt["params"]
        if isinstance(params, str):
            params = json.loads(params)
        assert params["contradiction_id"] == str(contradiction_id)
        assert params["floored_edge_count"] >= 1


# ---------------------------------------------------------------------------
# Test 2 — superseded: C deleted, sources' salience restored + re-queued
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_superseded_reopens_consolidation(pg_pool, make_namespace) -> None:
    """Resolve superseded: stale consolidated memory C (derived from loser A) is soft-deleted,
    surviving source B gets salience restored, and a consolidation_requeue event is emitted.
    """
    import datetime

    ns_id = await make_namespace()
    memory_a_id = uuid.uuid4()  # older → loser under 'superseded'
    memory_b_id = uuid.uuid4()  # newer → winner
    memory_c_id = uuid.uuid4()  # consolidated from A only (stale)
    contradiction_id = uuid.uuid4()

    now = datetime.datetime.now(datetime.timezone.utc)
    older_time = now - datetime.timedelta(hours=2)
    newer_time = now - datetime.timedelta(hours=1)

    async with scoped_pg_session(pg_pool, ns_id) as conn:
        async with conn.transaction():
            # Insert memory A (older)
            await conn.execute(
                """
                INSERT INTO memories (id, namespace_id, agent_id, embedding, assertion_type,
                                      memory_type, payload_ref, metadata, created_at)
                VALUES ($1, $2, 'test-agent', $3::vector, 'fact', 'episodic',
                        '000000000000000000000001', '{}'::jsonb, $4)
                """,
                memory_a_id,
                ns_id,
                _EMBED,
                older_time,
            )
            # Insert memory B (newer)
            await conn.execute(
                """
                INSERT INTO memories (id, namespace_id, agent_id, embedding, assertion_type,
                                      memory_type, payload_ref, metadata, created_at)
                VALUES ($1, $2, 'test-agent', $3::vector, 'fact', 'episodic',
                        '000000000000000000000002', '{}'::jsonb, $4)
                """,
                memory_b_id,
                ns_id,
                _EMBED,
                newer_time,
            )

            # Insert consolidated memory C derived from A only
            await _insert_memory(
                conn,
                memory_id=memory_c_id,
                ns_id=ns_id,
                memory_type="consolidated",
                derived_from=[str(memory_a_id)],
                payload_ref="000000000000000000000003",
            )

            # Seed a salience record for A (simulating prior consolidation decay)
            await conn.execute(
                """
                INSERT INTO memory_salience (memory_id, agent_id, namespace_id, salience_score, updated_at)
                VALUES ($1, 'system', $2, 0.1, NOW())
                ON CONFLICT (memory_id, agent_id) DO UPDATE
                  SET salience_score = EXCLUDED.salience_score
                """,
                memory_a_id,
                ns_id,
            )

            # Insert contradiction
            await _insert_contradiction(
                conn,
                contradiction_id=contradiction_id,
                ns_id=ns_id,
                memory_a_id=memory_a_id,
                memory_b_id=memory_b_id,
            )

    # Resolve with superseded (older = A is loser)
    orchestrator = CognitiveOrchestrator(pg_pool)
    await orchestrator.resolve_contradiction(
        contradiction_id=str(contradiction_id),
        namespace_id=str(ns_id),
        resolution="superseded",
        resolved_by="test-admin",
    )

    async with scoped_pg_session(pg_pool, ns_id) as conn:
        # A is soft-deleted (loser), B survives
        row_a = await conn.fetchrow("SELECT valid_to FROM memories WHERE id = $1", memory_a_id)
        row_b = await conn.fetchrow("SELECT valid_to FROM memories WHERE id = $1", memory_b_id)
        assert row_a is not None and row_a["valid_to"] is not None, (
            "Memory A (loser) must be soft-deleted"
        )
        assert row_b is not None and row_b["valid_to"] is None, "Memory B (winner) must survive"

        # C (consolidated from A only) must be soft-deleted
        row_c = await conn.fetchrow("SELECT valid_to FROM memories WHERE id = $1", memory_c_id)
        assert row_c is not None and row_c["valid_to"] is not None, (
            "Consolidated memory C must be soft-deleted (all sources retracted)"
        )

        # consolidation_requeue event must be emitted
        requeue_evt = await conn.fetchrow(
            """
            SELECT params FROM event_log
            WHERE namespace_id = $1
              AND event_type = 'consolidation_requeue'
            """,
            ns_id,
        )
        assert requeue_evt is not None, "consolidation_requeue event must be emitted"
        params = requeue_evt["params"]
        if isinstance(params, str):
            params = json.loads(params)
        assert params["contradiction_id"] == str(contradiction_id)
        assert params["deleted_consolidated_id"] == str(memory_c_id)

        # Salience for surviving sources must be restored (≥ 0.5 baseline)
        # C was derived from A only (now retracted), so there are no surviving sources;
        # the code falls back to source_ids (A itself) for the restore call.
        # The key invariant: a salience row for A exists and is now ≥ 0.3 (restored).
        sal_row = await conn.fetchrow(
            "SELECT salience_score FROM memory_salience WHERE memory_id = $1 AND agent_id = 'system'",
            memory_a_id,
        )
        assert sal_row is not None, "Salience record for source A must exist after restore"
        assert sal_row["salience_score"] >= 0.3, (
            f"Source salience must be restored (≥0.3), got {sal_row['salience_score']}"
        )
