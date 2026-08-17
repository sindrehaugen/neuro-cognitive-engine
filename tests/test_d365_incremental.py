"""Integration tests for D365 incremental sync — Batch 114.

Verifies:
  1. First tick (no prior cursor) performs a full pull and seeds the cursor
     to max(modifiedon) seen.
  2. Second tick applies a ``modifiedon gt <cursor − 5 min overlap>`` filter
     so only the delta is fetched.
  3. Weekly tick performs a full pull (no cursor filter) and retires an entity
     that is absent from the full pull (delete reconciliation).

Dataverse is mocked; the integration DB (NCE_INTEGRATION_PG_DSN) is required
for cursor persistence via ``d365_integrations.last_sync_stats``.

Run with::

    pytest -m integration tests/test_d365_incremental.py -q
"""

from __future__ import annotations

import json
import types
import uuid
from collections.abc import AsyncIterator
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

from nce.auth import set_namespace_context
from nce.vertical_modules.dynamics365.sync import DataverseSyncEngine

# ---------------------------------------------------------------------------
# DB schema helpers
# ---------------------------------------------------------------------------

_SCHEMA_GUARD_SQL = """
SELECT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name   = 'd365_integrations'
      AND column_name  = 'last_sync_stats'
)
"""

_D365_INT_INSERT = """
INSERT INTO d365_integrations
    (id, namespace_id, org_url, status, last_sync_stats)
VALUES ($1::uuid, $2::uuid, $3, 'ACTIVE', '{}'::jsonb)
ON CONFLICT (namespace_id, org_url) DO UPDATE
    SET status = 'ACTIVE', last_sync_stats = '{}'::jsonb, updated_at = NOW()
RETURNING id
"""

_D365_INT_CLEANUP = """
DELETE FROM d365_integrations WHERE namespace_id = $1::uuid
"""

_NS_INSERT = """
INSERT INTO namespaces (id, slug, metadata)
VALUES ($1::uuid, $2, '{"d365": {"enabled": true}}'::jsonb)
ON CONFLICT (id) DO NOTHING
"""

_NS_CLEANUP = """
DELETE FROM namespaces WHERE id = $1::uuid
"""

# ---------------------------------------------------------------------------
# Mock Dataverse client
# ---------------------------------------------------------------------------


class _MockDataverseClient:
    """Minimal mock that records paginate() calls and returns controlled pages."""

    def __init__(self) -> None:
        # List of calls: each entry is {"entity_set": str, "filter_expr": str|None, ...}
        self.calls: list[dict[str, Any]] = []
        # pages_by_entity: entity_set → list of records to return
        self.pages_by_entity: dict[str, list[dict[str, Any]]] = {}

    def set_page(self, entity_set: str, records: list[dict[str, Any]]) -> None:
        self.pages_by_entity[entity_set] = records

    def paginate(
        self,
        entity_set: str,
        *,
        select: list[str] | None = None,
        filter_expr: str | None = None,
        page_size: int = 1000,
    ) -> AsyncIterator[dict[str, Any]]:
        self.calls.append(
            {
                "entity_set": entity_set,
                "select": select,
                "filter_expr": filter_expr,
                "page_size": page_size,
            }
        )
        records = self.pages_by_entity.get(entity_set, [])

        async def _gen() -> AsyncIterator[dict[str, Any]]:
            for r in records:
                yield r

        return _gen()

    async def track_changes(
        self,
        entity_set: str,
        *,
        select: list[str] | None = None,
        delta_link: str | None = None,
        page_size: int = 1000,
    ) -> tuple[list[dict[str, Any]], list[str], str | None]:
        # Minimal stub — returns empty change set; weekly tests override this.
        return [], [], None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def d365_pg_scope(pg_pool: asyncpg.Pool):
    """Create an isolated namespace + d365_integrations row for the test, clean up after."""
    async with pg_pool.acquire() as conn:
        has_table = await conn.fetchval(_SCHEMA_GUARD_SQL)
    if not has_table:
        pytest.skip("d365_integrations.last_sync_stats column not found — apply schema.sql first")

    ns_id = uuid.uuid4()
    org_url = "https://test-incremental.crm.dynamics.com"

    # Create a minimal namespace row (required by FK).
    async with pg_pool.acquire() as conn:
        await conn.execute(_NS_INSERT, ns_id, f"test-d365-incr-{ns_id.hex[:8]}")
        await conn.execute(_D365_INT_INSERT, uuid.uuid4(), ns_id, org_url)

    yield pg_pool, ns_id

    async with pg_pool.acquire() as conn:
        await conn.execute(_D365_INT_CLEANUP, ns_id)
        # kg_nodes rows reference namespaces via FK — delete before namespace.
        await conn.execute(
            "DELETE FROM kg_nodes WHERE namespace_id = $1::uuid",
            ns_id,
        )
        await conn.execute(_NS_CLEANUP, ns_id)


# ---------------------------------------------------------------------------
# Stub sync method — replaces sync_accounts to avoid kg_nodes DB upserts.
# The real sync methods call _iter_entity which calls _observe_modifiedon
# when _use_cursor_paginate=True; this stub replicates that without DB writes.
# ---------------------------------------------------------------------------


async def _stub_sync_accounts(engine: DataverseSyncEngine) -> dict[str, Any]:
    """Iterate accounts from the mock client, observe modifiedon, skip DB upserts."""
    count = 0
    async for record in engine._iter_entity("accounts", select=["accountid", "name", "modifiedon"]):
        count += 1
    return {"entity": "accounts", "upserted": count}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_first_tick_seeds_cursor(d365_pg_scope: tuple[asyncpg.Pool, uuid.UUID]) -> None:
    """First incremental tick (no prior cursor) performs a full pull and persists the cursor.

    Verifies:
    - No ``modifiedon gt`` filter is applied on the first tick (no cursor).
    - After the tick the cursor map in ``last_sync_stats`` is seeded with
      ``max(modifiedon)`` seen during the pull.
    """
    pool, ns_id = d365_pg_scope

    t2 = "2026-06-01T11:00:00Z"

    client = _MockDataverseClient()
    client.set_page(
        "accounts",
        [
            {"accountid": "a1", "name": "Acme Corp", "modifiedon": "2026-06-01T10:00:00Z"},
            {"accountid": "a2", "name": "Beta Corp", "modifiedon": t2},
        ],
    )

    async with pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, ns_id)
            engine = DataverseSyncEngine(conn, ns_id, client)
            # Replace sync_accounts to avoid kg_nodes upserts (schema mismatch).
            engine.sync_accounts = types.MethodType(_stub_sync_accounts, engine)
            await engine.run_incremental_sync(entity_types=["accounts"])

    # No filter on the first tick — no prior cursor.
    account_calls = [c for c in client.calls if c["entity_set"] == "accounts"]
    assert account_calls, "accounts entity-set was not paginated"
    first_filter = account_calls[0]["filter_expr"]
    assert first_filter is None or "modifiedon gt" not in (first_filter or ""), (
        f"First tick must NOT apply modifiedon filter, got: {first_filter!r}"
    )

    # Cursor must be seeded in last_sync_stats.
    async with pool.acquire() as check_conn:
        row = await check_conn.fetchrow(
            "SELECT last_sync_stats FROM d365_integrations "
            "WHERE namespace_id = $1::uuid AND status = 'ACTIVE'",
            str(ns_id),
        )
    assert row is not None, "d365_integrations row not found after tick"
    cursors = _parse_stats(row).get("cursors", {})
    assert "accounts" in cursors, f"accounts cursor not seeded; cursors={cursors}"
    assert cursors["accounts"] == t2, (
        f"Expected cursor {t2!r} (max modifiedon), got {cursors['accounts']!r}"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_second_tick_issues_modifiedon_filter(
    d365_pg_scope: tuple[asyncpg.Pool, uuid.UUID],
) -> None:
    """Second incremental tick uses the persisted cursor with the 5-min overlap.

    Flow:
      1. Seed a cursor for the ``accounts`` entity-set via ``_save_cursor_map``.
      2. Run a second incremental tick.
      3. Assert ``modifiedon gt`` filter is applied using ``cursor - 300 s``.
      4. Cursor is advanced to ``max(modifiedon)`` of the delta records.
    """
    pool, ns_id = d365_pg_scope

    seeded_cursor = "2026-06-01T11:00:00Z"
    await _seed_cursor(pool, ns_id, {"accounts": seeded_cursor})

    delta_ts = "2026-06-01T12:00:00Z"
    client = _MockDataverseClient()
    client.set_page("accounts", [{"accountid": "a2", "name": "Delta Corp", "modifiedon": delta_ts}])

    async with pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, ns_id)
            engine = DataverseSyncEngine(conn, ns_id, client)
            engine.sync_accounts = types.MethodType(_stub_sync_accounts, engine)
            await engine.run_incremental_sync(entity_types=["accounts"])

    # Filter must be applied with overlap-adjusted cursor.
    account_calls = [c for c in client.calls if c["entity_set"] == "accounts"]
    assert account_calls, "accounts entity-set was not paginated on second tick"
    applied_filter = account_calls[0]["filter_expr"] or ""
    assert "modifiedon gt" in applied_filter, (
        f"Second tick must apply modifiedon gt filter; got: {applied_filter!r}"
    )
    # 2026-06-01T11:00:00Z - 300 s = 2026-06-01T10:55:00Z
    assert "2026-06-01T10:55:00Z" in applied_filter, (
        f"Expected overlap-adjusted timestamp in filter; got: {applied_filter!r}"
    )

    # Cursor must be advanced to max(modifiedon) of the delta records.
    async with pool.acquire() as check_conn:
        row = await check_conn.fetchrow(
            "SELECT last_sync_stats FROM d365_integrations "
            "WHERE namespace_id = $1::uuid AND status = 'ACTIVE'",
            str(ns_id),
        )
    cursors = _parse_stats(row).get("cursors", {})
    assert cursors.get("accounts") == delta_ts, (
        f"Cursor not advanced to delta max; cursors={cursors}"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_weekly_tick_full_pull_reconciles_removed_entity(
    d365_pg_scope: tuple[asyncpg.Pool, uuid.UUID],
) -> None:
    """Weekly full-refresh pull retires a graph entity absent from Dataverse.

    Flow:
      1. Seed a cursor so an incremental tick would apply a filter.
      2. Insert a kg_nodes row tagged with a known d365_source_id (previously synced).
      3. Run the weekly full tick — Dataverse returns only one surviving account.
      4. ``detect_and_retire_deletions`` is mocked to call ``_retire_source``
         for the removed entity.

    Asserts:
    - Weekly tick performs a full pull (no ``modifiedon gt`` filter).
    - ``detect_and_retire_deletions`` is called.
    - The removed entity is gone from kg_nodes.
    - The cursor is seeded from ``max(modifiedon)`` of the surviving records.
    """
    pool, ns_id = d365_pg_scope

    # Seed a prior cursor — weekly tick must ignore it.
    await _seed_cursor(pool, ns_id, {"accounts": "2026-06-01T00:00:00Z"})

    removed_source_id = str(uuid.uuid4())

    # Insert a kg_nodes row for the entity that was deleted from Dataverse.
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO kg_nodes (label, entity_type, namespace_id, d365_source_id, change_origin)
            VALUES ($1, 'D365_Account', $2::uuid, $3, 'sync')
            ON CONFLICT (label, namespace_id) DO NOTHING
            """,
            f"Account:RemovedCorp-{removed_source_id[:8]}",
            str(ns_id),
            removed_source_id,
        )

    surviving_ts = "2026-06-10T08:00:00Z"
    client = _MockDataverseClient()
    client.set_page(
        "accounts",
        [{"accountid": "survivor-1", "name": "Surviving Corp", "modifiedon": surviving_ts}],
    )

    retire_calls: list[dict[str, Any]] = []

    async def _mock_retire(self_engine: DataverseSyncEngine) -> dict[str, Any]:
        retire_calls.append({"called": True})
        removed = await self_engine._retire_source([removed_source_id])
        return {"enabled": True, "removed": 1, "rows_retired": removed}

    stats: dict[str, Any] = {}
    async with pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, ns_id)
            engine = DataverseSyncEngine(conn, ns_id, client)
            engine.sync_accounts = types.MethodType(_stub_sync_accounts, engine)
            engine.detect_and_retire_deletions = types.MethodType(_mock_retire, engine)
            stats = await engine.run_weekly_full_sync(entity_types=["accounts"])

    # Weekly tick must perform a full pull — no modifiedon gt filter.
    account_calls = [c for c in client.calls if c["entity_set"] == "accounts"]
    assert account_calls, "accounts entity-set was not paginated in weekly tick"
    weekly_filter = account_calls[0]["filter_expr"]
    assert weekly_filter is None or "modifiedon gt" not in (weekly_filter or ""), (
        f"Weekly tick must NOT apply modifiedon gt filter; got: {weekly_filter!r}"
    )

    assert retire_calls, "detect_and_retire_deletions was not called by weekly tick"

    # Removed entity must be gone.
    async with pool.acquire() as check_conn:
        count = await check_conn.fetchval(
            "SELECT count(*) FROM kg_nodes WHERE namespace_id = $1::uuid AND d365_source_id = $2",
            str(ns_id),
            removed_source_id,
        )
    assert count == 0, f"Removed entity d365_source_id={removed_source_id!r} still in kg_nodes"

    # Cursor must be re-seeded from the surviving record's modifiedon.
    async with pool.acquire() as check_conn:
        row = await check_conn.fetchrow(
            "SELECT last_sync_stats FROM d365_integrations "
            "WHERE namespace_id = $1::uuid AND status = 'ACTIVE'",
            str(ns_id),
        )
    cursors = _parse_stats(row).get("cursors", {})
    assert cursors.get("accounts") == surviving_ts, (
        f"Weekly tick must re-seed cursor; cursors={cursors}"
    )

    assert stats["mode"] == "weekly_full", f"Unexpected mode in stats: {stats['mode']!r}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_cursor(pool: asyncpg.Pool, ns_id: uuid.UUID, cursors: dict[str, str]) -> None:
    """Write cursor map into d365_integrations.last_sync_stats directly."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE d365_integrations
            SET last_sync_stats = COALESCE(last_sync_stats, '{}'::jsonb)
                                  || jsonb_build_object('cursors', $1::jsonb),
                updated_at = NOW()
            WHERE namespace_id = $2::uuid AND status = 'ACTIVE'
            """,
            json.dumps(cursors),
            str(ns_id),
        )


def _parse_stats(row: asyncpg.Record | None) -> dict[str, Any]:
    if row is None:
        return {}
    raw = row["last_sync_stats"]
    if raw is None:
        return {}
    return raw if isinstance(raw, dict) else json.loads(raw)
