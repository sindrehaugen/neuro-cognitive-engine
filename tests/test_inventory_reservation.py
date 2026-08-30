"""Tests for the Inventory engine's reservation primitive (Module 11, Wave 8
— Batch 136, RE-SCOPED — ``nce/vertical_modules/inventory/reservation.py``).

Scope note: this wave is the reservation primitive ONLY (``do_reserve_stock``
/ ``do_release_stock``, moving ``qty_reserved``) — phantom-BOM kitting is
explicitly out of scope (Batch 136b) and nothing here exercises or assumes
any kit representation.

Covers, per ``do_reserve_stock`` / ``do_release_stock``:

  1. Input validation (bool/None/NaN/non-positive quantities, malformed
     ``project_id``) — pure logic, no DB, exercised through the PUBLIC
     functions (never a reimplementation of the coercion helpers' logic).
  2. The reservation algebra: reserving moves ``available`` (via
     ``qty_reserved``), never ``qty_on_hand`` — the physical count is
     untouched.
  3. The FULL three-term guard, not a shortcut: an item with existing
     ``qty_reserved``/``qty_blocked`` must refuse a reservation that would
     push reservations past ``on_hand - reserved - blocked``, even though
     ``qty_on_hand`` alone would appear to have room. This is the
     discriminating case a shortcut guard (``WHERE qty_on_hand >= n``,
     dropping the ``- reserved - blocked`` terms) would pass incorrectly.
  4. The oversell/lost-update guard under REAL concurrent connections
     (``asyncio.gather`` over separate pool connections, not sequential
     calls — sequential calls cannot distinguish an atomic guard from a
     racy read-then-check), for BOTH reserve and release.
  5. ``do_release_stock``'s own guard: cannot release more than is
     currently reserved.
  6. The ``INVENTORY_ITEM -[reserved_for]-> PROJECT`` edge: written by
     reserve, upserted (idempotent) on a repeat reservation, and left
     UNTOUCHED by release (module's documented "Honest scope limits").
  7. Namespace isolation via a real FORCE RLS proof through a genuine
     ``nce_app`` pool — never the superuser ``pg_pool`` fixture, which
     bypasses FORCE RLS entirely. (Unlike ``do_stock_levels``, every
     reservation call requires an explicit ``location`` — a globally-unique
     ``stock_locations.id`` — so a same-sku-different-namespace WHERE-clause
     test would pass even with the query's own ``namespace_id`` filter
     removed, proving nothing; RLS is the real, load-bearing proof here.)
  8. Decimal quantization to the column's own 3dp scale, using values where
     ``Decimal(str(x))`` and ``Decimal(x)`` DIVERGE at 3dp (same divergent
     floats ``test_inventory_stock.py`` uses, so this suite inherits that
     already-proven discriminating power rather than picking new floats that
     might not diverge).
  9. No ledger row: reserving/releasing never appends to
     ``inventory_transactions`` (the module's explicit "not a ledger
     movement" decision).

Integration tests are ``@pytest.mark.integration`` — require a live Postgres
with migration 050 applied, and are wired into ci.yml's "Integration — M11
Inventory" step alongside the other Inventory integration suites. Pure-logic
validation tests need no DB and always run in the unit job.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse, urlunparse

import asyncpg  # type: ignore[import-untyped]
import pytest

from nce.auth import set_namespace_context
from nce.config import cfg
from nce.vertical_modules.inventory.reservation import (
    InsufficientAvailableError,
    OverReleaseError,
    do_release_stock,
    do_reserve_stock,
)

_PROJECT_A = "PROJECT:QUOTE-RESERVE-A"
_PROJECT_B = "PROJECT:QUOTE-RESERVE-B"

# ---------------------------------------------------------------------------
# Pure-logic tests: quantity/project_id validation, exercised through the
# PUBLIC functions — mirrors test_inventory_stock.py's own convention.
#
# Validation raises before engine.pg_pool is ever touched, so a dummy engine
# with pg_pool=None is safe here.
# ---------------------------------------------------------------------------


class _DummyEngine:
    pg_pool = None


def _base_params(**overrides: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "namespace_id": uuid.uuid4(),
        "sku": "SKU-VALIDATE",
        "qty": 1,
        "location": uuid.uuid4(),
        "project_id": _PROJECT_A,
    }
    params.update(overrides)
    return params


@pytest.mark.asyncio
async def test_reserve_rejects_bool_qty() -> None:
    """``isinstance(True, int)`` is ``True`` in Python — a bool qty must not
    silently pass as a quantity of 1."""
    with pytest.raises(ValueError, match="bool is not a quantity"):
        await do_reserve_stock(_DummyEngine(), _base_params(qty=True))


@pytest.mark.asyncio
async def test_release_rejects_bool_qty() -> None:
    with pytest.raises(ValueError, match="bool is not a quantity"):
        await do_release_stock(_DummyEngine(), _base_params(qty=False))


@pytest.mark.asyncio
async def test_reserve_rejects_none_qty() -> None:
    params = _base_params()
    params["qty"] = None
    with pytest.raises(ValueError, match="a quantity is required"):
        await do_reserve_stock(_DummyEngine(), params)


@pytest.mark.asyncio
async def test_reserve_rejects_nan_qty() -> None:
    with pytest.raises(ValueError, match="must be finite"):
        await do_reserve_stock(_DummyEngine(), _base_params(qty=float("nan")))


@pytest.mark.asyncio
async def test_reserve_rejects_infinite_qty() -> None:
    with pytest.raises(ValueError, match="must be finite"):
        await do_reserve_stock(_DummyEngine(), _base_params(qty=float("inf")))


@pytest.mark.asyncio
async def test_reserve_rejects_zero_qty() -> None:
    with pytest.raises(ValueError, match=r"must be > 0"):
        await do_reserve_stock(_DummyEngine(), _base_params(qty=0))


@pytest.mark.asyncio
async def test_reserve_rejects_negative_qty() -> None:
    with pytest.raises(ValueError, match=r"must be > 0"):
        await do_reserve_stock(_DummyEngine(), _base_params(qty=-1))


@pytest.mark.asyncio
async def test_reserve_rejects_missing_project_id() -> None:
    params = _base_params()
    params["project_id"] = None
    with pytest.raises(ValueError, match="'project_id' is required"):
        await do_reserve_stock(_DummyEngine(), params)


@pytest.mark.asyncio
async def test_reserve_rejects_project_id_without_prefix() -> None:
    with pytest.raises(ValueError, match="must be a PROJECT label"):
        await do_reserve_stock(_DummyEngine(), _base_params(project_id="QUOTE-001"))


@pytest.mark.asyncio
async def test_reserve_rejects_project_id_prefix_only() -> None:
    with pytest.raises(ValueError, match="must include a quote id"):
        await do_reserve_stock(_DummyEngine(), _base_params(project_id="PROJECT:"))


@pytest.mark.asyncio
async def test_release_rejects_project_id_without_prefix() -> None:
    with pytest.raises(ValueError, match="must be a PROJECT label"):
        await do_release_stock(_DummyEngine(), _base_params(project_id="not-a-label"))


# ---------------------------------------------------------------------------
# Integration helpers — seed stock_locations / inventory_items rows directly
# (owner pool), matching test_inventory_stock.py's convention.
# ---------------------------------------------------------------------------


class _EngineStub:
    def __init__(self, pg_pool: asyncpg.Pool) -> None:  # type: ignore[type-arg]
        self.pg_pool = pg_pool


def _app_dsn() -> str:
    """Rewrite the integration DSN onto the restricted ``nce_app`` role.

    Verbatim in shape from ``tests/test_inventory_stock.py``'s own
    ``_app_dsn`` (itself verbatim from ``tests/test_agreements_review.py``).
    """
    primary = (
        os.environ.get("NCE_INTEGRATION_PG_DSN")
        or os.environ.get("PG_DSN")
        or os.environ.get("DATABASE_URL")
        or cfg.PG_DSN
    )
    parsed = urlparse(primary)
    netloc = parsed.hostname or ""
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    app_pass = cfg.NCE_APP_PASSWORD or "nce_app_secret"
    netloc = f"nce_app:{app_pass}@{netloc}"
    return urlunparse(parsed._replace(netloc=netloc))


async def _seed_location(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    name: str,
    kind: str = "warehouse",
) -> uuid.UUID:
    async with pg_pool.acquire() as conn:
        location_id = await conn.fetchval(
            "INSERT INTO stock_locations (namespace_id, kind, name, parent_id, level) "
            "VALUES ($1, $2, $3, NULL, 0) RETURNING id",
            namespace_id,
            kind,
            name,
        )
    assert location_id is not None
    return location_id


async def _seed_item(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    sku: str,
    location_id: uuid.UUID,
    on_hand: str,
    reserved: str = "0",
    blocked: str = "0",
) -> None:
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO inventory_items "
            "(namespace_id, sku, location_id, qty_on_hand, qty_reserved, qty_blocked) "
            "VALUES ($1, $2, $3, $4, $5, $6)",
            namespace_id,
            sku,
            location_id,
            Decimal(on_hand),
            Decimal(reserved),
            Decimal(blocked),
        )


async def _get_item_row(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    sku: str,
    location_id: uuid.UUID,
) -> dict[str, Decimal]:
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT qty_on_hand, qty_reserved, qty_blocked FROM inventory_items "
            "WHERE namespace_id = $1 AND sku = $2 AND location_id = $3",
            namespace_id,
            sku,
            location_id,
        )
    assert row is not None
    return {
        "on_hand": row["qty_on_hand"],
        "reserved": row["qty_reserved"],
        "blocked": row["qty_blocked"],
    }


async def _fetch_reserved_for_edges(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> list[dict[str, Any]]:
    async with pg_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT subject_label, predicate, object_label, confidence, change_origin "
            "FROM kg_edges WHERE namespace_id = $1 AND predicate = 'reserved_for'",
            namespace_id,
        )
    return [dict(r) for r in rows]


async def _ledger_row_count(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    sku: str,
) -> int:
    async with pg_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM inventory_transactions WHERE namespace_id = $1 AND sku = $2",
            namespace_id,
            sku,
        )
    return int(count)


# ---------------------------------------------------------------------------
# 1. do_reserve_stock — the reservation algebra
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reserve_moves_available_never_on_hand(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """Reserving 4 of 10 on_hand: on_hand stays 10 (no physical stock moves),
    qty_reserved becomes 4, available becomes 6."""
    loc = await _seed_location(pg_pool, namespace_id, "Warehouse")
    await _seed_item(pg_pool, namespace_id, "SKU-RESERVE-BASIC", loc, on_hand="10")
    engine = _EngineStub(pg_pool)

    result = await do_reserve_stock(
        engine,
        {
            "namespace_id": namespace_id,
            "sku": "SKU-RESERVE-BASIC",
            "qty": 4,
            "location": loc,
            "project_id": _PROJECT_A,
        },
    )

    assert result["ok"] is True
    assert result["on_hand"] == Decimal("10.000"), "reserving must NEVER move on_hand"
    assert result["reserved"] == Decimal("4.000")
    assert result["blocked"] == Decimal("0.000")
    assert result["available"] == Decimal("6.000")

    row = await _get_item_row(pg_pool, namespace_id, "SKU-RESERVE-BASIC", loc)
    assert row["on_hand"] == Decimal("10.000")
    assert row["reserved"] == Decimal("4.000")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reserve_refuses_using_full_three_term_identity_not_on_hand_shortcut(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """The discriminating case: on_hand=10 looks like there is plenty of
    room, but reserved=6 and blocked=3 already consume all but 1 unit of
    `available`. A guard that checks ONLY `qty_on_hand >= n` (dropping the
    `- reserved - blocked` terms) would wrongly ALLOW a reservation of 5
    here; the real guard must refuse anything over 1."""
    loc = await _seed_location(pg_pool, namespace_id, "Warehouse")
    await _seed_item(
        pg_pool,
        namespace_id,
        "SKU-RESERVE-TIGHT",
        loc,
        on_hand="10",
        reserved="6",
        blocked="3",
    )
    engine = _EngineStub(pg_pool)

    # Exactly the available amount (1) succeeds.
    ok = await do_reserve_stock(
        engine,
        {
            "namespace_id": namespace_id,
            "sku": "SKU-RESERVE-TIGHT",
            "qty": 1,
            "location": loc,
            "project_id": _PROJECT_A,
        },
    )
    assert ok["available"] == Decimal("0.000")
    assert ok["reserved"] == Decimal("7.000")

    # Now available is 0 — even a tiny further reservation must be refused,
    # despite on_hand (10) - reserved (7) = 3 looking like room exists if
    # blocked were (wrongly) ignored.
    with pytest.raises(InsufficientAvailableError) as excinfo:
        await do_reserve_stock(
            engine,
            {
                "namespace_id": namespace_id,
                "sku": "SKU-RESERVE-TIGHT",
                "qty": 1,
                "location": loc,
                "project_id": _PROJECT_A,
            },
        )
    assert excinfo.value.available == Decimal("0.000")
    assert excinfo.value.on_hand == Decimal("10.000")
    assert excinfo.value.reserved == Decimal("7.000")
    assert excinfo.value.blocked == Decimal("3.000")

    row = await _get_item_row(pg_pool, namespace_id, "SKU-RESERVE-TIGHT", loc)
    assert row["reserved"] == Decimal("7.000"), "the refused call must not have changed the row"
    assert row["on_hand"] == Decimal("10.000")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reserve_against_nonexistent_row_treated_as_zero_available(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    loc = await _seed_location(pg_pool, namespace_id, "Warehouse")
    engine = _EngineStub(pg_pool)

    with pytest.raises(InsufficientAvailableError) as excinfo:
        await do_reserve_stock(
            engine,
            {
                "namespace_id": namespace_id,
                "sku": "SKU-NEVER-STOCKED",
                "qty": 1,
                "location": loc,
                "project_id": _PROJECT_A,
            },
        )
    assert excinfo.value.available == Decimal("0.000")
    assert excinfo.value.on_hand == Decimal("0.000")


# ---------------------------------------------------------------------------
# 2. do_release_stock
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_release_moves_available_never_on_hand_and_refuses_over_release(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    loc = await _seed_location(pg_pool, namespace_id, "Warehouse")
    await _seed_item(pg_pool, namespace_id, "SKU-RELEASE", loc, on_hand="10", reserved="6")
    engine = _EngineStub(pg_pool)

    result = await do_release_stock(
        engine,
        {
            "namespace_id": namespace_id,
            "sku": "SKU-RELEASE",
            "qty": 4,
            "location": loc,
            "project_id": _PROJECT_A,
        },
    )
    assert result["ok"] is True
    assert result["on_hand"] == Decimal("10.000"), "releasing must NEVER move on_hand"
    assert result["reserved"] == Decimal("2.000")
    assert result["available"] == Decimal("8.000")

    with pytest.raises(OverReleaseError) as excinfo:
        await do_release_stock(
            engine,
            {
                "namespace_id": namespace_id,
                "sku": "SKU-RELEASE",
                "qty": 100,
                "location": loc,
                "project_id": _PROJECT_A,
            },
        )
    assert excinfo.value.currently_reserved == Decimal("2.000")
    assert excinfo.value.requested == Decimal("100.000")

    row = await _get_item_row(pg_pool, namespace_id, "SKU-RELEASE", loc)
    assert row["reserved"] == Decimal("2.000"), "a refused release must not touch the row"
    assert row["on_hand"] == Decimal("10.000")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_release_against_nonexistent_row_treated_as_zero_reserved(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    loc = await _seed_location(pg_pool, namespace_id, "Warehouse")
    engine = _EngineStub(pg_pool)

    with pytest.raises(OverReleaseError) as excinfo:
        await do_release_stock(
            engine,
            {
                "namespace_id": namespace_id,
                "sku": "SKU-NEVER-RESERVED",
                "qty": 1,
                "location": loc,
                "project_id": _PROJECT_A,
            },
        )
    assert excinfo.value.currently_reserved == Decimal("0.000")


# ---------------------------------------------------------------------------
# 3. Graph mirror — reserved_for edge
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reserve_upserts_reserved_for_edge_idempotently(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    loc = await _seed_location(pg_pool, namespace_id, "Warehouse")
    await _seed_item(pg_pool, namespace_id, "SKU-EDGE", loc, on_hand="10")
    engine = _EngineStub(pg_pool)

    await do_reserve_stock(
        engine,
        {
            "namespace_id": namespace_id,
            "sku": "SKU-EDGE",
            "qty": 2,
            "location": loc,
            "project_id": _PROJECT_A,
        },
    )
    edges = await _fetch_reserved_for_edges(pg_pool, namespace_id)
    assert len(edges) == 1
    edge = edges[0]
    assert edge["subject_label"] == f"InventoryItem:SKU-EDGE:{loc}"
    assert edge["predicate"] == "reserved_for"
    assert edge["object_label"] == _PROJECT_A
    assert float(edge["confidence"]) == 1.0
    assert edge["change_origin"] == "agent"

    # A second reservation for the SAME project upserts (still one row), not
    # a duplicate — ON CONFLICT (subject, predicate, object, namespace) DO
    # UPDATE, matching the natural key.
    await do_reserve_stock(
        engine,
        {
            "namespace_id": namespace_id,
            "sku": "SKU-EDGE",
            "qty": 1,
            "location": loc,
            "project_id": _PROJECT_A,
        },
    )
    edges_after = await _fetch_reserved_for_edges(pg_pool, namespace_id)
    assert len(edges_after) == 1, "the second reservation must upsert, not duplicate, the edge"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_two_projects_reserving_same_row_each_get_their_own_edge(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """Pins the module's own documented honest scope limit: qty_reserved is
    a single AGGREGATE column, so two different projects reserving against
    the same (sku, location) both contribute to the SAME counter, but each
    gets its own reserved_for edge (different object_label)."""
    loc = await _seed_location(pg_pool, namespace_id, "Warehouse")
    await _seed_item(pg_pool, namespace_id, "SKU-MULTI-PROJECT", loc, on_hand="10")
    engine = _EngineStub(pg_pool)

    await do_reserve_stock(
        engine,
        {
            "namespace_id": namespace_id,
            "sku": "SKU-MULTI-PROJECT",
            "qty": 3,
            "location": loc,
            "project_id": _PROJECT_A,
        },
    )
    result_b = await do_reserve_stock(
        engine,
        {
            "namespace_id": namespace_id,
            "sku": "SKU-MULTI-PROJECT",
            "qty": 2,
            "location": loc,
            "project_id": _PROJECT_B,
        },
    )
    assert result_b["reserved"] == Decimal("5.000"), "both projects share ONE aggregate counter"

    edges = await _fetch_reserved_for_edges(pg_pool, namespace_id)
    object_labels = {e["object_label"] for e in edges}
    assert object_labels == {_PROJECT_A, _PROJECT_B}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_release_does_not_touch_the_reserved_for_edge(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """Module's documented decision: the edge is a historical/audit
    association, never deleted or downgraded by release."""
    loc = await _seed_location(pg_pool, namespace_id, "Warehouse")
    await _seed_item(pg_pool, namespace_id, "SKU-EDGE-PERSIST", loc, on_hand="10", reserved="5")
    engine = _EngineStub(pg_pool)

    await do_reserve_stock(
        engine,
        {
            "namespace_id": namespace_id,
            "sku": "SKU-EDGE-PERSIST",
            "qty": 1,
            "location": loc,
            "project_id": _PROJECT_A,
        },
    )
    edges_before = await _fetch_reserved_for_edges(pg_pool, namespace_id)
    assert len(edges_before) == 1

    await do_release_stock(
        engine,
        {
            "namespace_id": namespace_id,
            "sku": "SKU-EDGE-PERSIST",
            "qty": 6,
            "location": loc,
            "project_id": _PROJECT_A,
        },
    )
    edges_after = await _fetch_reserved_for_edges(pg_pool, namespace_id)
    assert edges_after == edges_before, "release must not delete or modify the reserved_for edge"


# ---------------------------------------------------------------------------
# 4. No ledger row — the module's explicit "not a ledger movement" decision.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reserve_and_release_append_no_inventory_transactions_row(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    loc = await _seed_location(pg_pool, namespace_id, "Warehouse")
    await _seed_item(pg_pool, namespace_id, "SKU-NO-LEDGER", loc, on_hand="10")
    engine = _EngineStub(pg_pool)

    await do_reserve_stock(
        engine,
        {
            "namespace_id": namespace_id,
            "sku": "SKU-NO-LEDGER",
            "qty": 4,
            "location": loc,
            "project_id": _PROJECT_A,
        },
    )
    await do_release_stock(
        engine,
        {
            "namespace_id": namespace_id,
            "sku": "SKU-NO-LEDGER",
            "qty": 4,
            "location": loc,
            "project_id": _PROJECT_A,
        },
    )

    assert await _ledger_row_count(pg_pool, namespace_id, "SKU-NO-LEDGER") == 0, (
        "a reservation moves no physical stock — it must never append to inventory_transactions"
    )


# ---------------------------------------------------------------------------
# 5. Decimal quantization — same divergent floats test_inventory_stock.py uses.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reserve_qty_is_quantised_to_3dp_before_binding(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """Round-trips floats through the real NUMERIC(18,3) column, reusing the
    two divergent values test_inventory_stock.py's own quantization test
    established (their nearest binary double sits just below the decimal
    tie ROUND_HALF_UP would round up, so Decimal(str(x)) and Decimal(x)
    disagree at 3dp)."""
    loc = await _seed_location(pg_pool, namespace_id, "Warehouse")
    await _seed_item(pg_pool, namespace_id, "SKU-RESERVE-QUANT", loc, on_hand="10")
    engine = _EngineStub(pg_pool)

    async def _reserve(qty: float) -> dict[str, Any]:
        return await do_reserve_stock(
            engine,
            {
                "namespace_id": namespace_id,
                "sku": "SKU-RESERVE-QUANT",
                "qty": qty,
                "location": loc,
                "project_id": _PROJECT_A,
            },
        )

    # Divergent #1: Decimal(x) would yield 0.004 here, one tick short.
    result = await _reserve(0.0045)
    assert result["reserved"] == Decimal("0.005"), (
        "0.0045 must quantise via Decimal(str(x)) to 0.005; 0.004 means the "
        "raw binary-float value was quantised instead"
    )

    # Divergent #2: Decimal(x) would yield 2.675 here (cumulative would be
    # 0.005 + 2.675 = 2.680 instead of the correct 2.681).
    result = await _reserve(2.6755)
    assert result["reserved"] == Decimal("2.681"), (
        "2.6755 must quantise via Decimal(str(x)) to 2.676 (cumulative "
        "0.005 + 2.676 = 2.681); 2.680 would mean 2.6755 quantised to "
        "2.675 instead — the raw binary-float value, not Decimal(str(x))"
    )

    row = await _get_item_row(pg_pool, namespace_id, "SKU-RESERVE-QUANT", loc)
    assert row["reserved"] == Decimal("2.681")


# ---------------------------------------------------------------------------
# 6. Namespace isolation — real FORCE RLS proof (see module docstring's
# item 7 above for why a WHERE-clause-only test would not be discriminating
# for this API shape).
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reserve_and_release_are_rls_isolated(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    make_namespace: Any,
) -> None:
    """FORCE RLS defense-in-depth: the ENGINE ITSELF holds an ``nce_app``
    pool (precedent: test_inventory_stock.py's own
    test_rows_written_by_do_transfer_stock_are_rls_isolated), so
    scoped_pg_session's ``SET LOCAL nce.namespace_id`` is load-bearing on
    the write path for BOTH do_reserve_stock and do_release_stock."""
    ns_a = await make_namespace()
    ns_b = await make_namespace()
    loc_a = await _seed_location(pg_pool, ns_a, "A1")
    await _seed_item(pg_pool, ns_a, "SKU-RLS-RESERVE", loc_a, on_hand="20")

    app_pool = await asyncpg.create_pool(_app_dsn(), min_size=1, max_size=2)
    engine = _EngineStub(app_pool)
    try:
        result = await do_reserve_stock(
            engine,
            {
                "namespace_id": ns_a,
                "sku": "SKU-RLS-RESERVE",
                "qty": 5,
                "location": loc_a,
                "project_id": _PROJECT_A,
            },
        )
        assert result["reserved"] == Decimal("5.000")

        released = await do_release_stock(
            engine,
            {
                "namespace_id": ns_a,
                "sku": "SKU-RLS-RESERVE",
                "qty": 2,
                "location": loc_a,
                "project_id": _PROJECT_A,
            },
        )
        assert released["reserved"] == Decimal("3.000")

        # Belt-and-braces: ns_b cannot see ns_a's row even when explicitly
        # asking for ns_a's namespace_id — RLS, not the WHERE clause, refuses.
        async with app_pool.acquire() as conn, conn.transaction():
            await set_namespace_context(conn, ns_b)
            visible_from_b = await conn.fetchval(
                "SELECT COUNT(*) FROM inventory_items WHERE namespace_id = $1", ns_a
            )
        assert visible_from_b == 0, "ns_b must not see ns_a's rows written through nce_app"
    finally:
        await app_pool.close()


# ---------------------------------------------------------------------------
# 7. REAL concurrency — the module's headline structural claim. asyncio.gather
# over SEPARATE pool connections, never sequential calls on one connection.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_reserve_never_exceeds_available(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """3 concurrent do_reserve_stock(qty=4) calls against available=10, each
    over its OWN pool connection. Exactly 2 must succeed, 1 must be
    correctly refused, and the final reserved total must never exceed
    on_hand (10 - 4 - 4 = 2 available left, reserved = 8)."""
    loc = await _seed_location(pg_pool, namespace_id, "Warehouse")
    await _seed_item(pg_pool, namespace_id, "SKU-RESERVE-RACE", loc, on_hand="10")
    engine = _EngineStub(pg_pool)

    async def _reserve() -> dict[str, Any]:
        return await do_reserve_stock(
            engine,
            {
                "namespace_id": namespace_id,
                "sku": "SKU-RESERVE-RACE",
                "qty": 4,
                "location": loc,
                "project_id": _PROJECT_A,
            },
        )

    results = await asyncio.gather(_reserve(), _reserve(), _reserve(), return_exceptions=True)

    successes = [r for r in results if not isinstance(r, BaseException)]
    failures = [r for r in results if isinstance(r, BaseException)]

    assert len(successes) == 2, f"expected exactly 2 successes, got: {results}"
    assert len(failures) == 1, f"expected exactly 1 refusal, got: {results}"
    assert isinstance(failures[0], InsufficientAvailableError), (
        f"the refusal must be the module's own domain error, not a raw DB "
        f"exception, got {type(failures[0])}: {failures[0]}"
    )

    row = await _get_item_row(pg_pool, namespace_id, "SKU-RESERVE-RACE", loc)
    assert row["reserved"] == Decimal("8.000"), (
        f"expected 4 + 4 = 8 reserved, got {row['reserved']}"
    )
    assert row["on_hand"] == Decimal("10.000"), "on_hand must never move under a reservation race"
    available = row["on_hand"] - row["reserved"] - row["blocked"]
    assert available == Decimal("2.000")
    assert available >= 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_release_never_exceeds_reserved(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """Same race, on the release side: 3 concurrent do_release_stock(qty=4)
    calls against reserved=8. Exactly 2 must succeed (8 - 4 - 4 = 0
    reserved), 1 must be refused, and reserved must never go negative."""
    loc = await _seed_location(pg_pool, namespace_id, "Warehouse")
    await _seed_item(pg_pool, namespace_id, "SKU-RELEASE-RACE", loc, on_hand="20", reserved="8")
    engine = _EngineStub(pg_pool)

    async def _release() -> dict[str, Any]:
        return await do_release_stock(
            engine,
            {
                "namespace_id": namespace_id,
                "sku": "SKU-RELEASE-RACE",
                "qty": 4,
                "location": loc,
                "project_id": _PROJECT_A,
            },
        )

    results = await asyncio.gather(_release(), _release(), _release(), return_exceptions=True)

    successes = [r for r in results if not isinstance(r, BaseException)]
    failures = [r for r in results if isinstance(r, BaseException)]

    assert len(successes) == 2, f"expected exactly 2 successes, got: {results}"
    assert len(failures) == 1, f"expected exactly 1 refusal, got: {results}"
    assert isinstance(failures[0], OverReleaseError), (
        f"the refusal must be the module's own domain error, not a raw DB "
        f"exception, got {type(failures[0])}: {failures[0]}"
    )

    row = await _get_item_row(pg_pool, namespace_id, "SKU-RELEASE-RACE", loc)
    assert row["reserved"] == Decimal("0.000"), (
        f"expected 8 - 4 - 4 = 0 reserved, got {row['reserved']}"
    )
    assert row["reserved"] >= 0
