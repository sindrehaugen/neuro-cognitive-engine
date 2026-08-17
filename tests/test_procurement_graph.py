"""Integration tests for Procurement graph upserts (Wave 6 — graph-upserts).

Validates:
  a. PO and PROCUREMENT_MATCH nodes upsert idempotently (no duplicates on re-run).
  b. ``confidence`` is on edges only — kg_nodes rows have no confidence column
     (structural assertion, not a value check).
  c. ``procurement_source_id`` and ``change_origin='sync'`` are persisted on both
     nodes and edges.
  d. Edges carry ``confidence`` in [0, 1] mapped from a 0–100 score.
  e. RLS isolates data across namespaces (ns_a rows invisible from ns_b).
  f. VENDOR/SKU labels exist in kg_edges even without matching kg_nodes rows
     (cross-engine edge write, no FK enforcement on kg_edges).

Fixtures used:
  ``pg_app_conn`` — asyncpg connection as nce_app (RLS enforced).
  ``make_namespace`` — factory that inserts a new namespace row (owner role).
  ``set_namespace_context`` (nce.auth) — sets the GUC required by RLS.
  ``seed_node_ownership_registry`` — seeds PO + PROCUREMENT_MATCH ownership rows.

Runs as @pytest.mark.integration — requires a live Postgres with schema.sql and
migration 036 applied (run scratch/_apply_probe_b032.py first).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import asyncpg  # type: ignore[import-untyped]
import pytest

from nce.auth import set_namespace_context
from nce.entity_resolution.ownership import OwnershipError
from nce.entity_resolution.ownership_seed import seed_node_ownership_registry
from nce.vertical_modules.procurement.graph import (
    _po_label,
    _procurement_match_label,
    _score_to_confidence,
    _sku_label,
    _vendor_label,
    upsert_matched_by_edge,
    upsert_offers_edge,
    upsert_po_node,
    upsert_procurement_match_node,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MOCK_EMIT = "nce.vertical_modules.procurement.graph.emit_graph_write"


async def _seed(conn: asyncpg.Connection, ns: object) -> None:  # type: ignore[type-arg]
    """Seed ownership registry and set namespace GUC in one transaction."""
    async with conn.transaction():
        await set_namespace_context(conn, ns)
        await seed_node_ownership_registry(conn, ns)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestProcurementGraphUpserts:
    """Integration test suite for procurement graph.py (Wave 6)."""

    # ------------------------------------------------------------------
    # a. PO node upserts idempotently (no duplicate on re-run)
    # ------------------------------------------------------------------

    async def test_upsert_po_node_idempotent(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        make_namespace: object,
    ) -> None:
        """upsert_po_node is idempotent — two calls produce exactly one kg_nodes row."""
        ns = await make_namespace()  # type: ignore[operator]
        await _seed(pg_app_conn, ns)

        with patch(_MOCK_EMIT, new_callable=AsyncMock):
            for _ in range(2):
                async with pg_app_conn.transaction():
                    await set_namespace_context(pg_app_conn, ns)
                    await upsert_po_node(
                        pg_app_conn,
                        ns,
                        po_number="PO-IDEM-001",
                        source_id="src-po-001",
                    )

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            count = await pg_app_conn.fetchval(
                "SELECT COUNT(*) FROM kg_nodes WHERE label = $1 AND namespace_id = $2",
                _po_label("PO-IDEM-001"),
                ns,
            )
        assert count == 1, f"Expected 1 PO node, got {count}"

    # ------------------------------------------------------------------
    # a. PROCUREMENT_MATCH node upserts idempotently
    # ------------------------------------------------------------------

    async def test_upsert_procurement_match_node_idempotent(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        make_namespace: object,
    ) -> None:
        """upsert_procurement_match_node is idempotent — two calls produce one row."""
        ns = await make_namespace()  # type: ignore[operator]
        await _seed(pg_app_conn, ns)

        with patch(_MOCK_EMIT, new_callable=AsyncMock):
            for _ in range(2):
                async with pg_app_conn.transaction():
                    await set_namespace_context(pg_app_conn, ns)
                    await upsert_procurement_match_node(
                        pg_app_conn,
                        ns,
                        match_id="MATCH-IDEM-001",
                        source_id="src-match-001",
                    )

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            count = await pg_app_conn.fetchval(
                "SELECT COUNT(*) FROM kg_nodes WHERE label = $1 AND namespace_id = $2",
                _procurement_match_label("MATCH-IDEM-001"),
                ns,
            )
        assert count == 1, f"Expected 1 PROCUREMENT_MATCH node, got {count}"

    # ------------------------------------------------------------------
    # b. kg_nodes has no confidence column (structural assertion)
    # ------------------------------------------------------------------

    async def test_kg_nodes_has_no_confidence_column(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
    ) -> None:
        """Structural: kg_nodes must not have a confidence column (rule 7)."""
        row = await pg_app_conn.fetchrow(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name  = 'kg_nodes'
              AND column_name = 'confidence'
            """
        )
        assert row is None, (
            "kg_nodes must NOT have a confidence column — confidence belongs to kg_edges only"
        )

    # ------------------------------------------------------------------
    # c. procurement_source_id and change_origin='sync' are persisted
    # ------------------------------------------------------------------

    async def test_node_procurement_source_id_and_change_origin(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        make_namespace: object,
    ) -> None:
        """PO node row has procurement_source_id set and change_origin='sync'."""
        ns = await make_namespace()  # type: ignore[operator]
        await _seed(pg_app_conn, ns)

        label = _po_label("PO-SRC-001")

        with patch(_MOCK_EMIT, new_callable=AsyncMock):
            async with pg_app_conn.transaction():
                await set_namespace_context(pg_app_conn, ns)
                await upsert_po_node(
                    pg_app_conn,
                    ns,
                    po_number="PO-SRC-001",
                    source_id="src-abc-123",
                )

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            row = await pg_app_conn.fetchrow(
                """
                SELECT change_origin, procurement_source_id
                FROM kg_nodes
                WHERE label = $1 AND namespace_id = $2
                """,
                label,
                ns,
            )

        assert row is not None, "PO node row not found after upsert"
        assert row["change_origin"] == "sync", (
            f"Expected change_origin='sync', got {row['change_origin']!r}"
        )
        assert row["procurement_source_id"] == "src-abc-123", (
            f"Expected procurement_source_id='src-abc-123', got {row['procurement_source_id']!r}"
        )

    # ------------------------------------------------------------------
    # c + d. Edge has confidence in [0,1] and procurement_source_id set
    # ------------------------------------------------------------------

    async def test_matched_by_edge_confidence_and_source_id(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        make_namespace: object,
    ) -> None:
        """PO -[matched_by]-> PROCUREMENT_MATCH edge has confidence in [0,1] and source_id."""
        ns = await make_namespace()  # type: ignore[operator]
        await _seed(pg_app_conn, ns)

        po_label = _po_label("PO-CONF-001")
        match_label = _procurement_match_label("MATCH-CONF-001")
        raw_score = 87.5
        expected_conf = _score_to_confidence(raw_score)  # 0.875

        with patch(_MOCK_EMIT, new_callable=AsyncMock):
            async with pg_app_conn.transaction():
                await set_namespace_context(pg_app_conn, ns)
                await upsert_po_node(pg_app_conn, ns, po_number="PO-CONF-001")
                await upsert_procurement_match_node(pg_app_conn, ns, match_id="MATCH-CONF-001")
                await upsert_matched_by_edge(
                    pg_app_conn,
                    ns,
                    po_number="PO-CONF-001",
                    match_id="MATCH-CONF-001",
                    confidence=expected_conf,
                    source_id="src-edge-001",
                )

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            edge = await pg_app_conn.fetchrow(
                """
                SELECT confidence, change_origin, procurement_source_id
                FROM kg_edges
                WHERE subject_label = $1
                  AND predicate      = 'matched_by'
                  AND object_label   = $2
                  AND namespace_id   = $3
                """,
                po_label,
                match_label,
                ns,
            )

        assert edge is not None, "matched_by edge not found after upsert"
        assert 0.0 <= edge["confidence"] <= 1.0, (
            f"Edge confidence {edge['confidence']} is not in [0, 1]"
        )
        assert abs(edge["confidence"] - expected_conf) < 1e-6, (
            f"Edge confidence {edge['confidence']!r} != expected {expected_conf!r}"
        )
        assert edge["change_origin"] == "sync", (
            f"Expected change_origin='sync', got {edge['change_origin']!r}"
        )
        assert edge["procurement_source_id"] == "src-edge-001", (
            f"Expected procurement_source_id='src-edge-001', got {edge['procurement_source_id']!r}"
        )

    # ------------------------------------------------------------------
    # d. offers edge has confidence in [0,1]
    # ------------------------------------------------------------------

    async def test_offers_edge_confidence(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        make_namespace: object,
    ) -> None:
        """VENDOR -[offers]-> SKU edge has confidence in [0,1]; cross-engine labels accepted."""
        ns = await make_namespace()  # type: ignore[operator]
        await _seed(pg_app_conn, ns)

        vendor = _vendor_label("VENDOR-ACME")
        sku = _sku_label("Biamp", "TesiraFORTE-CI")
        raw_score = 72.0
        conf = _score_to_confidence(raw_score)  # 0.72

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            await upsert_offers_edge(
                pg_app_conn,
                ns,
                vendor_id="VENDOR-ACME",
                manufacturer="Biamp",
                mfr_part_no="TesiraFORTE-CI",
                confidence=conf,
                source_id="src-offer-001",
            )

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            edge = await pg_app_conn.fetchrow(
                """
                SELECT confidence, change_origin, procurement_source_id
                FROM kg_edges
                WHERE subject_label = $1
                  AND predicate      = 'offers'
                  AND object_label   = $2
                  AND namespace_id   = $3
                """,
                vendor,
                sku,
                ns,
            )

        assert edge is not None, "offers edge not found after upsert"
        assert 0.0 <= edge["confidence"] <= 1.0, (
            f"Edge confidence {edge['confidence']} is not in [0, 1]"
        )
        assert abs(edge["confidence"] - conf) < 1e-6, (
            f"Edge confidence {edge['confidence']!r} != expected {conf!r}"
        )
        assert edge["change_origin"] == "sync"
        assert edge["procurement_source_id"] == "src-offer-001"

    # ------------------------------------------------------------------
    # d. _score_to_confidence maps and clamps correctly (unit-style inside integration)
    # ------------------------------------------------------------------

    async def test_score_to_confidence_mapping(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
    ) -> None:
        """_score_to_confidence maps 0–100 to 0–1 and clamps out-of-range values."""
        assert _score_to_confidence(0.0) == 0.0
        assert _score_to_confidence(100.0) == 1.0
        assert abs(_score_to_confidence(50.0) - 0.5) < 1e-9
        assert _score_to_confidence(-5.0) == 0.0, "Negative score must clamp to 0.0"
        assert _score_to_confidence(110.0) == 1.0, "Score > 100 must clamp to 1.0"

    # ------------------------------------------------------------------
    # e. RLS isolates across namespaces
    # ------------------------------------------------------------------

    async def test_rls_isolation_across_namespaces(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        make_namespace: object,
    ) -> None:
        """Rows written to ns_a are invisible when the GUC is set to ns_b."""
        ns_a = await make_namespace()  # type: ignore[operator]
        ns_b = await make_namespace()  # type: ignore[operator]

        await _seed(pg_app_conn, ns_a)
        await _seed(pg_app_conn, ns_b)

        po_number = "PO-RLS-ISOLATION-001"
        label = _po_label(po_number)

        # Write PO node under ns_a.
        with patch(_MOCK_EMIT, new_callable=AsyncMock):
            async with pg_app_conn.transaction():
                await set_namespace_context(pg_app_conn, ns_a)
                await upsert_po_node(pg_app_conn, ns_a, po_number=po_number)

        # Verify row is visible under ns_a.
        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns_a)
            row_a = await pg_app_conn.fetchrow(
                "SELECT label FROM kg_nodes WHERE label = $1 AND namespace_id = $2",
                label,
                ns_a,
            )
        assert row_a is not None, "PO node not visible under its own namespace ns_a"

        # Verify row is NOT visible under ns_b (RLS).
        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns_b)
            row_b = await pg_app_conn.fetchrow(
                "SELECT label FROM kg_nodes WHERE label = $1 AND namespace_id = $2",
                label,
                ns_a,  # query by ns_a but GUC is set to ns_b
            )
        assert row_b is None, (
            "RLS isolation failed: ns_a PO node is visible when GUC is set to ns_b"
        )

    # ------------------------------------------------------------------
    # Ownership: upsert raises OwnershipError for unseeded namespace
    # ------------------------------------------------------------------

    async def test_po_node_raises_for_unseeded_namespace(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        make_namespace: object,
    ) -> None:
        """upsert_po_node raises OwnershipError on an unseeded namespace."""
        ns = await make_namespace()  # type: ignore[operator]

        with patch(_MOCK_EMIT, new_callable=AsyncMock):
            async with pg_app_conn.transaction():
                await set_namespace_context(pg_app_conn, ns)
                with pytest.raises(OwnershipError) as exc_info:
                    await upsert_po_node(pg_app_conn, ns, po_number="PO-NO-SEED")

        err = exc_info.value
        assert err.node_type == "PO"
        assert err.owner_engine is None, "Deny-by-default: owner_engine must be None"

    # ------------------------------------------------------------------
    # f. Cross-engine edge write succeeds without kg_nodes FK
    # ------------------------------------------------------------------

    async def test_offers_edge_cross_engine_no_kg_nodes_required(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        make_namespace: object,
    ) -> None:
        """offers edge can reference VENDOR/SKU labels with no matching kg_nodes rows."""
        ns = await make_namespace()  # type: ignore[operator]
        await _seed(pg_app_conn, ns)

        # Do NOT upsert VENDOR or SKU nodes — they belong to other engines.
        vendor = _vendor_label("ORPHAN-VENDOR")
        sku = _sku_label("OrphanMfr", "ORPHAN-PART-999")
        conf = _score_to_confidence(55.0)

        # Must not raise even though no kg_nodes rows exist for these labels.
        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            await upsert_offers_edge(
                pg_app_conn,
                ns,
                vendor_id="ORPHAN-VENDOR",
                manufacturer="OrphanMfr",
                mfr_part_no="ORPHAN-PART-999",
                confidence=conf,
            )

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            edge = await pg_app_conn.fetchrow(
                """
                SELECT confidence FROM kg_edges
                WHERE subject_label = $1
                  AND predicate = 'offers'
                  AND object_label = $2
                  AND namespace_id = $3
                """,
                vendor,
                sku,
                ns,
            )
        assert edge is not None, "Cross-engine offers edge not found after upsert"
        assert abs(edge["confidence"] - conf) < 1e-6
