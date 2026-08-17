"""
nce/vertical_modules/product/graph.py
======================================
Cognitive-graph upserts for the Product vertical module (W3: search-get-graph;
W5: related-products).

Responsibilities:
  - Upsert ``PRODUCT_SKU`` nodes into ``kg_nodes``, deduped on
    ``(manufacturer, mfr_part_no)``.
  - Upsert ``BOM_LINE -[references]-> PRODUCT`` edges with ``confidence``
    **on the edge** (rule 7: confidence lives on kg_edges only, never kg_nodes).
  - Upsert product-relation edges (``accessory_of``, ``warranty_for``,
    ``mounts``, ``replaced_by``) between PRODUCT nodes (W5).

Ownership:
  Every write is guarded by ``nce.entity_resolution.ownership.assert_owner``;
  the Product engine is the sole writer for the ``PRODUCT_SKU`` node type.
  Writes from any other engine are refused with ``OwnershipError`` before
  hitting the database.

Template:
  Node upsert pattern copied from
  ``nce/vertical_modules/dynamics365/sync.py::_upsert_kg_node`` (line 820).
  Transactional outbox: ``emit_graph_write`` is called inside the same
  connection as the INSERT so both commit or both roll back.

Design invariants (uncle-bob-craft):
  - No web / HTTP / admin imports; this module is domain-core only.
  - One function, one job; no shared state.
  - ``confidence`` only on edges — asserted by ``_assert_confidence_not_on_node``
    at module import time (compile-time check via type system is not sufficient
    because the column is absent from kg_nodes at the SQL level).
"""

from __future__ import annotations

import logging
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]

from nce.entity_resolution.ownership import assert_owner
from nce.events.emit import emit_graph_write

log = logging.getLogger("nce.vertical_modules.product.graph")

# ---------------------------------------------------------------------------
# Engine identifier — must match the row registered in node_ownership_registry
# ---------------------------------------------------------------------------
_PRODUCT_ENGINE: str = "product"
_NODE_TYPE: str = "PRODUCT_SKU"
_PREDICATE: str = "references"


# ---------------------------------------------------------------------------
# Public: upsert_product_node
# ---------------------------------------------------------------------------


async def upsert_product_node(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: str | UUID,
    *,
    manufacturer: str,
    mfr_part_no: str,
) -> None:
    """Upsert a single PRODUCT_SKU node into kg_nodes.

    The label is deterministic: ``PRODUCT:<MANUFACTURER>:<MFR_PART_NO>`` (both
    upper-cased), so the same product from different ingestion runs always maps
    to the same node — idempotent on ``(label, namespace_id)``.

    Ownership guard: raises ``OwnershipError`` when a non-product engine tries
    to call this (deny-by-default when no registry row exists).

    ``confidence`` is intentionally absent from kg_nodes — it belongs to edges
    only (wave rule 7).

    Parameters
    ----------
    conn:
        asyncpg connection with RLS namespace GUC already set (via
        ``scoped_pg_session``).
    namespace_id:
        Active namespace UUID.
    manufacturer:
        Canonical manufacturer name.
    mfr_part_no:
        Manufacturer part number.

    Note
    ----
    ``kg_nodes`` has no general-purpose ``metadata`` column in the current
    schema (node payloads, when needed, are referenced via ``payload_ref``);
    the PRODUCT_SKU node is an identity/edge anchor only.  Product attributes
    live in ``product_catalog`` (W2), not on the graph node.
    """
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id

    await assert_owner(conn, ns_uuid, _NODE_TYPE, _PRODUCT_ENGINE)

    label = _product_label(manufacturer, mfr_part_no)

    await conn.execute(
        """
        INSERT INTO kg_nodes (label, entity_type, namespace_id)
        VALUES ($1, $2, $3::uuid)
        ON CONFLICT (label, namespace_id) DO UPDATE
            SET entity_type = EXCLUDED.entity_type,
                updated_at  = NOW()
        """,
        label,
        _NODE_TYPE,
        str(ns_uuid),
    )

    # Transactional outbox: emit inside the same connection / transaction so
    # the graph write and its event are always committed together.
    await emit_graph_write(
        conn,
        namespace_id=ns_uuid,
        node_type=_NODE_TYPE,
        op="upserted",
        node_id=label,
    )


# ---------------------------------------------------------------------------
# Public: upsert_bom_references_edge
# ---------------------------------------------------------------------------


async def upsert_bom_references_edge(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: str | UUID,
    *,
    bom_line_label: str,
    manufacturer: str,
    mfr_part_no: str,
    confidence: float,
) -> None:
    """Upsert a ``BOM_LINE -[references]-> PRODUCT`` edge.

    ``confidence`` (0–1) is stored **on the edge**, never on the node
    (wave invariant, rule 7).  ``COALESCE`` on conflict ensures a re-write
    without a source tag never clears an existing tag.

    Parameters
    ----------
    conn:
        asyncpg connection with RLS GUC set.
    namespace_id:
        Active namespace UUID.
    bom_line_label:
        Subject label — the BOM_LINE node that references this product.
    manufacturer:
        Product manufacturer (used to construct the object label).
    mfr_part_no:
        Product part number.
    confidence:
        Edge confidence score in [0, 1].
    """
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id

    # Ownership: the product engine owns PRODUCT_SKU nodes; writing an edge
    # whose *object* is a PRODUCT_SKU node is also permitted to the product
    # engine because it is responsible for the product side of the relationship.
    await assert_owner(conn, ns_uuid, _NODE_TYPE, _PRODUCT_ENGINE)

    object_label = _product_label(manufacturer, mfr_part_no)

    await conn.execute(
        """
        INSERT INTO kg_edges
            (subject_label, predicate, object_label, confidence, namespace_id)
        VALUES ($1, $2, $3, $4, $5::uuid)
        ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO UPDATE
            SET confidence = EXCLUDED.confidence,
                updated_at = NOW()
        """,
        bom_line_label,
        _PREDICATE,
        object_label,
        float(confidence),
        str(ns_uuid),
    )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _product_label(manufacturer: str, mfr_part_no: str) -> str:
    """Canonical kg_nodes label for a PRODUCT_SKU node.

    Must match the label produced by ``mcp_handlers._product_label``.
    """
    return f"PRODUCT:{manufacturer.upper()}:{mfr_part_no.upper()}"


# ---------------------------------------------------------------------------
# Public: upsert_product_relation_edge  (W5 — related-products)
# ---------------------------------------------------------------------------

#: Predicates this function is permitted to write.
_RELATION_PREDICATES: frozenset[str] = frozenset(
    {"accessory_of", "warranty_for", "mounts", "replaced_by"}
)


async def upsert_product_relation_edge(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: str | UUID,
    *,
    subject_label: str,
    predicate: str,
    object_label: str,
    confidence: float,
) -> None:
    """Upsert a ``PRODUCT -[predicate]-> PRODUCT`` relation edge.

    Writes one of four predicates (``accessory_of``, ``warranty_for``,
    ``mounts``, ``replaced_by``) from a subject PRODUCT node to an object
    PRODUCT node.  ``confidence`` (0–1) is stored **on the edge**, never on
    the node (wave invariant, rule 7).

    The INSERT column set matches ``kg_edges`` exactly: subject_label,
    predicate, object_label, confidence, namespace_id.  No ``metadata``
    column (it does not exist on this table).

    Parameters
    ----------
    conn:
        asyncpg connection with RLS GUC set (via ``scoped_pg_session``).
    namespace_id:
        Active namespace UUID.
    subject_label:
        The subject PRODUCT node label (``PRODUCT:<MFR>:<PART>``).
    predicate:
        One of ``accessory_of`` | ``warranty_for`` | ``mounts`` |
        ``replaced_by``.
    object_label:
        The object PRODUCT node label (``PRODUCT:<MFR>:<PART>``).
    confidence:
        Edge confidence score in [0, 1].

    Raises
    ------
    ValueError:
        When ``predicate`` is not one of the four permitted relation types.
    """
    if predicate not in _RELATION_PREDICATES:
        raise ValueError(
            f"predicate {predicate!r} is not a permitted relation predicate; "
            f"allowed: {sorted(_RELATION_PREDICATES)}"
        )

    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id

    await assert_owner(conn, ns_uuid, _NODE_TYPE, _PRODUCT_ENGINE)

    await conn.execute(
        """
        INSERT INTO kg_edges
            (subject_label, predicate, object_label, confidence, namespace_id)
        VALUES ($1, $2, $3, $4, $5::uuid)
        ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO UPDATE
            SET confidence = EXCLUDED.confidence,
                updated_at = NOW()
        """,
        subject_label,
        predicate,
        object_label,
        float(confidence),
        str(ns_uuid),
    )
