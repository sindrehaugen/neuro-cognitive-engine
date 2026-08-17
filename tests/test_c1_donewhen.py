"""C1 done-when integration proof.

Asserts three invariants that define C1 as complete end-to-end:

  1. Sub-threshold match → queue, never auto-merge
     A score below the human-review threshold lands in ``entity_merge_queue``
     with status ``'pending'``.  No ``kg_nodes`` or ``kg_edges`` row is
     mutated.

  2. Survivorship provenance is auditable in the ledger
     After ``survive()`` picks a winner, ``append_survivorship_provenance()``
     appends a row to ``v3_cognitive_ledger``.  The provenance payload is
     query-accessible (``event='field_survivorship'``, correct field_name,
     winning_source and reason).

  3. Cross-engine write to a non-owned node type is refused
     ``assert_owner()`` raises ``OwnershipError`` when the writing engine is
     not the registered owner.  The registered owner passes without error.

All tests are ``@pytest.mark.integration`` (require a live Postgres DB via
``pg_pool`` + ``make_namespace`` fixtures from conftest.py).
"""

from __future__ import annotations

import json
from uuid import UUID, uuid4

import pytest

from nce.auth import set_namespace_context
from nce.db_utils import scoped_pg_session
from nce.entity_resolution.merge_queue import enqueue, list_pending
from nce.entity_resolution.ownership import OwnershipError, assert_owner
from nce.entity_resolution.survivorship import (
    REASON_SOURCE_TRUST,
    append_survivorship_provenance,
    survive,
)

# ---------------------------------------------------------------------------
# Named constants — no magic numbers in test bodies
# ---------------------------------------------------------------------------

# A score that is deliberately below any auto-merge threshold: human review
# is required.  pg_trgm scores are in [0, 1]; 0.55 is well below a typical
# 0.85 auto-merge cut-off.
_SUB_THRESHOLD_SCORE: float = 0.55

# Engine identifiers used across all three done-when blocks.
_OWNER_ENGINE: str = "engine-netops"
_NON_OWNER_ENGINE: str = "engine-crm"


# ---------------------------------------------------------------------------
# Seed helpers (arrange-phase helpers, not test logic)
# ---------------------------------------------------------------------------


async def _insert_kg_node(
    conn,  # asyncpg.Connection — already namespace-scoped
    *,
    namespace_id: UUID,
    label: str,
    entity_type: str,
) -> UUID:
    """Insert one ``kg_nodes`` row and return its id."""
    node_id: UUID = await conn.fetchval(
        """
        INSERT INTO kg_nodes (label, entity_type, namespace_id, change_origin)
        VALUES ($1, $2, $3, 'agent')
        RETURNING id
        """,
        label,
        entity_type,
        namespace_id,
    )
    assert node_id is not None
    return node_id


async def _count_kg_rows(conn, *, namespace_id: UUID) -> tuple[int, int]:
    """Return (kg_nodes count, kg_edges count) for the namespace."""
    nodes: int = await conn.fetchval(
        "SELECT count(*) FROM kg_nodes WHERE namespace_id = $1",
        namespace_id,
    )
    edges: int = await conn.fetchval(
        "SELECT count(*) FROM kg_edges WHERE namespace_id = $1",
        namespace_id,
    )
    return nodes, edges


async def _seed_ownership_row(
    conn,
    namespace_id: UUID,
    *,
    node_type: str,
    owner_engine: str,
) -> None:
    """Insert a node-type-wide ownership row into ``node_ownership_registry``."""
    await conn.execute(
        """
        INSERT INTO node_ownership_registry (namespace_id, node_type, owner_engine)
        VALUES ($1, $2, $3)
        """,
        namespace_id,
        node_type,
        owner_engine,
    )


# ---------------------------------------------------------------------------
# Done-when #1 — sub-threshold match → queue, never auto-merge
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sub_threshold_match_lands_in_queue_and_never_auto_merges(
    pg_pool,
    make_namespace,
) -> None:
    """A sub-threshold score enqueues a pending review row; kg_nodes/kg_edges are untouched.

    Arrange:
      - fresh namespace with one kg_nodes seed row
    Act:
      - enqueue() with _SUB_THRESHOLD_SCORE pointing at the seed node
    Assert:
      - exactly one pending row exists in entity_merge_queue
      - the queue row carries status='pending', the correct score and node_type
      - kg_nodes count is unchanged (same as after seed)
      - kg_edges count is zero (no edge was created)
    """
    # --- Arrange -------------------------------------------------------
    ns: UUID = await make_namespace()
    node_type = f"device-{uuid4().hex[:8]}"
    candidate_payload = {"name": "Cisco Catalyst 9300", "serial": "FOC2042XYZ1"}

    async with scoped_pg_session(pg_pool, ns) as conn:
        target_id = await _insert_kg_node(
            conn,
            namespace_id=ns,
            label="Cisco Catalyst 9300",
            entity_type=node_type,
        )
        nodes_after_seed, edges_after_seed = await _count_kg_rows(conn, namespace_id=ns)

    # --- Act -----------------------------------------------------------
    async with scoped_pg_session(pg_pool, ns) as conn:
        queue_id: UUID = await enqueue(
            conn,
            namespace_id=ns,
            node_type=node_type,
            candidate=candidate_payload,
            target=target_id,
            score=_SUB_THRESHOLD_SCORE,
        )

    # --- Assert --------------------------------------------------------
    async with scoped_pg_session(pg_pool, ns) as conn:
        pending_rows = await list_pending(conn, namespace_id=ns)
        nodes_final, edges_final = await _count_kg_rows(conn, namespace_id=ns)

    # Queue invariant: exactly one pending row with the right metadata.
    assert len(pending_rows) == 1, (
        f"Expected 1 pending row in entity_merge_queue, got {len(pending_rows)}"
    )
    queued = pending_rows[0]
    assert queued["id"] == queue_id
    assert queued["status"] == "pending"
    assert queued["node_type"] == node_type
    assert abs(queued["score"] - _SUB_THRESHOLD_SCORE) < 1e-9, (
        f"Queue row score {queued['score']!r} != expected {_SUB_THRESHOLD_SCORE!r}"
    )
    assert queued["target_node_id"] == target_id

    # No-auto-merge invariant: kg_nodes and kg_edges row counts are unchanged.
    assert nodes_final == nodes_after_seed, (
        f"kg_nodes mutated: was {nodes_after_seed}, now {nodes_final} — auto-merge occurred"
    )
    assert edges_final == 0, (
        f"kg_edges mutated: expected 0, got {edges_final} — an edge was created illegally"
    )
    assert edges_after_seed == 0  # belt-and-braces: seed produced no edges either


# ---------------------------------------------------------------------------
# Done-when #2 — survivorship provenance is auditable in the ledger
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_survivorship_provenance_is_auditable_in_ledger(
    pg_pool,
    make_namespace,
) -> None:
    """survive() picks the winner; append_survivorship_provenance() writes an auditable ledger row.

    Arrange:
      - two field candidates with different source_trust values so the
        winner is decided by source_trust (REASON_SOURCE_TRUST)
    Act:
      - survive() to elect the winner
      - append_survivorship_provenance() to persist the provenance
    Assert:
      - v3_cognitive_ledger has a row with model_version='survivorship/v1'
      - the tlx_scores JSONB contains:
          event='field_survivorship'
          field_name == the name passed to append_survivorship_provenance
          winning_source == the high-trust source
          reason == REASON_SOURCE_TRUST
      - the ledger row was appended (SELECT count=1, no UPDATE/DELETE)
    """
    # --- Arrange -------------------------------------------------------
    ns: UUID = await make_namespace()
    field_name = "hostname"
    entity_id = f"entity-{uuid4().hex[:16]}"

    # High-trust candidate wins on source_trust.
    high_trust_candidate = {
        "value": "sw-core-01.example.net",
        "source": "netops-cmdb",
        "source_trust": 0.95,
        "as_of": "2025-01-01T00:00:00Z",
        "confidence": 0.80,
    }
    # Low-trust candidate loses despite being more recent.
    low_trust_candidate = {
        "value": "sw-core-01-old",
        "source": "legacy-dhcp",
        "source_trust": 0.40,
        "as_of": "2025-06-01T00:00:00Z",
        "confidence": 0.90,
    }
    all_candidates = [high_trust_candidate, low_trust_candidate]

    # --- Act -----------------------------------------------------------
    result = survive(all_candidates)

    assert result["provenance"]["reason"] == REASON_SOURCE_TRUST, (
        f"Expected winner decided by source_trust, got {result['provenance']['reason']!r}"
    )
    assert result["value"] == high_trust_candidate["value"]

    ledger_id: UUID = await append_survivorship_provenance(
        pg_pool,
        namespace_id=ns,
        entity_id=entity_id,
        field_name=field_name,
        winning_value=result["value"],
        winning_source=result["provenance"]["source"],
        reason=result["provenance"]["reason"],
        all_candidates=all_candidates,
    )

    # --- Assert --------------------------------------------------------
    async with scoped_pg_session(pg_pool, ns) as conn:
        ledger_row = await conn.fetchrow(
            """
            SELECT id, model_version, tlx_scores
            FROM v3_cognitive_ledger
            WHERE id = $1
              AND namespace_id = $2
            """,
            ledger_id,
            ns,
        )

    assert ledger_row is not None, (
        f"Ledger row {ledger_id} not found in v3_cognitive_ledger for namespace {ns}"
    )
    assert ledger_row["model_version"] == "survivorship/v1", (
        f"model_version={ledger_row['model_version']!r}, expected 'survivorship/v1'"
    )

    # Deserialise the JSONB provenance payload.
    tlx: dict = (
        ledger_row["tlx_scores"]
        if isinstance(ledger_row["tlx_scores"], dict)
        else json.loads(ledger_row["tlx_scores"])
    )

    assert tlx.get("event") == "field_survivorship", (
        f"tlx_scores.event={tlx.get('event')!r}, expected 'field_survivorship'"
    )
    assert tlx.get("field_name") == field_name, (
        f"tlx_scores.field_name={tlx.get('field_name')!r}, expected {field_name!r}"
    )
    assert tlx.get("winning_source") == high_trust_candidate["source"], (
        f"tlx_scores.winning_source={tlx.get('winning_source')!r}, "
        f"expected {high_trust_candidate['source']!r}"
    )
    assert tlx.get("reason") == REASON_SOURCE_TRUST, (
        f"tlx_scores.reason={tlx.get('reason')!r}, expected {REASON_SOURCE_TRUST!r}"
    )

    # Append-only invariant: the row was inserted exactly once.
    async with scoped_pg_session(pg_pool, ns) as conn:
        count: int = await conn.fetchval(
            """
            SELECT count(*)
            FROM v3_cognitive_ledger
            WHERE id = $1
              AND namespace_id = $2
            """,
            ledger_id,
            ns,
        )
    assert count == 1, f"Expected exactly 1 provenance row in v3_cognitive_ledger, found {count}"


# ---------------------------------------------------------------------------
# Done-when #3 — cross-engine write to non-owned node type is refused
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cross_engine_write_is_refused_owner_write_passes(
    pg_pool,
    make_namespace,
) -> None:
    """assert_owner() refuses a non-owner engine; the registered owner passes.

    Arrange:
      - register _OWNER_ENGINE as the sole writer for the node type
      - _NON_OWNER_ENGINE has no registry row for this type
    Act:
      - assert_owner() with _NON_OWNER_ENGINE → must raise OwnershipError
      - assert_owner() with _OWNER_ENGINE → must not raise
    Assert:
      - OwnershipError.writer_engine == _NON_OWNER_ENGINE
      - OwnershipError.owner_engine == _OWNER_ENGINE
      - owner call completes without exception
    """
    # --- Arrange -------------------------------------------------------
    ns: UUID = await make_namespace()
    node_type = f"managed-device-{uuid4().hex[:8]}"

    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, ns)
            await _seed_ownership_row(
                conn,
                ns,
                node_type=node_type,
                owner_engine=_OWNER_ENGINE,
            )

    # --- Act + Assert (non-owner is refused) ---------------------------
    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, ns)
            with pytest.raises(OwnershipError) as exc_info:
                await assert_owner(conn, ns, node_type, _NON_OWNER_ENGINE)

    err = exc_info.value
    assert err.writer_engine == _NON_OWNER_ENGINE, (
        f"OwnershipError.writer_engine={err.writer_engine!r}, expected {_NON_OWNER_ENGINE!r}"
    )
    assert err.owner_engine == _OWNER_ENGINE, (
        f"OwnershipError.owner_engine={err.owner_engine!r}, expected {_OWNER_ENGINE!r}"
    )
    assert err.node_type == node_type, (
        f"OwnershipError.node_type={err.node_type!r}, expected {node_type!r}"
    )

    # --- Act + Assert (owner passes without exception) -----------------
    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, ns)
            # Must not raise — the owner engine is allowed to write.
            await assert_owner(conn, ns, node_type, _OWNER_ENGINE)
