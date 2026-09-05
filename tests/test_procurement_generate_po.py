"""Integration tests for ``do_generate_po`` (Module 1, Wave 10 — generate-po).

Validates:
  a. Without ``confirm=True`` the @governed gate returns ``pending_approval``
     and no PO node is written (confirm-only default).
  b. With ``confirm=True`` a single draft ``PO`` node is written to kg_nodes.
  c. An ``event_log`` audit row is written by the @governed decorator.
  d. Retry with the same idempotency key is a NO-OP — no duplicate PO node,
     status ``already_executed``.
  e. ``NetsetPoTransport.place_order`` raises ``NotImplementedError`` with a
     clear message (🔴 unbuilt-API stub check — pure unit test, no DB).
  f. ``NettailerPoTransport.place_order`` raises ``NotImplementedError`` (stub
     body documented as Wave 11 wiring point).

Fixtures used:
  ``pg_app_conn`` — asyncpg connection as nce_app (RLS enforced).
  ``pg_pool``     — pool used to build ``_FakeEngine`` for do_resolve_bids.
  ``make_namespace`` — factory that inserts a fresh namespace row.

Runs as @pytest.mark.integration — requires a live Postgres with schema applied.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import asyncpg  # type: ignore[import-untyped]
import pytest

from nce.auth import set_namespace_context
from nce.entity_resolution.ownership_seed import seed_node_ownership_registry
from nce.vertical_modules.procurement.bids import upsert_bid_projection
from nce.vertical_modules.procurement.po import _derive_po_idempotency_key, do_generate_po
from nce.vertical_modules.procurement.transports import NetsetPoTransport, NettailerPoTransport

# ---------------------------------------------------------------------------
# Constants & helpers
# ---------------------------------------------------------------------------

_MOCK_EMIT = "nce.vertical_modules.procurement.graph.emit_graph_write"


class _FakeEngine:
    """Minimal engine stand-in — do_resolve_bids only needs pg_pool."""

    def __init__(self, pool: asyncpg.Pool) -> None:  # type: ignore[type-arg]
        self.pg_pool = pool


# Minimal weights dict accepted by do_rank_suppliers / do_calculate_tco.
# TCO_WEIGHTS keys match tco.py: freight, warranty, stock, delivery_risk.
_WEIGHTS: dict = {
    "TCO_WEIGHTS": {
        "freight": 0.05,
        "warranty": 0.03,
        "stock": 0.02,
        "delivery_risk": 0.05,
    },
    "SCORING_WEIGHTS": {
        "tco": 0.30,
        "delivery_reliability": 0.20,
        "bid_price": 0.20,
        "tier_bundling": 0.15,
        "rebate_proximity": 0.15,
    },
}

_BOM_LINE: dict = {"quantity": 10, "unit_price": 100.0}

_CANDIDATES: list[dict] = [
    {
        "supplier_id": "SUP-001",
        "unit_price": 95.0,
        "own_stock": True,
        "lead_time_days": 3,
        "delivery_reliability": 0.95,
        "supplier_tier": 1,
        "rebate_proximity": 0.8,
        "bundles_well": True,
    },
    {
        "supplier_id": "SUP-002",
        "unit_price": 110.0,
        "own_stock": False,
        "lead_time_days": 7,
        "delivery_reliability": 0.80,
        "supplier_tier": 2,
        "rebate_proximity": 0.5,
        "bundles_well": False,
    },
]


async def _seed(conn: asyncpg.Connection, ns: object) -> None:  # type: ignore[type-arg]
    """Seed ownership registry and set namespace GUC inside a transaction."""
    async with conn.transaction():
        await set_namespace_context(conn, ns)
        await seed_node_ownership_registry(conn, ns)


async def _seed_bids(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    ns: uuid.UUID,
    artnrs: list[str],
) -> None:
    """Insert minimal BID rows into the consumer cache."""
    rows = [
        {"artnr": artnr, "leverandor": "SUP-001", "bid_id": f"BID-{artnr}", "pris": 90.0}
        for artnr in artnrs
    ]
    async with conn.transaction():
        await set_namespace_context(conn, ns)
        await upsert_bid_projection(conn, ns, rows)


# ---------------------------------------------------------------------------
# Integration test class
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestDoGeneratePo:
    """Integration tests for do_generate_po (Wave 10)."""

    # ------------------------------------------------------------------
    # a. Unconfirmed → pending_approval, no PO node written
    # ------------------------------------------------------------------

    async def test_unconfirmed_returns_pending_approval(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
        make_namespace: object,
    ) -> None:
        """Without confirm=True, @governed returns pending_approval; no PO written."""
        ns = await make_namespace()  # type: ignore[operator]
        await _seed(pg_app_conn, ns)

        artnrs = ["ARTNR-W10-001"]
        await _seed_bids(pg_app_conn, ns, artnrs)

        engine = _FakeEngine(pg_pool)
        ikey = _derive_po_idempotency_key(str(ns), artnrs, "v1")

        with patch(_MOCK_EMIT, new_callable=AsyncMock):
            async with pg_app_conn.transaction():
                await set_namespace_context(pg_app_conn, ns)
                result = await do_generate_po(
                    pg_app_conn,
                    ns,
                    idempotency_key=ikey,
                    confirm=False,  # no confirm → should NOT execute
                    engine=engine,
                    po_number="PO-W10-PEND-001",
                    bom_line=_BOM_LINE,
                    candidates=_CANDIDATES,
                    weights=_WEIGHTS,
                    artnrs=artnrs,
                )

        assert result["status"] == "pending_approval"
        assert result["action_type"] == "generate_po"

        # Confirm no PO node was written.
        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            count = await pg_app_conn.fetchval(
                "SELECT COUNT(*) FROM kg_nodes WHERE label = $1 AND namespace_id = $2",
                "PO:PO-W10-PEND-001",
                ns,
            )
        assert count == 0, f"Expected 0 PO nodes (unconfirmed), got {count}"

    # ------------------------------------------------------------------
    # b. Confirmed → one draft PO node written
    # ------------------------------------------------------------------

    async def test_confirmed_creates_draft_po_node(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
        make_namespace: object,
    ) -> None:
        """confirm=True → exactly one draft PO node in kg_nodes; result reports it."""
        ns = await make_namespace()  # type: ignore[operator]
        await _seed(pg_app_conn, ns)

        artnrs = ["ARTNR-W10-002"]
        await _seed_bids(pg_app_conn, ns, artnrs)

        engine = _FakeEngine(pg_pool)
        ikey = _derive_po_idempotency_key(str(ns), artnrs, "v1")

        with patch(_MOCK_EMIT, new_callable=AsyncMock):
            async with pg_app_conn.transaction():
                await set_namespace_context(pg_app_conn, ns)
                result = await do_generate_po(
                    pg_app_conn,
                    ns,
                    idempotency_key=ikey,
                    confirm=True,
                    engine=engine,
                    po_number="PO-W10-DRAFT-001",
                    bom_line=_BOM_LINE,
                    candidates=_CANDIDATES,
                    weights=_WEIGHTS,
                    artnrs=artnrs,
                )

        assert result["status"] == "executed"
        inner = result["result"]
        assert inner["po_label"] == "PO:PO-W10-DRAFT-001"
        assert inner["po_number"] == "PO-W10-DRAFT-001"
        assert "ranked_winner" in inner
        assert "bid_results" in inner

        # Exactly one PO node in kg_nodes.
        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            count = await pg_app_conn.fetchval(
                "SELECT COUNT(*) FROM kg_nodes WHERE label = $1 AND namespace_id = $2",
                "PO:PO-W10-DRAFT-001",
                ns,
            )
        assert count == 1, f"Expected 1 PO node, got {count}"

    # ------------------------------------------------------------------
    # c. event_log audit row written by @governed
    # ------------------------------------------------------------------

    async def test_confirmed_writes_event_log_audit(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
        make_namespace: object,
    ) -> None:
        """confirm=True → @governed appends one event_log audit row."""
        ns = await make_namespace()  # type: ignore[operator]
        await _seed(pg_app_conn, ns)

        artnrs = ["ARTNR-W10-003"]
        await _seed_bids(pg_app_conn, ns, artnrs)

        engine = _FakeEngine(pg_pool)
        ikey = _derive_po_idempotency_key(str(ns), artnrs, "v1-audit")

        # Count event_log rows before.
        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            before = await pg_app_conn.fetchval(
                "SELECT COUNT(*) FROM event_log WHERE namespace_id = $1",
                ns,
            )

        with patch(_MOCK_EMIT, new_callable=AsyncMock):
            async with pg_app_conn.transaction():
                await set_namespace_context(pg_app_conn, ns)
                result = await do_generate_po(
                    pg_app_conn,
                    ns,
                    idempotency_key=ikey,
                    confirm=True,
                    engine=engine,
                    po_number="PO-W10-AUDIT-001",
                    bom_line=_BOM_LINE,
                    candidates=_CANDIDATES,
                    weights=_WEIGHTS,
                    artnrs=artnrs,
                )

        assert result["status"] == "executed"

        # At least one new audit row should exist after.
        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            after = await pg_app_conn.fetchval(
                "SELECT COUNT(*) FROM event_log WHERE namespace_id = $1",
                ns,
            )
        assert after > before, f"Expected event_log rows to grow; before={before} after={after}"

    # ------------------------------------------------------------------
    # d. Retry same idempotency key → NO-OP, no duplicate PO node
    # ------------------------------------------------------------------

    async def test_retry_same_key_is_noop(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
        make_namespace: object,
    ) -> None:
        """Same idempotency key on second call → already_executed; no duplicate PO."""
        ns = await make_namespace()  # type: ignore[operator]
        await _seed(pg_app_conn, ns)

        artnrs = ["ARTNR-W10-004"]
        await _seed_bids(pg_app_conn, ns, artnrs)

        engine = _FakeEngine(pg_pool)
        ikey = _derive_po_idempotency_key(str(ns), artnrs, "v1-retry")

        with patch(_MOCK_EMIT, new_callable=AsyncMock):
            # First call — executes.
            async with pg_app_conn.transaction():
                await set_namespace_context(pg_app_conn, ns)
                result1 = await do_generate_po(
                    pg_app_conn,
                    ns,
                    idempotency_key=ikey,
                    confirm=True,
                    engine=engine,
                    po_number="PO-W10-RETRY-001",
                    bom_line=_BOM_LINE,
                    candidates=_CANDIDATES,
                    weights=_WEIGHTS,
                    artnrs=artnrs,
                )
            assert result1["status"] == "executed"

            # Second call with the same key — NO-OP.
            async with pg_app_conn.transaction():
                await set_namespace_context(pg_app_conn, ns)
                result2 = await do_generate_po(
                    pg_app_conn,
                    ns,
                    idempotency_key=ikey,
                    confirm=True,
                    engine=engine,
                    po_number="PO-W10-RETRY-001",
                    bom_line=_BOM_LINE,
                    candidates=_CANDIDATES,
                    weights=_WEIGHTS,
                    artnrs=artnrs,
                )
            assert result2["status"] == "already_executed"

        # Only one PO node — no duplicate.
        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            count = await pg_app_conn.fetchval(
                "SELECT COUNT(*) FROM kg_nodes WHERE label = $1 AND namespace_id = $2",
                "PO:PO-W10-RETRY-001",
                ns,
            )
        assert count == 1, f"Expected 1 PO node after retry, got {count}"


# ---------------------------------------------------------------------------
# Pure unit tests — transport stubs (no DB required)
# ---------------------------------------------------------------------------


def test_netset_transport_raises_not_implemented() -> None:
    """NetsetPoTransport.place_order raises NotImplementedError with a clear message."""
    import asyncio

    transport = NetsetPoTransport()
    with pytest.raises(NotImplementedError) as exc_info:
        asyncio.run(
            transport.place_order(
                "PO-NETSET-001",
                "NETSET-SUP-001",
                [{"artnr": "A001", "quantity": 5}],
                namespace_id=str(uuid.uuid4()),
                idempotency_key="test-key",
            )
        )
    msg = str(exc_info.value)
    assert "Netset Order API not yet available" in msg, f"Unexpected message: {msg}"


def test_nettailer_transport_raises_not_implemented() -> None:
    """NettailerPoTransport.place_order raises NotImplementedError (Wave 11 wiring point)."""
    import asyncio

    transport = NettailerPoTransport()
    with pytest.raises(NotImplementedError) as exc_info:
        asyncio.run(
            transport.place_order(
                "PO-NETTAILER-001",
                "NETT-SUP-001",
                [{"artnr": "B001", "quantity": 2}],
                namespace_id=str(uuid.uuid4()),
                idempotency_key="test-key-2",
            )
        )
    msg = str(exc_info.value)
    assert "Wave 11" in msg or "do_submit_po" in msg, f"Unexpected message: {msg}"


def test_derive_idempotency_key_is_stable() -> None:
    """Same inputs always produce the same idempotency key."""
    ns = str(uuid.uuid4())
    artnrs = ["A001", "B002"]
    k1 = _derive_po_idempotency_key(ns, artnrs, "v1")
    k2 = _derive_po_idempotency_key(ns, artnrs, "v1")
    assert k1 == k2


def test_derive_idempotency_key_order_independent() -> None:
    """Artnr list order does not affect the idempotency key (sorted internally)."""
    ns = str(uuid.uuid4())
    k1 = _derive_po_idempotency_key(ns, ["B002", "A001"], "v1")
    k2 = _derive_po_idempotency_key(ns, ["A001", "B002"], "v1")
    assert k1 == k2
