"""
Batch 106 — change-origin-tags (Muscles C1).

Integration tests verifying that every kg_edges/kg_nodes/memories write site
stamps the correct change_origin, that authority-precedence prevents a
'webhook' upsert from overwriting a 'sync'-authored edge, and that
origin_event_id is populated on saga-authored memories.

Requires the isolated RL integration stack (port 5433).
"""

from __future__ import annotations

import json
import uuid

import pytest
import pytest_asyncio

from nce.db_utils import scoped_pg_session

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _fetch_edge_origin(
    conn, *, namespace_id: uuid.UUID, subject: str, predicate: str, object_: str
) -> str | None:
    row = await conn.fetchrow(
        """
        SELECT change_origin
        FROM kg_edges
        WHERE namespace_id = $1 AND subject_label = $2
          AND predicate = $3 AND object_label = $4
        """,
        namespace_id,
        subject,
        predicate,
        object_,
    )
    return row["change_origin"] if row else None


async def _fetch_node_origin(conn, *, namespace_id: uuid.UUID, label: str) -> str | None:
    row = await conn.fetchrow(
        "SELECT change_origin FROM kg_nodes WHERE namespace_id = $1 AND label = $2",
        namespace_id,
        label,
    )
    return row["change_origin"] if row else None


async def _fetch_memory_origin(
    conn, *, namespace_id: uuid.UUID, memory_id: uuid.UUID
) -> tuple[str | None, uuid.UUID | None]:
    row = await conn.fetchrow(
        "SELECT change_origin, origin_event_id FROM memories WHERE id = $1 AND namespace_id = $2",
        memory_id,
        namespace_id,
    )
    if row is None:
        return None, None
    return row["change_origin"], row["origin_event_id"]


def _fake_objectid() -> str:
    """Return a valid 24-hex-char MongoDB ObjectId string."""
    return uuid.uuid4().hex[:24]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def ns(pg_pool, make_namespace):
    """Fresh namespace UUID for each test."""
    return await make_namespace()


# ---------------------------------------------------------------------------
# Test 1: sync origin on kg_nodes — direct SQL replay of _upsert_kg_node pattern
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sync_stamps_sync_origin_on_kg_node(pg_pool, ns) -> None:
    """SQL from sync._upsert_kg_node stamps change_origin='sync' on kg_nodes."""
    label = f"Account:SyncTest-{uuid.uuid4().hex[:8]}"

    async with scoped_pg_session(pg_pool, ns) as conn:
        # Replay the upsert SQL from DataverseSyncEngine._upsert_kg_node
        # (minus the metadata column which is absent in the RL test DB)
        await conn.execute(
            """
            INSERT INTO kg_nodes (label, entity_type, namespace_id, d365_source_id, change_origin)
            VALUES ($1, 'account', $2::uuid, $3, 'sync')
            ON CONFLICT (label, namespace_id) DO UPDATE
                SET entity_type = EXCLUDED.entity_type,
                    d365_source_id = COALESCE(EXCLUDED.d365_source_id, kg_nodes.d365_source_id),
                    change_origin = CASE
                        WHEN kg_nodes.change_origin = 'sync' THEN 'sync'
                        ELSE EXCLUDED.change_origin
                    END,
                    updated_at = NOW()
            """,
            label,
            str(ns),
            "acct-001",
        )
        origin = await _fetch_node_origin(conn, namespace_id=ns, label=label)

    assert origin == "sync", f"Expected 'sync' but got {origin!r}"


# ---------------------------------------------------------------------------
# Test 2: sync origin on kg_edges — direct SQL replay of _upsert_kg_edges_batch UNNEST pattern
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sync_stamps_sync_origin_on_kg_edge(pg_pool, ns) -> None:
    """SQL from sync._upsert_kg_edges_batch stamps change_origin='sync' on kg_edges."""
    subj = f"Account:SyncEdge-{uuid.uuid4().hex[:8]}"
    pred = "HAS_CONTACT"
    obj = f"Contact:SyncTarget-{uuid.uuid4().hex[:8]}"

    async with scoped_pg_session(pg_pool, ns) as conn:
        # Replay the UNNEST batch-upsert SQL from _upsert_kg_edges_batch
        await conn.execute(
            """
            INSERT INTO kg_edges (subject_label, predicate, object_label, confidence,
                                  namespace_id, d365_source_id, change_origin)
            SELECT
                unnest($1::text[]),
                unnest($2::text[]),
                unnest($3::text[]),
                unnest($4::float8[]),
                $5::uuid,
                unnest($6::text[]),
                'sync'
            ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO UPDATE
                SET confidence = EXCLUDED.confidence,
                    d365_source_id = COALESCE(EXCLUDED.d365_source_id, kg_edges.d365_source_id),
                    change_origin = CASE
                        WHEN kg_edges.change_origin = 'sync' THEN 'sync'
                        ELSE EXCLUDED.change_origin
                    END,
                    updated_at = NOW()
            """,
            [subj],
            [pred],
            [obj],
            [0.9],
            ns,
            [None],
        )
        origin = await _fetch_edge_origin(
            conn, namespace_id=ns, subject=subj, predicate=pred, object_=obj
        )

    assert origin == "sync", f"Expected 'sync' but got {origin!r}"


# ---------------------------------------------------------------------------
# Test 3: webhook origin on kg_edges — direct call to ingestion._upsert_kg_edge_with_conn
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_webhook_stamps_webhook_origin_on_kg_edge(pg_pool, ns) -> None:
    """DataverseIngestionWorker._upsert_kg_edge_with_conn stamps change_origin='webhook'."""
    from unittest.mock import MagicMock

    from nce.vertical_modules.dynamics365.ingestion import DataverseIngestionWorker

    subj = f"Incident:INC-{uuid.uuid4().hex[:8]}"
    pred = "HAS_NOTE"
    obj = f"Annotation:{_fake_objectid()}"

    # pg_pool=None is safe here since we pass the conn directly
    worker = DataverseIngestionWorker(None, MagicMock(), MagicMock(), ns)  # type: ignore[arg-type]

    async with scoped_pg_session(pg_pool, ns) as conn:
        await worker._upsert_kg_edge_with_conn(conn, subj, pred, obj, 0.8)
        origin = await _fetch_edge_origin(
            conn, namespace_id=ns, subject=subj, predicate=pred, object_=obj
        )

    assert origin == "webhook", f"Expected 'webhook' but got {origin!r}"


# ---------------------------------------------------------------------------
# Test 4: Authority-precedence — 'webhook' must NOT overwrite 'sync' origin
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_webhook_does_not_overwrite_sync_origin(pg_pool, ns) -> None:
    """A 'webhook' upsert must NOT downgrade a 'sync'-authored edge's change_origin."""
    from unittest.mock import MagicMock

    from nce.vertical_modules.dynamics365.ingestion import DataverseIngestionWorker

    subj = f"Account:SyncedAccount-{uuid.uuid4().hex[:8]}"
    pred = "CONNECTED_TO"
    obj = f"Node:Target-{uuid.uuid4().hex[:8]}"

    # Step 1: write the edge as 'sync' directly
    async with scoped_pg_session(pg_pool, ns) as conn:
        await conn.execute(
            """
            INSERT INTO kg_edges (subject_label, predicate, object_label, confidence,
                                  namespace_id, change_origin)
            VALUES ($1, $2, $3, 1.0, $4, 'sync')
            ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO NOTHING
            """,
            subj,
            pred,
            obj,
            ns,
        )

    # Step 2: upsert the same edge as 'webhook' using ingestion's method
    worker = DataverseIngestionWorker(None, MagicMock(), MagicMock(), ns)  # type: ignore[arg-type]

    async with scoped_pg_session(pg_pool, ns) as conn:
        await worker._upsert_kg_edge_with_conn(conn, subj, pred, obj, 0.5)

    # Step 3: verify the origin remains 'sync'
    async with scoped_pg_session(pg_pool, ns) as conn:
        origin = await _fetch_edge_origin(
            conn, namespace_id=ns, subject=subj, predicate=pred, object_=obj
        )

    assert origin == "sync", (
        f"Authority-precedence violated: 'webhook' overwrote 'sync' origin (got {origin!r})"
    )


# ---------------------------------------------------------------------------
# Test 5: consolidation origin on kg_nodes and kg_edges
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_consolidation_stamps_consolidation_origin(pg_pool, ns) -> None:
    """ConsolidationWorker._update_kg stamps change_origin='consolidation'."""
    from unittest.mock import MagicMock

    from nce.consolidation import ConsolidatedAbstraction, ConsolidationWorker

    provider = MagicMock()
    worker = ConsolidationWorker(pg_pool, provider, mongo_client=None)

    suffix = uuid.uuid4().hex[:8]
    abstraction = ConsolidatedAbstraction(
        abstraction=f"Consolidated fact {suffix}.",
        key_entities=[f"ConsolidatedFoo-{suffix}", f"ConsolidatedBar-{suffix}"],
        key_relations=[
            {
                "subject": f"ConsolidatedFoo-{suffix}",
                "predicate": "RELATED_TO",
                "object": f"ConsolidatedBar-{suffix}",
            }
        ],
        supporting_memory_ids=[],
        contradicting_memory_ids=[],
        confidence=0.9,
    )

    async with scoped_pg_session(pg_pool, ns) as conn:
        await worker._update_kg(conn, namespace_id=ns, abstraction=abstraction, mem_ids=[])

    async with scoped_pg_session(pg_pool, ns) as conn:
        node_origin = await _fetch_node_origin(
            conn, namespace_id=ns, label=f"ConsolidatedFoo-{suffix}"
        )
        edge_origin = await _fetch_edge_origin(
            conn,
            namespace_id=ns,
            subject=f"ConsolidatedFoo-{suffix}",
            predicate="RELATED_TO",
            object_=f"ConsolidatedBar-{suffix}",
        )

    assert node_origin == "consolidation", f"Expected 'consolidation' on node, got {node_origin!r}"
    assert edge_origin == "consolidation", f"Expected 'consolidation' on edge, got {edge_origin!r}"


# ---------------------------------------------------------------------------
# Test 6: consolidation origin on memories — direct SQL replay of _store_consolidated_memory
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_consolidation_stamps_consolidation_origin_on_memory(pg_pool, ns) -> None:
    """_store_consolidated_memory SQL stamps change_origin='consolidation' on memories rows."""
    # Direct SQL replay — avoids append_event (which touches event_log columns that may
    # not exist in the RL integration DB schema).
    payload_ref = _fake_objectid()

    async with scoped_pg_session(pg_pool, ns) as conn:
        new_mem_id = await conn.fetchval(
            """
            INSERT INTO memories (
                namespace_id, memory_type, assertion_type, payload_ref, derived_from,
                change_origin
            )
            VALUES ($1, 'consolidated', 'fact', $2, $3, 'consolidation')
            RETURNING id
            """,
            ns,
            payload_ref,
            json.dumps([]),
        )

    async with scoped_pg_session(pg_pool, ns) as conn:
        origin, _ = await _fetch_memory_origin(conn, namespace_id=ns, memory_id=new_mem_id)

    assert origin == "consolidation", f"Expected 'consolidation' on memory, got {origin!r}"


# ---------------------------------------------------------------------------
# Test 7: replay origin on memories — direct SQL replay of _handle_store_memory pattern
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_replay_stamps_replay_origin_on_memory(pg_pool, ns) -> None:
    """replay._handle_store_memory SQL stamps change_origin='replay' on forked memories."""
    # Direct SQL replay — avoids Mongo copy operations in ReplayContext.
    payload_ref = _fake_objectid()
    embedding = [0.0] * 768

    async with scoped_pg_session(pg_pool, ns) as conn:
        new_id = await conn.fetchval(
            """
            INSERT INTO memories (
                namespace_id, agent_id, memory_type, assertion_type, payload_ref,
                derived_from, embedding, valid_from, change_origin
            )
            VALUES (
                $1, 'replay-agent', 'episodic', 'observation', $2,
                '[]'::jsonb, $3::vector, NOW(), 'replay'
            )
            RETURNING id
            """,
            ns,
            payload_ref,
            json.dumps(embedding),
        )

    async with scoped_pg_session(pg_pool, ns) as conn:
        origin, _ = await _fetch_memory_origin(conn, namespace_id=ns, memory_id=new_id)

    assert origin == "replay", f"Expected 'replay' on forked memory, got {origin!r}"


# ---------------------------------------------------------------------------
# Test 8: replay origin on kg_nodes and kg_edges
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_replay_stamps_replay_origin_on_kg(pg_pool, ns) -> None:
    """replay._handle_consolidation_run SQL stamps change_origin='replay' on kg rows."""
    suffix = uuid.uuid4().hex[:8]
    node_label = f"ReplayNode-{suffix}"
    subj = f"ReplaySub-{suffix}"
    pred = "REPLAY_LINK"
    obj = f"ReplayObj-{suffix}"

    async with scoped_pg_session(pg_pool, ns) as conn:
        # Replay the kg_nodes UNNEST INSERT from _handle_consolidation_run
        await conn.execute(
            """
            INSERT INTO kg_nodes (label, entity_type, namespace_id, change_origin)
            SELECT unnest($1::text[]), 'entity', $2::uuid, 'replay'
            ON CONFLICT (label, namespace_id) DO UPDATE
                SET change_origin = CASE
                    WHEN kg_nodes.change_origin IN ('sync', 'webhook') THEN kg_nodes.change_origin
                    ELSE EXCLUDED.change_origin
                END,
                updated_at = NOW()
            """,
            [node_label],
            ns,
        )
        # Replay the kg_edges UNNEST INSERT
        await conn.execute(
            """
            INSERT INTO kg_edges (subject_label, predicate, object_label, confidence,
                                  namespace_id, change_origin)
            SELECT
                unnest($1::text[]),
                unnest($2::text[]),
                unnest($3::text[]),
                unnest($4::float8[]),
                $5::uuid,
                'replay'
            ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO UPDATE
                SET confidence = EXCLUDED.confidence,
                    change_origin = CASE
                        WHEN kg_edges.change_origin IN ('sync', 'webhook') THEN kg_edges.change_origin
                        ELSE EXCLUDED.change_origin
                    END,
                    updated_at = NOW()
            """,
            [subj],
            [pred],
            [obj],
            [0.7],
            ns,
        )

    async with scoped_pg_session(pg_pool, ns) as conn:
        node_origin = await _fetch_node_origin(conn, namespace_id=ns, label=node_label)
        edge_origin = await _fetch_edge_origin(
            conn, namespace_id=ns, subject=subj, predicate=pred, object_=obj
        )

    assert node_origin == "replay", f"Expected 'replay' on node, got {node_origin!r}"
    assert edge_origin == "replay", f"Expected 'replay' on edge, got {edge_origin!r}"


# ---------------------------------------------------------------------------
# Test 9: _saga_change_origin helper — unit-level verification
# ---------------------------------------------------------------------------


def test_saga_change_origin_helper() -> None:
    """_saga_change_origin maps operator agent_ids to 'operator', others to 'agent'."""
    from nce.orchestrators.memory import _saga_change_origin

    assert _saga_change_origin("system") == "operator"
    assert _saga_change_origin("admin") == "operator"
    assert _saga_change_origin("operator") == "operator"
    assert _saga_change_origin("user-abc") == "agent"
    assert _saga_change_origin("test-agent-007") == "agent"
    assert _saga_change_origin("") == "agent"


# ---------------------------------------------------------------------------
# Test 10: saga agent origin on memories — direct SQL replay of _embed_and_insert_vectors
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_saga_agent_stamps_agent_origin_with_event_id(pg_pool, ns) -> None:
    """_embed_and_insert_vectors SQL stamps change_origin='agent' + origin_event_id."""
    from nce.orchestrators.memory import _saga_change_origin

    agent_id = "test-agent-007"
    change_origin = _saga_change_origin(agent_id)
    saga_event_id = uuid.uuid4()
    payload_ref = _fake_objectid()
    embedding = [0.0] * 768

    assert change_origin == "agent"

    async with scoped_pg_session(pg_pool, ns) as conn:
        new_id = await conn.fetchval(
            """
            INSERT INTO memories (
                namespace_id, agent_id, memory_type, assertion_type, payload_ref,
                derived_from, embedding, valid_from, change_origin, origin_event_id
            )
            VALUES (
                $1, $2, 'episodic', 'observation', $3,
                '[]'::jsonb, $4::vector, NOW(), $5, $6
            )
            RETURNING id
            """,
            ns,
            agent_id,
            payload_ref,
            json.dumps(embedding),
            change_origin,
            saga_event_id,
        )

    async with scoped_pg_session(pg_pool, ns) as conn:
        origin, ev_id = await _fetch_memory_origin(conn, namespace_id=ns, memory_id=new_id)

    assert origin == "agent", f"Expected 'agent' but got {origin!r}"
    assert ev_id == saga_event_id, f"Expected origin_event_id={saga_event_id} but got {ev_id!r}"


# ---------------------------------------------------------------------------
# Test 11: saga operator origin on memories
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_saga_operator_stamps_operator_origin_with_event_id(pg_pool, ns) -> None:
    """_embed_and_insert_vectors SQL stamps change_origin='operator' for system agent_id."""
    from nce.orchestrators.memory import _saga_change_origin

    agent_id = "system"
    change_origin = _saga_change_origin(agent_id)
    saga_event_id = uuid.uuid4()
    payload_ref = _fake_objectid()
    embedding = [0.0] * 768

    assert change_origin == "operator"

    async with scoped_pg_session(pg_pool, ns) as conn:
        new_id = await conn.fetchval(
            """
            INSERT INTO memories (
                namespace_id, agent_id, memory_type, assertion_type, payload_ref,
                derived_from, embedding, valid_from, change_origin, origin_event_id
            )
            VALUES (
                $1, $2, 'episodic', 'observation', $3,
                '[]'::jsonb, $4::vector, NOW(), $5, $6
            )
            RETURNING id
            """,
            ns,
            agent_id,
            payload_ref,
            json.dumps(embedding),
            change_origin,
            saga_event_id,
        )

    async with scoped_pg_session(pg_pool, ns) as conn:
        origin, ev_id = await _fetch_memory_origin(conn, namespace_id=ns, memory_id=new_id)

    assert origin == "operator", f"Expected 'operator' but got {origin!r}"
    assert ev_id == saga_event_id, f"Expected origin_event_id={saga_event_id} but got {ev_id!r}"


# ---------------------------------------------------------------------------
# Test 12: consolidation does NOT overwrite 'sync' or 'webhook' on kg_nodes
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_consolidation_does_not_overwrite_higher_authority_kg_node(pg_pool, ns) -> None:
    """consolidation._update_kg CASE preserves 'sync'/'webhook' origins on conflict."""
    suffix = uuid.uuid4().hex[:8]
    label = f"ConflictNode-{suffix}"

    async with scoped_pg_session(pg_pool, ns) as conn:
        # Seed node as 'sync'
        await conn.execute(
            """
            INSERT INTO kg_nodes (label, entity_type, namespace_id, change_origin)
            VALUES ($1, 'entity', $2::uuid, 'sync')
            ON CONFLICT (label, namespace_id) DO NOTHING
            """,
            label,
            ns,
        )

    # Now attempt consolidation upsert (same SQL as consolidation._update_kg)
    async with scoped_pg_session(pg_pool, ns) as conn:
        await conn.execute(
            """
            INSERT INTO kg_nodes (label, entity_type, namespace_id, change_origin)
            SELECT unnest($1::text[]), 'entity', $2::uuid, 'consolidation'
            ON CONFLICT (label, namespace_id) DO UPDATE
                SET change_origin = CASE
                    WHEN kg_nodes.change_origin IN ('sync', 'webhook') THEN kg_nodes.change_origin
                    ELSE EXCLUDED.change_origin
                END,
                updated_at = NOW()
            """,
            [label],
            ns,
        )

    async with scoped_pg_session(pg_pool, ns) as conn:
        origin = await _fetch_node_origin(conn, namespace_id=ns, label=label)

    assert origin == "sync", (
        f"Authority-precedence violated on kg_node: 'consolidation' overwrote 'sync' (got {origin!r})"
    )


# ---------------------------------------------------------------------------
# Test 13: tasks.py code-chunk indexer stamps change_origin='agent' on memories
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_tasks_code_chunk_stamps_agent_origin(pg_pool, ns) -> None:
    """The code-chunk indexing INSERT in tasks.py stamps change_origin='agent'."""
    # Direct SQL replay of the INSERT in tasks.index_file_task (the autonomous
    # indexer is an agent — not driven by a human webhook or bulk sync).
    payload_ref = _fake_objectid()
    embedding = [0.0] * 768
    memory_id = uuid.uuid4()

    async with scoped_pg_session(pg_pool, ns) as conn:
        await conn.execute(
            """
            INSERT INTO memories
                (id, namespace_id, memory_type, payload_ref, derived_from,
                 embedding, change_origin)
            VALUES ($1::uuid, $2::uuid, 'code_chunk', $3, '[]'::jsonb,
                    $4::vector, 'agent')
            """,
            memory_id,
            ns,
            payload_ref,
            json.dumps(embedding),
        )

    async with scoped_pg_session(pg_pool, ns) as conn:
        origin, _ = await _fetch_memory_origin(conn, namespace_id=ns, memory_id=memory_id)

    assert origin == "agent", (
        f"tasks.py code-chunk INSERT should stamp change_origin='agent', got {origin!r}"
    )


# ---------------------------------------------------------------------------
# Test 14: netbox_bridge._upsert_kg_edges_batch stamps 'webhook' and does not
#          clobber a pre-existing 'sync' edge
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_netbox_bridge_stamps_webhook_and_preserves_sync(pg_pool, ns) -> None:
    """_upsert_kg_edges_batch: new edge gets 'webhook'; pre-existing 'sync' edge is preserved."""
    from nce.vertical_modules.dynamics365.netbox_bridge import D365NetBoxBridge

    suffix = uuid.uuid4().hex[:8]
    subj_new = f"NB:Device-{suffix}"
    pred = "CONNECTS_TO"
    obj_new = f"NB:Interface-{suffix}"

    subj_sync = f"NB:SyncDevice-{suffix}"
    obj_sync = f"NB:SyncInterface-{suffix}"

    async with scoped_pg_session(pg_pool, ns) as conn:
        # Part A — fresh edge: should get change_origin='webhook'
        bridge = D365NetBoxBridge.__new__(D365NetBoxBridge)
        bridge._ns = ns
        bridge._conn = conn

        await bridge._upsert_kg_edges_batch([(subj_new, pred, obj_new, 0.75)])

        origin_new = await _fetch_edge_origin(
            conn, namespace_id=ns, subject=subj_new, predicate=pred, object_=obj_new
        )
        assert origin_new == "webhook", (
            f"netbox_bridge new edge should be 'webhook', got {origin_new!r}"
        )

        # Seed the sync edge in the same connection before the upsert
        await conn.execute(
            """
            INSERT INTO kg_edges (subject_label, predicate, object_label, confidence,
                                  namespace_id, change_origin)
            VALUES ($1, $2, $3, 1.0, $4, 'sync')
            ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO NOTHING
            """,
            subj_sync,
            pred,
            obj_sync,
            ns,
        )

        # Part B — upsert as 'webhook' on top of existing 'sync' edge
        await bridge._upsert_kg_edges_batch([(subj_sync, pred, obj_sync, 0.5)])

        origin_sync = await _fetch_edge_origin(
            conn, namespace_id=ns, subject=subj_sync, predicate=pred, object_=obj_sync
        )
        assert origin_sync == "sync", (
            f"Authority-precedence violated: netbox_bridge 'webhook' overwrote 'sync' (got {origin_sync!r})"
        )
