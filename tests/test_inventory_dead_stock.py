"""Tests for the Inventory engine's dead-stock reconcile
(Module 11, Wave 10c — Batch 138c —
``nce/vertical_modules/inventory/reconcile.py``).

The wave's correctness criterion is an ABSENCE: it must be impossible for
``do_reconcile_dead_stock`` to notice a divergence between ``inventory_items``
and the ``inventory_transactions`` ledger and keep going. So the load-bearing
assertion here is the raise itself — assertion (a) — and it is proven
discriminating: with the ``raise LedgerDivergenceError`` replaced by a
``log.warning(...)`` plus an ``ok=True`` return (mutated in an out-of-tree
copy, never in this tree), that test goes RED.

What each tier covers:

  * **Unit tier** (plain, no marker — always runs in the unit job): the pure,
    DB-free classifier ``classify_dead_stock_pairs`` and every ``params``
    validator, driven through the PUBLIC entry points. The classifier split is
    what makes the loud-failure behaviour testable with no Postgres at all.
  * **Integration tier** (``@pytest.mark.integration``): the SQL dead-set
    predicate, agreement with the REAL writers (``do_transfer_stock`` and
    ``do_dispose_rma_weee`` — B138b's disposal leg), the read-only guarantee
    on both the clean and the raising path, and namespace isolation driven
    through an unprivileged ``nce_app`` pool.

Backdating is done by SEEDING, not by mutating: ``inventory_transactions`` is
append-only (``nce_app`` holds only SELECT/INSERT), so every aged ledger row
is INSERTed with an explicit ``created_at``. The one case that needs a *fresh*
pair (assertion d, real writers) passes ``dead_stock_days=0`` instead of
ageing real writer output.

``node_ownership_registry`` is seeded in THIS file (``_seed_ownership``,
copied from ``tests/test_inventory_stock.py``), never in ``tests/conftest.py``
— seeding it there would silently disarm the deliberate deny-by-default
proofs in ``tests/test_project_convert.py`` and
``tests/test_system_design_graph.py``.

Wired into CI by the existing ``pytest tests/test_inventory_*.py -m
integration`` glob in the "Integration — M11 Inventory" step (Batch 152a's
prefix glob) — the file name is what earns the wiring.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse, urlunparse

import asyncpg  # type: ignore[import-untyped]
import pytest

from nce.auth import set_namespace_context
from nce.config import cfg
from nce.entity_resolution.ownership_seed import seed_node_ownership_registry
from nce.vertical_modules.inventory.reconcile import (
    LedgerDivergenceError,
    classify_dead_stock_pairs,
    do_reconcile_dead_stock,
    load_inventory_dead_stock_config,
)
from nce.vertical_modules.inventory.rma import (
    WEEE_AWAITING_COLLECTION,
    do_dispose_rma_weee,
    do_record_rma,
)
from nce.vertical_modules.inventory.stock import do_transfer_stock

# ---------------------------------------------------------------------------
# Unit tier — the pure classifier. No Postgres, no engine, no fixtures.
# ---------------------------------------------------------------------------

_T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _row(
    sku: str,
    *,
    on_hand: str,
    ledger_sum: str | None,
    txn_count: int,
    items_created_at: datetime = _T0,
    ledger_first_at: datetime | None = _T0,
) -> dict[str, Any]:
    return {
        "sku": sku,
        "location_id": uuid.uuid4(),
        "on_hand": Decimal(on_hand),
        "items_created_at": items_created_at,
        "ledger_txn_count": txn_count,
        "ledger_sum": None if ledger_sum is None else Decimal(ledger_sum),
        "ledger_first_at": ledger_first_at,
    }


def test_classifier_ledger_born_equal_is_balanced() -> None:
    buckets = classify_dead_stock_pairs(
        [_row("SKU-EQ", on_hand="10.000", ledger_sum="10", txn_count=2)]
    )
    assert [entry["sku"] for entry in buckets.balanced] == ["SKU-EQ"]
    assert buckets.divergent == []
    assert buckets.unreconcilable_opening_balance == []


def test_classifier_ledger_born_unequal_is_divergent_with_difference() -> None:
    buckets = classify_dead_stock_pairs(
        [_row("SKU-DIV", on_hand="7", ledger_sum="10", txn_count=1)]
    )
    assert buckets.balanced == []
    assert buckets.unreconcilable_opening_balance == []
    assert len(buckets.divergent) == 1
    assert buckets.divergent[0]["difference"] == Decimal("-3")


def test_classifier_no_ledger_rows_is_unreconcilable() -> None:
    buckets = classify_dead_stock_pairs(
        [
            _row(
                "SKU-NOLEDGER",
                on_hand="4",
                ledger_sum=None,
                txn_count=0,
                ledger_first_at=None,
            )
        ]
    )
    assert [e["sku"] for e in buckets.unreconcilable_opening_balance] == ["SKU-NOLEDGER"]
    assert buckets.balanced == []
    assert buckets.divergent == []


def test_classifier_pre_ledger_pair_is_never_asserted_about_either_way() -> None:
    """The items row predates its own ledger history, and its quantity does
    NOT match SUM(delta). It must still land in
    ``unreconcilable_opening_balance`` — never ``divergent`` (that would raise
    on data that was never wrong) and never ``balanced`` (that would excuse a
    real divergence)."""
    buckets = classify_dead_stock_pairs(
        [
            _row(
                "SKU-PRELEDGER",
                on_hand="5",
                ledger_sum="2",
                txn_count=1,
                items_created_at=_T0,
                ledger_first_at=_T0 + timedelta(days=1),
            )
        ]
    )
    assert [e["sku"] for e in buckets.unreconcilable_opening_balance] == ["SKU-PRELEDGER"]
    assert buckets.balanced == []
    assert buckets.divergent == []


def test_classifier_equal_timestamps_are_ledger_born() -> None:
    """``items.created_at == MIN(txn.created_at)`` is the ledger-born case:
    the writer creates the row and appends its ledger row in the SAME
    transaction, and ``now()`` is the transaction timestamp."""
    buckets = classify_dead_stock_pairs(
        [_row("SKU-BORN", on_hand="3", ledger_sum="4", txn_count=1)]
    )
    assert [e["sku"] for e in buckets.divergent] == ["SKU-BORN"]


def test_classifier_ledger_history_predating_the_row_is_treated_as_ledger_born() -> None:
    """``items.created_at > MIN(txn.created_at)`` is anomalous and is
    deliberately classified as ledger-born, so it surfaces as a divergence
    rather than being quietly excused."""
    buckets = classify_dead_stock_pairs(
        [
            _row(
                "SKU-ANOMALY",
                on_hand="5",
                ledger_sum="2",
                txn_count=1,
                items_created_at=_T0 + timedelta(days=1),
                ledger_first_at=_T0,
            )
        ]
    )
    assert [e["sku"] for e in buckets.divergent] == ["SKU-ANOMALY"]


def test_classifier_never_raises_and_collects_every_divergence() -> None:
    """The classifier reports; the caller decides. If it raised on the first
    divergence it would hide every divergence after it."""
    buckets = classify_dead_stock_pairs(
        [
            _row("SKU-D1", on_hand="1", ledger_sum="2", txn_count=1),
            _row("SKU-D2", on_hand="9", ledger_sum="4", txn_count=1),
            _row("SKU-OK", on_hand="6", ledger_sum="6", txn_count=1),
        ]
    )
    assert [e["sku"] for e in buckets.divergent] == ["SKU-D1", "SKU-D2"]
    assert [e["sku"] for e in buckets.balanced] == ["SKU-OK"]


def test_divergence_error_names_every_pair_and_carries_structured_data() -> None:
    loc_a, loc_b = uuid.uuid4(), uuid.uuid4()
    err = LedgerDivergenceError(
        [
            {
                "sku": "SKU-A",
                "location_id": str(loc_a),
                "on_hand": Decimal("1"),
                "ledger_sum": Decimal("2"),
                "difference": Decimal("-1"),
            },
            {
                "sku": "SKU-B",
                "location_id": str(loc_b),
                "on_hand": Decimal("9"),
                "ledger_sum": Decimal("4"),
                "difference": Decimal("5"),
            },
        ]
    )
    rendered = str(err)
    assert "SKU-A" in rendered and "SKU-B" in rendered
    assert str(loc_a) in rendered and str(loc_b) in rendered
    # Structured, not a message a caller has to re-parse.
    assert [pair["sku"] for pair in err.pairs] == ["SKU-A", "SKU-B"]
    assert err.pairs[1]["difference"] == Decimal("5")


# ---------------------------------------------------------------------------
# Unit tier — params validation, through the PUBLIC entry point. Validation
# raises before ``engine.pg_pool`` is ever touched, so a dummy engine with
# ``pg_pool = None`` is safe (test_inventory_stock.py's _DummyEngine idiom).
# ---------------------------------------------------------------------------


class _DummyEngine:
    pg_pool = None


@pytest.mark.asyncio
async def test_reconcile_rejects_missing_namespace_id() -> None:
    with pytest.raises(ValueError, match="'namespace_id' is required"):
        await do_reconcile_dead_stock(_DummyEngine(), {})


@pytest.mark.asyncio
async def test_reconcile_rejects_bool_dead_stock_days() -> None:
    """``isinstance(True, int)`` is ``True`` in Python — a bool must not
    silently pass as a window of 1 day."""
    with pytest.raises(ValueError, match="bool is not a number of days"):
        await do_reconcile_dead_stock(
            _DummyEngine(), {"namespace_id": uuid.uuid4(), "dead_stock_days": True}
        )


@pytest.mark.asyncio
async def test_reconcile_rejects_negative_dead_stock_days() -> None:
    with pytest.raises(ValueError, match=r"must be >= 0"):
        await do_reconcile_dead_stock(
            _DummyEngine(), {"namespace_id": uuid.uuid4(), "dead_stock_days": -1}
        )


@pytest.mark.asyncio
async def test_reconcile_rejects_string_dead_stock_days() -> None:
    with pytest.raises(ValueError, match="expected a non-negative int"):
        await do_reconcile_dead_stock(
            _DummyEngine(), {"namespace_id": uuid.uuid4(), "dead_stock_days": "90"}
        )


def test_config_file_supplies_the_default_window() -> None:
    config = load_inventory_dead_stock_config()
    assert config["dead_stock_days"] == 90
    assert isinstance(config["dead_stock_days"], int)
    assert not isinstance(config["dead_stock_days"], bool)


# ---------------------------------------------------------------------------
# Integration helpers — seed directly through the owner pool, each scoping its
# own SQL by an explicit namespace_id (this suite does not rely on RLS for its
# own scaffolding). Shapes copied from tests/test_inventory_stock.py.
# ---------------------------------------------------------------------------


class _EngineStub:
    def __init__(self, pg_pool: asyncpg.Pool) -> None:  # type: ignore[type-arg]
        self.pg_pool = pg_pool


def _app_dsn() -> str:
    """Rewrite the integration DSN onto the restricted ``nce_app`` role."""
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


async def _seed_ownership(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """Seed the node-ownership registry so Inventory's guarded graph mirror
    passes for this namespace. NOT called from conftest.py on purpose."""
    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, namespace_id)
            await seed_node_ownership_registry(conn, namespace_id)


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
    return uuid.UUID(str(location_id))


async def _days_ago(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    days: int,
) -> datetime:
    """One server-side timestamp, reused for both the items row and its ledger
    row so a ledger-born pair's two ``created_at`` values are EXACTLY equal."""
    async with pg_pool.acquire() as conn:
        value = await conn.fetchval("SELECT now() - make_interval(days => $1::int)", days)
    assert isinstance(value, datetime)
    return value


async def _seed_item(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    sku: str,
    location_id: uuid.UUID,
    on_hand: str,
    created_at: datetime | None = None,
) -> None:
    async with pg_pool.acquire() as conn:
        if created_at is None:
            await conn.execute(
                "INSERT INTO inventory_items "
                "(namespace_id, sku, location_id, qty_on_hand) VALUES ($1, $2, $3, $4)",
                namespace_id,
                sku,
                location_id,
                Decimal(on_hand),
            )
        else:
            await conn.execute(
                "INSERT INTO inventory_items "
                "(namespace_id, sku, location_id, qty_on_hand, created_at) "
                "VALUES ($1, $2, $3, $4, $5)",
                namespace_id,
                sku,
                location_id,
                Decimal(on_hand),
                created_at,
            )


async def _seed_txn(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    sku: str,
    location_id: uuid.UUID,
    delta: str,
    created_at: datetime,
) -> None:
    """INSERT (never UPDATE) an aged ledger row. ``adjustment`` is the one
    open-signed category, so a seeded row can go either way."""
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO inventory_transactions "
            "(namespace_id, sku, location_id, delta, reason_category, created_at) "
            "VALUES ($1, $2, $3, $4, 'adjustment', $5)",
            namespace_id,
            sku,
            location_id,
            Decimal(delta),
            created_at,
        )


async def _drive_on_hand_off_the_ledger(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    sku: str,
    location_id: uuid.UUID,
    on_hand: str,
) -> None:
    """UPDATE this test's OWN seeded ``inventory_items`` row — test data, not
    tree mutation, and never the append-only ledger.

    The namespace context is set EXPLICITLY and the affected-row count is
    asserted. ``inventory_items`` is FORCE RLS, which applies to the table
    owner too: a pooled connection carrying a previous test's
    ``app.namespace_id`` would make this UPDATE match zero rows *silently*,
    and the divergence this helper exists to create would never appear. A
    silently-ineffective seed is exactly the shape that produces a green test
    proving nothing.
    """
    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, namespace_id)
            status = await conn.execute(
                "UPDATE inventory_items SET qty_on_hand = $4 "
                "WHERE namespace_id = $1 AND sku = $2 AND location_id = $3",
                namespace_id,
                sku,
                location_id,
                Decimal(on_hand),
            )
    assert status == "UPDATE 1", f"seeding UPDATE affected no row: {status!r}"


_WATCHED_TABLES = (
    "inventory_items",
    "inventory_transactions",
    "inventory_rma",
    "kg_nodes",
    "outbox_events",
)


async def _snapshot(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> dict[str, tuple[int, Any]]:
    """Row count + max(updated_at) per watched table, scoped by namespace where
    the table carries one. Any write at all — including a "reconciled_at"
    marker — moves one of these numbers."""
    snap: dict[str, tuple[int, Any]] = {}
    async with pg_pool.acquire() as conn:
        for table in _WATCHED_TABLES:
            columns = {
                record["column_name"]
                for record in await conn.fetch(
                    "SELECT column_name FROM information_schema.columns WHERE table_name = $1",
                    table,
                )
            }
            assert columns, f"{table} does not exist — the snapshot would be vacuous"
            where = " WHERE namespace_id = $1" if "namespace_id" in columns else ""
            args = [namespace_id] if where else []
            updated = "max(updated_at)::text" if "updated_at" in columns else "NULL::text"
            record = await conn.fetchrow(
                f"SELECT count(*) AS n, {updated} AS u FROM {table}{where}",  # noqa: S608
                *args,
            )
            assert record is not None
            snap[table] = (int(record["n"]), record["u"])
    return snap


# ---------------------------------------------------------------------------
# Integration tier
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_divergent_dead_pair_makes_the_reconcile_raise(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """🔴 Assertion (a) — THE assertion this wave exists to survive.

    A ledger-born pair (items row + ledger rows summing to it, both aged past
    the window) is driven off the ledger. ``do_reconcile_dead_stock`` must
    RAISE, naming that pair with its on_hand, ledger_sum and difference.

    Discriminating: with the ``raise LedgerDivergenceError`` replaced by a
    ``log.warning(...)`` + an ``ok=True`` return, this goes RED.
    """
    born_at = await _days_ago(pg_pool, 200)
    loc = await _seed_location(pg_pool, namespace_id, "Depot-A")
    await _seed_item(pg_pool, namespace_id, "SKU-RAISE", loc, "10", created_at=born_at)
    await _seed_txn(pg_pool, namespace_id, "SKU-RAISE", loc, "10", born_at)
    await _drive_on_hand_off_the_ledger(pg_pool, namespace_id, "SKU-RAISE", loc, "7")

    with pytest.raises(LedgerDivergenceError) as excinfo:
        await do_reconcile_dead_stock(_EngineStub(pg_pool), {"namespace_id": namespace_id})

    pairs = excinfo.value.pairs
    assert [pair["sku"] for pair in pairs] == ["SKU-RAISE"]
    assert pairs[0]["location_id"] == str(loc)
    assert pairs[0]["on_hand"] == Decimal("7")
    assert pairs[0]["ledger_sum"] == Decimal("10")
    assert pairs[0]["difference"] == Decimal("-3")
    rendered = str(excinfo.value)
    assert "SKU-RAISE" in rendered and str(loc) in rendered


@pytest.mark.integration
@pytest.mark.asyncio
async def test_every_divergence_is_named_not_just_the_first(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """🔴 Assertion (b). Goes RED if the raise is moved inside the
    classification loop — the second pair would never be reached."""
    born_at = await _days_ago(pg_pool, 200)
    loc = await _seed_location(pg_pool, namespace_id, "Depot-B")
    for sku, ledger, on_hand in (("SKU-D1", "10", "7"), ("SKU-D2", "4", "9")):
        await _seed_item(pg_pool, namespace_id, sku, loc, ledger, created_at=born_at)
        await _seed_txn(pg_pool, namespace_id, sku, loc, ledger, born_at)
        await _drive_on_hand_off_the_ledger(pg_pool, namespace_id, sku, loc, on_hand)

    with pytest.raises(LedgerDivergenceError) as excinfo:
        await do_reconcile_dead_stock(_EngineStub(pg_pool), {"namespace_id": namespace_id})

    assert sorted(pair["sku"] for pair in excinfo.value.pairs) == ["SKU-D1", "SKU-D2"]
    rendered = str(excinfo.value)
    assert "SKU-D1" in rendered and "SKU-D2" in rendered


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pre_ledger_pair_is_reported_not_judged(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """🔴 Assertion (c). The items row predates its own ledger history, and
    ``qty_on_hand != SUM(delta)``. It is reported in
    ``unreconcilable_opening_balance`` — not ``divergent`` (no raise) and not
    ``balanced``."""
    item_at = await _days_ago(pg_pool, 300)
    ledger_at = await _days_ago(pg_pool, 200)
    assert item_at < ledger_at
    loc = await _seed_location(pg_pool, namespace_id, "Depot-C")
    await _seed_item(pg_pool, namespace_id, "SKU-PRE", loc, "5", created_at=item_at)
    await _seed_txn(pg_pool, namespace_id, "SKU-PRE", loc, "2", ledger_at)

    result = await do_reconcile_dead_stock(_EngineStub(pg_pool), {"namespace_id": namespace_id})

    assert result["ok"] is True
    assert result["divergent"] == []
    assert [entry["sku"] for entry in result["balanced"]] == []
    unreconcilable = result["unreconcilable_opening_balance"]
    assert [entry["sku"] for entry in unreconcilable] == ["SKU-PRE"]
    assert unreconcilable[0]["on_hand"] == Decimal("5")
    assert unreconcilable[0]["ledger_sum"] == Decimal("2")
    assert result["dead_pairs"] == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pair_with_no_ledger_history_is_unreconcilable(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """A pair with NO ledger row is dead (nothing has moved) and its opening
    balance is unknown — reported, never asserted about."""
    loc = await _seed_location(pg_pool, namespace_id, "Depot-Silent")
    await _seed_item(pg_pool, namespace_id, "SKU-SILENT", loc, "8")

    result = await do_reconcile_dead_stock(_EngineStub(pg_pool), {"namespace_id": namespace_id})

    unreconcilable = result["unreconcilable_opening_balance"]
    assert [entry["sku"] for entry in unreconcilable] == ["SKU-SILENT"]
    assert unreconcilable[0]["ledger_sum"] is None
    assert unreconcilable[0]["ledger_txn_count"] == 0
    assert result["divergent"] == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pair_built_by_the_real_writers_reconciles_as_balanced(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """🔴 Assertion (d) — agreement with the REAL writers, the cross-wave
    assertion. The destination pair is created purely by ``do_transfer_stock``
    and then reduced by ``do_dispose_rma_weee`` (B138b's disposal leg, where
    nothing arrives). ``dead_stock_days=0`` makes the fresh pair dead without
    ageing real writer output. If the disposal leg ever loses its ledger
    append, ``qty_on_hand`` and ``SUM(delta)`` part company and this test goes
    red."""
    await _seed_ownership(pg_pool, namespace_id)
    loc_a = await _seed_location(pg_pool, namespace_id, "Source")
    loc_b = await _seed_location(pg_pool, namespace_id, "Destination")
    await _seed_item(pg_pool, namespace_id, "SKU-REAL", loc_a, "10")
    engine = _EngineStub(pg_pool)

    await do_transfer_stock(
        engine,
        {
            "namespace_id": namespace_id,
            "sku": "SKU-REAL",
            "qty": 10,
            "from_location": loc_a,
            "to_location": loc_b,
        },
    )
    await do_record_rma(
        engine,
        {
            "namespace_id": namespace_id,
            "rma_ref": "RMA-DEADSTOCK-1",
            "sku": "SKU-REAL",
            "location": loc_b,
            "qty": 3,
            "reason": "faulty on arrival",
            "weee_state": WEEE_AWAITING_COLLECTION,
        },
    )
    await do_dispose_rma_weee(
        engine,
        {
            "namespace_id": namespace_id,
            "rma_ref": "RMA-DEADSTOCK-1",
            "disposal_ref": "TAKEBACK-1",
        },
    )

    result = await do_reconcile_dead_stock(
        engine, {"namespace_id": namespace_id, "dead_stock_days": 0}
    )

    assert result["ok"] is True
    assert result["divergent"] == []
    assert result["unreconcilable_opening_balance"] == []
    assert [entry["sku"] for entry in result["balanced"]] == ["SKU-REAL"]
    assert result["balanced"][0]["location_id"] == str(loc_b)
    assert result["balanced"][0]["on_hand"] == Decimal("7")
    assert result["balanced"][0]["ledger_sum"] == Decimal("7")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pair_with_recent_movement_is_not_in_the_dead_set(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """Assertion (e). At the configured default window a pair whose latest
    ledger row is INSIDE the window is absent from all three buckets — goes
    RED if the ``MAX(txn.created_at) < now() - make_interval(...)`` predicate
    is dropped (the pair is divergent, so it would raise)."""
    born_at = await _days_ago(pg_pool, 300)
    recent = await _days_ago(pg_pool, 1)
    loc = await _seed_location(pg_pool, namespace_id, "Depot-Live")
    await _seed_item(pg_pool, namespace_id, "SKU-LIVE", loc, "10", created_at=born_at)
    await _seed_txn(pg_pool, namespace_id, "SKU-LIVE", loc, "10", born_at)
    await _seed_txn(pg_pool, namespace_id, "SKU-LIVE", loc, "1", recent)
    await _drive_on_hand_off_the_ledger(pg_pool, namespace_id, "SKU-LIVE", loc, "999")

    result = await do_reconcile_dead_stock(_EngineStub(pg_pool), {"namespace_id": namespace_id})

    assert result["dead_stock_days"] == load_inventory_dead_stock_config()["dead_stock_days"]
    all_skus = [
        entry["sku"]
        for bucket in ("balanced", "unreconcilable_opening_balance", "divergent")
        for entry in result[bucket]
    ]
    assert "SKU-LIVE" not in all_skus
    assert result["dead_pairs"] == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reconcile_writes_nothing_on_the_clean_and_the_raising_path(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """🔴 Assertion (f) — read-only. Row counts and ``updated_at`` values of
    inventory_items / inventory_transactions / inventory_rma / kg_nodes /
    outbox_events are unchanged across BOTH a clean call and a raising one."""
    born_at = await _days_ago(pg_pool, 200)
    loc = await _seed_location(pg_pool, namespace_id, "Depot-RO")
    await _seed_item(pg_pool, namespace_id, "SKU-RO", loc, "10", created_at=born_at)
    await _seed_txn(pg_pool, namespace_id, "SKU-RO", loc, "10", born_at)
    engine = _EngineStub(pg_pool)

    before_clean = await _snapshot(pg_pool, namespace_id)
    clean = await do_reconcile_dead_stock(engine, {"namespace_id": namespace_id})
    assert clean["divergent"] == []
    assert [entry["sku"] for entry in clean["balanced"]] == ["SKU-RO"], clean
    assert await _snapshot(pg_pool, namespace_id) == before_clean

    await _drive_on_hand_off_the_ledger(pg_pool, namespace_id, "SKU-RO", loc, "7")
    before_raise = await _snapshot(pg_pool, namespace_id)
    with pytest.raises(LedgerDivergenceError):
        await do_reconcile_dead_stock(engine, {"namespace_id": namespace_id})
    assert await _snapshot(pg_pool, namespace_id) == before_raise


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reconcile_scopes_by_namespace_explicitly(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    make_namespace: Any,
) -> None:
    """🔴 Assertion (g). A divergence seeded in namespace B does not appear in,
    and does not raise from, a reconcile of namespace A.

    Driven twice on purpose. Through the unprivileged ``nce_app`` pool
    (FORCE-RLS-subject) it proves the production path. Through the OWNER pool
    — which BYPASSES FORCE RLS, and where a false proof has shipped three
    times — it proves the explicit ``namespace_id = $1::uuid`` predicate is
    what does the scoping, not RLS: drop that predicate and this half raises.
    """
    ns_a = await make_namespace()
    ns_b = await make_namespace()
    born_at = await _days_ago(pg_pool, 200)
    loc_b = await _seed_location(pg_pool, ns_b, "B-Depot")
    await _seed_item(pg_pool, ns_b, "SKU-OTHER-NS", loc_b, "10", created_at=born_at)
    await _seed_txn(pg_pool, ns_b, "SKU-OTHER-NS", loc_b, "10", born_at)
    await _drive_on_hand_off_the_ledger(pg_pool, ns_b, "SKU-OTHER-NS", loc_b, "7")

    # Namespace A's own dead set: one honest, balanced pair.
    loc_a = await _seed_location(pg_pool, ns_a, "A-Depot")
    await _seed_item(pg_pool, ns_a, "SKU-MINE", loc_a, "4", created_at=born_at)
    await _seed_txn(pg_pool, ns_a, "SKU-MINE", loc_a, "4", born_at)

    app_pool = await asyncpg.create_pool(_app_dsn(), min_size=1, max_size=2)
    assert app_pool is not None
    try:
        via_app = await do_reconcile_dead_stock(_EngineStub(app_pool), {"namespace_id": ns_a})
    finally:
        await app_pool.close()

    via_owner = await do_reconcile_dead_stock(_EngineStub(pg_pool), {"namespace_id": ns_a})

    for result in (via_app, via_owner):
        assert result["divergent"] == []
        assert [entry["sku"] for entry in result["balanced"]] == ["SKU-MINE"]
        assert result["dead_pairs"] == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dead_stock_days_override_narrows_the_window(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """Assertion (h)'s integration half: omitting ``dead_stock_days`` uses the
    config file's value, and an explicit override is honoured."""
    born_at = await _days_ago(pg_pool, 30)
    loc = await _seed_location(pg_pool, namespace_id, "Depot-Window")
    await _seed_item(pg_pool, namespace_id, "SKU-WINDOW", loc, "6", created_at=born_at)
    await _seed_txn(pg_pool, namespace_id, "SKU-WINDOW", loc, "6", born_at)
    engine = _EngineStub(pg_pool)

    default_run = await do_reconcile_dead_stock(engine, {"namespace_id": namespace_id})
    assert default_run["dead_stock_days"] == 90
    assert default_run["dead_pairs"] == 0

    override_run = await do_reconcile_dead_stock(
        engine, {"namespace_id": namespace_id, "dead_stock_days": 10}
    )
    assert override_run["dead_stock_days"] == 10
    assert [entry["sku"] for entry in override_run["balanced"]] == ["SKU-WINDOW"]
