"""
nce/vertical_modules/procurement/graph.py
==========================================
Cognitive-graph upserts for the Procurement vertical module (Wave 6: graph-upserts).

Responsibilities:
  - Upsert ``PO`` (purchase order) nodes into ``kg_nodes``, owned by Procurement.
  - Upsert ``PROCUREMENT_MATCH`` nodes into ``kg_nodes``, owned by Procurement.
  - Upsert ``VENDOR -[offers]-> SKU`` edges (cross-engine: VENDOR is a future
    Vendors engine node; SKU uses the canonical PRODUCT_SKU label from Product).
  - Upsert ``PO -[matched_by]-> PROCUREMENT_MATCH`` edges with ``confidence``
    **on the edge** (rule 7: confidence lives on kg_edges only, never kg_nodes).

Ownership (Contract A §9.1):
  PO and PROCUREMENT_MATCH nodes are owned by Procurement; every own-node write
  is guarded by ``nce.entity_resolution.ownership.assert_owner``.
  VENDOR and SKU/PRODUCT_SKU nodes are owned by future engines (Vendors, Product).
  Procurement writes edges that reference those labels by string only — kg_edges
  has no FK to kg_nodes, so cross-engine edge writes are always safe.

Template:
  Node upsert pattern copied from
  ``nce/vertical_modules/dynamics365/sync.py::_upsert_kg_node`` (line 820).
  Edge upsert pattern mirrors
  ``nce/vertical_modules/dynamics365/sync.py::_upsert_kg_edges_batch`` (line 863).
  Transactional outbox: ``emit_graph_write`` is called inside the same connection
  as the INSERT so both commit or both roll back.

Design invariants (uncle-bob-craft):
  - No web / HTTP / admin imports; this module is domain-core only.
  - One function, one job; no shared state.
  - ``confidence`` only on edges — never on nodes (wave rule 7).
  - ``procurement_source_id`` and ``change_origin='sync'`` are set on all derived
    writes (§2.3 per-vertical source-id pattern; tags all graph rows as sync-origin).
  - No ``metadata`` column on kg_nodes or kg_edges (does not exist in this schema).
"""

from __future__ import annotations

import logging
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]

from nce.entity_resolution.ownership import assert_owner
from nce.events.emit import emit_graph_write

log = logging.getLogger("nce.vertical_modules.procurement.graph")

# ---------------------------------------------------------------------------
# Engine identifier and node types — must match node-ownership.json entries
# ---------------------------------------------------------------------------
_PROCUREMENT_ENGINE: str = "procurement"
_NODE_TYPE_PO: str = "PO"
_NODE_TYPE_MATCH: str = "PROCUREMENT_MATCH"

# Edge predicates written by this module.
_PREDICATE_OFFERS: str = "offers"
_PREDICATE_MATCHED_BY: str = "matched_by"

# ---------------------------------------------------------------------------
# Label helpers
# ---------------------------------------------------------------------------


def _po_label(po_number: str) -> str:
    """Canonical kg_nodes label for a PO node: ``PO:<PO_NUMBER>``."""
    return f"PO:{po_number.upper()}"


def _procurement_match_label(match_id: str) -> str:
    """Canonical kg_nodes label for a PROCUREMENT_MATCH node: ``PROCUREMENT_MATCH:<ID>``."""
    return f"PROCUREMENT_MATCH:{match_id.upper()}"


def _vendor_label(vendor_id: str) -> str:
    """Label for a VENDOR node (owned by future Vendors engine, referenced by edge only)."""
    return f"VENDOR:{vendor_id.upper()}"


def _sku_label(manufacturer: str, mfr_part_no: str) -> str:
    """Canonical PRODUCT_SKU label — must match ``product/graph.py::_product_label``."""
    return f"PRODUCT:{manufacturer.upper()}:{mfr_part_no.upper()}"


def _score_to_confidence(score_0_100: float) -> float:
    """Map a 0–100 match score to a 0–1 edge confidence, clamped to [0, 1]."""
    return max(0.0, min(1.0, score_0_100 / 100.0))


# ---------------------------------------------------------------------------
# Public: upsert_po_node
# ---------------------------------------------------------------------------


async def upsert_po_node(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: str | UUID,
    *,
    po_number: str,
    source_id: str | None = None,
) -> None:
    """Upsert a single PO node into kg_nodes.

    The label is deterministic: ``PO:<PO_NUMBER>`` (upper-cased), so the same
    purchase order across different ingestion runs always maps to the same node —
    idempotent on ``(label, namespace_id)``.

    Ownership guard: raises ``OwnershipError`` when a non-procurement engine
    attempts this write (deny-by-default when no registry row exists).

    ``confidence`` is intentionally absent from kg_nodes — it belongs to edges
    only (wave rule 7).

    Parameters
    ----------
    conn:
        asyncpg connection with RLS namespace GUC already set.
    namespace_id:
        Active namespace UUID.
    po_number:
        Purchase order number; used to construct the deterministic label.
    source_id:
        Optional procurement source record ID (stored as ``procurement_source_id``
        for retirement tracking). COALESCE on conflict so a later untagged write
        never clears an existing tag.
    """
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id

    await assert_owner(conn, ns_uuid, _NODE_TYPE_PO, _PROCUREMENT_ENGINE)

    label = _po_label(po_number)

    await conn.execute(
        """
        INSERT INTO kg_nodes
            (label, entity_type, namespace_id, change_origin, procurement_source_id)
        VALUES ($1, $2, $3::uuid, 'sync', $4)
        ON CONFLICT (label, namespace_id) DO UPDATE
            SET entity_type            = EXCLUDED.entity_type,
                change_origin          = 'sync',
                procurement_source_id  = COALESCE(
                    EXCLUDED.procurement_source_id,
                    kg_nodes.procurement_source_id
                ),
                updated_at             = NOW()
        """,
        label,
        _NODE_TYPE_PO,
        str(ns_uuid),
        source_id,
    )

    # Transactional outbox: emit inside the same connection / transaction so
    # the graph write and its event are always committed together.
    await emit_graph_write(
        conn,
        namespace_id=ns_uuid,
        node_type=_NODE_TYPE_PO,
        op="upserted",
        node_id=label,
    )


# ---------------------------------------------------------------------------
# Public: upsert_procurement_match_node
# ---------------------------------------------------------------------------


async def upsert_procurement_match_node(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: str | UUID,
    *,
    match_id: str,
    source_id: str | None = None,
) -> None:
    """Upsert a single PROCUREMENT_MATCH node into kg_nodes.

    The label is deterministic: ``PROCUREMENT_MATCH:<MATCH_ID>`` (upper-cased).

    Ownership guard: raises ``OwnershipError`` for non-procurement engines.

    Parameters
    ----------
    conn:
        asyncpg connection with RLS namespace GUC already set.
    namespace_id:
        Active namespace UUID.
    match_id:
        Procurement match record ID; used to construct the deterministic label.
    source_id:
        Optional procurement source record ID for retirement tracking.
    """
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id

    await assert_owner(conn, ns_uuid, _NODE_TYPE_MATCH, _PROCUREMENT_ENGINE)

    label = _procurement_match_label(match_id)

    await conn.execute(
        """
        INSERT INTO kg_nodes
            (label, entity_type, namespace_id, change_origin, procurement_source_id)
        VALUES ($1, $2, $3::uuid, 'sync', $4)
        ON CONFLICT (label, namespace_id) DO UPDATE
            SET entity_type            = EXCLUDED.entity_type,
                change_origin          = 'sync',
                procurement_source_id  = COALESCE(
                    EXCLUDED.procurement_source_id,
                    kg_nodes.procurement_source_id
                ),
                updated_at             = NOW()
        """,
        label,
        _NODE_TYPE_MATCH,
        str(ns_uuid),
        source_id,
    )

    await emit_graph_write(
        conn,
        namespace_id=ns_uuid,
        node_type=_NODE_TYPE_MATCH,
        op="upserted",
        node_id=label,
    )


# ---------------------------------------------------------------------------
# Public: upsert_offers_edge  (VENDOR -[offers]-> SKU)
# ---------------------------------------------------------------------------


async def upsert_offers_edge(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: str | UUID,
    *,
    vendor_id: str,
    manufacturer: str,
    mfr_part_no: str,
    confidence: float,
    source_id: str | None = None,
) -> None:
    """Upsert a ``VENDOR -[offers]-> PRODUCT_SKU`` edge in kg_edges.

    VENDOR and PRODUCT_SKU nodes are owned by other engines (Vendors and Product
    respectively). Procurement writes the edge by label — kg_edges has no FK to
    kg_nodes, so cross-engine edge writes are always permitted.

    ``confidence`` (0–1) is stored **on the edge**, never on either node
    (wave invariant, rule 7). Pass the 0–100 raw match score through
    ``_score_to_confidence`` before calling this function, or supply a pre-scaled
    value already in [0, 1].

    Parameters
    ----------
    conn:
        asyncpg connection with RLS namespace GUC already set.
    namespace_id:
        Active namespace UUID.
    vendor_id:
        Vendor identifier used to construct the subject label.
    manufacturer:
        Product manufacturer (used with ``mfr_part_no`` for the object label).
    mfr_part_no:
        Manufacturer part number.
    confidence:
        Edge confidence score in [0, 1].
    source_id:
        Optional procurement source record ID for retirement tracking.
    """
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id

    subject = _vendor_label(vendor_id)
    obj = _sku_label(manufacturer, mfr_part_no)

    await conn.execute(
        """
        INSERT INTO kg_edges
            (subject_label, predicate, object_label, confidence,
             namespace_id, change_origin, procurement_source_id)
        VALUES ($1, $2, $3, $4, $5::uuid, 'sync', $6)
        ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO UPDATE
            SET confidence            = EXCLUDED.confidence,
                change_origin         = 'sync',
                procurement_source_id = COALESCE(
                    EXCLUDED.procurement_source_id,
                    kg_edges.procurement_source_id
                ),
                updated_at            = NOW()
        """,
        subject,
        _PREDICATE_OFFERS,
        obj,
        float(confidence),
        str(ns_uuid),
        source_id,
    )


# ---------------------------------------------------------------------------
# Public: upsert_matched_by_edge  (PO -[matched_by]-> PROCUREMENT_MATCH)
# ---------------------------------------------------------------------------


async def upsert_matched_by_edge(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: str | UUID,
    *,
    po_number: str,
    match_id: str,
    confidence: float,
    source_id: str | None = None,
) -> None:
    """Upsert a ``PO -[matched_by]-> PROCUREMENT_MATCH`` edge in kg_edges.

    Both endpoints (PO and PROCUREMENT_MATCH) are owned by Procurement.
    ``confidence`` (0–1) is stored **on the edge** only (rule 7).

    Parameters
    ----------
    conn:
        asyncpg connection with RLS namespace GUC already set.
    namespace_id:
        Active namespace UUID.
    po_number:
        Purchase order number — used to construct the subject label.
    match_id:
        Procurement match ID — used to construct the object label.
    confidence:
        Edge confidence score in [0, 1]. Derive from the 0–100 match score
        via ``_score_to_confidence``.
    source_id:
        Optional procurement source record ID for retirement tracking.
    """
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id

    subject = _po_label(po_number)
    obj = _procurement_match_label(match_id)

    await conn.execute(
        """
        INSERT INTO kg_edges
            (subject_label, predicate, object_label, confidence,
             namespace_id, change_origin, procurement_source_id)
        VALUES ($1, $2, $3, $4, $5::uuid, 'sync', $6)
        ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO UPDATE
            SET confidence            = EXCLUDED.confidence,
                change_origin         = 'sync',
                procurement_source_id = COALESCE(
                    EXCLUDED.procurement_source_id,
                    kg_edges.procurement_source_id
                ),
                updated_at            = NOW()
        """,
        subject,
        _PREDICATE_MATCHED_BY,
        obj,
        float(confidence),
        str(ns_uuid),
        source_id,
    )
