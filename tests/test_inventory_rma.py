"""Tests for the Inventory engine's returns/RMA + WEEE disposal state module
(Module 11, Wave 10 — Batch 138 — ``nce/vertical_modules/inventory/rma.py``),
migration 053's ``inventory_rma`` table.

**This wave records; it does not move.** ``do_record_rma`` writes exactly one
``inventory_rma`` row and mirrors exactly one ``INVENTORY_RMA`` kg_nodes row —
it writes NO ``inventory_transactions`` row, calls nothing in ``stock.py``,
and leaves ``inventory_items`` untouched. Batch 138b owns both stock legs
(restock-on-return, permanent WEEE disposal); Batch 138c reconciles dead
stock. The seam this file exists to prove clean is assertion (b) below —
that recording an RMA moves zero stock and appends zero ledger rows, and
that the assertion actually discriminates (goes RED the moment a stock
movement is added to this module).

Covers, per the wave's acceptance table:

  (a) INSERT-only idempotency: re-recording the same ``rma_ref`` returns the
      EXISTING row unmodified (``created=False``), row count stays at 1.
  (b) 🔴 the seam: recording an RMA appends ZERO ``inventory_transactions``
      rows and leaves ``inventory_items.qty_on_hand`` byte-identical.
  (c) ``weee_state='disposed'`` with no ``disposal_ref`` is refused twice
      over — the Python validator (unit tier) AND the DB CHECK (integration
      tier, direct INSERT).
  (d) the four ``weee_state`` values round-trip; an unknown value is refused
      by both the validator and the CHECK.
  (e) 🔴 through a real ``nce_app`` pool (never the owner ``pg_pool``): FORCE
      RLS isolates namespaces, and ``DELETE`` is refused (no-DELETE grant).
  (f) the guarded ``INVENTORY_RMA`` kg_nodes mirror + exactly one
      ``outbox_events`` row.
  (g) 🔴 in an unseeded namespace, ``do_record_rma`` raises ``OwnershipError``
      and rolls back the row it had already written — not just the mirror.
  (h) the composite FK refuses a ``location_id`` belonging to another
      namespace.

Unit-tier validator tests (no DB) are driven through the PUBLIC
``do_record_rma`` with a ``_DummyEngine`` whose ``pg_pool`` is ``None`` —
every validated field is rejected before any DB call, mirroring
``test_inventory_stock.py`` / ``test_inventory_transactions.py``'s
``_DummyEngine`` convention.

Integration tests are ``@pytest.mark.integration`` — wired into ci.yml's
"Integration — M11 Inventory" step alongside ``test_inventory_stock.py`` /
``test_inventory_transactions.py``.

RED/GREEN mutation-discrimination proof for (b), (e), (g): performed via an
OUT-OF-TREE pytest plugin / live-DB grant toggle (never an in-tree edit —
register item: B130's audit corrupted two files it had declared "verified
clean" doing exactly that). See the wave's own report for the verbatim
before/after pytest summaries and the sha256 hashes proving the tree was
never touched.
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
from nce.entity_resolution.ownership import OwnershipError
from nce.entity_resolution.ownership_seed import seed_node_ownership_registry
from nce.vertical_modules.inventory.rma import (
    WEEE_AWAITING_COLLECTION,
    WEEE_DISPOSED,
    WEEE_NOT_APPLICABLE,
    WEEE_PENDING,
    do_record_rma,
)

# ---------------------------------------------------------------------------
# 1. Pure-logic validation (no DB) — driven through the PUBLIC do_record_rma,
# never a reimplementation of its validators. Validation raises before
# engine.pg_pool is ever touched, so a dummy engine with pg_pool=None is safe
# (mirrors test_inventory_stock.py / test_inventory_transactions.py's
# _DummyEngine convention).
# ---------------------------------------------------------------------------


class _DummyEngine:
    pg_pool = None


def _base_params(**overrides: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "namespace_id": uuid.uuid4(),
        "rma_ref": "RMA-VALIDATE-1",
        "sku": "SKU-VALIDATE",
        "location": uuid.uuid4(),
        "qty": 1,
        "reason": "customer return",
    }
    params.update(overrides)
    return params


@pytest.mark.asyncio
async def test_rejects_missing_namespace_id() -> None:
    params = _base_params()
    del params["namespace_id"]
    with pytest.raises(ValueError, match="'namespace_id' is required"):
        await do_record_rma(_DummyEngine(), params)


@pytest.mark.asyncio
async def test_rejects_missing_rma_ref() -> None:
    params = _base_params(rma_ref="")
    with pytest.raises(ValueError, match="'rma_ref' is required"):
        await do_record_rma(_DummyEngine(), params)


@pytest.mark.asyncio
async def test_rejects_missing_sku() -> None:
    params = _base_params(sku="   ")
    with pytest.raises(ValueError, match="'sku' is required"):
        await do_record_rma(_DummyEngine(), params)


@pytest.mark.asyncio
async def test_rejects_missing_location() -> None:
    params = _base_params()
    del params["location"]
    with pytest.raises(ValueError, match="a location id is required"):
        await do_record_rma(_DummyEngine(), params)


@pytest.mark.asyncio
async def test_rejects_bool_qty() -> None:
    """``isinstance(True, int)`` is ``True`` in Python — a bool qty must not
    silently pass as a quantity of 1."""
    with pytest.raises(ValueError, match="bool is not a number"):
        await do_record_rma(_DummyEngine(), _base_params(qty=True))


@pytest.mark.asyncio
async def test_rejects_none_qty() -> None:
    params = _base_params()
    params["qty"] = None
    with pytest.raises(ValueError, match="expected int/float/Decimal"):
        await do_record_rma(_DummyEngine(), params)


@pytest.mark.asyncio
async def test_rejects_nan_qty() -> None:
    with pytest.raises(ValueError, match="must be finite"):
        await do_record_rma(_DummyEngine(), _base_params(qty=float("nan")))


@pytest.mark.asyncio
async def test_rejects_infinite_qty() -> None:
    with pytest.raises(ValueError, match="must be finite"):
        await do_record_rma(_DummyEngine(), _base_params(qty=float("inf")))


@pytest.mark.asyncio
async def test_rejects_zero_qty() -> None:
    with pytest.raises(ValueError, match=r"qty must be > 0"):
        await do_record_rma(_DummyEngine(), _base_params(qty=0))


@pytest.mark.asyncio
async def test_rejects_negative_qty() -> None:
    with pytest.raises(ValueError, match=r"qty must be > 0"):
        await do_record_rma(_DummyEngine(), _base_params(qty=-1))


@pytest.mark.asyncio
async def test_rejects_missing_reason() -> None:
    params = _base_params(reason="")
    with pytest.raises(ValueError, match="'reason' is required"):
        await do_record_rma(_DummyEngine(), params)


@pytest.mark.asyncio
async def test_rejects_unknown_weee_state() -> None:
    """(c)/(d)'s Python-validator half."""
    with pytest.raises(ValueError, match="weee_state must be one of"):
        await do_record_rma(_DummyEngine(), _base_params(weee_state="scrapped"))


@pytest.mark.asyncio
async def test_disposed_without_disposal_ref_refused_by_validator() -> None:
    """(c)'s Python-validator half — the Python mirror of migration 053's
    ``inventory_rma_disposed_requires_ref`` CHECK."""
    with pytest.raises(ValueError, match="requires a non-empty disposal_ref"):
        await do_record_rma(_DummyEngine(), _base_params(weee_state=WEEE_DISPOSED))


@pytest.mark.asyncio
async def test_disposed_with_blank_disposal_ref_refused_by_validator() -> None:
    """A whitespace-only disposal_ref must not count as "present"."""
    with pytest.raises(ValueError, match="requires a non-empty disposal_ref"):
        await do_record_rma(
            _DummyEngine(), _base_params(weee_state=WEEE_DISPOSED, disposal_ref="   ")
        )


# ---------------------------------------------------------------------------
# Integration helpers — mirrors test_inventory_stock.py's helpers verbatim in
# shape (same file family, same idioms; one idiom, not two).
# ---------------------------------------------------------------------------


class _EngineStub:
    def __init__(self, pg_pool: asyncpg.Pool) -> None:  # type: ignore[type-arg]
        self.pg_pool = pg_pool


def _app_dsn() -> str:
    """Rewrite the integration DSN onto the restricted ``nce_app`` role.

    Verbatim in shape from ``tests/test_inventory_stock.py::_app_dsn`` /
    ``tests/test_agreements_review.py::_app_dsn`` — the in-repo precedent for
    driving a vertical module through a REAL FORCE-RLS-subject connection
    instead of the superuser ``pg_pool``.
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
    """Seed the node-ownership registry so the guarded graph mirror
    (``_upsert_inventory_rma_kg_node``) passes for this namespace. Copied
    from ``tests/test_inventory_stock.py::_seed_ownership`` verbatim in
    shape — one idiom, not two. NOT called from conftest.py's fixtures on
    purpose: seeding centrally would silently disarm the deliberate
    deny-by-default proofs at ``tests/test_project_convert.py:587`` and
    ``tests/test_system_design_graph.py:549``."""
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


async def _count_inventory_transactions(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    sku: str,
    location_id: uuid.UUID,
) -> int:
    async with pg_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM inventory_transactions "
            "WHERE namespace_id = $1 AND sku = $2 AND location_id = $3",
            namespace_id,
            sku,
            location_id,
        )
    return int(count)


async def _fetch_rma_row(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    rma_ref: str,
) -> asyncpg.Record:  # type: ignore[type-arg]
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM inventory_rma WHERE namespace_id = $1 AND rma_ref = $2",
            namespace_id,
            rma_ref,
        )
    assert row is not None
    return row


# ---------------------------------------------------------------------------
# (a) INSERT-only idempotency
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_record_rma_writes_pending_row_and_rerecord_is_a_pure_readback(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """(a) One row, ``stock_movement_state='pending'``; re-recording the SAME
    ``rma_ref`` returns ``created=False``, row count stays at 1, and the full
    row is byte-identical before/after — including ``updated_at``, which an
    ``ON CONFLICT ... DO UPDATE`` would have bumped.

    Goes RED if the ``ON CONFLICT ... DO NOTHING`` in
    ``do_record_rma`` is turned into a ``DO UPDATE``."""
    await _seed_ownership(pg_pool, namespace_id)
    loc = await _seed_location(pg_pool, namespace_id, "Warehouse")
    engine = _EngineStub(pg_pool)
    params = {
        "namespace_id": namespace_id,
        "rma_ref": "RMA-IDEMPOTENT-1",
        "sku": "SKU-RMA-A",
        "location": loc,
        "qty": 3,
        "reason": "damaged in transit",
    }

    first = await do_record_rma(engine, params)
    assert first["ok"] is True
    assert first["created"] is True
    assert first["stock_movement_state"] == "pending"
    assert first["qty"] == Decimal("3.000")

    before = await _fetch_rma_row(pg_pool, namespace_id, "RMA-IDEMPOTENT-1")

    # Re-record the SAME rma_ref, with DIFFERENT field values — if this were
    # an upsert those values would land; since it is INSERT-only, they must
    # not.
    second = await do_record_rma(
        engine,
        {
            "namespace_id": namespace_id,
            "rma_ref": "RMA-IDEMPOTENT-1",
            "sku": "SKU-DIFFERENT",
            "location": loc,
            "qty": 999,
            "reason": "a different reason entirely",
        },
    )
    assert second["created"] is False
    assert second["sku"] == "SKU-RMA-A", "must return the EXISTING row, not the new params"
    assert second["qty"] == Decimal("3.000")

    async with pg_pool.acquire() as conn:
        row_count = await conn.fetchval(
            "SELECT COUNT(*) FROM inventory_rma WHERE namespace_id = $1 AND rma_ref = $2",
            namespace_id,
            "RMA-IDEMPOTENT-1",
        )
    assert row_count == 1, "re-recording must never create a second row"

    after = await _fetch_rma_row(pg_pool, namespace_id, "RMA-IDEMPOTENT-1")
    assert dict(before) == dict(after), (
        "re-recording an existing rma_ref must change NO column, including updated_at"
    )


# ---------------------------------------------------------------------------
# (b) 🔴 THE SEAM — recording an RMA moves zero stock, appends zero ledger
# rows. This is the assertion the whole wave exists to prove clean, and the
# one Batch 138b's contract depends on.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_record_rma_moves_zero_stock_and_appends_zero_ledger_rows(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """(b) 🔴 the seam. ``inventory_items.qty_on_hand`` is seeded NON-ZERO
    first so "unchanged" is a real observation, not a comparison of two
    zeroes. Goes RED the instant any stock movement is added to this module
    — see the wave's report for the RED/GREEN mutation proof (an out-of-tree
    pytest plugin that adds exactly such a movement)."""
    await _seed_ownership(pg_pool, namespace_id)
    loc = await _seed_location(pg_pool, namespace_id, "Warehouse")
    sku = "SKU-RMA-SEAM"
    await _seed_item(pg_pool, namespace_id, sku, loc, on_hand="42.500")
    engine = _EngineStub(pg_pool)

    before_ledger = await _count_inventory_transactions(pg_pool, namespace_id, sku, loc)
    before_on_hand = await _get_on_hand(pg_pool, namespace_id, sku, loc)
    assert before_on_hand == Decimal("42.500"), "seed must be non-zero for this to prove anything"

    result = await do_record_rma(
        engine,
        {
            "namespace_id": namespace_id,
            "rma_ref": "RMA-SEAM-1",
            "sku": sku,
            "location": loc,
            "qty": 2,
            "reason": "customer return",
        },
    )
    assert result["ok"] is True
    assert result["stock_movement_state"] == "pending"

    after_ledger = await _count_inventory_transactions(pg_pool, namespace_id, sku, loc)
    after_on_hand = await _get_on_hand(pg_pool, namespace_id, sku, loc)

    assert after_ledger == before_ledger == 0, (
        "do_record_rma must append ZERO inventory_transactions rows"
    )
    assert after_on_hand == before_on_hand == Decimal("42.500"), (
        "do_record_rma must leave inventory_items.qty_on_hand byte-identical"
    )


# ---------------------------------------------------------------------------
# (c)/(d) WEEE state — DB-level CHECK halves (Python-validator halves are in
# the unit-tier section above).
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_disposed_without_disposal_ref_refused_by_db_check(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """(c)'s DB half: a direct INSERT bypassing the Python validator entirely
    must still be refused by ``inventory_rma_disposed_requires_ref``. Goes
    RED if that CHECK is dropped from the migration — the Python validator
    half cannot mask a dropped DB constraint because this INSERT never calls
    it."""
    loc = await _seed_location(pg_pool, namespace_id, "Warehouse")
    async with pg_pool.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO inventory_rma "
                "(namespace_id, rma_ref, sku, location_id, qty, reason, weee_state) "
                "VALUES ($1, 'RMA-CHECK-1', 'SKU-X', $2, 1, 'return', 'disposed')",
                namespace_id,
                loc,
            )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_weee_states_round_trip_and_unknown_value_refused_by_db_check(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """(d): all four weee_state values round-trip through do_record_rma, and
    a value outside that set is refused by the DB CHECK on a direct INSERT
    (the Python-validator half is
    ``test_rejects_unknown_weee_state`` above). Goes RED if the CHECK's
    value list in migration 053 is loosened to accept an arbitrary value."""
    await _seed_ownership(pg_pool, namespace_id)
    loc = await _seed_location(pg_pool, namespace_id, "Warehouse")
    engine = _EngineStub(pg_pool)

    for i, state in enumerate(
        (WEEE_NOT_APPLICABLE, WEEE_PENDING, WEEE_AWAITING_COLLECTION, WEEE_DISPOSED)
    ):
        params: dict[str, Any] = {
            "namespace_id": namespace_id,
            "rma_ref": f"RMA-WEEE-{i}",
            "sku": "SKU-WEEE-ROUNDTRIP",
            "location": loc,
            "qty": 1,
            "reason": "return",
            "weee_state": state,
        }
        if state == WEEE_DISPOSED:
            params["disposal_ref"] = "TAKEBACK-REF-001"
        result = await do_record_rma(engine, params)
        assert result["weee_state"] == state, f"state {state!r} did not round-trip"

    async with pg_pool.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO inventory_rma "
                "(namespace_id, rma_ref, sku, location_id, qty, reason, weee_state) "
                "VALUES ($1, 'RMA-WEEE-BOGUS', 'SKU-X', $2, 1, 'return', 'incinerated')",
                namespace_id,
                loc,
            )


# ---------------------------------------------------------------------------
# (e) 🔴 FORCE RLS + no-DELETE grant, through a REAL nce_app pool.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_nce_app_pool_isolates_by_namespace_and_refuses_delete(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    make_namespace: Any,
) -> None:
    """(e) 🔴 driven through a REAL ``nce_app`` pool (``_app_dsn()``), never
    the superuser ``pg_pool`` — the owner pool bypasses FORCE RLS and has
    shipped a false proof three times already (B67, B120, B130).

    Goes RED if the ``FORCE ROW LEVEL SECURITY`` line is dropped (ns_b would
    see ns_a's row) or if the grant list is widened to include ``DELETE``
    (the DELETE would succeed instead of raising)."""
    ns_a = await make_namespace()
    ns_b = await make_namespace()
    await _seed_ownership(pg_pool, ns_a)
    loc_a = await _seed_location(pg_pool, ns_a, "Warehouse A")

    app_pool = await asyncpg.create_pool(_app_dsn(), min_size=1, max_size=2)
    engine = _EngineStub(app_pool)
    try:
        result = await do_record_rma(
            engine,
            {
                "namespace_id": ns_a,
                "rma_ref": "RMA-RLS-1",
                "sku": "SKU-RLS",
                "location": loc_a,
                "qty": 1,
                "reason": "return",
            },
        )
        rma_id = result["rma_id"]

        # ns_a sees its own row through nce_app...
        async with app_pool.acquire() as conn, conn.transaction():
            await set_namespace_context(conn, ns_a)
            visible_from_a = await conn.fetchval(
                "SELECT COUNT(*) FROM inventory_rma WHERE namespace_id = $1", ns_a
            )
        assert visible_from_a == 1, "ns_a must see the row do_record_rma wrote"

        # ...and ns_b does not, even when it asks for ns_a's namespace_id
        # EXPLICITLY — RLS, not a WHERE clause, is what refuses this.
        async with app_pool.acquire() as conn, conn.transaction():
            await set_namespace_context(conn, ns_b)
            visible_from_b = await conn.fetchval(
                "SELECT COUNT(*) FROM inventory_rma WHERE namespace_id = $1", ns_a
            )
        assert visible_from_b == 0, "ns_b must not see ns_a's inventory_rma row"

        # No DELETE grant — nce_app can never erase compliance evidence.
        async with app_pool.acquire() as conn, conn.transaction():
            await set_namespace_context(conn, ns_a)
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.execute("DELETE FROM inventory_rma WHERE id = $1", uuid.UUID(rma_id))
    finally:
        await app_pool.close()


# ---------------------------------------------------------------------------
# (f) Graph mirror — guarded kg_nodes upsert + exactly one outbox event.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_kg_node_and_outbox_event_are_written_when_ownership_seeded(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """(f) NOT guard-discriminating on its own (mirrors
    ``test_inventory_stock.py``'s labelling convention) — this proves the
    happy path upserts correctly; ``test_unseeded_namespace_...`` below is
    the guard-discriminating proof. Goes RED if ``emit_graph_write`` is
    removed from ``_upsert_inventory_rma_kg_node``."""
    await _seed_ownership(pg_pool, namespace_id)
    loc = await _seed_location(pg_pool, namespace_id, "Warehouse")
    engine = _EngineStub(pg_pool)

    await do_record_rma(
        engine,
        {
            "namespace_id": namespace_id,
            "rma_ref": "RMA-MIRROR-1",
            "sku": "SKU-MIRROR",
            "location": loc,
            "qty": 1,
            "reason": "return",
        },
    )

    label = "InventoryRma:RMA-MIRROR-1"
    async with pg_pool.acquire() as conn:
        node = await conn.fetchrow(
            "SELECT label, entity_type FROM kg_nodes WHERE namespace_id = $1 AND label = $2",
            namespace_id,
            label,
        )
        outbox_count = await conn.fetchval(
            "SELECT COUNT(*) FROM outbox_events "
            "WHERE namespace_id = $1 AND aggregate_type = 'INVENTORY_RMA' AND aggregate_id = $2",
            namespace_id,
            label,
        )

    assert node is not None
    assert node["entity_type"] == "INVENTORY_RMA"
    assert outbox_count == 1, "exactly one outbox_events row must be emitted for this node"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unseeded_namespace_ownership_error_rolls_back_the_row(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """(g) 🔴 guard-discriminating — namespace_id here is deliberately LEFT
    UNSEEDED (contrast every other test in this file). ``do_record_rma`` must
    raise ``OwnershipError`` the moment its graph mirror runs, and the
    refusal must roll back the WHOLE transaction: the ``inventory_rma`` row
    this call had already written, not merely the kg_nodes mirror.

    Goes RED if the ``assert_owner`` call is removed from
    ``_upsert_inventory_rma_kg_node`` — see the wave's report for the
    RED/GREEN mutation proof."""
    loc = await _seed_location(pg_pool, namespace_id, "Warehouse")
    engine = _EngineStub(pg_pool)

    with pytest.raises(OwnershipError) as excinfo:
        await do_record_rma(
            engine,
            {
                "namespace_id": namespace_id,
                "rma_ref": "RMA-UNSEEDED-1",
                "sku": "SKU-UNSEEDED",
                "location": loc,
                "qty": 1,
                "reason": "return",
            },
        )
    assert excinfo.value.owner_engine is None, "deny-by-default: no registry row means no owner"

    async with pg_pool.acquire() as conn:
        row = await conn.fetchval(
            "SELECT 1 FROM inventory_rma WHERE namespace_id = $1 AND rma_ref = $2",
            namespace_id,
            "RMA-UNSEEDED-1",
        )
    assert row is None, (
        "a refused mirror must roll back the authoritative inventory_rma row too — "
        "not just the mirror"
    )

    async with pg_pool.acquire() as conn:
        node = await conn.fetchval(
            "SELECT 1 FROM kg_nodes WHERE namespace_id = $1 AND label = $2",
            namespace_id,
            "InventoryRma:RMA-UNSEEDED-1",
        )
    assert node is None, "a refused mirror must not have written a kg_nodes row either"


# ---------------------------------------------------------------------------
# (h) Composite location FK — never cross a tenant boundary.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_location_fk_is_composite_and_refuses_cross_namespace_location(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    make_namespace: Any,
) -> None:
    """(h) Pins ``inventory_rma_location_fk``: an RMA can never reference
    another tenant's location, even though the location id is a real row.
    Goes RED if the FK is made non-composite (``location_id`` alone)."""
    ns_a = await make_namespace()
    ns_b = await make_namespace()
    loc_in_a = await _seed_location(pg_pool, ns_a, "Warehouse A")

    async with pg_pool.acquire() as conn:
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await conn.execute(
                "INSERT INTO inventory_rma "
                "(namespace_id, rma_ref, sku, location_id, qty, reason) "
                "VALUES ($1, 'RMA-CROSS-TENANT', 'SKU-X', $2, 1, 'return')",
                ns_b,
                loc_in_a,
            )
