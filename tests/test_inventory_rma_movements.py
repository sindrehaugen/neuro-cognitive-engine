"""Tests for the two RMA stock legs (Module 11, Wave 10b — Batch 138b —
``nce/vertical_modules/inventory/rma.py::do_restock_from_rma`` /
``do_dispose_rma_weee``).

Batch 138 (``tests/test_inventory_rma.py``) proved ``do_record_rma`` records
and moves NO stock. This file proves the opposite half: that each stock leg
actually moves ``inventory_items`` AND appends a typed ``inventory_transactions``
row, inside the SAME transaction as the quantity write, and that a rolled-back
leg leaves neither a ledger row nor a settled RMA behind.

🔴 The disposal leg is the one this file exists to discriminate. A test that
asserts only ``qty_on_hand`` after ``do_dispose_rma_weee`` would pass
identically whether or not the ledger append happened — assertion (a) below
also counts and inspects the ``inventory_transactions`` row for exactly that
reason (nothing arrives on this leg; the ledger row is the only trace).

Covers, per the wave's acceptance table:
  (a) 🔴 the disposal ledger row — qty_on_hand AND exactly one ledger row.
  (b) the restock leg creates inventory_items + ledger row + kg_nodes.
  (c) 🔴 atomicity — an oversell disposal leaves everything byte-identical.
  (d) 🔴 idempotency — a second call on a settled rma_ref is refused, and the
      totals after both calls are exactly one ledger row / one qty change.
  (e) WEEE-scope guard + the disposal_ref requirement (Python AND the CHECK).
  (f) an unknown rma_ref raises RmaNotFoundError and writes nothing.

Unit-tier validator tests use a ``_DummyEngine`` with ``pg_pool = None`` —
every validated field (namespace_id/rma_ref/disposal_ref) is rejected before
any DB call, mirroring ``test_inventory_stock.py`` / ``test_inventory_rma.py``'s
own convention.

``_seed_ownership`` is copied verbatim in shape from ``test_inventory_stock.py``
(same idiom as ``tests/test_agreements_authoring.py:113``) and is NOT added to
``conftest.py`` — that would silently disarm the deliberate deny-by-default
proofs at ``tests/test_project_convert.py:587`` and
``tests/test_system_design_graph.py:549``. Any assertion about ``nce_app``
behaviour builds its engine from the UNPRIVILEGED pool via ``_app_dsn()`` —
the owner pool bypasses FORCE RLS and has shipped a false proof three times
(B67, B120, B130).
"""

from __future__ import annotations

import os
import uuid
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse, urlunparse

import asyncpg  # type: ignore[import-untyped]
import pytest

from nce.auth import set_namespace_context
from nce.config import cfg
from nce.entity_resolution.ownership_seed import seed_node_ownership_registry
from nce.vertical_modules.inventory.rma import (
    WEEE_AWAITING_COLLECTION,
    RmaAlreadySettledError,
    RmaNotFoundError,
    RmaNotWeeeScopeError,
    do_dispose_rma_weee,
    do_record_rma,
    do_restock_from_rma,
)
from nce.vertical_modules.inventory.stock import InsufficientStockError

# ---------------------------------------------------------------------------
# 1. Pure-logic validation (no DB) — driven through the PUBLIC functions,
# never a reimplementation of their validators.
# ---------------------------------------------------------------------------


class _DummyEngine:
    pg_pool = None


@pytest.mark.asyncio
async def test_restock_rejects_missing_namespace_id() -> None:
    with pytest.raises(ValueError, match="'namespace_id' is required"):
        await do_restock_from_rma(_DummyEngine(), {"rma_ref": "RMA-1"})


@pytest.mark.asyncio
async def test_restock_rejects_missing_rma_ref() -> None:
    with pytest.raises(ValueError, match="'rma_ref' is required"):
        await do_restock_from_rma(_DummyEngine(), {"namespace_id": uuid.uuid4(), "rma_ref": ""})


@pytest.mark.asyncio
async def test_dispose_rejects_missing_disposal_ref_before_any_db_call() -> None:
    """The Python mirror of migration 053's ``inventory_rma_disposed_requires_ref``
    CHECK — refused before ``engine.pg_pool`` is ever touched (a ``_DummyEngine``
    with ``pg_pool = None`` is safe here)."""
    with pytest.raises(ValueError, match="disposal_ref"):
        await do_dispose_rma_weee(
            _DummyEngine(), {"namespace_id": uuid.uuid4(), "rma_ref": "RMA-1"}
        )


@pytest.mark.asyncio
async def test_dispose_rejects_blank_disposal_ref() -> None:
    with pytest.raises(ValueError, match="disposal_ref"):
        await do_dispose_rma_weee(
            _DummyEngine(),
            {"namespace_id": uuid.uuid4(), "rma_ref": "RMA-1", "disposal_ref": "   "},
        )


@pytest.mark.asyncio
async def test_dispose_rejects_missing_namespace_id() -> None:
    with pytest.raises(ValueError, match="'namespace_id' is required"):
        await do_dispose_rma_weee(_DummyEngine(), {"rma_ref": "RMA-1", "disposal_ref": "TB-1"})


# ---------------------------------------------------------------------------
# Integration helpers — copied in shape from tests/test_inventory_stock.py
# (same idiom as tests/test_agreements_authoring.py:113), NOT imported and
# NOT added to conftest.py (see module docstring).
# ---------------------------------------------------------------------------


class _EngineStub:
    def __init__(self, pg_pool: asyncpg.Pool) -> None:  # type: ignore[type-arg]
        self.pg_pool = pg_pool


def _app_dsn() -> str:
    """Rewrite the integration DSN onto the restricted ``nce_app`` role.
    Verbatim in shape from ``tests/test_inventory_stock.py::_app_dsn``."""
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
    """Seed the node-ownership registry so the guarded graph mirror passes
    for this namespace. Copied in shape from
    ``tests/test_inventory_stock.py::_seed_ownership`` — NOT called from
    conftest.py's fixtures on purpose (see module docstring)."""
    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, namespace_id)
            await seed_node_ownership_registry(conn, namespace_id)


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


async def _get_on_hand(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    sku: str,
    location_id: uuid.UUID,
) -> Decimal | None:
    async with pg_pool.acquire() as conn:
        return await conn.fetchval(  # type: ignore[no-any-return]
            "SELECT qty_on_hand FROM inventory_items "
            "WHERE namespace_id = $1 AND sku = $2 AND location_id = $3",
            namespace_id,
            sku,
            location_id,
        )


async def _ledger_rows(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    sku: str,
    location_id: uuid.UUID,
) -> list[Any]:
    async with pg_pool.acquire() as conn:
        return await conn.fetch(
            "SELECT delta, reason_category, ref FROM inventory_transactions "
            "WHERE namespace_id = $1 AND sku = $2 AND location_id = $3",
            namespace_id,
            sku,
            location_id,
        )


async def _rma_state(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    rma_ref: str,
) -> Any:
    async with pg_pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT stock_movement_state, weee_state, disposal_ref FROM inventory_rma "
            "WHERE namespace_id = $1 AND rma_ref = $2",
            namespace_id,
            rma_ref,
        )


async def _seed_rma(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    *,
    rma_ref: str,
    sku: str,
    location_id: uuid.UUID,
    qty: Decimal,
    weee_state: str = "not_applicable",
) -> None:
    """Seed one ``inventory_rma`` row through the PUBLIC, sole writer
    ``do_record_rma`` — never a direct INSERT — so this file never
    reimplements Batch 138's own write path."""
    engine = _EngineStub(pg_pool)
    result = await do_record_rma(
        engine,
        {
            "namespace_id": namespace_id,
            "rma_ref": rma_ref,
            "sku": sku,
            "location": location_id,
            "qty": qty,
            "reason": "customer return",
            "weee_state": weee_state,
        },
    )
    assert result["ok"] is True
    assert result["stock_movement_state"] == "pending"


# ---------------------------------------------------------------------------
# 2. do_dispose_rma_weee — the leg nothing arrives for.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_disposal_decrements_and_appends_exactly_one_ledger_row(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """🔴 Assertion (a). A test that only checked ``qty_on_hand == 7`` would
    pass identically with the ``append_transaction`` call deleted from the
    disposal leg — that is exactly the defect this wave exists to prevent, so
    this asserts the ledger row's full content too."""
    await _seed_ownership(pg_pool, namespace_id)
    loc = await _seed_location(pg_pool, namespace_id, "Depot")
    await _seed_item(pg_pool, namespace_id, "SKU-DISPOSE", loc, on_hand="10")
    await _seed_rma(
        pg_pool,
        namespace_id,
        rma_ref="RMA-DISPOSE-1",
        sku="SKU-DISPOSE",
        location_id=loc,
        qty=Decimal("3"),
        weee_state=WEEE_AWAITING_COLLECTION,
    )
    engine = _EngineStub(pg_pool)

    result = await do_dispose_rma_weee(
        engine,
        {"namespace_id": namespace_id, "rma_ref": "RMA-DISPOSE-1", "disposal_ref": "TB-SCHEME-1"},
    )

    assert result["ok"] is True
    assert result["on_hand"] == Decimal("7.000")
    assert result["stock_movement_state"] == "disposed"
    assert result["weee_state"] == "disposed"

    on_hand = await _get_on_hand(pg_pool, namespace_id, "SKU-DISPOSE", loc)
    assert on_hand == Decimal("7.000")

    rows = await _ledger_rows(pg_pool, namespace_id, "SKU-DISPOSE", loc)
    assert len(rows) == 1
    assert rows[0]["delta"] == Decimal("-3.000")
    assert rows[0]["reason_category"] == "adjustment"
    assert rows[0]["ref"] == "rma:RMA-DISPOSE-1"

    state = await _rma_state(pg_pool, namespace_id, "RMA-DISPOSE-1")
    assert state["stock_movement_state"] == "disposed"
    assert state["weee_state"] == "disposed"
    assert state["disposal_ref"] == "TB-SCHEME-1"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_disposal_oversell_is_atomic(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """🔴 Assertion (c). Disposing MORE than is on hand raises
    ``InsufficientStockError`` and leaves everything byte-identical: this
    goes RED if the claim ``UPDATE`` were committed in its own transaction,
    or if ``append_transaction`` were given its own connection instead of the
    already-open ``conn``."""
    await _seed_ownership(pg_pool, namespace_id)
    loc = await _seed_location(pg_pool, namespace_id, "Depot")
    await _seed_item(pg_pool, namespace_id, "SKU-OVERSELL", loc, on_hand="2")
    await _seed_rma(
        pg_pool,
        namespace_id,
        rma_ref="RMA-OVERSELL-1",
        sku="SKU-OVERSELL",
        location_id=loc,
        qty=Decimal("5"),
        weee_state=WEEE_AWAITING_COLLECTION,
    )
    engine = _EngineStub(pg_pool)

    with pytest.raises(InsufficientStockError):
        await do_dispose_rma_weee(
            engine,
            {
                "namespace_id": namespace_id,
                "rma_ref": "RMA-OVERSELL-1",
                "disposal_ref": "TB-SCHEME-2",
            },
        )

    on_hand = await _get_on_hand(pg_pool, namespace_id, "SKU-OVERSELL", loc)
    assert on_hand == Decimal("2.000")

    rows = await _ledger_rows(pg_pool, namespace_id, "SKU-OVERSELL", loc)
    assert len(rows) == 0

    state = await _rma_state(pg_pool, namespace_id, "RMA-OVERSELL-1")
    assert state["stock_movement_state"] == "pending"
    assert state["weee_state"] == WEEE_AWAITING_COLLECTION


@pytest.mark.integration
@pytest.mark.asyncio
async def test_disposal_twice_is_refused_and_settles_once(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """🔴 Assertion (d). Calling the same leg twice on the same ``rma_ref``
    raises ``RmaAlreadySettledError`` the second time — this goes RED if the
    ``AND stock_movement_state = 'pending'`` predicate were dropped from
    ``_claim_rma``, which would double-decrement and double-ledger instead."""
    await _seed_ownership(pg_pool, namespace_id)
    loc = await _seed_location(pg_pool, namespace_id, "Depot")
    await _seed_item(pg_pool, namespace_id, "SKU-TWICE", loc, on_hand="10")
    await _seed_rma(
        pg_pool,
        namespace_id,
        rma_ref="RMA-TWICE-1",
        sku="SKU-TWICE",
        location_id=loc,
        qty=Decimal("4"),
        weee_state=WEEE_AWAITING_COLLECTION,
    )
    engine = _EngineStub(pg_pool)
    params = {"namespace_id": namespace_id, "rma_ref": "RMA-TWICE-1", "disposal_ref": "TB-3"}

    await do_dispose_rma_weee(engine, params)
    with pytest.raises(RmaAlreadySettledError) as exc_info:
        await do_dispose_rma_weee(engine, params)
    assert exc_info.value.stock_movement_state == "disposed"

    on_hand = await _get_on_hand(pg_pool, namespace_id, "SKU-TWICE", loc)
    assert on_hand == Decimal("6.000")

    rows = await _ledger_rows(pg_pool, namespace_id, "SKU-TWICE", loc)
    assert len(rows) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_disposal_refused_when_rma_not_weee_scope(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """Assertion (e), first half: a disposal attempt against an RMA whose
    ``weee_state == 'not_applicable'`` is a contradiction, refused before any
    stock moves — goes RED if the ``AND weee_state <> 'not_applicable'``
    predicate were dropped from ``_claim_rma``."""
    await _seed_ownership(pg_pool, namespace_id)
    loc = await _seed_location(pg_pool, namespace_id, "Depot")
    await _seed_item(pg_pool, namespace_id, "SKU-NOTWEEE", loc, on_hand="10")
    await _seed_rma(
        pg_pool,
        namespace_id,
        rma_ref="RMA-NOTWEEE-1",
        sku="SKU-NOTWEEE",
        location_id=loc,
        qty=Decimal("2"),
        weee_state="not_applicable",
    )
    engine = _EngineStub(pg_pool)

    with pytest.raises(RmaNotWeeeScopeError):
        await do_dispose_rma_weee(
            engine,
            {"namespace_id": namespace_id, "rma_ref": "RMA-NOTWEEE-1", "disposal_ref": "TB-4"},
        )

    on_hand = await _get_on_hand(pg_pool, namespace_id, "SKU-NOTWEEE", loc)
    assert on_hand == Decimal("10.000")
    rows = await _ledger_rows(pg_pool, namespace_id, "SKU-NOTWEEE", loc)
    assert len(rows) == 0
    state = await _rma_state(pg_pool, namespace_id, "RMA-NOTWEEE-1")
    assert state["stock_movement_state"] == "pending"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_direct_dml_disposed_without_ref_is_refused_by_the_check(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """Assertion (e), second half: even bypassing Python entirely, migration
    053's ``inventory_rma_disposed_requires_ref`` CHECK refuses a direct
    UPDATE that sets ``weee_state = 'disposed'`` with no ``disposal_ref`` —
    the storage-level half of the guard, independent of this module."""
    await _seed_ownership(pg_pool, namespace_id)
    loc = await _seed_location(pg_pool, namespace_id, "Depot")
    await _seed_rma(
        pg_pool,
        namespace_id,
        rma_ref="RMA-CHECK-1",
        sku="SKU-CHECK",
        location_id=loc,
        qty=Decimal("1"),
        weee_state=WEEE_AWAITING_COLLECTION,
    )

    async with pg_pool.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "UPDATE inventory_rma SET weee_state = 'disposed', disposal_ref = NULL "
                "WHERE namespace_id = $1 AND rma_ref = $2",
                namespace_id,
                "RMA-CHECK-1",
            )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unknown_rma_ref_raises_not_found_and_writes_nothing(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """Assertion (f): an unknown ``rma_ref`` raises ``RmaNotFoundError`` and
    writes nothing anywhere — goes RED if the diagnostic branch collapsed
    into a silent no-op return instead of raising."""
    engine = _EngineStub(pg_pool)

    with pytest.raises(RmaNotFoundError):
        await do_restock_from_rma(
            engine, {"namespace_id": namespace_id, "rma_ref": "RMA-DOES-NOT-EXIST"}
        )


# ---------------------------------------------------------------------------
# 3. do_restock_from_rma — a repairable unit returns to stock.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_restock_creates_item_row_ledger_and_kg_nodes(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """Assertion (b). At a location holding NO row for the SKU yet, the
    restock leg creates ``inventory_items`` at ``qty_on_hand == qty``,
    appends exactly one ``+qty`` ledger row, and creates the
    ``InventoryItem:{sku}:{loc}`` + ``StockLocation:{loc}`` kg_nodes — goes
    RED if the restock leg's ``append_transaction`` or
    ``mirror_item_at_location`` call were deleted."""
    await _seed_ownership(pg_pool, namespace_id)
    loc = await _seed_location(pg_pool, namespace_id, "Returns Depot")
    await _seed_rma(
        pg_pool,
        namespace_id,
        rma_ref="RMA-RESTOCK-1",
        sku="SKU-RESTOCK",
        location_id=loc,
        qty=Decimal("6"),
        weee_state="not_applicable",
    )
    engine = _EngineStub(pg_pool)

    result = await do_restock_from_rma(
        engine, {"namespace_id": namespace_id, "rma_ref": "RMA-RESTOCK-1"}
    )

    assert result["ok"] is True
    assert result["on_hand"] == Decimal("6.000")
    assert result["stock_movement_state"] == "restocked"

    on_hand = await _get_on_hand(pg_pool, namespace_id, "SKU-RESTOCK", loc)
    assert on_hand == Decimal("6.000")

    rows = await _ledger_rows(pg_pool, namespace_id, "SKU-RESTOCK", loc)
    assert len(rows) == 1
    assert rows[0]["delta"] == Decimal("6.000")
    assert rows[0]["reason_category"] == "adjustment"
    assert rows[0]["ref"] == "rma:RMA-RESTOCK-1"

    async with pg_pool.acquire() as conn:
        node_rows = await conn.fetch(
            "SELECT label, entity_type FROM kg_nodes WHERE namespace_id = $1", namespace_id
        )
    labels = {r["label"]: r["entity_type"] for r in node_rows}
    assert labels[f"StockLocation:{loc}"] == "STOCK_LOCATION"
    assert labels[f"InventoryItem:SKU-RESTOCK:{loc}"] == "INVENTORY_ITEM"

    state = await _rma_state(pg_pool, namespace_id, "RMA-RESTOCK-1")
    assert state["stock_movement_state"] == "restocked"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_restock_twice_is_refused(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """The restock leg's own idempotency proof, mirroring the disposal leg's
    (assertion (d)) — a second call on an already-restocked ``rma_ref``
    raises ``RmaAlreadySettledError`` rather than accumulating stock again."""
    await _seed_ownership(pg_pool, namespace_id)
    loc = await _seed_location(pg_pool, namespace_id, "Returns Depot")
    await _seed_rma(
        pg_pool,
        namespace_id,
        rma_ref="RMA-RESTOCK-TWICE",
        sku="SKU-RESTOCK-TWICE",
        location_id=loc,
        qty=Decimal("2"),
    )
    engine = _EngineStub(pg_pool)
    params = {"namespace_id": namespace_id, "rma_ref": "RMA-RESTOCK-TWICE"}

    await do_restock_from_rma(engine, params)
    with pytest.raises(RmaAlreadySettledError):
        await do_restock_from_rma(engine, params)

    on_hand = await _get_on_hand(pg_pool, namespace_id, "SKU-RESTOCK-TWICE", loc)
    assert on_hand == Decimal("2.000")
    rows = await _ledger_rows(pg_pool, namespace_id, "SKU-RESTOCK-TWICE", loc)
    assert len(rows) == 1
