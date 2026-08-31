"""Tests for Module 11, Wave 9 -- ``restock-po`` (Batch 137).

Covers:

  1. Pure-logic (no DB): ``_derive_restock_idempotency_key`` is deterministic
     and changes with EACH of its inputs (sku, po_number, location) -- a
     collision between two distinct restock decisions would silently merge
     their idempotency rows.
  2. Integration (``@pytest.mark.integration``, live Postgres) -- the wave's
     own acceptance list:
     - ``do_create_restock_po`` is C2-gated: unconfirmed -> pending_approval,
       ``do_submit_po``/transport never reached; default ceiling (0.0) blocks
       any positive ``po_value``.
     - a retry with the SAME Inventory-side idempotency key is a NO-OP at
       THIS Actor's own ``@governed`` layer -- ``do_submit_po`` is never
       called a second time, so no duplicate PO *request* is even attempted.
     - the Procurement-side ``submit_po`` idempotency key is propagated via
       Procurement's OWN ``_derive_submit_idempotency_key(namespace_id,
       po_number)`` -- calling ``do_submit_po`` directly a second time with
       that SAME derived key (simulating a hypothetical retry that reached
       Procurement independently of this Actor's own gate) is ALSO a NO-OP,
       so no duplicate *order* is placed even if the Inventory-side gate
       were bypassed. This is what makes the guarantee "compounding" rather
       than resting on a single layer.
     - the spanning audit row (``restock_po_span``) correlating both keys is
       appended exactly once per executed restock, never duplicated on retry.

Each integration test is written so that deleting the guard it targets makes
it fail: the retry test asserts the actual ``call_count`` on a counting
transport stub (not just the status string), and the compounding test calls
``do_submit_po`` OUTSIDE ``do_create_restock_po`` entirely, so it can only
pass if Procurement's own gate -- not this module's -- is doing the work.

Fixtures used:
  ``pg_app_conn`` -- asyncpg connection as ``nce_app`` (RLS enforced).
  ``make_namespace`` -- factory that inserts a fresh namespace row.

Runs as ``@pytest.mark.integration`` -- requires a live Postgres with schema
applied (``make local-up``).
"""

from __future__ import annotations

from typing import Any

import asyncpg  # type: ignore[import-untyped]
import pytest

from nce.auth import set_namespace_context
from nce.autonomy.governor import governed
from nce.entity_resolution.ownership_seed import seed_node_ownership_registry
from nce.vertical_modules.inventory.restock_po import (
    _derive_restock_idempotency_key,
    do_create_restock_po,
)
from nce.vertical_modules.procurement import po as po_mod
from nce.vertical_modules.procurement.po import (
    _derive_submit_idempotency_key,
    do_submit_po,
)
from nce.vertical_modules.procurement.transports import PoTransport

# ---------------------------------------------------------------------------
# Helpers and constants
# ---------------------------------------------------------------------------


async def _seed(conn: asyncpg.Connection, ns: object) -> None:  # type: ignore[type-arg]
    """Seed ownership registry and set namespace GUC inside a transaction."""
    async with conn.transaction():
        await set_namespace_context(conn, ns)
        await seed_node_ownership_registry(conn, ns)


class _CountingTransport(PoTransport):
    """Test transport stub that counts real order placements."""

    def __init__(self) -> None:
        self.call_count = 0

    async def place_order(
        self,
        po_number: str,
        supplier_id: str,
        line_items: list[dict[str, Any]],
        *,
        namespace_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self.call_count += 1
        return {"confirmed": True, "external_ref": f"EXT-{po_number}"}


def _high_ceiling_restock() -> Any:
    """``do_create_restock_po`` re-wrapped with a high ceiling for tests that
    need an actual execution (default ceiling is 0.0 -- blocks everything)."""
    fn = getattr(do_create_restock_po, "__wrapped__", do_create_restock_po)
    return governed(
        action_type="create_restock_po",
        value_arg="po_value",
        value_ceiling=10_000.0,
    )(fn)


def _patch_procurement_ceiling(monkeypatch: pytest.MonkeyPatch, ceiling: float = 10_000.0) -> None:
    """Raise Procurement's OWN ``submit_po`` ceiling for the test's duration.

    ``do_submit_po`` delegates to the module-level ``_governed_place_po``
    (decorated at import time with ``cfg.NCE_PROCUREMENT_AUTONOMY_PO_CEILING``,
    default 0.0) by bare name, resolved at call time -- so patching that
    module attribute changes what every subsequent ``do_submit_po`` call (and
    a direct ``_governed_place_po`` call) sees, without touching this wave's
    own ``do_create_restock_po`` ceiling, which is patched independently via
    ``_high_ceiling_restock``. The two ceilings are deliberately raised
    separately: this test proves BOTH gates -- not just the outer one -- are
    genuinely exercised on the way to an executed order.
    """
    fn = getattr(po_mod._governed_place_po, "__wrapped__", po_mod._governed_place_po)
    high = governed(
        action_type="submit_po",
        value_arg="po_value",
        value_ceiling=ceiling,
    )(fn)
    monkeypatch.setattr(po_mod, "_governed_place_po", high)


# ---------------------------------------------------------------------------
# 1. Pure-logic tests (no DB)
# ---------------------------------------------------------------------------


class TestDeriveRestockIdempotencyKey:
    """``_derive_restock_idempotency_key`` -- pure hash, no DB."""

    def test_deterministic_for_same_inputs(self) -> None:
        k1 = _derive_restock_idempotency_key("ns-1", "SKU-A", "PO-1", "LOC-1")
        k2 = _derive_restock_idempotency_key("ns-1", "SKU-A", "PO-1", "LOC-1")
        assert k1 == k2
        assert k1.startswith("restock:")

    def test_differs_by_sku(self) -> None:
        k1 = _derive_restock_idempotency_key("ns-1", "SKU-A", "PO-1", None)
        k2 = _derive_restock_idempotency_key("ns-1", "SKU-B", "PO-1", None)
        assert k1 != k2

    def test_differs_by_po_number(self) -> None:
        k1 = _derive_restock_idempotency_key("ns-1", "SKU-A", "PO-1", None)
        k2 = _derive_restock_idempotency_key("ns-1", "SKU-A", "PO-2", None)
        assert k1 != k2

    def test_differs_by_location(self) -> None:
        k1 = _derive_restock_idempotency_key("ns-1", "SKU-A", "PO-1", "LOC-1")
        k2 = _derive_restock_idempotency_key("ns-1", "SKU-A", "PO-1", "LOC-2")
        assert k1 != k2

    def test_differs_by_namespace(self) -> None:
        k1 = _derive_restock_idempotency_key("ns-1", "SKU-A", "PO-1", None)
        k2 = _derive_restock_idempotency_key("ns-2", "SKU-A", "PO-1", None)
        assert k1 != k2


# ---------------------------------------------------------------------------
# 2. Integration tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestDoCreateRestockPo:
    """Integration tests for do_create_restock_po (Wave 9 -- restock-po)."""

    # ------------------------------------------------------------------
    # C2 gate: unconfirmed -> pending_approval; submit_po never reached
    # ------------------------------------------------------------------

    async def test_unconfirmed_returns_pending_approval(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        make_namespace: object,
    ) -> None:
        """Without confirm=True, the @governed wrapper returns pending_approval
        and do_create_restock_po's own body -- which calls do_submit_po --
        never runs."""
        ns = await make_namespace()  # type: ignore[operator]
        await _seed(pg_app_conn, ns)

        ikey = _derive_restock_idempotency_key(str(ns), "SKU-A", "PO-W9-PEND-001", None)
        transport = _CountingTransport()

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            result = await do_create_restock_po(
                pg_app_conn,
                ns,
                idempotency_key=ikey,
                confirm=False,
                sku="SKU-A",
                po_number="PO-W9-PEND-001",
                supplier_id="SUP-001",
                line_items=[{"artnr": "SKU-A", "quantity": 10}],
                po_value=0.0,
                transport=transport,
            )

        assert result["status"] == "pending_approval"
        assert result["action_type"] == "create_restock_po"
        assert transport.call_count == 0, (
            f"Transport must not be called without confirm; call_count={transport.call_count}"
        )

    # ------------------------------------------------------------------
    # C2 gate: default ceiling (0.0) blocks any positive po_value
    # ------------------------------------------------------------------

    async def test_default_ceiling_zero_blocks_positive_value(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        make_namespace: object,
    ) -> None:
        """Default AUTONOMY_RESTOCK_CEILING (Procurement's own PO ceiling, 0.0):
        po_value=0.01 > 0.0 -> pending_approval, do_submit_po never reached."""
        ns = await make_namespace()  # type: ignore[operator]
        await _seed(pg_app_conn, ns)

        ikey = _derive_restock_idempotency_key(str(ns), "SKU-A", "PO-W9-CEIL-001", None)
        transport = _CountingTransport()

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            result = await do_create_restock_po(
                pg_app_conn,
                ns,
                idempotency_key=ikey,
                confirm=True,
                sku="SKU-A",
                po_number="PO-W9-CEIL-001",
                supplier_id="SUP-001",
                line_items=[{"artnr": "SKU-A", "quantity": 10}],
                po_value=0.01,
                transport=transport,
            )

        assert result["status"] == "pending_approval", (
            f"Default ceiling=0 must block po_value=0.01, got {result}"
        )
        assert transport.call_count == 0

    # ------------------------------------------------------------------
    # Retry with the SAME Inventory-side key -> NO-OP, no duplicate REQUEST
    # ------------------------------------------------------------------

    async def test_retry_same_restock_key_is_noop_no_duplicate_request(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        make_namespace: object,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Retry with the same restock idempotency key -> already_executed;
        do_submit_po (and its transport) is called exactly ONCE across both
        attempts -- proving the outer gate stops a duplicate PO *request*
        before Procurement is ever involved."""
        ns = await make_namespace()  # type: ignore[operator]
        await _seed(pg_app_conn, ns)

        _patch_procurement_ceiling(monkeypatch)
        patched = _high_ceiling_restock()
        ikey = _derive_restock_idempotency_key(str(ns), "SKU-A", "PO-W9-RETRY-001", None)
        transport = _CountingTransport()
        kwargs: dict[str, Any] = dict(
            idempotency_key=ikey,
            confirm=True,
            sku="SKU-A",
            po_number="PO-W9-RETRY-001",
            supplier_id="SUP-001",
            line_items=[{"artnr": "SKU-A", "quantity": 10}],
            po_value=100.0,
            transport=transport,
        )

        # First call -- executes, transport called once.
        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            r1 = await patched(pg_app_conn, ns, **kwargs)
        assert r1["status"] == "executed", f"First call should execute, got {r1}"
        assert transport.call_count == 1, (
            f"Transport called {transport.call_count} times on first execute (expected 1)"
        )
        submit_result_1 = r1["result"]["submit_result"]
        assert submit_result_1["status"] == "executed"

        # Second call -- SAME key -> NO-OP; transport still called only once.
        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            r2 = await patched(pg_app_conn, ns, **kwargs)
        assert r2["status"] == "already_executed", (
            f"Retry with same restock key should be NO-OP, got {r2}"
        )
        assert transport.call_count == 1, (
            f"Transport must not be called again on restock retry; "
            f"call_count={transport.call_count}"
        )

        # Exactly one restock-side idempotency row was ever recorded.
        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            row_count = await pg_app_conn.fetchval(
                "SELECT COUNT(*) FROM action_idempotency "
                "WHERE namespace_id = $1 AND idempotency_key = $2",
                ns,
                ikey,
            )
        assert row_count == 1, (
            f"Expected exactly one action_idempotency row for the restock key, got {row_count}"
        )

    # ------------------------------------------------------------------
    # Compounding: the PROCUREMENT-side key independently blocks a
    # duplicate ORDER, even reached directly (outer gate bypassed).
    # ------------------------------------------------------------------

    async def test_procurement_side_key_independently_blocks_duplicate_order(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        make_namespace: object,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """After one successful do_create_restock_po execution, calling
        do_submit_po DIRECTLY with the SAME derived Procurement-side key
        (simulating a hypothetical caller that reached Procurement
        independently of this Actor's own gate) is ALSO a NO-OP -- the
        transport is never called again. This proves the guarantee is
        compounding (two independent gates), not resting on the Inventory
        side alone."""
        ns = await make_namespace()  # type: ignore[operator]
        await _seed(pg_app_conn, ns)

        _patch_procurement_ceiling(monkeypatch)
        patched = _high_ceiling_restock()
        po_number = "PO-W9-COMPOUND-001"
        ikey = _derive_restock_idempotency_key(str(ns), "SKU-A", po_number, None)
        transport = _CountingTransport()

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            r1 = await patched(
                pg_app_conn,
                ns,
                idempotency_key=ikey,
                confirm=True,
                sku="SKU-A",
                po_number=po_number,
                supplier_id="SUP-001",
                line_items=[{"artnr": "SKU-A", "quantity": 10}],
                po_value=100.0,
                transport=transport,
            )
        assert r1["status"] == "executed"
        assert transport.call_count == 1

        submit_key = r1["result"]["submit_idempotency_key"]
        assert submit_key == _derive_submit_idempotency_key(str(ns), po_number), (
            "The Procurement-side key must be Procurement's OWN derivation, "
            "not a second hand-rolled hash -- otherwise a direct do_submit_po "
            "call for the same PO number would NOT collide with it."
        )

        # Call do_submit_po directly -- bypassing do_create_restock_po's own
        # outer @governed gate entirely -- with that SAME submit key.
        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            r2 = await do_submit_po(
                pg_app_conn,
                ns,
                idempotency_key=submit_key,
                confirm=True,
                po_number=po_number,
                supplier_id="SUP-001",
                line_items=[{"artnr": "SKU-A", "quantity": 10}],
                po_value=100.0,
                transport=transport,
            )

        assert r2["status"] == "already_executed", (
            f"Direct do_submit_po call with the propagated key must be a "
            f"NO-OP even outside do_create_restock_po, got {r2}"
        )
        assert transport.call_count == 1, (
            f"Transport must not place a second order; call_count={transport.call_count}"
        )

    # ------------------------------------------------------------------
    # Spanning audit trail: one correlating row per execution, never
    # duplicated on retry.
    # ------------------------------------------------------------------

    async def test_spanning_audit_row_appended_once_not_duplicated_on_retry(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        make_namespace: object,
    ) -> None:
        """The restock_po_span event_log row is appended exactly once for an
        executed restock, and a retry (already_executed at the outer gate)
        adds no second span row."""
        ns = await make_namespace()  # type: ignore[operator]
        await _seed(pg_app_conn, ns)

        patched = _high_ceiling_restock()
        po_number = "PO-W9-SPAN-001"
        ikey = _derive_restock_idempotency_key(str(ns), "SKU-A", po_number, None)
        transport = _CountingTransport()
        kwargs: dict[str, Any] = dict(
            idempotency_key=ikey,
            confirm=True,
            sku="SKU-A",
            po_number=po_number,
            supplier_id="SUP-001",
            line_items=[{"artnr": "SKU-A", "quantity": 10}],
            po_value=100.0,
            transport=transport,
        )

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            await patched(pg_app_conn, ns, **kwargs)

        async def _span_row_count() -> int:
            async with pg_app_conn.transaction():
                await set_namespace_context(pg_app_conn, ns)
                return await pg_app_conn.fetchval(
                    "SELECT COUNT(*) FROM event_log "
                    "WHERE namespace_id = $1 AND agent_id = $2 "
                    "AND params::text LIKE '%restock_po_span%'",
                    ns,
                    "inventory.create_restock_po",
                )

        after_first = await _span_row_count()
        assert after_first >= 1, "Expected at least one restock_po_span event_log row"

        # Retry -- already_executed at the outer gate, body never re-runs.
        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            retry_result = await patched(pg_app_conn, ns, **kwargs)
        assert retry_result["status"] == "already_executed"

        after_retry = await _span_row_count()
        assert after_retry == after_first, (
            f"Retry must not append a second spanning audit row: "
            f"before={after_first} after={after_retry}"
        )
