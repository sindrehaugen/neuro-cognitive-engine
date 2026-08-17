"""Integration tests for economy/graph.py — Wave 6 (graph-postings).

Validates the Acceptance criteria from Batch_121_Module_8_Wave_6.md:

  a. INVOICE / POSTING / PERIOD / MARGIN nodes upsert idempotently.
  b. The Procurement boundary edge (``PO -[posted_to]-> INVOICE``) is
     CONSUMED (read), never re-derived or written, by
     ``upsert_invoice_from_procurement`` / ``find_posted_to_po``.
  c. ``economy_source_id`` and ``change_origin='agent'`` are persisted on
     both nodes and edges.
  d. ``economy_postings`` enforces sum=0 — both at the Python level
     (``persist_financial_event`` re-validates after quantising to øre) and
     at the STORAGE level (``trg_economy_postings_assert_balanced``, proven
     by a raw INSERT that bypasses ``persist_financial_event`` entirely).
  e. ``persist_financial_event`` is idempotent on replay (natural key
     ``(namespace_id, event_id, line_no)``).
  f. FORCE RLS isolates ``economy_postings`` per tenant.
  g. Ownership guard: an unseeded namespace refuses a POSTING node write
     (deny-by-default).

Integration tests are ``@pytest.mark.integration`` — require a live Postgres
with schema.sql + migration 048 applied. A handful of plain (unmarked)
pure-logic tests for the øre-quantisation / exact-sum helpers sit alongside
them and need no DB (mirrors test_economy_cascade.py's convention).
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import asyncpg  # type: ignore[import-untyped]
import pytest

from nce.auth import set_namespace_context
from nce.entity_resolution.ownership import OwnershipError, assert_owner
from nce.entity_resolution.ownership_seed import seed_node_ownership_registry
from nce.vertical_modules.economy.events import UnbalancedPostingsError, do_emit_financial_event
from nce.vertical_modules.economy.graph import (
    _invoice_label,
    _margin_label,
    _period_label,
    _posting_label,
    _project_label,
    _quantise_ore,
    _sum_exact,
    find_posted_to_po,
    persist_financial_event,
    upsert_has_margin_edge,
    upsert_invoice_from_procurement,
    upsert_invoice_node,
    upsert_margin_node,
    upsert_period_node,
    upsert_posting_node,
    upsert_recognized_in_edge,
)

# ---------------------------------------------------------------------------
# Pure-logic tests: no DB — run even when the integration tests below skip
# for a local-environment reason.
# ---------------------------------------------------------------------------


class TestQuantiseOre:
    def test_exact_ore_amount_is_unaffected(self) -> None:
        result = _quantise_ore(Decimal("42.50"), "x")
        assert result == Decimal("42.50")
        assert result.as_tuple().exponent == -2

    def test_third_decimal_quantises_ties_away_from_zero(self) -> None:
        assert _quantise_ore(Decimal("100.005"), "x") == Decimal("100.01")

    def test_third_decimal_rounds_down_below_half(self) -> None:
        assert _quantise_ore(Decimal("100.004"), "x") == Decimal("100.00")

    def test_negative_third_decimal_ties_away_from_zero(self) -> None:
        assert _quantise_ore(Decimal("-100.005"), "x") == Decimal("-100.01")


class TestSumExact:
    def test_balanced_amounts_sum_to_zero(self) -> None:
        assert _sum_exact([Decimal("100.00"), Decimal("-100.00")]) == Decimal("0")

    def test_unbalanced_amounts_sum_nonzero(self) -> None:
        assert _sum_exact([Decimal("100.00"), Decimal("-50.00")]) == Decimal("50.00")

    def test_empty_list_sums_to_zero(self) -> None:
        assert _sum_exact([]) == Decimal("0")


# ---------------------------------------------------------------------------
# Integration test helpers
# ---------------------------------------------------------------------------


async def _seed(conn: asyncpg.Connection, ns: object) -> None:  # type: ignore[type-arg]
    """Seed ownership registry and set namespace GUC in one transaction."""
    async with conn.transaction():
        await set_namespace_context(conn, ns)
        await seed_node_ownership_registry(conn, ns)


async def _seed_posted_to_edge(
    pg_pool: asyncpg.Pool,
    ns_uuid: uuid.UUID,
    *,
    po_number: str,
    invoice_id: str,
) -> None:
    """Seed a ``PO -[posted_to]-> INVOICE`` edge directly, simulating
    Procurement's own write (Economy never writes this edge — see
    graph.py's ``find_posted_to_po``)."""
    async with pg_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO kg_edges (subject_label, predicate, object_label, namespace_id)
            VALUES ($1, 'posted_to', $2, $3::uuid)
            ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO NOTHING
            """,
            f"PO:{po_number.upper()}",
            _invoice_label(invoice_id),
            str(ns_uuid),
        )


def _balanced_event(event_type: str, **extra: Any) -> dict[str, Any]:
    """Build a normalised event via the real do_emit_financial_event —
    persist_financial_event must only ever be handed a normalised event."""
    return do_emit_financial_event(
        0.01,
        {
            "type": event_type,
            "postings": [
                {"account": "4300", "amount": Decimal("1500.00")},
                {"account": "2400", "amount": Decimal("-1500.00")},
            ],
            **extra,
        },
    )


async def _count_postings(pg_pool: asyncpg.Pool, ns_uuid: uuid.UUID, event_id: str) -> int:
    async with pg_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM economy_postings WHERE namespace_id = $1::uuid AND event_id = $2",
            str(ns_uuid),
            event_id,
        )
    return int(count)


async def _sum_postings(pg_pool: asyncpg.Pool, ns_uuid: uuid.UUID, event_id: str) -> Decimal:
    async with pg_pool.acquire() as conn:
        total = await conn.fetchval(
            "SELECT COALESCE(SUM(amount), 0) FROM economy_postings "
            "WHERE namespace_id = $1::uuid AND event_id = $2",
            str(ns_uuid),
            event_id,
        )
    return total if isinstance(total, Decimal) else Decimal(total)


# ---------------------------------------------------------------------------
# a. Node upserts idempotent
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestEconomyGraphNodeUpserts:
    async def test_upsert_invoice_node_idempotent(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        make_namespace: Any,
    ) -> None:
        ns = await make_namespace()
        await _seed(pg_app_conn, ns)

        for _ in range(2):
            async with pg_app_conn.transaction():
                await set_namespace_context(pg_app_conn, ns)
                await upsert_invoice_node(pg_app_conn, ns, invoice_id="INV-IDEM-001")

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            count = await pg_app_conn.fetchval(
                "SELECT COUNT(*) FROM kg_nodes WHERE label = $1 AND namespace_id = $2",
                _invoice_label("INV-IDEM-001"),
                ns,
            )
        assert count == 1

    async def test_upsert_posting_node_idempotent(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        make_namespace: Any,
    ) -> None:
        ns = await make_namespace()
        await _seed(pg_app_conn, ns)
        event_id = f"hash-{uuid.uuid4().hex}"

        for _ in range(2):
            async with pg_app_conn.transaction():
                await set_namespace_context(pg_app_conn, ns)
                await upsert_posting_node(pg_app_conn, ns, event_id=event_id)

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            count = await pg_app_conn.fetchval(
                "SELECT COUNT(*) FROM kg_nodes WHERE label = $1 AND namespace_id = $2",
                _posting_label(event_id),
                ns,
            )
        assert count == 1

    async def test_upsert_period_node_idempotent(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        make_namespace: Any,
    ) -> None:
        ns = await make_namespace()
        await _seed(pg_app_conn, ns)

        for _ in range(2):
            async with pg_app_conn.transaction():
                await set_namespace_context(pg_app_conn, ns)
                await upsert_period_node(pg_app_conn, ns, period_id="2026-08")

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            count = await pg_app_conn.fetchval(
                "SELECT COUNT(*) FROM kg_nodes WHERE label = $1 AND namespace_id = $2",
                _period_label("2026-08"),
                ns,
            )
        assert count == 1

    async def test_upsert_margin_node_idempotent(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        make_namespace: Any,
    ) -> None:
        ns = await make_namespace()
        await _seed(pg_app_conn, ns)
        quote_id = f"Q-{uuid.uuid4().hex[:8]}"

        for _ in range(2):
            async with pg_app_conn.transaction():
                await set_namespace_context(pg_app_conn, ns)
                await upsert_margin_node(pg_app_conn, ns, quote_id=quote_id)

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            count = await pg_app_conn.fetchval(
                "SELECT COUNT(*) FROM kg_nodes WHERE label = $1 AND namespace_id = $2",
                _margin_label(quote_id),
                ns,
            )
        assert count == 1

    async def test_node_economy_source_id_and_change_origin(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        make_namespace: Any,
    ) -> None:
        ns = await make_namespace()
        await _seed(pg_app_conn, ns)
        label = _invoice_label("INV-SRC-001")

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            await upsert_invoice_node(
                pg_app_conn, ns, invoice_id="INV-SRC-001", source_id="src-abc-123"
            )

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            row = await pg_app_conn.fetchrow(
                "SELECT change_origin, economy_source_id FROM kg_nodes "
                "WHERE label = $1 AND namespace_id = $2",
                label,
                ns,
            )
        assert row is not None
        assert row["change_origin"] == "agent"
        assert row["economy_source_id"] == "src-abc-123"

    async def test_posting_node_raises_for_unseeded_namespace(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        make_namespace: Any,
    ) -> None:
        """Deny-by-default (Contract A): no ownership row -> OwnershipError."""
        ns = await make_namespace()

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            with pytest.raises(OwnershipError) as exc_info:
                await upsert_posting_node(pg_app_conn, ns, event_id="deadbeef")

        err = exc_info.value
        assert err.node_type == "POSTING"
        assert err.owner_engine is None


# ---------------------------------------------------------------------------
# MARGIN is a per-dimension node (00-ENGINES-ROADMAP.md §9.1, the
# margin-trinity worked example): 'signed' = Sales, 'estimated' = Project,
# 'actual' = Economy. node-ownership.json registers ONLY the 'actual' row for
# 'economy' — no node-type-wide (transition=null) row exists. These three
# tests each fail if either half of that fix is reverted: restoring a
# transition:null row for MARGIN in node-ownership.json, OR removing the
# transition="actual" argument from graph.py's write path.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestMarginPerDimensionOwnership:
    async def test_economy_writing_actual_dimension_succeeds(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        make_namespace: Any,
    ) -> None:
        """Economy owns MARGIN's 'actual' dimension — the real write path
        (upsert_margin_node, which passes transition='actual' internally)
        must succeed and land a row. Fails if graph.py stops passing the
        transition argument (falls back to a node-type-wide check that no
        longer has a matching registry row)."""
        ns = await make_namespace()
        await _seed(pg_app_conn, ns)
        quote_id = f"Q-ACTUAL-{uuid.uuid4().hex[:8]}"

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            label = await upsert_margin_node(pg_app_conn, ns, quote_id=quote_id)

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            row = await pg_app_conn.fetchrow(
                "SELECT entity_type FROM kg_nodes WHERE label = $1 AND namespace_id = $2",
                label,
                ns,
            )
        assert row is not None
        assert row["entity_type"] == "MARGIN"

    async def test_economy_writing_margin_without_transition_is_denied(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        make_namespace: Any,
    ) -> None:
        """No transition=null row is registered for MARGIN, so even Economy
        itself is denied a node-type-wide write. Fails (i.e. does NOT raise)
        if node-ownership.json's MARGIN row is reverted back to
        transition: null — that would silently re-claim the whole node type."""
        ns = await make_namespace()
        await _seed(pg_app_conn, ns)

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            with pytest.raises(OwnershipError) as exc_info:
                await assert_owner(pg_app_conn, ns, "MARGIN", "economy", transition=None)

        err = exc_info.value
        assert err.owner_engine is None, (
            "MARGIN must have no node-type-wide (transition=null) owner row"
        )

    async def test_other_engine_writing_signed_dimension_is_denied(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        make_namespace: Any,
    ) -> None:
        """The 'signed' dimension (Sales' eventual slot) is genuinely free —
        NOT pre-claimed by Economy via a node-type-wide fallback row. Proven
        by asserting owner_engine is None (not merely that OwnershipError was
        raised): if a stale transition=null/'economy' row existed, the
        transition-fallback lookup in assert_owner would resolve owner_engine
        to 'economy' instead, and this assertion would catch it."""
        ns = await make_namespace()
        await _seed(pg_app_conn, ns)

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            with pytest.raises(OwnershipError) as exc_info:
                await assert_owner(pg_app_conn, ns, "MARGIN", "sales", transition="signed")

        err = exc_info.value
        assert err.owner_engine is None, (
            "the 'signed' dimension must be genuinely unclaimed, not pre-owned by economy"
        )


# ---------------------------------------------------------------------------
# b. Edges + boundary-edge consumption
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestEconomyGraphEdges:
    async def test_recognized_in_edge_and_source_id(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        make_namespace: Any,
    ) -> None:
        ns = await make_namespace()
        await _seed(pg_app_conn, ns)

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            await upsert_invoice_node(pg_app_conn, ns, invoice_id="INV-REC-001")
            await upsert_period_node(pg_app_conn, ns, period_id="2026-08")
            await upsert_recognized_in_edge(
                pg_app_conn,
                ns,
                invoice_id="INV-REC-001",
                period_id="2026-08",
                source_id="src-edge-001",
            )

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            edge = await pg_app_conn.fetchrow(
                """
                SELECT confidence, change_origin, economy_source_id FROM kg_edges
                WHERE subject_label = $1 AND predicate = 'recognized_in'
                  AND object_label = $2 AND namespace_id = $3
                """,
                _invoice_label("INV-REC-001"),
                _period_label("2026-08"),
                ns,
            )
        assert edge is not None
        assert edge["confidence"] == 1.0
        assert edge["change_origin"] == "agent"
        assert edge["economy_source_id"] == "src-edge-001"

    async def test_has_margin_edge_links_project_to_margin(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        make_namespace: Any,
    ) -> None:
        """PROJECT is owned by another engine — the edge must write even
        though no PROJECT_PROJECT node exists (kg_edges has no FK to kg_nodes)."""
        ns = await make_namespace()
        await _seed(pg_app_conn, ns)
        quote_id = f"Q-{uuid.uuid4().hex[:8]}"

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            await upsert_margin_node(pg_app_conn, ns, quote_id=quote_id)
            await upsert_has_margin_edge(pg_app_conn, ns, quote_id=quote_id)

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            edge = await pg_app_conn.fetchrow(
                """
                SELECT confidence FROM kg_edges
                WHERE subject_label = $1 AND predicate = 'has' AND object_label = $2
                  AND namespace_id = $3
                """,
                _project_label(quote_id),
                _margin_label(quote_id),
                ns,
            )
        assert edge is not None
        assert edge["confidence"] == 1.0

    async def test_upsert_invoice_from_procurement_consumes_boundary_edge(
        self,
        pg_pool: asyncpg.Pool,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        make_namespace: Any,
    ) -> None:
        """The PO -[posted_to]-> INVOICE edge is seeded exactly as
        Procurement would write it; Economy's upsert must READ it, never
        write a PO node or the edge itself."""
        ns = await make_namespace()
        await _seed(pg_app_conn, ns)
        invoice_id = f"INV-BOUNDARY-{uuid.uuid4().hex[:8]}"
        po_number = "PO-9001"
        await _seed_posted_to_edge(pg_pool, ns, po_number=po_number, invoice_id=invoice_id)

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            result = await upsert_invoice_from_procurement(pg_app_conn, ns, invoice_id=invoice_id)

        assert result["invoice_label"] == _invoice_label(invoice_id)
        assert result["po_label"] == f"PO:{po_number}"

        # Economy must not have created a PO kg_nodes row.
        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            po_node = await pg_app_conn.fetchval(
                "SELECT COUNT(*) FROM kg_nodes WHERE label = $1 AND namespace_id = $2",
                f"PO:{po_number}",
                ns,
            )
        assert po_node == 0, "Economy must never write a PO node — Procurement owns it"

    async def test_find_posted_to_po_returns_none_when_no_boundary_edge(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        make_namespace: Any,
    ) -> None:
        """Graceful degradation — never fabricated (mirrors cascade.py's
        margin-trinity 'no signed baseline' rule)."""
        ns = await make_namespace()
        await _seed(pg_app_conn, ns)

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            po_label = await find_posted_to_po(pg_app_conn, ns, "INV-NO-PO-001")

        assert po_label is None


# ---------------------------------------------------------------------------
# d + e. economy_postings: sum=0 guard + idempotency (application level)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestPersistFinancialEvent:
    async def test_persist_balanced_event_writes_rows_and_posting_node(
        self,
        pg_pool: asyncpg.Pool,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        make_namespace: Any,
    ) -> None:
        ns = await make_namespace()
        await _seed(pg_app_conn, ns)
        event = _balanced_event("economy.invoice.approved", approval_id="A-1")

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            written = await persist_financial_event(pg_app_conn, ns, event)

        assert written == 2
        assert await _count_postings(pg_pool, ns, event["hash"]) == 2
        assert await _sum_postings(pg_pool, ns, event["hash"]) == Decimal("0.00")

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            node = await pg_app_conn.fetchval(
                "SELECT COUNT(*) FROM kg_nodes WHERE label = $1 AND namespace_id = $2",
                _posting_label(event["hash"]),
                ns,
            )
        assert node == 1, "persist_financial_event must upsert the POSTING node"

    async def test_persist_event_with_no_postings_is_a_noop(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        make_namespace: Any,
    ) -> None:
        ns = await make_namespace()
        await _seed(pg_app_conn, ns)
        event = do_emit_financial_event(
            0.01, {"type": "economy.project.margin_recalculated", "postings": None}
        )

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            written = await persist_financial_event(pg_app_conn, ns, event)

        assert written == 0

    async def test_persist_financial_event_is_idempotent_on_replay(
        self,
        pg_pool: asyncpg.Pool,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        make_namespace: Any,
    ) -> None:
        ns = await make_namespace()
        await _seed(pg_app_conn, ns)
        event = _balanced_event("economy.invoice.approved", approval_id="A-2")

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            first = await persist_financial_event(pg_app_conn, ns, event)
        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            second = await persist_financial_event(pg_app_conn, ns, event)

        assert first == 2
        assert second == 0, "a replay of the identical event must insert 0 new rows"
        assert await _count_postings(pg_pool, ns, event["hash"]) == 2

    async def test_persist_financial_event_rejects_raw_unnormalised_amount(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        make_namespace: Any,
    ) -> None:
        """persist_financial_event must only ever receive the NORMALISED
        event do_emit_financial_event produces (Decimal amounts) — a raw
        float slipped in is a caller bug, not something to silently coerce."""
        ns = await make_namespace()
        await _seed(pg_app_conn, ns)
        bad_event = {
            "type": "economy.invoice.approved",
            "hash": "deadbeef",
            "postings": [{"account": "4300", "amount": 100.0}],
        }

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            with pytest.raises(ValueError, match="must already be an exact Decimal"):
                await persist_financial_event(pg_app_conn, ns, bad_event)

    async def test_persist_financial_event_raises_when_quantising_unbalances(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        make_namespace: Any,
    ) -> None:
        """A normalised event whose legs, once quantised to øre, no longer
        sum within tolerance is refused rather than silently persisted."""
        ns = await make_namespace()
        await _seed(pg_app_conn, ns)
        event = {
            "type": "economy.invoice.approved",
            "hash": "unbalanced-after-quantise",
            "postings": [
                {"account": "4300", "amount": Decimal("100.00")},
                {"account": "2400", "amount": Decimal("-99.00")},
            ],
        }

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            with pytest.raises(UnbalancedPostingsError):
                await persist_financial_event(pg_app_conn, ns, event)


# ---------------------------------------------------------------------------
# d. The STORAGE-level sum=0 backstop — proven by bypassing
# persist_financial_event entirely with a raw INSERT.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestEconomyPostingsDbTrigger:
    async def test_db_trigger_rejects_unbalanced_postings_inserted_directly(
        self,
        pg_pool: asyncpg.Pool,
        namespace_id: uuid.UUID,
    ) -> None:
        """Bypasses persist_financial_event's Python-level re-check
        entirely — a raw multi-row INSERT proves the DB-level trigger
        (trg_economy_postings_assert_balanced), not the Python guard, is
        what rejects the imbalance. If that trigger were dropped, this test
        would go green against a genuinely broken ledger."""
        event_id = f"unbalanced-{uuid.uuid4().hex}"

        async with pg_pool.acquire() as conn:
            with pytest.raises(asyncpg.PostgresError, match="does not balance"):
                await conn.execute(
                    """
                    INSERT INTO economy_postings
                        (namespace_id, event_id, event_type, line_no, account, amount)
                    VALUES
                        ($1::uuid, $2, 'test.unbalanced', 0, '4300', 100.00),
                        ($1::uuid, $2, 'test.unbalanced', 1, '2400', -50.00)
                    """,
                    str(namespace_id),
                    event_id,
                )

        async with pg_pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM economy_postings WHERE namespace_id = $1::uuid AND event_id = $2",
                str(namespace_id),
                event_id,
            )
        assert count == 0, "the whole statement (both rows) must roll back — no partial write"

    async def test_db_trigger_allows_balanced_postings_inserted_directly(
        self,
        pg_pool: asyncpg.Pool,
        namespace_id: uuid.UUID,
    ) -> None:
        event_id = f"balanced-{uuid.uuid4().hex}"

        async with pg_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO economy_postings
                    (namespace_id, event_id, event_type, line_no, account, amount)
                VALUES
                    ($1::uuid, $2, 'test.balanced', 0, '4300', 100.00),
                    ($1::uuid, $2, 'test.balanced', 1, '2400', -100.00)
                """,
                str(namespace_id),
                event_id,
            )
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM economy_postings WHERE namespace_id = $1::uuid AND event_id = $2",
                str(namespace_id),
                event_id,
            )
        assert count == 2

    async def test_db_trigger_allows_zero_row_replay_no_op(
        self,
        pg_pool: asyncpg.Pool,
        namespace_id: uuid.UUID,
    ) -> None:
        """ON CONFLICT DO NOTHING excludes skipped rows from the transition
        table, so a full replay (every line already present) must not trip
        the balance guard — idempotency and the sum=0 guard must not fight."""
        event_id = f"replay-{uuid.uuid4().hex}"

        async with pg_pool.acquire() as conn:
            insert_sql = """
                INSERT INTO economy_postings
                    (namespace_id, event_id, event_type, line_no, account, amount)
                VALUES
                    ($1::uuid, $2, 'test.replay', 0, '4300', 100.00),
                    ($1::uuid, $2, 'test.replay', 1, '2400', -100.00)
                ON CONFLICT (namespace_id, event_id, line_no) DO NOTHING
            """
            await conn.execute(insert_sql, str(namespace_id), event_id)
            # Replay: every (namespace_id, event_id, line_no) already exists,
            # so ON CONFLICT DO NOTHING skips both — must not raise.
            await conn.execute(insert_sql, str(namespace_id), event_id)

            count = await conn.fetchval(
                "SELECT COUNT(*) FROM economy_postings WHERE namespace_id = $1::uuid AND event_id = $2",
                str(namespace_id),
                event_id,
            )
        assert count == 2, "replay must not duplicate rows"


# ---------------------------------------------------------------------------
# Round-3 storage-layer fixes: append-only (WORM) grants + non-empty
# `account` CHECK. Both are proven independently of graph.py -- via a raw
# connection as the real nce_app role (grants) / a raw INSERT (CHECK).
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestEconomyPostingsRound3StorageFixes:
    async def test_nce_app_cannot_update_a_posting_row(
        self,
        pg_pool: asyncpg.Pool,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        namespace_id: uuid.UUID,
    ) -> None:
        """nce_app is granted only SELECT, INSERT on economy_postings
        (append-only/WORM, mirrors event_log) -- an UPDATE must be refused
        at the grant level, even against a row that genuinely exists and
        even though nce_app has SELECT/INSERT on the very same table."""
        event_id = f"round3-update-{uuid.uuid4().hex}"

        # economy_postings is FORCE RLS, so every statement -- including this
        # setup INSERT -- must run inside a namespace-scoped transaction.  A
        # raw pool connection only appears to work where the pool role happens
        # to hold rolbypassrls; where it does not, get_nce_namespace() raises
        # "nce.namespace_id is not set for this transaction".
        async with pg_pool.acquire() as conn, conn.transaction():
            await set_namespace_context(conn, namespace_id)
            await conn.execute(
                """
                INSERT INTO economy_postings
                    (namespace_id, event_id, event_type, line_no, account, amount)
                VALUES
                    ($1::uuid, $2, 'test.round3', 0, '4300', 100.00),
                    ($1::uuid, $2, 'test.round3', 1, '2400', -100.00)
                """,
                str(namespace_id),
                event_id,
            )

        # The namespace GUC must be set on THIS connection too: nce_app does not
        # hold rolbypassrls, so the UPDATE evaluates tenant_isolation_policy and
        # get_nce_namespace() would raise before the grant check ever surfaces.
        # pytest.raises sits outside the transaction so asyncpg rolls back.
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError) as exc_info:
            async with pg_app_conn.transaction():
                await set_namespace_context(pg_app_conn, namespace_id)
                await pg_app_conn.execute(
                    "UPDATE economy_postings SET amount = amount + 50 WHERE event_id = $1",
                    event_id,
                )
        assert (
            "permission denied" in str(exc_info.value).lower()
            or "privilege" in str(exc_info.value).lower()
        )

        # The row must be untouched -- the UPDATE never reached the table.
        async with pg_pool.acquire() as conn, conn.transaction():
            await set_namespace_context(conn, namespace_id)
            total = await conn.fetchval(
                "SELECT COALESCE(SUM(amount), 0) FROM economy_postings WHERE event_id = $1",
                event_id,
            )
        assert total == Decimal("0.00")

    async def test_nce_app_cannot_delete_a_posting_row(
        self,
        pg_pool: asyncpg.Pool,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        namespace_id: uuid.UUID,
    ) -> None:
        """Same WORM discipline for DELETE -- corrections must be
        compensating reversal postings, never an in-place delete."""
        event_id = f"round3-delete-{uuid.uuid4().hex}"

        # FORCE RLS -- see the note in the UPDATE test above.
        async with pg_pool.acquire() as conn, conn.transaction():
            await set_namespace_context(conn, namespace_id)
            await conn.execute(
                """
                INSERT INTO economy_postings
                    (namespace_id, event_id, event_type, line_no, account, amount)
                VALUES
                    ($1::uuid, $2, 'test.round3', 0, '4300', 100.00),
                    ($1::uuid, $2, 'test.round3', 1, '2400', -100.00)
                """,
                str(namespace_id),
                event_id,
            )

        # See the note in the UPDATE test -- nce_app needs the namespace GUC set
        # on its own connection or RLS raises before the grant check surfaces.
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError) as exc_info:
            async with pg_app_conn.transaction():
                await set_namespace_context(pg_app_conn, namespace_id)
                await pg_app_conn.execute(
                    "DELETE FROM economy_postings WHERE event_id = $1",
                    event_id,
                )
        assert (
            "permission denied" in str(exc_info.value).lower()
            or "privilege" in str(exc_info.value).lower()
        )

        # The rows must still exist -- the DELETE never reached the table.
        async with pg_pool.acquire() as conn, conn.transaction():
            await set_namespace_context(conn, namespace_id)
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM economy_postings WHERE event_id = $1",
                event_id,
            )
        assert count == 2

    async def test_empty_account_rejected_at_db_level(
        self,
        pg_pool: asyncpg.Pool,
        namespace_id: uuid.UUID,
    ) -> None:
        """Bypasses graph.py's Python-level guard entirely -- a raw INSERT
        with an empty `account` on one leg of an otherwise-balanced event
        must be rejected by ck_economy_postings_account_nonempty, the
        STORAGE-level backstop (Batch 118's lesson: balancing to zero is
        necessary, never sufficient)."""
        event_id = f"round3-empty-account-{uuid.uuid4().hex}"

        async with pg_pool.acquire() as conn:
            with pytest.raises(asyncpg.exceptions.CheckViolationError):
                await conn.execute(
                    """
                    INSERT INTO economy_postings
                        (namespace_id, event_id, event_type, line_no, account, amount)
                    VALUES
                        ($1::uuid, $2, 'test.empty_account', 0, '', 100.00),
                        ($1::uuid, $2, 'test.empty_account', 1, '2400', -100.00)
                    """,
                    str(namespace_id),
                    event_id,
                )

        async with pg_pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM economy_postings WHERE event_id = $1",
                event_id,
            )
        assert count == 0, "the whole statement must roll back -- no partial write"

    async def test_whitespace_only_account_rejected_at_db_level(
        self,
        pg_pool: asyncpg.Pool,
        namespace_id: uuid.UUID,
    ) -> None:
        """TRIM() in the CHECK also catches a whitespace-only account -- a
        bare `account <> ''` (without TRIM) would let this slip through."""
        event_id = f"round3-whitespace-account-{uuid.uuid4().hex}"

        async with pg_pool.acquire() as conn:
            with pytest.raises(asyncpg.exceptions.CheckViolationError):
                await conn.execute(
                    """
                    INSERT INTO economy_postings
                        (namespace_id, event_id, event_type, line_no, account, amount)
                    VALUES
                        ($1::uuid, $2, 'test.whitespace_account', 0, '   ', 100.00),
                        ($1::uuid, $2, 'test.whitespace_account', 1, '2400', -100.00)
                    """,
                    str(namespace_id),
                    event_id,
                )

        async with pg_pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM economy_postings WHERE event_id = $1",
                event_id,
            )
        assert count == 0


# ---------------------------------------------------------------------------
# f. FORCE RLS isolates economy_postings per tenant
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rls_isolates_economy_postings_between_namespaces(
    pg_pool: asyncpg.Pool,
    pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
    make_namespace: Any,
) -> None:
    ns_a = await make_namespace()
    ns_b = await make_namespace()
    await _seed(pg_app_conn, ns_a)
    event = _balanced_event("economy.invoice.approved", approval_id="RLS-1")

    async with pg_app_conn.transaction():
        await set_namespace_context(pg_app_conn, ns_a)
        await persist_financial_event(pg_app_conn, ns_a, event)

    async with pg_app_conn.transaction():
        await set_namespace_context(pg_app_conn, ns_b)
        visible_from_b = await pg_app_conn.fetchval(
            "SELECT COUNT(*) FROM economy_postings WHERE event_id = $1",
            event["hash"],
        )
    assert visible_from_b == 0, "ns_b must not see ns_a's postings"

    async with pg_app_conn.transaction():
        await set_namespace_context(pg_app_conn, ns_a)
        visible_from_a = await pg_app_conn.fetchval(
            "SELECT COUNT(*) FROM economy_postings WHERE event_id = $1",
            event["hash"],
        )
    assert visible_from_a == 2, "ns_a must see its own postings"
