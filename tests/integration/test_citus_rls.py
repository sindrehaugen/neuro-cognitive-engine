"""Citus distributed-table RLS + 2PC matrix.

OUTCOME: DESCOPE (Batch 122)
==============================
This test file was authored as part of the Batch 122 decision gate.
The matrix could not be executed because no suitable Docker image exists
that bundles BOTH Citus 12 and pgvector on PostgreSQL 16 — both are required
by nce/schema.sql and migration 010_citus_sharding.sql.

Failing evidence collected during Batch 122
-------------------------------------------
1. Image tag ``citusdata/citus:12-pg16`` does not exist on Docker Hub.
   ``docker manifest inspect citusdata/citus:12-pg16``
   → "no such manifest: docker.io/citusdata/citus:12-pg16"

2. Nearest available image ``citusdata/citus:12.1.1`` (PostgreSQL 16.1) lacks pgvector.
   ``docker run --rm citusdata/citus:12.1.1 …``
   → ``SELECT name FROM pg_available_extensions WHERE name IN ('citus','vector')``
   → only ``citus`` returned; ``vector`` (pgvector) absent.

3. ``pgvector/pgvector:pg16`` (the image used by the live integration stack) has no
   Citus extension.
   → ``SELECT name FROM pg_available_extensions WHERE name = 'citus'``
   → zero rows.

4. nce/schema.sql line 15: ``CREATE EXTENSION IF NOT EXISTS vector;``
   Both extensions are mandatory in the same PostgreSQL instance.
   Building a custom Citus + pgvector image is out of scope for this batch
   (batch rule 3: "No new modules/deps in nce/ runtime code").

Decision: migration 010_citus_sharding.sql moved to nce/migrations/optional/
Rationale documented in docs/citus_descope.md.

These tests are left in place so the matrix can be executed in a future batch
that resolves the image dependency (e.g., by building a custom image or by
using a pgvector-enabled Citus distribution).  They are marked
``skip_citus_unavailable`` so the CI Citus job skips them cleanly when the
container is not present, and the skip message names the resolution path.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
import pytest_asyncio

# ---------------------------------------------------------------------------
# Skip guard
# ---------------------------------------------------------------------------
# Tests are skipped unless a Citus container is reachable via CITUS_TEST_DSN.
# The CI job sets CITUS_TEST_DSN when it successfully brings up the Citus
# compose profile.  Local runs without the compose profile get a clean skip.

_CITUS_DSN = os.environ.get("CITUS_TEST_DSN", "")
_SKIP_REASON = (
    "CITUS_TEST_DSN not set: Citus image unavailable. "
    "Resolve by building a combined citusdata/citus:12-pg16 + pgvector image "
    "and re-running with the 'citus' compose profile.  "
    "See docs/citus_descope.md §Resolution Path."
)

skip_citus_unavailable = pytest.mark.skipif(
    not _CITUS_DSN,
    reason=_SKIP_REASON,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def citus_pool():
    """Asyncpg pool connected to the Citus coordinator under test."""
    import asyncpg

    pool = await asyncpg.create_pool(_CITUS_DSN, min_size=2, max_size=4)
    yield pool
    await pool.close()


@pytest_asyncio.fixture
async def citus_ns(citus_pool):
    """Create a fresh namespace on Citus and return its UUID."""
    ns_id = uuid4()
    async with citus_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO namespaces (id, name) VALUES ($1, $2)",
            ns_id,
            f"citus-test-{ns_id.hex[:8]}",
        )
    yield ns_id
    # cleanup
    async with citus_pool.acquire() as conn:
        await conn.execute("DELETE FROM namespaces WHERE id = $1", ns_id)


# ---------------------------------------------------------------------------
# Matrix tests (skipped until Citus image is available)
# ---------------------------------------------------------------------------


@skip_citus_unavailable
@pytest.mark.integration
@pytest.mark.asyncio
async def test_rls_holds_on_distributed_table_with_guc_propagation(citus_pool, citus_ns) -> None:
    """(a) RLS holds on a distributed table with GUC propagation set.

    Asserts:
    - INSERT under tenant A is invisible to tenant B (cross-tenant isolation).
    - SET LOCAL nce.namespace_id propagates to worker shards via
      citus.propagate_set_commands = 'local'.
    - A missing GUC raises an error (fail-closed), never a silent leak.
    """
    import asyncpg

    ns_a = citus_ns
    ns_b = uuid4()
    async with citus_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO namespaces (id, name) VALUES ($1, $2)",
            ns_b,
            f"citus-test-b-{ns_b.hex[:8]}",
        )

    try:
        async with citus_pool.acquire() as conn:
            # Insert under tenant A.
            async with conn.transaction():
                await conn.execute("SET LOCAL nce.namespace_id = $1", str(ns_a))
                mem_id = await conn.fetchval(
                    """
                    INSERT INTO memories
                        (namespace_id, content, embedding)
                    VALUES ($1, 'citus-rls-test', '[0.1,0.2,0.3]'::vector)
                    RETURNING id
                    """,
                    ns_a,
                )

            # Query under tenant B — must see zero rows.
            async with conn.transaction():
                await conn.execute("SET LOCAL nce.namespace_id = $1", str(ns_b))
                count = await conn.fetchval(
                    "SELECT count(*) FROM memories WHERE id = $1",
                    mem_id,
                )
            assert count == 0, f"Cross-tenant leak: tenant B saw memory {mem_id} owned by tenant A"

            # Query without GUC must raise (fail-closed).
            with pytest.raises(asyncpg.PostgresError, match="nce.namespace_id"):
                await conn.fetchval("SELECT get_nce_namespace()")
    finally:
        async with citus_pool.acquire() as conn:
            await conn.execute("DELETE FROM namespaces WHERE id = $1", ns_b)


@skip_citus_unavailable
@pytest.mark.integration
@pytest.mark.asyncio
async def test_cross_shard_2pc_saga_write(citus_pool, citus_ns) -> None:
    """(b) Cross-shard 2PC works for a saga write.

    Writes to event_log (WORM table, must always use 2pc) and verifies
    the row is durably committed and visible under the same namespace.
    """
    ns_id = citus_ns
    async with citus_pool.acquire() as conn:
        # Confirm 2PC is active (migration 010 sets it database-level).
        proto = await conn.fetchval("SHOW citus.multi_shard_commit_protocol")
        assert proto == "2pc", f"Expected 2pc commit protocol, got: {proto!r}"

        # Write an event_log row within a transaction (required for GUC propagation).
        async with conn.transaction():
            await conn.execute("SET LOCAL nce.namespace_id = $1", str(ns_id))
            event_id = await conn.fetchval(
                """
                INSERT INTO event_log
                    (namespace_id, event_type, payload, occurred_at)
                VALUES ($1, 'citus.2pc.test', '{}', now())
                RETURNING id
                """,
                ns_id,
            )

        # Read it back (new transaction, same namespace).
        async with conn.transaction():
            await conn.execute("SET LOCAL nce.namespace_id = $1", str(ns_id))
            found = await conn.fetchval(
                "SELECT id FROM event_log WHERE id = $1",
                event_id,
            )
        assert found == event_id, (
            f"2PC saga write not durable: event {event_id} not found after commit"
        )


@skip_citus_unavailable
@pytest.mark.integration
@pytest.mark.asyncio
async def test_event_seq_coordinator_local_allocation(citus_pool, citus_ns) -> None:
    """(c) event_seq coordinator-local allocation is correct under distribution.

    Migration 010 intentionally keeps event_sequences on the coordinator
    (not distributed) to preserve monotonic counter semantics.
    Asserts that event_sequences is NOT a distributed table.
    """
    async with citus_pool.acquire() as conn:
        is_distributed = await conn.fetchval(
            """
            SELECT count(*) > 0
            FROM pg_dist_partition
            WHERE logicalrelid = 'event_sequences'::regclass
            """,
        )
    assert not is_distributed, (
        "event_sequences must remain coordinator-local (monotonic counter design). "
        "See migration 010 PHASE 5 / TD-010-6 rationale."
    )


@skip_citus_unavailable
@pytest.mark.integration
@pytest.mark.asyncio
async def test_semantic_search_prunes_shards(citus_pool, citus_ns) -> None:
    """(d) semantic_search prunes shards when namespace_id is provided.

    A query to the distributed memories table that includes namespace_id
    in the WHERE clause must be routed to a single shard (shard pruning),
    not broadcast across all 32 shards.

    Uses EXPLAIN (FORMAT JSON) to verify shard_count = 1.
    """
    ns_id = citus_ns
    async with citus_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SET LOCAL nce.namespace_id = $1", str(ns_id))
            plan_rows = await conn.fetch(
                """
                EXPLAIN (FORMAT JSON)
                SELECT id, content
                FROM memories
                WHERE namespace_id = $1
                ORDER BY embedding <=> '[0.1,0.2,0.3]'::vector
                LIMIT 5
                """,
                ns_id,
            )

    plan_json = plan_rows[0][0]
    plan_text = str(plan_json)

    # Citus EXPLAIN reports "Task Count: 1" for a pruned single-shard query.
    # A full broadcast scan would report "Task Count: 32".
    assert "Task Count: 1" in plan_text, (
        f"Expected shard pruning (Task Count: 1) but got:\n{plan_text}"
    )
