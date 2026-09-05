"""Integration tests for namespace deletion (migration 055).

Before migration 055, 54 of the 94 foreign keys referencing ``namespaces`` were
NO ACTION, so ``DELETE FROM namespaces`` failed on the first child row. Nothing
could delete a tenant -- which is why the pytest fixtures leaked namespaces
(there was no teardown they could have written) and why real tenant
deprovisioning does not work.

These tests pin both the new behaviour and its deliberate boundary:

  a. Catalog ratchet: every FK referencing ``namespaces`` is ON DELETE CASCADE
     except three documented exclusions. A new table added with a NO ACTION FK
     re-breaks tenant deletion silently; this fails instead.
  b. Deleting a namespace removes its child rows.
  c. A namespace holding ``event_log`` rows still cannot be deleted. That is not
     an oversight: ``prevent_mutation()`` is a plain plpgsql trigger that RAISEs
     on DELETE, and triggers fire regardless of role, so ON DELETE CASCADE there
     would only trade an FK violation for a trigger exception.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator

import asyncpg  # type: ignore[import-untyped]
import pytest  # type: ignore[import-untyped]
import pytest_asyncio  # type: ignore[import-untyped]

from nce.event_log import EXPECTED_TENANT_RLS_TABLES

# Tables whose FK to namespaces is deliberately left NO ACTION.
#   event_log / event_parents -- WORM, see module docstring.
#   audit_log                 -- cascading destroys a tenant's audit trail on
#                                deletion; a retention decision, not a
#                                mechanical one.
#   namespaces                -- self-reference (parent_id); whether deleting a
#                                parent tenant deletes its children is a
#                                product decision.
EXPECTED_NON_CASCADE: frozenset[str] = frozenset(
    {
        "audit_log.audit_log_namespace_id_fkey",
        "event_log.event_log_namespace_id_fkey",
        "event_parents.event_parents_namespace_id_fkey",
        "namespaces.namespaces_parent_id_fkey",
    }
)

# Tenant tables that have NO foreign key to ``namespaces`` at all, so they never
# appear in _FK_SQL's result set. A table listed here is a known schema gap owed
# a migration -- listing it records the debt, it does not bless it.
#
# EMPTY as of migration 062. It previously held ``outbox_events`` and
# ``saga_execution_log`` -- the two tables TD-1's required-half check found, and
# debt item D1. Migration 062 gave both a REFERENCES namespaces(id) ON DELETE
# CASCADE (and purged the orphan rows that would otherwise have made
# ADD CONSTRAINT fail outright), so their entries are removed in that same
# commit. The third assertion below is what forces that pairing: a table listed
# here which has since grown its FK fails the gate, so the allowlist cannot rot
# into a silent exemption, and a migration cannot be "passed" by widening the
# schema while the excuse stays behind.
TENANT_TABLES_WITHOUT_NAMESPACE_FK: frozenset[str] = frozenset()

_FK_SQL = """
SELECT t.relname || '.' || c.conname AS ref, c.confdeltype::text AS del
FROM pg_constraint c
JOIN pg_class t  ON t.oid  = c.conrelid
JOIN pg_class ft ON ft.oid = c.confrelid
WHERE c.contype = 'f'
  AND ft.relname = 'namespaces'
  AND t.relispartition = false
"""


@pytest_asyncio.fixture
async def fk_pool() -> AsyncGenerator[asyncpg.Pool, None]:
    """Owner-role pool for this module only (see test_node_ownership_seed_bulk)."""

    dsn = (
        os.getenv("NCE_INTEGRATION_PG_DSN")
        or os.getenv("PG_DSN")
        or os.getenv("DATABASE_URL")
        or ""
    ).strip()
    if not dsn:
        pytest.skip("Integration tests need NCE_INTEGRATION_PG_DSN, PG_DSN, or DATABASE_URL")
    try:
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2, command_timeout=60)
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"Postgres not reachable for integration tests: {exc}")
    try:
        yield pool
    finally:
        await pool.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_every_namespace_fk_cascades_except_documented_exclusions(
    fk_pool: asyncpg.Pool,
) -> None:
    async with fk_pool.acquire() as conn:
        rows = await conn.fetch(_FK_SQL)

    assert rows, "no foreign keys reference namespaces -- schema not applied?"
    present = {r["ref"] for r in rows}
    non_cascade = {r["ref"] for r in rows if r["del"] != "c"}

    unexpected = sorted(non_cascade - EXPECTED_NON_CASCADE)
    assert not unexpected, (
        "these FKs to namespaces are not ON DELETE CASCADE, so deleting a tenant "
        "will fail on them: " + ", ".join(unexpected) + ". Add ON DELETE CASCADE "
        "in nce/schema.sql and to the migration 055 list, or -- if the rows must "
        "outlive their tenant -- add them to EXPECTED_NON_CASCADE with a reason."
    )

    # A table with no namespaces FK never enters ``rows`` at all, so every
    # assertion above is blind to it: the FK could be dropped outright and this
    # file would stay green. EXPECTED_TENANT_RLS_TABLES is the required half that
    # EXPECTED_NON_CASCADE (the allowlist half) was missing.
    tables_with_fk = {ref.split(".", 1)[0] for ref in present}
    missing = sorted(
        set(EXPECTED_TENANT_RLS_TABLES) - tables_with_fk - TENANT_TABLES_WITHOUT_NAMESPACE_FK
    )
    assert not missing, (
        "these tenant-scoped tables have NO foreign key to namespaces, so deleting "
        "a tenant silently orphans their rows: " + ", ".join(missing) + ". Add "
        "REFERENCES namespaces(id) ON DELETE CASCADE in nce/schema.sql plus a "
        "migration, or -- if the gap is knowingly deferred -- add the table to "
        "TENANT_TABLES_WITHOUT_NAMESPACE_FK with the reason."
    )

    repaired = sorted(TENANT_TABLES_WITHOUT_NAMESPACE_FK & tables_with_fk)
    assert not repaired, (
        "listed as having no namespaces FK but one now exists -- drop from "
        "TENANT_TABLES_WITHOUT_NAMESPACE_FK: " + ", ".join(repaired)
    )

    stale = sorted((EXPECTED_NON_CASCADE & present) - non_cascade)
    assert not stale, (
        "listed as deliberately non-cascading but now cascades -- drop from "
        "EXPECTED_NON_CASCADE: " + ", ".join(stale)
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_deleting_a_namespace_removes_its_child_rows(fk_pool: asyncpg.Pool) -> None:
    async with fk_pool.acquire() as conn:
        tr = conn.transaction()
        await tr.start()
        try:
            ns = await conn.fetchval(
                "INSERT INTO namespaces (slug) VALUES ($1) RETURNING id",
                f"pytest-fkcascade-{uuid.uuid4().hex}",
            )
            await conn.execute(
                "INSERT INTO kg_nodes (namespace_id, label) VALUES ($1, $2)", ns, "probe-node"
            )
            await conn.execute(
                "INSERT INTO dead_letter_queue "
                "(namespace_id, task_name, job_id, kwargs, error_message, attempt_count) "
                "VALUES ($1, 't', $2, '{}'::jsonb, 'e', 1)",
                ns,
                uuid.uuid4().hex,
            )
            assert (
                await conn.fetchval("SELECT count(*) FROM kg_nodes WHERE namespace_id = $1", ns)
                == 1
            )

            await conn.execute("DELETE FROM namespaces WHERE id = $1", ns)

            assert (
                await conn.fetchval("SELECT count(*) FROM kg_nodes WHERE namespace_id = $1", ns)
                == 0
            )
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM dead_letter_queue WHERE namespace_id = $1", ns
                )
                == 0
            )
        finally:
            await tr.rollback()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_namespace_holding_event_log_rows_still_cannot_be_deleted(
    fk_pool: asyncpg.Pool,
) -> None:
    """The WORM boundary is deliberate -- pin it so nobody 'fixes' it by accident."""
    async with fk_pool.acquire() as conn:
        tr = conn.transaction()
        await tr.start()
        try:
            ns = await conn.fetchval(
                "INSERT INTO namespaces (slug) VALUES ($1) RETURNING id",
                f"pytest-fkworm-{uuid.uuid4().hex}",
            )
            await conn.execute(
                "INSERT INTO event_log "
                "(namespace_id, agent_id, event_type, event_seq, params, signature, signature_key_id) "
                "VALUES ($1, 'a', 'probe', 1, '{}'::jsonb, $2, 'k1')",
                ns,
                b"\x00",
            )
            with pytest.raises(asyncpg.exceptions.ForeignKeyViolationError):
                await conn.execute("DELETE FROM namespaces WHERE id = $1", ns)
        finally:
            await tr.rollback()
