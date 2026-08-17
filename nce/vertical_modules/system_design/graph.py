"""
nce/vertical_modules/system_design/graph.py
============================================
Cognitive-graph upserts for the System Design vertical module (Wave 2:
functional-location-nodes).

Responsibilities:
  - Author the customer-site FUNCTIONAL_LOCATION tree
    (SITE > BUILDING > FLOOR > ROOM > POSITION) as kg_nodes with
    entity_type='FUNCTIONAL_LOCATION'.
  - Upsert DESIGN and DESIGN_LINE nodes into kg_nodes.
  - Write the structural edges:
      DESIGN           -[contains]->   FUNCTIONAL_LOCATION (root)
      FUNCTIONAL_LOCATION -[parent_of]-> child
      FUNCTIONAL_LOCATION -[needs]->    DESIGN_LINE
      DESIGN_LINE      -[references]-> PRODUCT (cross-engine, by label)
  - confidence on EDGES only (wave rule 7).

Design-intent encoding:
  entity_type='FUNCTIONAL_LOCATION' is itself the design-intent marker —
  these nodes represent authored customer-site intent until Wave 9 (NetBox
  promotion to as-built).  No phantom payload/metadata/state column is used;
  kg_nodes has no such column.

Ownership (Contract A §9.1):
  FUNCTIONAL_LOCATION, DESIGN, and DESIGN_LINE are owned by system_design;
  every own-node write is guarded by
  ``nce.entity_resolution.ownership.assert_owner``.
  PRODUCT nodes are owned by the Product engine — this module references
  them by label in edges only (kg_edges has no FK to kg_nodes).

Source-id:
  All derived writes tag ``change_origin='sync'`` + ``system_design_source_id``
  (§2.3 per-vertical source-id pattern; migration 037 adds the column).

Design invariants (uncle-bob-craft):
  - No web / HTTP / admin imports; domain core only.
  - One function, one job; no shared state.
  - ``confidence`` only on edges — never on nodes (wave rule 7).
  - No ``metadata``, ``payload``, or ``state`` column on kg_nodes — does not
    exist in this schema.
"""

from __future__ import annotations

import logging
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]

from nce.entity_resolution.ownership import assert_owner
from nce.events.emit import emit_graph_write

log = logging.getLogger("nce.vertical_modules.system_design.graph")

# ---------------------------------------------------------------------------
# Engine identifier and node types — must match node-ownership.json entries
# ---------------------------------------------------------------------------
_SYSTEM_DESIGN_ENGINE: str = "system_design"
_NODE_TYPE_FL: str = "FUNCTIONAL_LOCATION"
_NODE_TYPE_DESIGN: str = "DESIGN"
_NODE_TYPE_DESIGN_LINE: str = "DESIGN_LINE"

# Edge predicates written by this module.
_PRED_CONTAINS: str = "contains"
_PRED_PARENT_OF: str = "parent_of"
_PRED_NEEDS: str = "needs"
_PRED_REFERENCES: str = "references"

# Default edge confidence when the relationship is structural (certain).
_STRUCTURAL_CONFIDENCE: float = 1.0


# ---------------------------------------------------------------------------
# Label helpers
# ---------------------------------------------------------------------------


def _fl_label(namespace_slug: str, *path_parts: str) -> str:
    """Deterministic FUNCTIONAL_LOCATION label.

    ``FL:<namespace_slug>:<part1>:<part2>:...`` — upper-cased so the same
    site path always maps to the same node regardless of input casing.
    """
    parts = ":".join(p.upper() for p in path_parts)
    return f"FL:{namespace_slug.upper()}:{parts}"


def _design_label(design_id: str) -> str:
    """Canonical DESIGN label: ``DESIGN:<DESIGN_ID>`` (upper-cased)."""
    return f"DESIGN:{design_id.upper()}"


def _design_line_label(design_id: str, line_ref: str) -> str:
    """Canonical DESIGN_LINE label: ``DESIGN_LINE:<DESIGN_ID>:<LINE_REF>``."""
    return f"DESIGN_LINE:{design_id.upper()}:{line_ref.upper()}"


def _product_label(manufacturer: str, mfr_part_no: str) -> str:
    """Canonical PRODUCT label for cross-engine edge reference.

    Must match the label convention used by the Product engine.
    """
    return f"PRODUCT:{manufacturer.upper()}:{mfr_part_no.upper()}"


# ---------------------------------------------------------------------------
# Private: single-node upsert (assert_owner-guarded, outbox-emitting)
# ---------------------------------------------------------------------------


async def _upsert_fl_node(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    ns_uuid: UUID,
    label: str,
    source_id: str | None,
) -> None:
    """Upsert one FUNCTIONAL_LOCATION node, ownership-guarded."""
    await assert_owner(conn, ns_uuid, _NODE_TYPE_FL, _SYSTEM_DESIGN_ENGINE)
    await conn.execute(
        """
        INSERT INTO kg_nodes
            (label, entity_type, namespace_id, change_origin, system_design_source_id)
        VALUES ($1, $2, $3::uuid, 'sync', $4)
        ON CONFLICT (label, namespace_id) DO UPDATE
            SET entity_type               = EXCLUDED.entity_type,
                change_origin             = 'sync',
                system_design_source_id   = COALESCE(
                    EXCLUDED.system_design_source_id,
                    kg_nodes.system_design_source_id
                ),
                updated_at                = NOW()
        """,
        label,
        _NODE_TYPE_FL,
        str(ns_uuid),
        source_id,
    )
    await emit_graph_write(
        conn,
        namespace_id=ns_uuid,
        node_type=_NODE_TYPE_FL,
        op="upserted",
        node_id=label,
    )


async def _upsert_design_node(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    ns_uuid: UUID,
    label: str,
    source_id: str | None,
) -> None:
    """Upsert one DESIGN node, ownership-guarded."""
    await assert_owner(conn, ns_uuid, _NODE_TYPE_DESIGN, _SYSTEM_DESIGN_ENGINE)
    await conn.execute(
        """
        INSERT INTO kg_nodes
            (label, entity_type, namespace_id, change_origin, system_design_source_id)
        VALUES ($1, $2, $3::uuid, 'sync', $4)
        ON CONFLICT (label, namespace_id) DO UPDATE
            SET entity_type               = EXCLUDED.entity_type,
                change_origin             = 'sync',
                system_design_source_id   = COALESCE(
                    EXCLUDED.system_design_source_id,
                    kg_nodes.system_design_source_id
                ),
                updated_at                = NOW()
        """,
        label,
        _NODE_TYPE_DESIGN,
        str(ns_uuid),
        source_id,
    )
    await emit_graph_write(
        conn,
        namespace_id=ns_uuid,
        node_type=_NODE_TYPE_DESIGN,
        op="upserted",
        node_id=label,
    )


async def _upsert_design_line_node(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    ns_uuid: UUID,
    label: str,
    source_id: str | None,
) -> None:
    """Upsert one DESIGN_LINE node, ownership-guarded."""
    await assert_owner(conn, ns_uuid, _NODE_TYPE_DESIGN_LINE, _SYSTEM_DESIGN_ENGINE)
    await conn.execute(
        """
        INSERT INTO kg_nodes
            (label, entity_type, namespace_id, change_origin, system_design_source_id)
        VALUES ($1, $2, $3::uuid, 'sync', $4)
        ON CONFLICT (label, namespace_id) DO UPDATE
            SET entity_type               = EXCLUDED.entity_type,
                change_origin             = 'sync',
                system_design_source_id   = COALESCE(
                    EXCLUDED.system_design_source_id,
                    kg_nodes.system_design_source_id
                ),
                updated_at                = NOW()
        """,
        label,
        _NODE_TYPE_DESIGN_LINE,
        str(ns_uuid),
        source_id,
    )
    await emit_graph_write(
        conn,
        namespace_id=ns_uuid,
        node_type=_NODE_TYPE_DESIGN_LINE,
        op="upserted",
        node_id=label,
    )


# ---------------------------------------------------------------------------
# Private: edge upsert (no ownership guard — edges are always cross-engine safe)
# ---------------------------------------------------------------------------


async def _upsert_edge(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    ns_uuid: UUID,
    subject: str,
    predicate: str,
    obj: str,
    confidence: float,
    source_id: str | None,
) -> None:
    """Upsert a single kg_edge.  confidence (0–1) on edges only (rule 7)."""
    await conn.execute(
        """
        INSERT INTO kg_edges
            (subject_label, predicate, object_label, confidence,
             namespace_id, change_origin, system_design_source_id)
        VALUES ($1, $2, $3, $4, $5::uuid, 'sync', $6)
        ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO UPDATE
            SET confidence                = EXCLUDED.confidence,
                change_origin             = 'sync',
                system_design_source_id   = COALESCE(
                    EXCLUDED.system_design_source_id,
                    kg_edges.system_design_source_id
                ),
                updated_at                = NOW()
        """,
        subject,
        predicate,
        obj,
        float(confidence),
        str(ns_uuid),
        source_id,
    )


# ---------------------------------------------------------------------------
# Public: do_author_functional_location
# ---------------------------------------------------------------------------


async def do_author_functional_location(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: str | UUID,
    *,
    namespace_slug: str,
    design_id: str,
    site_name: str,
    buildings: list[dict],  # type: ignore[type-arg]
    design_lines: list[dict] | None = None,  # type: ignore[type-arg]
    source_id: str | None = None,
) -> dict:  # type: ignore[type-arg]
    """Author the design-intent functional-location tree and DESIGN/DESIGN_LINE nodes.

    Writes the customer-site ``SITE > BUILDING > FLOOR > ROOM > POSITION`` tree
    as ``kg_nodes`` with ``entity_type='FUNCTIONAL_LOCATION'``.  The entity_type
    itself is the design-intent marker — no phantom payload/state column used.

    Also upserts:
      - One ``DESIGN`` node keyed by *design_id*.
      - Zero or more ``DESIGN_LINE`` nodes from *design_lines*.

    Edges written (confidence on edges only — rule 7):
      DESIGN           -[contains]->   SITE (root FUNCTIONAL_LOCATION)
      SITE             -[parent_of]->  BUILDING
      BUILDING         -[parent_of]->  FLOOR
      FLOOR            -[parent_of]->  ROOM
      ROOM             -[parent_of]->  POSITION
      DESIGN           -[contains]->   DESIGN_LINE
      FUNCTIONAL_LOCATION -[needs]->  DESIGN_LINE  (site-level association)
      DESIGN_LINE      -[references]-> PRODUCT     (cross-engine, by label)

    Parameters
    ----------
    conn:
        asyncpg connection with RLS namespace GUC already set.
    namespace_id:
        Active namespace UUID.
    namespace_slug:
        Human-readable namespace slug — used as a deterministic prefix in
        FUNCTIONAL_LOCATION labels so sites from different namespaces never
        collide even if they share a name.
    design_id:
        Unique identifier for this design project.
    site_name:
        Top-level site name (root of the hierarchy).
    buildings:
        List of building dicts::

            {
                "name": str,
                "floors": [
                    {
                        "name": str,
                        "rooms": [
                            {
                                "name": str,
                                "positions": [str, ...],
                            }
                        ],
                    }
                ],
            }

    design_lines:
        Optional list of DESIGN_LINE dicts::

            {
                "line_ref": str,
                "manufacturer": str,
                "mfr_part_no": str,
                "confidence": float,   # 0–1, default 1.0
                "source_id": str | None,
            }

    source_id:
        Optional system_design source record ID for retirement tracking.
        Applied to all nodes and edges authored in this call.

    Returns
    -------
    dict
        ``{"authored": {"nodes": int, "edges": int}}``
    """
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id
    dl_list: list[dict] = design_lines or []  # type: ignore[type-arg]

    node_count = 0
    edge_count = 0

    # ------------------------------------------------------------------
    # 1. DESIGN node
    # ------------------------------------------------------------------
    design_lbl = _design_label(design_id)
    await _upsert_design_node(conn, ns_uuid, design_lbl, source_id)
    node_count += 1

    # ------------------------------------------------------------------
    # 2. SITE node (root of the functional-location tree)
    # ------------------------------------------------------------------
    site_lbl = _fl_label(namespace_slug, site_name)
    await _upsert_fl_node(conn, ns_uuid, site_lbl, source_id)
    node_count += 1

    # DESIGN -[contains]-> SITE
    await _upsert_edge(
        conn, ns_uuid, design_lbl, _PRED_CONTAINS, site_lbl, _STRUCTURAL_CONFIDENCE, source_id
    )
    edge_count += 1

    # ------------------------------------------------------------------
    # 3. BUILDING > FLOOR > ROOM > POSITION tree
    # ------------------------------------------------------------------
    for building in buildings:
        bld_name: str = building["name"]
        bld_lbl = _fl_label(namespace_slug, site_name, bld_name)
        await _upsert_fl_node(conn, ns_uuid, bld_lbl, source_id)
        node_count += 1

        # SITE -[parent_of]-> BUILDING
        await _upsert_edge(
            conn, ns_uuid, site_lbl, _PRED_PARENT_OF, bld_lbl, _STRUCTURAL_CONFIDENCE, source_id
        )
        edge_count += 1

        for floor in building.get("floors", []):
            flr_name: str = floor["name"]
            flr_lbl = _fl_label(namespace_slug, site_name, bld_name, flr_name)
            await _upsert_fl_node(conn, ns_uuid, flr_lbl, source_id)
            node_count += 1

            # BUILDING -[parent_of]-> FLOOR
            await _upsert_edge(
                conn,
                ns_uuid,
                bld_lbl,
                _PRED_PARENT_OF,
                flr_lbl,
                _STRUCTURAL_CONFIDENCE,
                source_id,
            )
            edge_count += 1

            for room in floor.get("rooms", []):
                room_name: str = room["name"]
                room_lbl = _fl_label(namespace_slug, site_name, bld_name, flr_name, room_name)
                await _upsert_fl_node(conn, ns_uuid, room_lbl, source_id)
                node_count += 1

                # FLOOR -[parent_of]-> ROOM
                await _upsert_edge(
                    conn,
                    ns_uuid,
                    flr_lbl,
                    _PRED_PARENT_OF,
                    room_lbl,
                    _STRUCTURAL_CONFIDENCE,
                    source_id,
                )
                edge_count += 1

                for pos_name in room.get("positions", []):
                    pos_lbl = _fl_label(
                        namespace_slug, site_name, bld_name, flr_name, room_name, pos_name
                    )
                    await _upsert_fl_node(conn, ns_uuid, pos_lbl, source_id)
                    node_count += 1

                    # ROOM -[parent_of]-> POSITION
                    await _upsert_edge(
                        conn,
                        ns_uuid,
                        room_lbl,
                        _PRED_PARENT_OF,
                        pos_lbl,
                        _STRUCTURAL_CONFIDENCE,
                        source_id,
                    )
                    edge_count += 1

    # ------------------------------------------------------------------
    # 4. DESIGN_LINE nodes + edges
    # ------------------------------------------------------------------
    for dl in dl_list:
        line_ref: str = dl["line_ref"]
        manufacturer: str = dl["manufacturer"]
        mfr_part_no: str = dl["mfr_part_no"]
        dl_conf: float = float(dl.get("confidence", _STRUCTURAL_CONFIDENCE))
        dl_source: str | None = dl.get("source_id") or source_id

        dl_lbl = _design_line_label(design_id, line_ref)
        await _upsert_design_line_node(conn, ns_uuid, dl_lbl, dl_source)
        node_count += 1

        # DESIGN -[contains]-> DESIGN_LINE
        await _upsert_edge(
            conn, ns_uuid, design_lbl, _PRED_CONTAINS, dl_lbl, _STRUCTURAL_CONFIDENCE, dl_source
        )
        edge_count += 1

        # SITE -[needs]-> DESIGN_LINE  (site-level association)
        await _upsert_edge(
            conn, ns_uuid, site_lbl, _PRED_NEEDS, dl_lbl, _STRUCTURAL_CONFIDENCE, dl_source
        )
        edge_count += 1

        # DESIGN_LINE -[references]-> PRODUCT  (cross-engine, by label)
        product_lbl = _product_label(manufacturer, mfr_part_no)
        await _upsert_edge(conn, ns_uuid, dl_lbl, _PRED_REFERENCES, product_lbl, dl_conf, dl_source)
        edge_count += 1

    log.info(
        "do_author_functional_location: ns=%s design=%s authored nodes=%d edges=%d",
        ns_uuid,
        design_id,
        node_count,
        edge_count,
    )
    return {"authored": {"nodes": node_count, "edges": edge_count}}
