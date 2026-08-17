"""Integration tests for the multi-parent causal DAG (Batch 120).

Acceptance criteria
-------------------
* A consolidation event records N parents in ``event_parents``.
* ``get_event_provenance`` returns multi-parent ancestry (``parent_event_ids``).
* ``detect_causal_cycles`` flags a synthetic cycle and passes a clean DAG.
* Existing single-parent events still resolve via the scalar column.
"""

from __future__ import annotations

import uuid

import asyncpg
import pytest
import pytest_asyncio

from nce.admin_handlers.fleet import detect_causal_cycles
from nce.event_log import append_event
from nce.replay import get_event_provenance

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def ns_id(pg_pool: asyncpg.Pool) -> uuid.UUID:
    """Create a fresh namespace for each test.

    Cleanup is best-effort: event_log may be WORM-protected on non-test DBs,
    so we skip any DELETE failures silently.  The namespace row itself is
    deleted last to keep the test DB tidy.
    """
    nid = uuid.uuid4()
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO namespaces (id, slug) VALUES ($1, $2)",
            nid,
            f"test-causal-dag-{nid}",
        )
    yield nid
    # Cleanup: best-effort, ignore WORM trigger errors.
    async with pg_pool.acquire() as conn:
        for stmt in (
            "DELETE FROM event_parents WHERE namespace_id = $1",
            "DELETE FROM event_log WHERE namespace_id = $1",
            "DELETE FROM event_sequences WHERE namespace_id = $1",
        ):
            try:
                await conn.execute(stmt, nid)
            except Exception:
                pass
        try:
            await conn.execute("DELETE FROM namespaces WHERE id = $1", nid)
        except Exception:
            pass


def _store_memory_params() -> dict:
    """Return a minimal valid params dict for a ``store_memory`` event."""
    return {
        "saga_id": str(uuid.uuid4()),
        "memory_id": str(uuid.uuid4()),
        "payload_ref": f"nomongo/{uuid.uuid4()}",
        "assertion_type": "fact",
        "entities": [],
        "triplets": [],
    }


def _consolidation_params(*, memory_id: uuid.UUID, payload_ref: str) -> dict:
    """Return a minimal valid params dict for a ``consolidation_run`` event.

    ``memory_id`` is included so ``get_event_provenance`` can locate this event
    by memory id (``params->>'memory_id'``).  ``consolidated_memory_id`` holds
    the same value as that is what the consolidation code normally stores.
    """
    mem_id_str = str(memory_id)
    return {
        "abstraction": "test abstraction",
        "key_entities": ["EntityA"],
        "key_relations": [{"subject": "A", "predicate": "rel", "object": "B"}],
        "supporting_memory_ids": [mem_id_str],
        "contradicting_memory_ids": [],
        "confidence": 0.9,
        "source_memories": [mem_id_str],
        "consolidated_memory_id": mem_id_str,
        "payload_ref": payload_ref,
        # Explicit memory_id key so get_event_provenance can find this event.
        "memory_id": mem_id_str,
    }


async def _append(conn, *, ns: uuid.UUID, etype: str, params: dict, **kw):
    """Call append_event inside an existing transaction."""
    return await append_event(
        conn=conn,
        namespace_id=ns,
        agent_id="test-agent",
        event_type=etype,
        params=params,
        **kw,
    )


# ---------------------------------------------------------------------------
# Test 1: multi-parent append records N rows in event_parents
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_consolidation_event_records_multiparent(pg_pool, ns_id):
    """append_event with parent_event_ids=[e1, e2, e3] writes 3 rows to event_parents."""
    async with pg_pool.acquire() as conn:
        parent_ids: list[uuid.UUID] = []
        async with conn.transaction():
            for _ in range(3):
                r = await _append(
                    conn, ns=ns_id, etype="store_memory", params=_store_memory_params()
                )
                parent_ids.append(r.event_id)

        # Consolidation event with 3 parents — needs full required-key set.
        src_mem = uuid.uuid4()
        async with conn.transaction():
            cons = await _append(
                conn,
                ns=ns_id,
                etype="consolidation_run",
                params=_consolidation_params(
                    memory_id=src_mem,
                    payload_ref=f"nomongo/{uuid.uuid4()}",
                ),
                parent_event_ids=parent_ids,
            )

        rows = await conn.fetch(
            "SELECT parent_event_id FROM event_parents WHERE event_id = $1",
            cons.event_id,
        )
        stored = {r["parent_event_id"] for r in rows}
        assert stored == set(parent_ids), f"Expected parent set {set(parent_ids)}, got {stored}"
        # Scalar column holds the first (primary) parent.
        scalar = await conn.fetchval(
            "SELECT parent_event_id FROM event_log WHERE id = $1",
            cons.event_id,
        )
        assert scalar == parent_ids[0]


# ---------------------------------------------------------------------------
# Test 2: get_event_provenance returns multi-parent ancestry
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_event_provenance_multiparent(pg_pool, ns_id):
    """get_event_provenance includes parent_event_ids for multi-parent events."""
    async with pg_pool.acquire() as conn:
        parent_ids: list[uuid.UUID] = []
        memory_id = uuid.uuid4()

        async with conn.transaction():
            for _ in range(2):
                r = await _append(
                    conn, ns=ns_id, etype="store_memory", params=_store_memory_params()
                )
                parent_ids.append(r.event_id)

        async with conn.transaction():
            await _append(
                conn,
                ns=ns_id,
                etype="consolidation_run",
                params=_consolidation_params(
                    memory_id=memory_id,
                    payload_ref=f"nomongo/{uuid.uuid4()}",
                ),
                parent_event_ids=parent_ids,
            )

    provenance = await get_event_provenance(pg_pool, memory_id)
    chain = provenance.get("chain", [])
    assert chain, "Expected non-empty chain"

    # The consolidation event is the last element (chain is root-first).
    cons_entry = chain[-1]
    stored_pids = set(cons_entry.get("parent_event_ids", []))
    expected_pids = {str(p) for p in parent_ids}
    assert stored_pids == expected_pids, (
        f"Expected parent_event_ids {expected_pids}, got {stored_pids}"
    )


# ---------------------------------------------------------------------------
# Test 3: detect_causal_cycles flags a synthetic cycle
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_detect_causal_cycles_flags_cycle(pg_pool, ns_id):
    """detect_causal_cycles returns has_cycle=True when a cycle is inserted."""
    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            r_a = await _append(conn, ns=ns_id, etype="store_memory", params=_store_memory_params())
            r_b = await _append(conn, ns=ns_id, etype="store_memory", params=_store_memory_params())

        # Directly insert synthetic cycle: A → B, B → A.
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO event_parents (event_id, parent_event_id, namespace_id) "
                "VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
                r_a.event_id,
                r_b.event_id,
                ns_id,
            )
            await conn.execute(
                "INSERT INTO event_parents (event_id, parent_event_id, namespace_id) "
                "VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
                r_b.event_id,
                r_a.event_id,
                ns_id,
            )

    result = await detect_causal_cycles(pg_pool, ns_id, depth_cap=10)
    assert result["has_cycle"] is True, f"Expected cycle, got: {result}"
    assert result["cycle_paths"], "Expected at least one cycle path entry"


# ---------------------------------------------------------------------------
# Test 4: detect_causal_cycles passes a clean DAG
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_detect_causal_cycles_clean_dag(pg_pool, ns_id):
    """detect_causal_cycles returns has_cycle=False for a simple clean DAG."""
    async with pg_pool.acquire() as conn:
        parent_ids: list[uuid.UUID] = []
        async with conn.transaction():
            for _ in range(3):
                r = await _append(
                    conn, ns=ns_id, etype="store_memory", params=_store_memory_params()
                )
                parent_ids.append(r.event_id)

        src_mem = uuid.uuid4()
        async with conn.transaction():
            await _append(
                conn,
                ns=ns_id,
                etype="consolidation_run",
                params=_consolidation_params(
                    memory_id=src_mem,
                    payload_ref=f"nomongo/{uuid.uuid4()}",
                ),
                parent_event_ids=parent_ids,
            )

    result = await detect_causal_cycles(pg_pool, ns_id)
    assert result["has_cycle"] is False, f"Expected no cycle, got: {result}"


# ---------------------------------------------------------------------------
# Test 5: single-parent events resolve via the scalar column
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_single_parent_resolves_via_scalar(pg_pool, ns_id):
    """A classic single-parent event resolves correctly in get_event_provenance."""
    async with pg_pool.acquire() as conn:
        memory_id = uuid.uuid4()

        async with conn.transaction():
            parent_r = await _append(
                conn, ns=ns_id, etype="store_memory", params=_store_memory_params()
            )

        async with conn.transaction():
            await _append(
                conn,
                ns=ns_id,
                etype="store_memory",
                params={
                    "saga_id": str(uuid.uuid4()),
                    "memory_id": str(memory_id),
                    "payload_ref": f"nomongo/{uuid.uuid4()}",
                    "assertion_type": "fact",
                    "entities": [],
                    "triplets": [],
                },
                parent_event_id=parent_r.event_id,
            )

    provenance = await get_event_provenance(pg_pool, memory_id)
    chain = provenance["chain"]
    assert chain, "Expected non-empty chain"

    child_entry = chain[-1]
    assert child_entry["parent_event_id"] == str(parent_r.event_id)
    # parent_event_ids should fall back to the scalar column.
    assert str(parent_r.event_id) in child_entry.get("parent_event_ids", [])
