"""Tests for the Inventory engine's stock-core concurrency module
(Module 11, Wave 2 — Batch 130 — ``nce/vertical_modules/inventory/stock.py``).

**Adversarial-audit fix-forward (Batch 130 rejection, finding #1):** the wave's
own docstring asserted, in detail, that this file exists and proves the
row-lock concurrency guard with REAL concurrent connections. It did not exist
in the diff — the audit's repo-wide grep for ``test_inventory_stock`` and for
``do_stock_levels|do_transfer_stock|do_record_consumption`` outside
``stock.py`` both came back empty. This file is that missing proof.

Covers, per ``do_stock_levels`` / ``do_transfer_stock`` / ``do_record_consumption``:

  1. Input validation (bool/None/NaN/non-positive quantities) — pure logic,
     no DB, exercised through the PUBLIC functions (never a reimplementation
     of ``_as_quantity``'s logic — Batch-repeat lesson: call the real
     function).
  2. The oversell/lost-update guard under REAL concurrent connections
     (``asyncio.gather`` over separate pool connections, not sequential calls
     on one connection — sequential calls would prove nothing about the
     race).
  3. The cross-location A→B / B→A opposite-direction lock ordering. This
     inverted in the second Batch-130 rejection: the previous revision of
     this file asserted the deadlock HAPPENS (and the module's docstring
     disclosed it as a same-SKU limitation), but the audit showed the cycle
     is really on the SKU-INDEPENDENT ``kg_nodes`` mirror rows, so CROSS-SKU
     two-way traffic deadlocked too — 60/120 rounds, i.e. every round. Both
     the cross-SKU and same-SKU cases are now asserted to produce ZERO
     ``DeadlockDetectedError`` (measured 0/120 and 0/24 after the canonical
     lock-order fix), with per-SKU conservation checked every round.
  4. Atomicity: a failed increment rolls back the paired decrement (same
     transaction).
  5. The full three-term reservation identity (``on_hand - reserved -
     blocked``), not a zero-reservation shortcut.
  6. Explicit namespace scoping in the query itself (not reliance on RLS —
     two namespaces holding the SAME sku at DIFFERENT location rows), plus a
     defense-in-depth FORCE RLS proof in which BOTH the write
     (``do_transfer_stock``) and the read (``do_stock_levels``) are driven
     through a real ``nce_app`` pool — not the superuser ``pg_pool`` fixture,
     which bypasses FORCE RLS on the write path and made the previous
     revision of that test non-discriminating.
  7. The KG mirror upsert (``STOCK_LOCATION`` / ``INVENTORY_ITEM`` nodes,
     the ``at`` edge).
  8. Decimal quantization to the column's own 3dp scale, round-tripped
     through a real DB write+read, using values where ``Decimal(str(x))`` and
     ``Decimal(x)`` DIVERGE at 3dp (the previous revision's only float,
     1.23456, quantises to 1.235 under both, so it could not detect the
     binary-float defect the module's docstring argues against at length).

Integration tests are ``@pytest.mark.integration`` — require a live Postgres
with migration 050 applied, and are wired into ci.yml's "Integration — M11
Inventory" step alongside ``test_inventory_tables.py``. Pure-logic validation
tests need no DB and always run in the unit job.
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
from nce.db_utils import scoped_pg_session
from nce.entity_resolution.ownership import OwnershipError
from nce.entity_resolution.ownership_seed import seed_node_ownership_registry
from nce.vertical_modules.inventory.stock import (
    InsufficientStockError,
    _upsert_kg_node,
    do_record_consumption,
    do_stock_levels,
    do_transfer_stock,
)

# ---------------------------------------------------------------------------
# Pure-logic tests: quantity validation, exercised through the PUBLIC
# functions (do_transfer_stock / do_record_consumption), not by reimplementing
# or directly poking _as_quantity — a test that re-derives the code path it
# protects cannot detect a divergence in that path.
#
# Validation raises before engine.pg_pool is ever touched (ns/sku/qty parsing
# happens before locations are parsed and before any DB call), so a dummy
# engine with pg_pool=None is safe here — mirrors test_inventory_tables.py's
# _DummyEngine convention for schema_seed.py's own input-validation tests.
# ---------------------------------------------------------------------------


class _DummyEngine:
    pg_pool = None


def _base_params(**overrides: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "namespace_id": uuid.uuid4(),
        "sku": "SKU-VALIDATE",
        "qty": 1,
    }
    params.update(overrides)
    return params


@pytest.mark.asyncio
async def test_transfer_rejects_bool_qty() -> None:
    """``isinstance(True, int)`` is ``True`` in Python — a bool qty must not
    silently pass as a quantity of 1."""
    with pytest.raises(ValueError, match="bool is not a quantity"):
        await do_transfer_stock(_DummyEngine(), _base_params(qty=True))


@pytest.mark.asyncio
async def test_record_consumption_rejects_bool_qty() -> None:
    with pytest.raises(ValueError, match="bool is not a quantity"):
        await do_record_consumption(_DummyEngine(), _base_params(qty=False, location=uuid.uuid4()))


@pytest.mark.asyncio
async def test_transfer_rejects_none_qty() -> None:
    params = _base_params()
    params["qty"] = None
    with pytest.raises(ValueError, match="a quantity is required"):
        await do_transfer_stock(_DummyEngine(), params)


@pytest.mark.asyncio
async def test_transfer_rejects_nan_qty() -> None:
    with pytest.raises(ValueError, match="must be finite"):
        await do_transfer_stock(_DummyEngine(), _base_params(qty=float("nan")))


@pytest.mark.asyncio
async def test_transfer_rejects_infinite_qty() -> None:
    with pytest.raises(ValueError, match="must be finite"):
        await do_transfer_stock(_DummyEngine(), _base_params(qty=float("inf")))


@pytest.mark.asyncio
async def test_transfer_rejects_zero_qty() -> None:
    with pytest.raises(ValueError, match=r"must be > 0"):
        await do_transfer_stock(_DummyEngine(), _base_params(qty=0))


@pytest.mark.asyncio
async def test_transfer_rejects_negative_qty() -> None:
    with pytest.raises(ValueError, match=r"must be > 0"):
        await do_transfer_stock(_DummyEngine(), _base_params(qty=-1))


@pytest.mark.asyncio
async def test_transfer_rejects_same_from_and_to_location() -> None:
    loc = uuid.uuid4()
    with pytest.raises(ValueError, match="must differ"):
        await do_transfer_stock(_DummyEngine(), _base_params(from_location=loc, to_location=loc))


# ---------------------------------------------------------------------------
# Integration helpers — seed stock_locations / inventory_items rows directly
# (owner pool), matching test_inventory_tables.py's convention. Every helper
# takes an explicit namespace_id and scopes its own SQL by it — this suite
# does not rely on RLS for its own scaffolding.
# ---------------------------------------------------------------------------


class _EngineStub:
    def __init__(self, pg_pool: asyncpg.Pool) -> None:  # type: ignore[type-arg]
        self.pg_pool = pg_pool


def _app_dsn() -> str:
    """Rewrite the integration DSN onto the restricted ``nce_app`` role.

    Verbatim in shape from ``tests/test_agreements_review.py::_app_dsn`` (the
    in-repo precedent for driving a vertical module through a REAL
    FORCE-RLS-subject connection instead of the superuser ``pg_pool``).
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


async def _seed_ownership(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """Seed the node-ownership registry so Inventory's guarded graph mirror
    (``_upsert_kg_node``, Batch 130a) passes for this namespace. Mirrors
    ``tests/test_agreements_authoring.py``'s ``_seed_ownership`` verbatim in
    shape. NOT called from conftest.py's fixtures on purpose — see the
    B130a wave's amendment for why (it would disarm two deliberate
    deny-by-default proofs elsewhere)."""
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


async def _get_on_hand(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    sku: str,
    location_id: uuid.UUID,
) -> Decimal:
    async with pg_pool.acquire() as conn:
        value = await conn.fetchval(
            "SELECT qty_on_hand FROM inventory_items "
            "WHERE namespace_id = $1 AND sku = $2 AND location_id = $3",
            namespace_id,
            sku,
            location_id,
        )
    assert value is not None
    return value  # type: ignore[no-any-return]


async def _set_on_hand(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    sku: str,
    location_id: uuid.UUID,
    on_hand: str,
) -> None:
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE inventory_items SET qty_on_hand = $4 "
            "WHERE namespace_id = $1 AND sku = $2 AND location_id = $3",
            namespace_id,
            sku,
            location_id,
            Decimal(on_hand),
        )


# ---------------------------------------------------------------------------
# 1. do_stock_levels — reservation algebra + explicit namespace scoping
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_do_stock_levels_computes_full_three_term_identity(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """Pins the reservation algebra: ``available = on_hand - reserved -
    blocked`` is always the full subtraction, not a shortcut that assumes
    zero reservations — seeded directly since this wave has no writer for
    qty_reserved/qty_blocked yet."""
    loc = await _seed_location(pg_pool, namespace_id, "Main Warehouse")
    await _seed_item(
        pg_pool, namespace_id, "SKU-RESERVE", loc, on_hand="10", reserved="3", blocked="2"
    )
    engine = _EngineStub(pg_pool)

    result = await do_stock_levels(
        engine, {"namespace_id": namespace_id, "sku": "SKU-RESERVE", "location": loc}
    )

    assert result["ok"] is True
    assert len(result["items"]) == 1
    item = result["items"][0]
    assert item["on_hand"] == Decimal("10.000")
    assert item["reserved"] == Decimal("3.000")
    assert item["blocked"] == Decimal("2.000")
    assert item["available"] == Decimal("5.000")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_do_stock_levels_scopes_by_namespace_explicitly(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    make_namespace: Any,
) -> None:
    """Two namespaces each hold a row for the SAME sku at a DIFFERENT
    location — proves the query's own WHERE clause scopes by namespace_id
    (this call goes through the owner pool, which bypasses FORCE RLS
    entirely, so a pass here is not an RLS artifact — see lesson: don't rely
    on RLS for correctness, and same-label rows across namespaces make a
    scoping bug a silent wrong-tenant answer, not a crash)."""
    ns_a = await make_namespace()
    ns_b = await make_namespace()
    loc_a = await _seed_location(pg_pool, ns_a, "Shared Name")
    loc_b = await _seed_location(pg_pool, ns_b, "Shared Name")
    await _seed_item(pg_pool, ns_a, "SKU-SHARED", loc_a, on_hand="7")
    await _seed_item(pg_pool, ns_b, "SKU-SHARED", loc_b, on_hand="999")
    engine = _EngineStub(pg_pool)

    result = await do_stock_levels(engine, {"namespace_id": ns_a, "sku": "SKU-SHARED"})

    assert len(result["items"]) == 1
    assert result["items"][0]["on_hand"] == Decimal("7.000")
    assert result["items"][0]["location_id"] == str(loc_a)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rows_written_by_do_transfer_stock_are_rls_isolated(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    make_namespace: Any,
) -> None:
    """FORCE RLS defense-in-depth with BOTH halves on a real ``nce_app`` pool.

    Batch 130 rejection, finding #3: the previous revision built the engine
    from the SUPERUSER ``pg_pool`` fixture, so ``do_transfer_stock``'s writes
    were never subject to FORCE RLS at all — only the read-back COUNTs went
    through ``nce_app``. Rebinding ``scoped_pg_session`` to a version that
    never called ``set_namespace_context`` left every test green, which is the
    definition of a non-discriminating proof.

    Here the ENGINE ITSELF holds an ``nce_app`` pool (precedent:
    ``tests/test_agreements_review.py``'s ``_app_dsn`` + ``EngineStub``), so
    ``scoped_pg_session``'s ``SET LOCAL nce.namespace_id`` is load-bearing on
    the write path: without it ``get_nce_namespace()`` is NULL, the
    ``tenant_isolation_policy`` ``WITH CHECK`` refuses the UPDATE/INSERT, and
    the transfer fails outright. Seeding still uses the owner pool (scaffolding
    only, exactly as the rest of this suite does)."""
    ns_a = await make_namespace()
    ns_b = await make_namespace()
    await _seed_ownership(pg_pool, ns_a)
    await _seed_ownership(pg_pool, ns_b)
    loc_a1 = await _seed_location(pg_pool, ns_a, "A1")
    loc_a2 = await _seed_location(pg_pool, ns_a, "A2")
    await _seed_item(pg_pool, ns_a, "SKU-RLS-WRITE", loc_a1, on_hand="20")

    app_pool = await asyncpg.create_pool(_app_dsn(), min_size=1, max_size=2)
    engine = _EngineStub(app_pool)
    try:
        # WRITE through nce_app — subject to FORCE RLS.
        result = await do_transfer_stock(
            engine,
            {
                "namespace_id": ns_a,
                "sku": "SKU-RLS-WRITE",
                "qty": 5,
                "from_location": loc_a1,
                "to_location": loc_a2,
            },
        )
        assert result["from_on_hand"] == Decimal("15.000")
        assert result["to_on_hand"] == Decimal("5.000")

        # READ through nce_app — ns_a sees its own two rows with the written
        # values...
        levels_a = await do_stock_levels(engine, {"namespace_id": ns_a, "sku": "SKU-RLS-WRITE"})
        by_location = {item["location_id"]: item["on_hand"] for item in levels_a["items"]}
        assert by_location == {
            str(loc_a1): Decimal("15.000"),
            str(loc_a2): Decimal("5.000"),
        }, f"ns_a must see both rows do_transfer_stock wrote, got {levels_a['items']}"

        # ...and ns_b sees nothing of them.
        levels_b = await do_stock_levels(engine, {"namespace_id": ns_b, "sku": "SKU-RLS-WRITE"})
        assert levels_b["items"] == [], "ns_b must not see ns_a's stock rows"

        # Belt-and-braces: the rows are invisible to an nce_app connection in
        # ns_b's context even when it asks for ns_a's namespace_id EXPLICITLY —
        # RLS, not the query's own WHERE clause, is what refuses this one.
        async with app_pool.acquire() as conn, conn.transaction():
            await set_namespace_context(conn, ns_b)
            visible_from_b = await conn.fetchval(
                "SELECT COUNT(*) FROM inventory_items WHERE namespace_id = $1", ns_a
            )
        assert visible_from_b == 0, "ns_b must not see ns_a's rows written by do_transfer_stock"

        # Module 11, Wave 11: the SAME nce_app-driven write also appended two
        # inventory_transactions ledger rows — proves the ledger append is
        # subject to FORCE RLS too (a namespace-context bug in the append
        # path would make its INSERT's WITH CHECK refuse the write outright,
        # aborting the whole transfer — so a passing transfer above already
        # implies this, but assert it directly rather than by inference).
        async with app_pool.acquire() as conn, conn.transaction():
            await set_namespace_context(conn, ns_a)
            ledger_count_a = await conn.fetchval(
                "SELECT COUNT(*) FROM inventory_transactions WHERE namespace_id = $1", ns_a
            )
        assert ledger_count_a == 2, "ns_a must see both ledger rows do_transfer_stock wrote"

        async with app_pool.acquire() as conn, conn.transaction():
            await set_namespace_context(conn, ns_b)
            ledger_count_b = await conn.fetchval(
                "SELECT COUNT(*) FROM inventory_transactions WHERE namespace_id = $1", ns_a
            )
        assert ledger_count_b == 0, "ns_b must not see ns_a's inventory_transactions rows"
    finally:
        await app_pool.close()


# ---------------------------------------------------------------------------
# 2. do_record_consumption / do_transfer_stock — business logic + quantization
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_record_consumption_decrements_and_refuses_when_insufficient(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    await _seed_ownership(pg_pool, namespace_id)
    loc = await _seed_location(pg_pool, namespace_id, "Warehouse")
    await _seed_item(pg_pool, namespace_id, "SKU-BASIC", loc, on_hand="10")
    engine = _EngineStub(pg_pool)

    result = await do_record_consumption(
        engine, {"namespace_id": namespace_id, "sku": "SKU-BASIC", "qty": 4, "location": loc}
    )
    assert result["ok"] is True
    assert result["on_hand"] == Decimal("6.000")

    with pytest.raises(InsufficientStockError) as excinfo:
        await do_record_consumption(
            engine, {"namespace_id": namespace_id, "sku": "SKU-BASIC", "qty": 100, "location": loc}
        )
    assert excinfo.value.available_on_hand == Decimal("6.000")
    assert excinfo.value.requested == Decimal("100.000")

    final = await _get_on_hand(pg_pool, namespace_id, "SKU-BASIC", loc)
    assert final == Decimal("6.000"), "a refused consumption must not touch the row"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_transfer_qty_is_quantised_to_3dp_before_binding(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """Round-trips floats through a real NUMERIC(18,3) column and pins the
    ``Decimal(str(x))``-not-``Decimal(x)`` choice stock.py argues for at
    length in its "Quantity precision" docstring section.

    Batch 130 rejection, finding #2: this test used to bind only 1.23456,
    which quantises to 1.235 under BOTH paths — so mutating
    ``Decimal(str(value))`` to ``Decimal(value)`` left the whole suite green
    and the docstring's central precision claim was untested. The two extra
    values below DIVERGE at 3dp, because their nearest binary double sits
    just BELOW the decimal tie that ROUND_HALF_UP would round up:

      * ``0.0045`` → binary double is 0.004499999999999999659…
        ``Decimal(str(x))`` → **0.005**, ``Decimal(x)`` → 0.004
      * ``2.6755`` → binary double is 2.675499999999999989341…
        ``Decimal(str(x))`` → **2.676**, ``Decimal(x)`` → 2.675

    Each assertion is checked on the value the module returned AND on the
    value read back out of the column, so a Postgres-side rounding
    difference could not mask a Python-side one."""
    await _seed_ownership(pg_pool, namespace_id)
    loc_a = await _seed_location(pg_pool, namespace_id, "From")
    loc_b = await _seed_location(pg_pool, namespace_id, "To")
    await _seed_item(pg_pool, namespace_id, "SKU-QUANT", loc_a, on_hand="10")
    engine = _EngineStub(pg_pool)

    async def _transfer(qty: float) -> dict[str, Any]:
        return await do_transfer_stock(
            engine,
            {
                "namespace_id": namespace_id,
                "sku": "SKU-QUANT",
                "qty": qty,
                "from_location": loc_a,
                "to_location": loc_b,
            },
        )

    # Plain ROUND_HALF_UP truncation of a 5dp value (no float divergence).
    result = await _transfer(1.23456)
    assert result["qty"] == Decimal("1.235")
    assert result["from_on_hand"] == Decimal("8.765")
    assert result["to_on_hand"] == Decimal("1.235")

    # Divergent #1: Decimal(x) would yield 0.004 here, one tick short.
    result = await _transfer(0.0045)
    assert result["qty"] == Decimal("0.005"), (
        "0.0045 must quantise via Decimal(str(x)) to 0.005; 0.004 means the "
        "raw binary-float value was quantised instead"
    )
    assert result["from_on_hand"] == Decimal("8.760")
    assert result["to_on_hand"] == Decimal("1.240")

    # Divergent #2: Decimal(x) would yield 2.675 here.
    result = await _transfer(2.6755)
    assert result["qty"] == Decimal("2.676"), (
        "2.6755 must quantise via Decimal(str(x)) to 2.676; 2.675 means the "
        "raw binary-float value was quantised instead"
    )
    assert result["from_on_hand"] == Decimal("6.084")
    assert result["to_on_hand"] == Decimal("3.916")

    # Same numbers, re-read from the real NUMERIC(18,3) columns.
    assert await _get_on_hand(pg_pool, namespace_id, "SKU-QUANT", loc_a) == Decimal("6.084")
    assert await _get_on_hand(pg_pool, namespace_id, "SKU-QUANT", loc_b) == Decimal("3.916")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_repeated_transfers_into_same_location_accumulate_not_overwrite(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """Pins ``increment_on_hand``'s claim of "ONE upsert statement... no
    read-then-write": a second transfer into an already-populated location
    must ADD, never overwrite. (Mutation-verified — see fix-forward report:
    dropping the '+' in the ON CONFLICT SET makes this go RED.)"""
    await _seed_ownership(pg_pool, namespace_id)
    loc_a = await _seed_location(pg_pool, namespace_id, "From")
    loc_b = await _seed_location(pg_pool, namespace_id, "To")
    await _seed_item(pg_pool, namespace_id, "SKU-ACCUM", loc_a, on_hand="100")
    engine = _EngineStub(pg_pool)

    r1 = await do_transfer_stock(
        engine,
        {
            "namespace_id": namespace_id,
            "sku": "SKU-ACCUM",
            "qty": 5,
            "from_location": loc_a,
            "to_location": loc_b,
        },
    )
    assert r1["to_on_hand"] == Decimal("5.000")

    r2 = await do_transfer_stock(
        engine,
        {
            "namespace_id": namespace_id,
            "sku": "SKU-ACCUM",
            "qty": 3,
            "from_location": loc_a,
            "to_location": loc_b,
        },
    )
    assert r2["to_on_hand"] == Decimal("8.000"), "must accumulate 5+3=8, not overwrite to 3"

    final_b = await _get_on_hand(pg_pool, namespace_id, "SKU-ACCUM", loc_b)
    assert final_b == Decimal("8.000")


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bogus_to_location",
    [
        pytest.param(uuid.UUID(int=0), id="to-sorts-first"),
        pytest.param(uuid.UUID(int=(1 << 128) - 1), id="to-sorts-last"),
    ],
)
async def test_transfer_rolls_back_from_decrement_when_to_location_is_invalid(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    bogus_to_location: uuid.UUID,
) -> None:
    """Atomicity: the decrement at from_location and the increment at
    to_location share ONE transaction (scoped_pg_session) — when the
    increment fails (bogus to_location), the decrement must not have stuck.
    (Mutation-verified — see fix-forward report: splitting the two writes
    into separate scoped_pg_session blocks makes this go RED.)

    Both canonical-order branches are forced DETERMINISTICALLY rather than
    sampled: a seeded ``from_location`` is a random uuid4, so a random bogus
    ``to_location`` would exercise whichever branch by a 50/50 coin flip per
    run. The nil UUID always sorts BEFORE it (the increment is attempted
    first) and the all-ones UUID always sorts AFTER it (the decrement is
    applied first, then rolled back). Stock is sufficient here, so the FK
    violation is the ONLY failure in both branches and the raised type is
    ValueError either way — see
    ``test_doubly_invalid_transfer_error_type_is_lock_order_dependent`` for
    the case where that is NOT true.

    Module 11, Wave 11 addition: for the "to-sorts-last" branch, the
    decrement AND its ``inventory_transactions`` ledger append both already
    ran (successfully) before the increment fails — the strongest available
    proof that the ledger append is inside the SAME transaction as the row
    write, not a separate one. If it were separate, this row would already
    be durably committed and the assertion below would go RED."""
    await _seed_ownership(pg_pool, namespace_id)
    loc_a = await _seed_location(pg_pool, namespace_id, "From")
    await _seed_item(pg_pool, namespace_id, "SKU-ATOMIC", loc_a, on_hand="10")
    engine = _EngineStub(pg_pool)

    with pytest.raises(ValueError, match="does not exist in this namespace"):
        await do_transfer_stock(
            engine,
            {
                "namespace_id": namespace_id,
                "sku": "SKU-ATOMIC",
                "qty": 3,
                "from_location": loc_a,
                "to_location": bogus_to_location,
            },
        )

    final = await _get_on_hand(pg_pool, namespace_id, "SKU-ATOMIC", loc_a)
    assert final == Decimal("10.000"), (
        "from_location's decrement must roll back when to_location's write fails"
    )

    async with pg_pool.acquire() as conn:
        ledger_count = await conn.fetchval(
            "SELECT COUNT(*) FROM inventory_transactions WHERE namespace_id = $1 AND sku = $2",
            namespace_id,
            "SKU-ATOMIC",
        )
    assert ledger_count == 0, (
        "a rolled-back transfer must leave NO inventory_transactions row behind — "
        "the ledger append is not co-transactional with the row write if this fails"
    )


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("bogus_to_location", "expected"),
    [
        pytest.param(uuid.UUID(int=0), ValueError, id="to-sorts-first-raises-fk-ValueError"),
        pytest.param(
            uuid.UUID(int=(1 << 128) - 1),
            InsufficientStockError,
            id="to-sorts-last-raises-InsufficientStockError",
        ),
    ],
)
async def test_doubly_invalid_transfer_error_type_is_lock_order_dependent(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    bogus_to_location: uuid.UUID,
    expected: type[Exception],
) -> None:
    """Pins the ONE behaviour the canonical lock order changed.

    When a transfer is BOTH short of stock at from_location AND names a
    to_location that is not a real stock_locations row, whichever write the
    canonical order runs FIRST decides the exception type: the nil UUID sorts
    first so the FK violation fires (ValueError), the all-ones UUID sorts last
    so the stock guard fires first (InsufficientStockError). Before the
    reorder it was always InsufficientStockError.

    This is documented in stock.py's 'Consequence' section; the test exists so
    the documented behaviour is ENFORCED rather than merely asserted in prose.
    Either way the transfer must roll back completely — checked below."""
    await _seed_ownership(pg_pool, namespace_id)
    loc_a = await _seed_location(pg_pool, namespace_id, "From")
    await _seed_item(pg_pool, namespace_id, "SKU-DOUBLY-INVALID", loc_a, on_hand="1")
    engine = _EngineStub(pg_pool)

    with pytest.raises(expected):
        await do_transfer_stock(
            engine,
            {
                "namespace_id": namespace_id,
                "sku": "SKU-DOUBLY-INVALID",
                "qty": 99,  # more than the 1 on hand -> the stock guard also fails
                "from_location": loc_a,
                "to_location": bogus_to_location,
            },
        )

    final = await _get_on_hand(pg_pool, namespace_id, "SKU-DOUBLY-INVALID", loc_a)
    assert final == Decimal("1.000"), "a doubly-invalid transfer must change nothing"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_increment_before_decrement_branch_still_refuses_oversell(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """Pins the surprising half of the canonical lock order.

    Because the order is ``sorted((from_location, to_location))`` and NOT the
    transfer's direction, a transfer whose ``to_location`` sorts FIRST applies
    its INCREMENT before its DECREMENT. Seeded location ids are random, so the
    other tests hit that branch only by luck; this one forces it by sorting the
    two seeded ids and transferring from the higher to the lower.

    Two things must survive the reordering: the ``WHERE qty_on_hand >= n``
    guard still refuses an oversell (it is one statement either way), and the
    increment that was ALREADY APPLIED at to_location rolls back with the
    refusal — same transaction, so no stock is conjured at the destination of
    a transfer that never happened."""
    await _seed_ownership(pg_pool, namespace_id)
    loc_1 = await _seed_location(pg_pool, namespace_id, "L1")
    loc_2 = await _seed_location(pg_pool, namespace_id, "L2")
    lower, higher = sorted((loc_1, loc_2))
    # from = higher, to = lower  ->  canonical order puts to_location first.
    await _seed_item(pg_pool, namespace_id, "SKU-ORDER", higher, on_hand="10")
    await _seed_item(pg_pool, namespace_id, "SKU-ORDER", lower, on_hand="1")
    engine = _EngineStub(pg_pool)

    def _params(qty: int) -> dict[str, Any]:
        return {
            "namespace_id": namespace_id,
            "sku": "SKU-ORDER",
            "qty": qty,
            "from_location": higher,
            "to_location": lower,
        }

    ok = await do_transfer_stock(engine, _params(4))
    assert ok["from_on_hand"] == Decimal("6.000")
    assert ok["to_on_hand"] == Decimal("5.000"), "1 + 4 = 5 at the destination"

    with pytest.raises(InsufficientStockError) as excinfo:
        await do_transfer_stock(engine, _params(999))
    assert excinfo.value.available_on_hand == Decimal("6.000")

    assert await _get_on_hand(pg_pool, namespace_id, "SKU-ORDER", higher) == Decimal("6.000")
    assert await _get_on_hand(pg_pool, namespace_id, "SKU-ORDER", lower) == Decimal("5.000"), (
        "the increment applied BEFORE the refused decrement must roll back with it"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_transfer_mirrors_into_kg_nodes_and_edges(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """The happy path: with the registry seeded, the KG mirror still upserts
    exactly as before. NOT guard-discriminating (Batch 130a Step 5(d)) — this
    proves the guard does not break an OWNED write, which passes identically
    whether or not ``assert_owner`` is even called. The discriminating proofs
    live in ``test_unregistered_node_type_is_refused_and_writes_nothing``,
    ``test_wrong_owner_is_refused_at_the_upsert_call_site``, and
    ``test_deny_by_default_end_to_end_rolls_back_the_authoritative_write``."""
    await _seed_ownership(pg_pool, namespace_id)
    loc_a = await _seed_location(pg_pool, namespace_id, "From")
    loc_b = await _seed_location(pg_pool, namespace_id, "To")
    await _seed_item(pg_pool, namespace_id, "SKU-MIRROR", loc_a, on_hand="10")
    engine = _EngineStub(pg_pool)

    await do_transfer_stock(
        engine,
        {
            "namespace_id": namespace_id,
            "sku": "SKU-MIRROR",
            "qty": 4,
            "from_location": loc_a,
            "to_location": loc_b,
        },
    )

    async with pg_pool.acquire() as conn:
        node_rows = await conn.fetch(
            "SELECT label, entity_type FROM kg_nodes WHERE namespace_id = $1", namespace_id
        )
        edge_rows = await conn.fetch(
            "SELECT subject_label, predicate, object_label, confidence FROM kg_edges "
            "WHERE namespace_id = $1",
            namespace_id,
        )

    labels = {r["label"]: r["entity_type"] for r in node_rows}
    assert labels[f"StockLocation:{loc_a}"] == "STOCK_LOCATION"
    assert labels[f"StockLocation:{loc_b}"] == "STOCK_LOCATION"
    assert labels[f"InventoryItem:SKU-MIRROR:{loc_a}"] == "INVENTORY_ITEM"
    assert labels[f"InventoryItem:SKU-MIRROR:{loc_b}"] == "INVENTORY_ITEM"

    assert len(edge_rows) == 2
    for edge in edge_rows:
        assert edge["predicate"] == "at"
        assert float(edge["confidence"]) == 1.0


# ---------------------------------------------------------------------------
# 3. REAL concurrency — the module's headline claim. asyncio.gather over
# SEPARATE pool connections, never sequential calls on one connection (which
# would prove nothing about the race — this is the exact claim the module's
# docstring made about this file before it existed).
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_record_consumption_never_oversells(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """3 concurrent do_record_consumption(qty=4) calls against on_hand=10,
    each over its OWN pool connection. Exactly 2 must succeed, 1 must be
    correctly refused, and the final balance must never go negative or drift
    from the arithmetic (10 - 4 - 4 = 2). (Mutation-verified — see
    fix-forward report: dropping the ``AND qty_on_hand >= $4`` guard makes
    this go RED — the request that would push it negative raises a raw
    asyncpg.CheckViolationError instead of the intended
    InsufficientStockError.)"""
    await _seed_ownership(pg_pool, namespace_id)
    loc = await _seed_location(pg_pool, namespace_id, "Warehouse")
    await _seed_item(pg_pool, namespace_id, "SKU-RACE", loc, on_hand="10")
    engine = _EngineStub(pg_pool)

    async def _consume() -> dict[str, Any]:
        return await do_record_consumption(
            engine, {"namespace_id": namespace_id, "sku": "SKU-RACE", "qty": 4, "location": loc}
        )

    results = await asyncio.gather(_consume(), _consume(), _consume(), return_exceptions=True)

    successes = [r for r in results if not isinstance(r, BaseException)]
    failures = [r for r in results if isinstance(r, BaseException)]

    assert len(successes) == 2, f"expected exactly 2 successes, got: {results}"
    assert len(failures) == 1, f"expected exactly 1 refusal, got: {results}"
    assert isinstance(failures[0], InsufficientStockError), (
        f"the refusal must be the module's own domain error, not a raw DB "
        f"exception, got {type(failures[0])}: {failures[0]}"
    )

    final = await _get_on_hand(pg_pool, namespace_id, "SKU-RACE", loc)
    assert final == Decimal("2.000"), f"expected 10 - 4 - 4 = 2, got {final}"
    assert final >= 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_transfers_from_same_location_never_oversell(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """Same race, through do_transfer_stock instead of do_record_consumption
    — 3 concurrent A→B transfers of qty=4 against on_hand=10 at A: exactly 2
    succeed, arithmetic exact on BOTH sides."""
    await _seed_ownership(pg_pool, namespace_id)
    loc_a = await _seed_location(pg_pool, namespace_id, "From")
    loc_b = await _seed_location(pg_pool, namespace_id, "To")
    await _seed_item(pg_pool, namespace_id, "SKU-RACE-XFER", loc_a, on_hand="10")
    engine = _EngineStub(pg_pool)

    async def _transfer() -> dict[str, Any]:
        return await do_transfer_stock(
            engine,
            {
                "namespace_id": namespace_id,
                "sku": "SKU-RACE-XFER",
                "qty": 4,
                "from_location": loc_a,
                "to_location": loc_b,
            },
        )

    results = await asyncio.gather(_transfer(), _transfer(), _transfer(), return_exceptions=True)

    successes = [r for r in results if not isinstance(r, BaseException)]
    failures = [r for r in results if isinstance(r, BaseException)]
    assert len(successes) == 2, f"expected exactly 2 successes, got: {results}"
    assert len(failures) == 1
    assert isinstance(failures[0], InsufficientStockError)

    final_a = await _get_on_hand(pg_pool, namespace_id, "SKU-RACE-XFER", loc_a)
    final_b = await _get_on_hand(pg_pool, namespace_id, "SKU-RACE-XFER", loc_b)
    assert final_a == Decimal("2.000")
    assert final_b == Decimal("8.000")
    assert final_a + final_b == Decimal("10.000")


# ---------------------------------------------------------------------------
# 4. Cross-location LOCK ORDERING — the assertion that INVERTED in the second
# Batch-130 rejection.
#
# The previous revision of this file asserted the A<->B opposite-direction
# deadlock HAPPENS (>= 1 round out of 8), matching a module docstring that
# disclosed it as an accepted same-SKU limitation. The audit then showed the
# real cycle sits on the two shared STOCK_LOCATION rows in kg_nodes, which are
# SKU-INDEPENDENT — so cross-SKU two-way traffic (four pairwise-disjoint
# inventory_items rows, zero row contention) deadlocked on 60/120 rounds,
# every round. Ordering only the inventory_items writes did not fix it either
# (12/24: the cycle relocates onto kg_nodes). Ordering BOTH the rows and the
# mirror by a canonical resource key measured 0/120 cross-SKU and 0/24
# same-SKU.
#
# These two tests therefore assert ZERO DeadlockDetectedError. They are the
# only thing standing between a future refactor and a silent return of a
# 100%-reproduction production deadlock, so if one ever goes red the fix is
# the lock order, never a loosened assertion.
# ---------------------------------------------------------------------------

# Reset-and-retry rounds per lock-ordering test. Pre-fix, the failure appeared
# on essentially EVERY round, so 8 rounds makes a false "it never happens"
# result implausible while keeping runtime modest (a deadlocking round costs
# ~1s — Postgres's default deadlock_timeout — a clean one costs milliseconds).
_LOCK_ORDER_ROUNDS = 8


def _assert_no_deadlock(results: list[Any], round_no: int) -> None:
    """Every side of a concurrent round must have COMMITTED.

    Names DeadlockDetectedError explicitly in the failure message so a
    regression reads as "the lock order broke", not as a generic flake — and
    still fails on any other exception, so a transfer that starts refusing for
    an unrelated reason cannot pass as "well, at least it didn't deadlock"."""
    for result in results:
        assert not isinstance(result, asyncpg.exceptions.DeadlockDetectedError), (
            f"round {round_no}: opposite-direction transfers deadlocked. The "
            f"canonical lock order in do_transfer_stock (rows AND mirror) is "
            f"broken — fix the order, do not loosen this assertion: {result}"
        )
        assert not isinstance(result, BaseException), (
            f"round {round_no}: unexpected failure {type(result).__name__}: {result}"
        )
        assert result["ok"] is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cross_sku_opposite_direction_transfers_do_not_deadlock(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """The SKU-INDEPENDENT half of the cycle: four pairwise-disjoint rows
    (SKU-X,a) (SKU-X,b) (SKU-Y,a) (SKU-Y,b), so the two transactions contend
    on NO inventory_items row at all — only on the two shared STOCK_LOCATION
    mirror nodes. transfer(SKU-X, a→b) and transfer(SKU-Y, b→a) run
    concurrently over separate pool connections; both must succeed, and each
    SKU's total must be conserved every round."""
    await _seed_ownership(pg_pool, namespace_id)
    loc_a = await _seed_location(pg_pool, namespace_id, "Warehouse A")
    loc_b = await _seed_location(pg_pool, namespace_id, "Warehouse B")
    for sku in ("SKU-LOCK-X", "SKU-LOCK-Y"):
        await _seed_item(pg_pool, namespace_id, sku, loc_a, on_hand="100")
        await _seed_item(pg_pool, namespace_id, sku, loc_b, on_hand="100")
    engine = _EngineStub(pg_pool)

    async def _transfer(sku: str, frm: uuid.UUID, to: uuid.UUID) -> dict[str, Any]:
        return await do_transfer_stock(
            engine,
            {
                "namespace_id": namespace_id,
                "sku": sku,
                "qty": 5,
                "from_location": frm,
                "to_location": to,
            },
        )

    for round_no in range(_LOCK_ORDER_ROUNDS):
        for sku in ("SKU-LOCK-X", "SKU-LOCK-Y"):
            await _set_on_hand(pg_pool, namespace_id, sku, loc_a, "100")
            await _set_on_hand(pg_pool, namespace_id, sku, loc_b, "100")

        results = await asyncio.gather(
            _transfer("SKU-LOCK-X", loc_a, loc_b),
            _transfer("SKU-LOCK-Y", loc_b, loc_a),
            return_exceptions=True,
        )

        _assert_no_deadlock(results, round_no)

        for sku in ("SKU-LOCK-X", "SKU-LOCK-Y"):
            final_a = await _get_on_hand(pg_pool, namespace_id, sku, loc_a)
            final_b = await _get_on_hand(pg_pool, namespace_id, sku, loc_b)
            assert final_a + final_b == Decimal("200.000"), (
                f"round {round_no}, {sku}: conservation violated — "
                f"a({final_a}) + b({final_b}) != 200"
            )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_same_sku_opposite_direction_transfers_do_not_deadlock(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """The row-contention half: ONE sku, A→B against B→A, so both
    transactions want both inventory_items rows AND both mirror nodes. The
    canonical (ascending-UUID) order means the second transaction simply
    queues behind the first — it must not deadlock, and total quantity must
    be conserved every round."""
    await _seed_ownership(pg_pool, namespace_id)
    loc_a = await _seed_location(pg_pool, namespace_id, "Warehouse A")
    loc_b = await _seed_location(pg_pool, namespace_id, "Warehouse B")
    await _seed_item(pg_pool, namespace_id, "SKU-LOCK-SAME", loc_a, on_hand="100")
    await _seed_item(pg_pool, namespace_id, "SKU-LOCK-SAME", loc_b, on_hand="100")
    engine = _EngineStub(pg_pool)

    async def _transfer(frm: uuid.UUID, to: uuid.UUID) -> dict[str, Any]:
        return await do_transfer_stock(
            engine,
            {
                "namespace_id": namespace_id,
                "sku": "SKU-LOCK-SAME",
                "qty": 5,
                "from_location": frm,
                "to_location": to,
            },
        )

    for round_no in range(_LOCK_ORDER_ROUNDS):
        await _set_on_hand(pg_pool, namespace_id, "SKU-LOCK-SAME", loc_a, "100")
        await _set_on_hand(pg_pool, namespace_id, "SKU-LOCK-SAME", loc_b, "100")

        results = await asyncio.gather(
            _transfer(loc_a, loc_b),
            _transfer(loc_b, loc_a),
            return_exceptions=True,
        )

        _assert_no_deadlock(results, round_no)

        final_a = await _get_on_hand(pg_pool, namespace_id, "SKU-LOCK-SAME", loc_a)
        final_b = await _get_on_hand(pg_pool, namespace_id, "SKU-LOCK-SAME", loc_b)
        assert final_a + final_b == Decimal("200.000"), (
            f"round {round_no}: conservation violated — a({final_a}) + b({final_b}) != 200"
        )


# ---------------------------------------------------------------------------
# 5. Contract A — the `assert_owner` guard on `_upsert_kg_node` (Batch 130a).
#
# (a)-(c) are guard-DISCRIMINATING: they must go RED when `assert_owner` is
# disarmed inside `_upsert_kg_node`. Proven by mutation with an out-of-tree
# pytest plugin (never an in-tree edit — see the wave's rule 11), RED then
# GREEN, both verbatim summary lines reported alongside this file's gate
# output. (d) is the pre-existing `test_transfer_mirrors_into_kg_nodes_and_edges`
# above, re-labelled NOT guard-discriminating now that it needs seeding. (e)
# below is also NOT guard-discriminating — its own docstring says so.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unregistered_node_type_is_refused_and_writes_nothing(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """Guard-discriminating (Batch 130a Step 5(a)). Drives `_upsert_kg_node`
    directly with an entity_type deliberately absent from
    node-ownership.json. Without the guard the INSERT of an unregistered
    entity_type simply succeeds — that is what makes this discriminating: it
    goes RED the instant `assert_owner` is disarmed (see this wave's
    out-of-tree pytest-plugin RED/GREEN proof)."""
    await _seed_ownership(pg_pool, namespace_id)
    label = f"NotOwned:pytest-{uuid.uuid4().hex}"

    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        with pytest.raises(OwnershipError) as excinfo:
            await _upsert_kg_node(conn, namespace_id, label, "NOT_AN_OWNED_TYPE")
    assert excinfo.value.owner_engine is None, "deny-by-default: no row means no owner"

    async with pg_pool.acquire() as conn:
        row = await conn.fetchval(
            "SELECT 1 FROM kg_nodes WHERE namespace_id = $1 AND label = $2",
            namespace_id,
            label,
        )
    assert row is None, "a refused node upsert must not have written a kg_nodes row"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_wrong_owner_is_refused_at_the_upsert_call_site(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """Guard-discriminating (Batch 130a Step 5(b)) AND owner-specific, not
    merely presence-checking. `PO` is registered to `procurement`
    (node-ownership.json) — an inventory write must still be refused, and the
    error must name the REAL owner. Deliberately drives `_upsert_kg_node`
    itself, NOT a bare `assert_owner(conn, ns, "STOCK_LOCATION",
    "procurement")` call — that form never enters the write path and is
    invariant under removal of the guard, which is the exact
    non-discriminating proof Batch 130 was rejected for."""
    await _seed_ownership(pg_pool, namespace_id)
    label = f"PO:pytest-{uuid.uuid4().hex}"

    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        with pytest.raises(OwnershipError) as excinfo:
            await _upsert_kg_node(conn, namespace_id, label, "PO")
    assert excinfo.value.owner_engine == "procurement", (
        "must name the REAL registered owner, proving the guard checks identity, "
        "not merely row presence"
    )

    async with pg_pool.acquire() as conn:
        row = await conn.fetchval(
            "SELECT 1 FROM kg_nodes WHERE namespace_id = $1 AND label = $2",
            namespace_id,
            label,
        )
    assert row is None, "a wrong-owner node upsert must not have written a kg_nodes row"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_deny_by_default_end_to_end_rolls_back_the_authoritative_write(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """Guard-discriminating (Batch 130a Step 5(c)) — the blast radius made
    executable. `namespace_id` here is deliberately left UNSEEDED (contrast
    every other test in this file). `do_transfer_stock` must raise
    `OwnershipError` the moment its graph mirror runs, and the refusal must
    roll back the WHOLE transaction: the authoritative `inventory_items`
    decrement at `from_location` AND the increment at `to_location`, AND
    (B139's `append_transaction` calls, which run BEFORE the mirror at each
    of `do_transfer_stock`'s call sites, same `scoped_pg_session`) the two
    `inventory_transactions` ledger rows this transfer already wrote before
    the refusal fired — proving the refusal aborts the row write AND the
    ledger append, not just the mirror."""
    loc_a = await _seed_location(pg_pool, namespace_id, "From")
    loc_b = await _seed_location(pg_pool, namespace_id, "To")
    await _seed_item(pg_pool, namespace_id, "SKU-UNSEEDED", loc_a, on_hand="10")
    engine = _EngineStub(pg_pool)

    with pytest.raises(OwnershipError):
        await do_transfer_stock(
            engine,
            {
                "namespace_id": namespace_id,
                "sku": "SKU-UNSEEDED",
                "qty": 4,
                "from_location": loc_a,
                "to_location": loc_b,
            },
        )

    assert await _get_on_hand(pg_pool, namespace_id, "SKU-UNSEEDED", loc_a) == Decimal("10.000"), (
        "the refused transfer's decrement at from_location must have rolled back"
    )

    async with pg_pool.acquire() as conn:
        to_row = await conn.fetchval(
            "SELECT qty_on_hand FROM inventory_items "
            "WHERE namespace_id = $1 AND sku = $2 AND location_id = $3",
            namespace_id,
            "SKU-UNSEEDED",
            loc_b,
        )
    assert to_row is None, (
        "the refused transfer's increment at to_location must have rolled back too "
        "— no row should exist there at all"
    )

    async with pg_pool.acquire() as conn:
        ledger_count = await conn.fetchval(
            "SELECT COUNT(*) FROM inventory_transactions WHERE namespace_id = $1 AND sku = $2",
            namespace_id,
            "SKU-UNSEEDED",
        )
    assert ledger_count == 0, (
        "the refused transfer's inventory_transactions ledger rows — appended "
        "by append_transaction BEFORE the mirror ran — must have rolled back "
        "too; the blast radius is the ledger as well as inventory_items"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_four_inventory_ownership_rows_are_seeded(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """NOT guard-discriminating (Batch 130a Step 5(e)) — this pins that the
    four `node-ownership.json` rows this wave added actually reach the
    per-namespace table on seed. Its discriminating mutation is deleting the
    four rows from `node-ownership.json`, never disarming `assert_owner` —
    do not read a pass here as proof the guard is wired; the tests above
    prove that. Scoped to `transition IS NULL`: inventory may also hold
    per-transition grants on OTHER engines' node types (e.g. BOM_LINE
    status:delivered, Batch 132a) alongside these four whole-node-type rows,
    and this test pins only the latter."""
    await _seed_ownership(pg_pool, namespace_id)

    async with pg_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT node_type, transition FROM node_ownership_registry "
            "WHERE namespace_id = $1 AND owner_engine = 'inventory' "
            "AND transition IS NULL",
            namespace_id,
        )

    by_type = {r["node_type"]: r["transition"] for r in rows}
    assert set(by_type) == {
        "STOCK_LOCATION",
        "INVENTORY_ITEM",
        "GOODS_RECEIPT",
        "INVENTORY_RMA",
    }, f"expected exactly the four inventory rows, got {sorted(by_type)}"
    assert all(transition is None for transition in by_type.values()), (
        "all four rows must be transition:null (whole-node-type ownership)"
    )
