"""Tests for the Inventory engine's locations-stock-tables schema
(Module 11, Wave 1 — Batch 129).

Validates the Acceptance criteria from
``vertical_modules/dev/prompts/Batch_129_Module_11_Wave_1.md``:

  1. ``stock_locations`` and ``inventory_items`` both exist and are
     FORCE-RLS isolated, proven via a real ``nce_app`` connection
     (``pg_app_conn``) — an owner-connection test would prove nothing
     (Batch 32's lesson).
  2. ``seed_warehouse_and_vans`` creates one warehouse + N vans, and is
     idempotent (a second call creates no duplicate rows).
  3. ``STOCK_LOCATION`` is NOT conflated with the customer-site
     ``FUNCTIONAL_LOCATION`` tree — ``stock_locations`` carries no
     ``functional_location`` column, and seeding never touches ``kg_nodes``
     at all (this wave is table-only, no graph writes).

Also pins the structural, load-bearing claims migration 050's docstrings
make ("never", "always", "cannot"):
  - ``stock_locations_hierarchy_shape``: a van can never carry a parent, and
    a zone/bin can never float parentless.
  - the composite self-referencing ``parent_id`` FK: a hierarchy edge can
    never cross a tenant/namespace boundary.
  - ``inventory_items_location_fk``: a stock row can never reference another
    tenant's location.
  - the partial unique index backing the seed's idempotency: a duplicate
    top-level ``(namespace, kind, name)`` insert is refused at the DB level,
    not just avoided by the seed helper's own care.

Integration tests are ``@pytest.mark.integration`` — require a live Postgres
with migration 050 applied. Pure-logic tests for the seed's input validation
sit alongside them and need no DB.
"""

from __future__ import annotations

import uuid
from typing import Any

import asyncpg  # type: ignore[import-untyped]
import pytest

from nce.auth import set_namespace_context
from nce.vertical_modules.inventory.schema_seed import (
    DEFAULT_VAN_COUNT,
    seed_warehouse_and_vans,
)

# ---------------------------------------------------------------------------
# Pure-logic tests: seed_warehouse_and_vans's input validation (no DB)
# ---------------------------------------------------------------------------


class _DummyEngine:
    """Stands in for NCEEngine in tests that never reach a DB call — the
    validation under test raises before ``engine.pg_pool`` is ever touched."""

    pg_pool = None


@pytest.mark.asyncio
async def test_seed_refuses_negative_van_count() -> None:
    with pytest.raises(ValueError, match="must be >= 0"):
        await seed_warehouse_and_vans(_DummyEngine(), uuid.uuid4(), van_count=-1)


@pytest.mark.asyncio
async def test_seed_refuses_bool_van_count() -> None:
    """``isinstance(True, int)`` is ``True`` in Python — a bool van_count
    must not silently pass as 0/1."""
    with pytest.raises(ValueError, match="must be a non-negative int"):
        await seed_warehouse_and_vans(_DummyEngine(), uuid.uuid4(), van_count=True)


@pytest.mark.asyncio
async def test_seed_refuses_non_int_van_count() -> None:
    with pytest.raises(ValueError, match="must be a non-negative int"):
        await seed_warehouse_and_vans(_DummyEngine(), uuid.uuid4(), van_count=2.5)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Integration test helpers
# ---------------------------------------------------------------------------


def _make_engine_stub(pg_pool: asyncpg.Pool) -> Any:  # type: ignore[type-arg]
    class _EngineStub:
        pass

    stub = _EngineStub()
    stub.pg_pool = pg_pool  # type: ignore[attr-defined]
    return stub


async def _column_names(pg_pool: asyncpg.Pool, table_name: str) -> set[str]:  # type: ignore[type-arg]
    async with pg_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_name = $1",
            table_name,
        )
    return {r["column_name"] for r in rows}


# ---------------------------------------------------------------------------
# 1. Tables exist with the expected shape
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_stock_locations_table_exists_with_expected_columns(
    pg_pool: asyncpg.Pool,
) -> None:
    columns = await _column_names(pg_pool, "stock_locations")
    assert {
        "id",
        "namespace_id",
        "kind",
        "name",
        "parent_id",
        "level",
        "vehicle_ref",
        "raw",
    } <= columns


@pytest.mark.integration
@pytest.mark.asyncio
async def test_inventory_items_table_exists_with_expected_columns(
    pg_pool: asyncpg.Pool,
) -> None:
    columns = await _column_names(pg_pool, "inventory_items")
    assert {
        "id",
        "namespace_id",
        "sku",
        "location_id",
        "qty_on_hand",
        "qty_reserved",
        "qty_blocked",
        "reorder_point",
    } <= columns


# ---------------------------------------------------------------------------
# 2. STOCK_LOCATION is not conflated with FUNCTIONAL_LOCATION
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_stock_locations_table_has_no_functional_location_column(
    pg_pool: asyncpg.Pool,
) -> None:
    """Pins docs' hardening #1: a warehouse/van is an internal LOGISTICS
    location, a different ontology from the customer-site
    FUNCTIONAL_LOCATION tree — this table must never grow a
    functional_location_id (or similarly-named) column."""
    columns = await _column_names(pg_pool, "stock_locations")
    assert not any("functional_location" in c for c in columns), (
        f"stock_locations must never carry a functional_location column — found: {columns}"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_seed_never_touches_kg_nodes(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """This wave is table-only — seeding a warehouse+vans must not create any
    graph rows (STOCK_LOCATION is not mirrored into kg_nodes yet, and
    certainly never as FUNCTIONAL_LOCATION)."""
    engine = _make_engine_stub(pg_pool)

    async with pg_pool.acquire() as conn:
        before = await conn.fetchval(
            "SELECT COUNT(*) FROM kg_nodes WHERE namespace_id = $1", namespace_id
        )

    await seed_warehouse_and_vans(engine, namespace_id, van_count=2)

    async with pg_pool.acquire() as conn:
        after = await conn.fetchval(
            "SELECT COUNT(*) FROM kg_nodes WHERE namespace_id = $1", namespace_id
        )
    assert after == before == 0


# ---------------------------------------------------------------------------
# 3. seed_warehouse_and_vans — creates one warehouse + N vans, idempotently
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_seed_creates_one_warehouse_and_default_van_count(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    engine = _make_engine_stub(pg_pool)

    result = await seed_warehouse_and_vans(engine, namespace_id)

    assert result["ok"] is True
    assert result["warehouse"]["created"] is True
    assert result["van_count"] == DEFAULT_VAN_COUNT
    assert all(v["created"] is True for v in result["vans"])

    async with pg_pool.acquire() as conn:
        warehouse_count = await conn.fetchval(
            "SELECT COUNT(*) FROM stock_locations WHERE namespace_id = $1 AND kind = 'warehouse'",
            namespace_id,
        )
        van_count = await conn.fetchval(
            "SELECT COUNT(*) FROM stock_locations WHERE namespace_id = $1 AND kind = 'van'",
            namespace_id,
        )
    assert warehouse_count == 1
    assert van_count == DEFAULT_VAN_COUNT


@pytest.mark.integration
@pytest.mark.asyncio
async def test_seed_vans_are_flat_top_level_not_nested_under_warehouse(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    engine = _make_engine_stub(pg_pool)
    await seed_warehouse_and_vans(engine, namespace_id, van_count=3)

    async with pg_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT kind, parent_id, level FROM stock_locations "
            "WHERE namespace_id = $1 AND kind IN ('warehouse', 'van')",
            namespace_id,
        )
    assert len(rows) == 4  # 1 warehouse + 3 vans
    for row in rows:
        assert row["parent_id"] is None, f"{row['kind']} must be flat top-level"
        assert row["level"] == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_seed_is_idempotent_second_call_creates_no_duplicate_rows(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    engine = _make_engine_stub(pg_pool)

    first = await seed_warehouse_and_vans(engine, namespace_id, van_count=4)
    second = await seed_warehouse_and_vans(engine, namespace_id, van_count=4)

    assert second["warehouse"]["created"] is False
    assert second["warehouse"]["id"] == first["warehouse"]["id"]
    assert all(v["created"] is False for v in second["vans"])
    assert {v["id"] for v in second["vans"]} == {v["id"] for v in first["vans"]}

    async with pg_pool.acquire() as conn:
        total = await conn.fetchval(
            "SELECT COUNT(*) FROM stock_locations WHERE namespace_id = $1", namespace_id
        )
    assert total == 5  # 1 warehouse + 4 vans, not 10


@pytest.mark.integration
@pytest.mark.asyncio
async def test_seed_with_zero_van_count_creates_only_the_warehouse(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    engine = _make_engine_stub(pg_pool)
    result = await seed_warehouse_and_vans(engine, namespace_id, van_count=0)
    assert result["van_count"] == 0
    assert result["vans"] == []


# ---------------------------------------------------------------------------
# 4. Structural hierarchy shape — mutation-verified claims
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_van_cannot_be_given_a_parent(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """Pins stock_locations_hierarchy_shape: a van must be flat top-level —
    inserting one with a parent_id is refused at the DB level."""
    engine = _make_engine_stub(pg_pool)
    seeded = await seed_warehouse_and_vans(engine, namespace_id, van_count=0)
    warehouse_id = seeded["warehouse"]["id"]

    async with pg_pool.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO stock_locations "
                "(namespace_id, kind, name, parent_id, level) "
                "VALUES ($1, 'van', 'Rogue Van', $2::uuid, 1)",
                namespace_id,
                warehouse_id,
            )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_zone_cannot_float_without_a_parent(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """Pins the other half of stock_locations_hierarchy_shape: a zone/bin
    must have a parent — a parentless zone is refused."""
    async with pg_pool.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO stock_locations "
                "(namespace_id, kind, name, parent_id, level) "
                "VALUES ($1, 'zone', 'Floating Zone', NULL, 1)",
                namespace_id,
            )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_zone_nested_under_a_warehouse_is_accepted(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """The positive case for the same CHECK: a properly-parented zone
    (level > 0, parent_id set) is valid — the constraint enforces shape, not
    a blanket refusal of all hierarchy."""
    engine = _make_engine_stub(pg_pool)
    seeded = await seed_warehouse_and_vans(engine, namespace_id, van_count=0)
    warehouse_id = seeded["warehouse"]["id"]

    async with pg_pool.acquire() as conn:
        zone_id = await conn.fetchval(
            "INSERT INTO stock_locations "
            "(namespace_id, kind, name, parent_id, level) "
            "VALUES ($1, 'zone', 'Zone A', $2::uuid, 1) RETURNING id",
            namespace_id,
            warehouse_id,
        )
    assert zone_id is not None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_parent_id_cannot_cross_a_namespace_boundary(
    pg_pool: asyncpg.Pool,
    make_namespace: Any,
) -> None:
    """Pins the composite self-referencing FK: a hierarchy edge can never
    cross a tenant boundary — a zone in ns_b cannot claim a warehouse from
    ns_a as its parent, even though the warehouse id is a real row."""
    ns_a = await make_namespace()
    ns_b = await make_namespace()
    engine = _make_engine_stub(pg_pool)
    seeded = await seed_warehouse_and_vans(engine, ns_a, van_count=0)
    warehouse_in_a = seeded["warehouse"]["id"]

    async with pg_pool.acquire() as conn:
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await conn.execute(
                "INSERT INTO stock_locations "
                "(namespace_id, kind, name, parent_id, level) "
                "VALUES ($1, 'zone', 'Cross-Tenant Zone', $2::uuid, 1)",
                ns_b,
                warehouse_in_a,
            )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_inventory_item_location_cannot_cross_a_namespace_boundary(
    pg_pool: asyncpg.Pool,
    make_namespace: Any,
) -> None:
    """Pins inventory_items_location_fk: a stock row can never reference
    another tenant's location."""
    ns_a = await make_namespace()
    ns_b = await make_namespace()
    engine = _make_engine_stub(pg_pool)
    seeded = await seed_warehouse_and_vans(engine, ns_a, van_count=0)
    warehouse_in_a = seeded["warehouse"]["id"]

    async with pg_pool.acquire() as conn:
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await conn.execute(
                "INSERT INTO inventory_items "
                "(namespace_id, sku, location_id) VALUES ($1, 'SKU-1', $2::uuid)",
                ns_b,
                warehouse_in_a,
            )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_duplicate_top_level_name_is_refused_at_the_db_level(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """Pins the partial unique index backing the seed's idempotency: a
    second warehouse (or van) with the SAME name is refused by the database
    itself, not merely avoided by the seed helper's own get-or-create care."""
    engine = _make_engine_stub(pg_pool)
    await seed_warehouse_and_vans(engine, namespace_id, van_count=0)

    async with pg_pool.acquire() as conn:
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                "INSERT INTO stock_locations "
                "(namespace_id, kind, name, parent_id, level) "
                "VALUES ($1, 'warehouse', 'Main Warehouse', NULL, 0)",
                namespace_id,
            )


# ---------------------------------------------------------------------------
# 5. FORCE RLS isolates both tables per tenant (Acceptance #1)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rls_isolates_stock_locations_between_namespaces(
    pg_pool: asyncpg.Pool,
    pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
    make_namespace: Any,
) -> None:
    """Uses pg_app_conn (the nce_app role), not the superuser pool — an
    owner-connection test would prove nothing against FORCE RLS
    (Batch 32's false-confidence lesson)."""
    ns_a = await make_namespace()
    ns_b = await make_namespace()
    engine = _make_engine_stub(pg_pool)
    await seed_warehouse_and_vans(engine, ns_a, van_count=1)

    async with pg_app_conn.transaction():
        await set_namespace_context(pg_app_conn, ns_b)
        visible_from_b = await pg_app_conn.fetchval(
            "SELECT COUNT(*) FROM stock_locations WHERE namespace_id = $1", ns_a
        )
    assert visible_from_b == 0, "ns_b must not see ns_a's stock_locations rows"

    async with pg_app_conn.transaction():
        await set_namespace_context(pg_app_conn, ns_a)
        visible_from_a = await pg_app_conn.fetchval(
            "SELECT COUNT(*) FROM stock_locations WHERE namespace_id = $1", ns_a
        )
    assert visible_from_a == 2, "ns_a must see its own rows (1 warehouse + 1 van)"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rls_isolates_inventory_items_between_namespaces(
    pg_pool: asyncpg.Pool,
    pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
    make_namespace: Any,
) -> None:
    ns_a = await make_namespace()
    ns_b = await make_namespace()
    engine = _make_engine_stub(pg_pool)
    seeded = await seed_warehouse_and_vans(engine, ns_a, van_count=0)
    warehouse_in_a = seeded["warehouse"]["id"]

    async with pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO inventory_items (namespace_id, sku, location_id, qty_on_hand) "
            "VALUES ($1, 'SKU-RLS-1', $2::uuid, 10)",
            ns_a,
            warehouse_in_a,
        )

    async with pg_app_conn.transaction():
        await set_namespace_context(pg_app_conn, ns_b)
        visible_from_b = await pg_app_conn.fetchval(
            "SELECT COUNT(*) FROM inventory_items WHERE namespace_id = $1", ns_a
        )
    assert visible_from_b == 0, "ns_b must not see ns_a's inventory_items rows"

    async with pg_app_conn.transaction():
        await set_namespace_context(pg_app_conn, ns_a)
        visible_from_a = await pg_app_conn.fetchval(
            "SELECT COUNT(*) FROM inventory_items WHERE namespace_id = $1", ns_a
        )
    assert visible_from_a == 1, "ns_a must see its own inventory_items row"
