"""Tests for the Inventory engine's transaction ledger + valuation module
(Module 11, Wave 11 — Batch 139 — ``nce/vertical_modules/inventory/transactions.py``).

Covers:

  1. Pure-logic validation (no DB) — ``append_transaction``'s typed-category
     and sign-consistency guards, and ``do_valuation``'s required-field /
     configured-method validation, exercised through the PUBLIC functions.
  2. Pure valuation math (no DB) — ``_compute_valuation``'s FIFO vs average
     arithmetic, called directly (it is pure and has no DB awareness at all;
     see the module's own "Dependency direction" docstring section). Picks
     quantities/costs where the two methods give DIFFERENT answers — a test
     that can't tell FIFO from average proves nothing about which one ran
     (Batch 130/131-class lesson, restated in this wave's brief). Also pins
     the "uncosted inbound still opens a zero-valued layer, never skipped"
     and "an outbound total exceeding what is layered clips at zero" claims
     — both would go undetected by a test that only checked the FIFO-vs-
     average headline number.
  3. Integration (``@pytest.mark.integration``, live Postgres):
     - every movement ``do_transfer_stock``/``do_record_consumption`` makes
       appends the matching typed ledger row(s), in the SAME transaction;
     - ``do_valuation`` computed against SEEDED ledger rows discriminates
       FIFO from average (never claims end-to-end costed inventory — see
       ``transactions.py``'s "Honest scope limit");
     - ``do_valuation`` never writes ``economy_postings`` (Inventory VALUES,
       Economy POSTS);
     - migration 051's structural claims: the typed ``reason_category``
       CHECK, the sign-matches-category CHECK (with ``adjustment``'s
       deliberate either-sign exemption), the WORM grant (nce_app has no
       UPDATE/DELETE), and FORCE RLS isolation between namespaces.

The atomicity claim that a movement's row write and its ledger row commit or
roll back TOGETHER (not merely both attempted) is pinned in
``tests/test_inventory_stock.py``'s extended
``test_transfer_rolls_back_from_decrement_when_to_location_is_invalid`` (the
"to-sorts-last" branch already applies the decrement + ledger append before
the increment fails) — not duplicated here. Likewise the ledger's own
FORCE-RLS write-path proof (through a real ``nce_app`` pool, not the owner
pool) is added to that same file's existing
``test_rows_written_by_do_transfer_stock_are_rls_isolated``, since it already
drives the whole transaction through ``nce_app``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import asyncpg  # type: ignore[import-untyped]
import pytest

from nce.auth import set_namespace_context
from nce.vertical_modules.inventory import transactions
from nce.vertical_modules.inventory.stock import do_record_consumption, do_transfer_stock
from nce.vertical_modules.inventory.transactions import (
    REASON_ADJUSTMENT,
    REASON_TRANSFER_IN,
    REASON_TRANSFER_OUT,
    _compute_valuation,
    append_transaction,
    do_valuation,
)

# ---------------------------------------------------------------------------
# 1. Pure-logic validation (no DB) — exercised through the PUBLIC functions.
# ---------------------------------------------------------------------------


class _DummyEngine:
    """Stands in for NCEEngine in tests that never reach a DB call — the
    validation under test raises before ``engine.pg_pool`` is ever touched."""

    pg_pool = None


@pytest.mark.asyncio
async def test_append_transaction_rejects_unknown_reason_category() -> None:
    with pytest.raises(ValueError, match="reason_category must be one of"):
        await append_transaction(
            None,  # type: ignore[arg-type]
            uuid.uuid4(),
            sku="SKU-X",
            location_id=uuid.uuid4(),
            delta=1,
            reason_category="bogus",
        )


@pytest.mark.asyncio
async def test_append_transaction_rejects_zero_delta() -> None:
    with pytest.raises(ValueError, match="must be non-zero"):
        await append_transaction(
            None,  # type: ignore[arg-type]
            uuid.uuid4(),
            sku="SKU-X",
            location_id=uuid.uuid4(),
            delta=0,
            reason_category=REASON_ADJUSTMENT,
        )


@pytest.mark.asyncio
async def test_append_transaction_rejects_positive_delta_for_transfer_out() -> None:
    with pytest.raises(ValueError, match="requires a negative delta"):
        await append_transaction(
            None,  # type: ignore[arg-type]
            uuid.uuid4(),
            sku="SKU-X",
            location_id=uuid.uuid4(),
            delta=5,
            reason_category=REASON_TRANSFER_OUT,
        )


@pytest.mark.asyncio
async def test_append_transaction_rejects_negative_delta_for_transfer_in() -> None:
    with pytest.raises(ValueError, match="requires a positive delta"):
        await append_transaction(
            None,  # type: ignore[arg-type]
            uuid.uuid4(),
            sku="SKU-X",
            location_id=uuid.uuid4(),
            delta=-5,
            reason_category=REASON_TRANSFER_IN,
        )


@pytest.mark.asyncio
async def test_do_valuation_rejects_bad_method(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        transactions, "load_inventory_valuation_config", lambda: {"method": "bogus"}
    )
    with pytest.raises(ValueError, match="'method' must be one of"):
        await do_valuation(
            _DummyEngine(), {"namespace_id": uuid.uuid4(), "sku": "SKU-X", "location": uuid.uuid4()}
        )


@pytest.mark.asyncio
async def test_do_valuation_rejects_missing_namespace_id() -> None:
    with pytest.raises(ValueError, match="'namespace_id' is required"):
        await do_valuation(_DummyEngine(), {"sku": "SKU-X", "location": uuid.uuid4()})


@pytest.mark.asyncio
async def test_do_valuation_rejects_missing_sku() -> None:
    with pytest.raises(ValueError, match="'sku' is required"):
        await do_valuation(_DummyEngine(), {"namespace_id": uuid.uuid4(), "location": uuid.uuid4()})


@pytest.mark.asyncio
async def test_do_valuation_rejects_missing_location() -> None:
    with pytest.raises(ValueError, match="a location id is required"):
        await do_valuation(_DummyEngine(), {"namespace_id": uuid.uuid4(), "sku": "SKU-X"})


# ---------------------------------------------------------------------------
# 2. Pure valuation math (no DB) — _compute_valuation called directly.
# ---------------------------------------------------------------------------


def test_compute_valuation_fifo_vs_average_discriminate() -> None:
    """The headline claim: pick quantities/costs where FIFO and average
    disagree. 10 @ 10.00 then 10 @ 20.00, then consume 10 — FIFO consumes the
    CHEAPEST (oldest) layer first, leaving the 20.00 layer (value 200.00);
    average blends to 15.00/unit first, leaving 10 units at 15.00 (150.00)."""
    rows = [
        {"delta": Decimal("10.000"), "unit_cost": Decimal("10.00")},
        {"delta": Decimal("10.000"), "unit_cost": Decimal("20.00")},
        {"delta": Decimal("-10.000"), "unit_cost": None},
    ]
    fifo = _compute_valuation(rows, "fifo")
    average = _compute_valuation(rows, "average")

    assert fifo.total_value == Decimal("200.00")
    assert fifo.remaining_qty == Decimal("10.000")
    assert average.total_value == Decimal("150.00")
    assert average.remaining_qty == Decimal("10.000")
    assert fifo.total_value != average.total_value, "must discriminate FIFO from average"


def test_compute_fifo_uncosted_inbound_layer_is_zero_valued_not_skipped() -> None:
    """A row without a unit_cost (every transfer_in stock.py writes this
    wave) still opens a layer — at zero — rather than being dropped. If this
    were changed to SKIP the row instead, remaining_qty would read 0, not 5."""
    rows: list[dict[str, Any]] = [{"delta": Decimal("5.000"), "unit_cost": None}]
    result = _compute_valuation(rows, "fifo")
    assert result.remaining_qty == Decimal("5.000"), "quantity must still be tracked"
    assert result.total_value == Decimal("0.00"), "but valued at zero, not silently dropped"


def test_compute_average_uncosted_inbound_layer_is_zero_valued_not_skipped() -> None:
    rows: list[dict[str, Any]] = [{"delta": Decimal("5.000"), "unit_cost": None}]
    result = _compute_valuation(rows, "average")
    assert result.remaining_qty == Decimal("5.000")
    assert result.total_value == Decimal("0.00")


def test_compute_fifo_clips_oversell_defensively() -> None:
    """An outbound total exceeding what is layered is clipped at zero rather
    than driven negative. This wave's own writers cannot produce this
    (inventory_items' own oversell guard already prevents it), but a
    directly-seeded ledger could, and this must not raise or go negative."""
    rows = [
        {"delta": Decimal("5.000"), "unit_cost": Decimal("10.00")},
        {"delta": Decimal("-9.000"), "unit_cost": None},
    ]
    result = _compute_valuation(rows, "fifo")
    assert result.remaining_qty == Decimal("0.000")
    assert result.total_value == Decimal("0.00")


def test_compute_average_clips_oversell_defensively() -> None:
    rows = [
        {"delta": Decimal("5.000"), "unit_cost": Decimal("10.00")},
        {"delta": Decimal("-9.000"), "unit_cost": None},
    ]
    result = _compute_valuation(rows, "average")
    assert result.remaining_qty == Decimal("0.000")
    assert result.total_value == Decimal("0.00")


def test_compute_valuation_rejects_unknown_method() -> None:
    with pytest.raises(ValueError, match="unknown method"):
        _compute_valuation([], "bogus")


# ---------------------------------------------------------------------------
# Integration helpers — seed directly via the owner pool, matching
# test_inventory_stock.py's convention. Every helper takes an explicit
# namespace_id and scopes its own SQL by it.
# ---------------------------------------------------------------------------


class _EngineStub:
    def __init__(self, pg_pool: asyncpg.Pool) -> None:  # type: ignore[type-arg]
        self.pg_pool = pg_pool


async def _seed_location(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    name: str,
) -> uuid.UUID:
    async with pg_pool.acquire() as conn:
        location_id = await conn.fetchval(
            "INSERT INTO stock_locations (namespace_id, kind, name, parent_id, level) "
            "VALUES ($1, 'warehouse', $2, NULL, 0) RETURNING id",
            namespace_id,
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
) -> None:
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO inventory_items (namespace_id, sku, location_id, qty_on_hand) "
            "VALUES ($1, $2, $3, $4)",
            namespace_id,
            sku,
            location_id,
            Decimal(on_hand),
        )


async def _seed_transaction(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    sku: str,
    location_id: uuid.UUID,
    delta: str,
    reason_category: str,
    unit_cost: str | None = None,
    created_at: datetime | None = None,
) -> uuid.UUID:
    """Scaffolding-only insert (owner pool, bypasses ``append_transaction``'s
    own validation on purpose) — stands in for a not-yet-built writer (e.g. a
    future goods receipt) so ``do_valuation`` can be proven against a known
    ledger shape. ``created_at`` is explicit, not left to ``now()``, so the
    FIFO/average ordering in a test is never a wall-clock-timing assumption."""
    async with pg_pool.acquire() as conn:
        row_id = await conn.fetchval(
            "INSERT INTO inventory_transactions "
            "(namespace_id, sku, location_id, delta, reason_category, unit_cost, created_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, COALESCE($7, now())) RETURNING id",
            namespace_id,
            sku,
            location_id,
            Decimal(delta),
            reason_category,
            Decimal(unit_cost) if unit_cost is not None else None,
            created_at,
        )
    assert row_id is not None
    return row_id


# ---------------------------------------------------------------------------
# 3a. Every movement appends the matching typed ledger row(s)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_do_transfer_stock_appends_transfer_out_and_transfer_in_rows(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    loc_a = await _seed_location(pg_pool, namespace_id, "From")
    loc_b = await _seed_location(pg_pool, namespace_id, "To")
    await _seed_item(pg_pool, namespace_id, "SKU-LEDGER-XFER", loc_a, on_hand="10")
    engine = _EngineStub(pg_pool)

    await do_transfer_stock(
        engine,
        {
            "namespace_id": namespace_id,
            "sku": "SKU-LEDGER-XFER",
            "qty": 4,
            "from_location": loc_a,
            "to_location": loc_b,
        },
    )

    async with pg_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT location_id, delta, reason_category, ref, unit_cost "
            "FROM inventory_transactions WHERE namespace_id = $1 AND sku = $2",
            namespace_id,
            "SKU-LEDGER-XFER",
        )
    assert len(rows) == 2, "one transfer must append exactly two ledger rows"
    by_category = {r["reason_category"]: r for r in rows}
    assert set(by_category) == {"transfer_out", "transfer_in"}

    out_row = by_category["transfer_out"]
    assert out_row["location_id"] == loc_a
    assert out_row["delta"] == Decimal("-4.000")
    assert out_row["ref"] == str(loc_b)
    assert out_row["unit_cost"] is None, "no cost source exists yet (see honest scope limit)"

    in_row = by_category["transfer_in"]
    assert in_row["location_id"] == loc_b
    assert in_row["delta"] == Decimal("4.000")
    assert in_row["ref"] == str(loc_a)
    assert in_row["unit_cost"] is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_do_record_consumption_appends_consumption_row_with_work_order_ref(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    loc = await _seed_location(pg_pool, namespace_id, "Warehouse")
    await _seed_item(pg_pool, namespace_id, "SKU-LEDGER-CONSUME", loc, on_hand="10")
    engine = _EngineStub(pg_pool)

    await do_record_consumption(
        engine,
        {
            "namespace_id": namespace_id,
            "sku": "SKU-LEDGER-CONSUME",
            "qty": 3,
            "location": loc,
            "work_order": "WO-42",
        },
    )

    async with pg_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT location_id, delta, reason_category, ref, unit_cost "
            "FROM inventory_transactions WHERE namespace_id = $1 AND sku = $2",
            namespace_id,
            "SKU-LEDGER-CONSUME",
        )
    assert len(rows) == 1
    row = rows[0]
    assert row["location_id"] == loc
    assert row["delta"] == Decimal("-3.000")
    assert row["reason_category"] == "consumption"
    assert row["ref"] == "WO-42", "work_order must now be retrievable from the ledger itself"
    assert row["unit_cost"] is None


# ---------------------------------------------------------------------------
# 3b. do_valuation — FIFO/average against seeded rows; no GL posting.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_do_valuation_fifo_vs_average_discriminate_against_seeded_rows(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: real ledger rows in Postgres, read back through
    do_valuation, with the configured method swapped via the module's own
    loader (never the on-disk file — a test must not mutate shared repo
    config). Same discriminating numbers as the pure math test above, this
    time proving the WHOLE read path, not just the arithmetic."""
    loc = await _seed_location(pg_pool, namespace_id, "Warehouse")
    sku = "SKU-VALUATION"
    base = datetime.now(timezone.utc)
    await _seed_transaction(
        pg_pool,
        namespace_id,
        sku,
        loc,
        delta="10.000",
        reason_category="adjustment",
        unit_cost="10.00",
        created_at=base,
    )
    await _seed_transaction(
        pg_pool,
        namespace_id,
        sku,
        loc,
        delta="10.000",
        reason_category="adjustment",
        unit_cost="20.00",
        created_at=base + timedelta(seconds=1),
    )
    await _seed_transaction(
        pg_pool,
        namespace_id,
        sku,
        loc,
        delta="-10.000",
        reason_category="consumption",
        created_at=base + timedelta(seconds=2),
    )
    engine = _EngineStub(pg_pool)

    monkeypatch.setattr(transactions, "load_inventory_valuation_config", lambda: {"method": "fifo"})
    fifo_result = await do_valuation(
        engine, {"namespace_id": namespace_id, "sku": sku, "location": loc}
    )
    assert fifo_result["ok"] is True
    assert fifo_result["method"] == "fifo"
    assert fifo_result["value"] == Decimal("200.00")
    assert fifo_result["remaining_qty"] == Decimal("10.000")

    monkeypatch.setattr(
        transactions, "load_inventory_valuation_config", lambda: {"method": "average"}
    )
    avg_result = await do_valuation(
        engine, {"namespace_id": namespace_id, "sku": sku, "location": loc}
    )
    assert avg_result["method"] == "average"
    assert avg_result["value"] == Decimal("150.00")
    assert avg_result["remaining_qty"] == Decimal("10.000")

    assert fifo_result["value"] != avg_result["value"], "must discriminate FIFO from average"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_do_valuation_does_not_write_economy_postings(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """Structural proof of the boundary: Inventory VALUES the stock, Economy
    POSTS it. do_valuation is a pure read — this asserts it leaves
    economy_postings untouched, not just that its import graph excludes
    nce.vertical_modules.economy (which a reviewer would have to trust)."""
    loc = await _seed_location(pg_pool, namespace_id, "Warehouse")
    sku = "SKU-NO-GL"
    await _seed_transaction(
        pg_pool,
        namespace_id,
        sku,
        loc,
        delta="5.000",
        reason_category="adjustment",
        unit_cost="1.00",
    )
    engine = _EngineStub(pg_pool)

    async with pg_pool.acquire() as conn:
        before = await conn.fetchval(
            "SELECT COUNT(*) FROM economy_postings WHERE namespace_id = $1", namespace_id
        )

    result = await do_valuation(engine, {"namespace_id": namespace_id, "sku": sku, "location": loc})
    assert result["value"] == Decimal("5.00")

    async with pg_pool.acquire() as conn:
        after = await conn.fetchval(
            "SELECT COUNT(*) FROM economy_postings WHERE namespace_id = $1", namespace_id
        )
    assert after == before == 0, "do_valuation must never post to the GL"


# ---------------------------------------------------------------------------
# 3c. Migration 051's structural claims
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reason_category_check_rejects_bogus_category(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    loc = await _seed_location(pg_pool, namespace_id, "Warehouse")
    async with pg_pool.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO inventory_transactions "
                "(namespace_id, sku, location_id, delta, reason_category) "
                "VALUES ($1, 'SKU-X', $2, 1, 'not_a_real_category')",
                namespace_id,
                loc,
            )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sign_mismatch_check_rejects_wrong_signed_category(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """Pins inventory_transactions_sign_matches_category: a 'transfer_out'
    row must be negative — the DB refuses a positive one even if the
    application-level guard were somehow bypassed."""
    loc = await _seed_location(pg_pool, namespace_id, "Warehouse")
    async with pg_pool.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO inventory_transactions "
                "(namespace_id, sku, location_id, delta, reason_category) "
                "VALUES ($1, 'SKU-X', $2, 5, 'transfer_out')",
                namespace_id,
                loc,
            )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_adjustment_category_accepts_either_sign_at_db_level(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """The positive case for the same CHECK: 'adjustment' is deliberately
    unconstrained in sign — a manual correction can go either way."""
    loc = await _seed_location(pg_pool, namespace_id, "Warehouse")
    async with pg_pool.acquire() as conn:
        pos_id = await conn.fetchval(
            "INSERT INTO inventory_transactions "
            "(namespace_id, sku, location_id, delta, reason_category) "
            "VALUES ($1, 'SKU-ADJ', $2, 3, 'adjustment') RETURNING id",
            namespace_id,
            loc,
        )
        neg_id = await conn.fetchval(
            "INSERT INTO inventory_transactions "
            "(namespace_id, sku, location_id, delta, reason_category) "
            "VALUES ($1, 'SKU-ADJ', $2, -3, 'adjustment') RETURNING id",
            namespace_id,
            loc,
        )
    assert pos_id is not None
    assert neg_id is not None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_worm_grants_refuse_update_and_delete(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """WORM proof: nce_app is granted only SELECT, INSERT (migration 051) —
    a correction must be a new row, never an UPDATE/DELETE of history."""
    loc = await _seed_location(pg_pool, namespace_id, "Warehouse")
    row_id = await _seed_transaction(
        pg_pool, namespace_id, "SKU-WORM", loc, delta="1.000", reason_category="adjustment"
    )

    async with pg_app_conn.transaction():
        await set_namespace_context(pg_app_conn, namespace_id)
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await pg_app_conn.execute(
                "UPDATE inventory_transactions SET ref = 'tampered' WHERE id = $1", row_id
            )

    async with pg_app_conn.transaction():
        await set_namespace_context(pg_app_conn, namespace_id)
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await pg_app_conn.execute("DELETE FROM inventory_transactions WHERE id = $1", row_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rls_isolates_inventory_transactions_between_namespaces(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
    make_namespace: Any,
) -> None:
    ns_a = await make_namespace()
    ns_b = await make_namespace()
    loc_a = await _seed_location(pg_pool, ns_a, "Warehouse")
    await _seed_transaction(
        pg_pool, ns_a, "SKU-RLS", loc_a, delta="2.000", reason_category="adjustment"
    )

    async with pg_app_conn.transaction():
        await set_namespace_context(pg_app_conn, ns_b)
        visible_from_b = await pg_app_conn.fetchval(
            "SELECT COUNT(*) FROM inventory_transactions WHERE namespace_id = $1", ns_a
        )
    assert visible_from_b == 0, "ns_b must not see ns_a's inventory_transactions rows"

    async with pg_app_conn.transaction():
        await set_namespace_context(pg_app_conn, ns_a)
        visible_from_a = await pg_app_conn.fetchval(
            "SELECT COUNT(*) FROM inventory_transactions WHERE namespace_id = $1", ns_a
        )
    assert visible_from_a == 1, "ns_a must see its own inventory_transactions row"
