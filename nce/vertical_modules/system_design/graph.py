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
from typing import Any
from uuid import UUID, uuid4

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
_PRED_RESPONSIBLE_FOR: str = "responsible_for"
_PRED_HAS_CATEGORY: str = "has_category"

# Default edge confidence when the relationship is structural (certain).
_STRUCTURAL_CONFIDENCE: float = 1.0


# ---------------------------------------------------------------------------
# Label helpers
# ---------------------------------------------------------------------------


def fl_label(namespace_slug: str, *path_parts: str) -> str:
    """Deterministic FUNCTIONAL_LOCATION label.

    ``FL:<namespace_slug>:<part1>:<part2>:...`` — upper-cased so the same
    site path always maps to the same node regardless of input casing.
    """
    parts = ":".join(p.upper() for p in path_parts if p)
    return f"FL:{namespace_slug.upper()}:{parts}"


_fl_label = fl_label


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
    conn: asyncpg.Connection | None,  # type: ignore[type-arg]
    ns_uuid: UUID,
    label: str,
    source_id: str | None,
) -> None:
    """Upsert one FUNCTIONAL_LOCATION node, ownership-guarded."""
    if conn is not None and hasattr(conn, "execute"):
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
    else:
        from nce.vertical_modules.system_design.fl_tree import _MEM_NODES

        bucket = _MEM_NODES.setdefault(str(ns_uuid), {})
        existing = bucket.get(label, {})
        bucket[label] = {
            "id": existing.get("id") or str(uuid4()),
            "label": label,
            "entity_type": _NODE_TYPE_FL,
            "namespace_id": str(ns_uuid),
            "change_origin": "sync",
            "system_design_source_id": source_id or existing.get("system_design_source_id"),
            "created_at": existing.get("created_at", "2026-09-19T12:00:00Z"),
            "updated_at": "2026-09-19T12:00:00Z",
        }


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
    conn: asyncpg.Connection | None,  # type: ignore[type-arg]
    ns_uuid: UUID,
    subject: str,
    predicate: str,
    obj: str,
    confidence: float,
    source_id: str | None,
) -> None:
    """Upsert a single kg_edge.  confidence (0–1) on edges only (rule 7)."""
    if conn is not None and hasattr(conn, "execute"):
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
    else:
        from nce.vertical_modules.system_design.fl_tree import _MEM_EDGES

        edges = _MEM_EDGES.setdefault(str(ns_uuid), [])
        for e in edges:
            if (
                e.get("subject_label") == subject
                and e.get("predicate") == predicate
                and e.get("object_label") == obj
            ):
                e["confidence"] = float(confidence)
                e["system_design_source_id"] = source_id or e.get("system_design_source_id")
                e["updated_at"] = "2026-09-19T12:00:00Z"
                return
        edges.append(
            {
                "subject_label": subject,
                "predicate": predicate,
                "object_label": obj,
                "confidence": float(confidence),
                "namespace_id": str(ns_uuid),
                "change_origin": "sync",
                "system_design_source_id": source_id,
                "created_at": "2026-09-19T12:00:00Z",
                "updated_at": "2026-09-19T12:00:00Z",
            }
        )


# ---------------------------------------------------------------------------
# Public Graph Primitives: upsert_fl_path & upsert_fl_edge
# ---------------------------------------------------------------------------


async def upsert_fl_path(
    conn: asyncpg.Connection | None,  # type: ignore[type-arg]
    namespace_id: str | UUID,
    *,
    namespace_slug: str,
    path_parts: list[str] | tuple[str, ...],
    source_id: str | None = None,
) -> str:
    """Upsert one FUNCTIONAL_LOCATION node identified by a path hierarchy.

    Generates deterministic label ``FL:<namespace_slug>:<part1>:<part2>:...``
    and performs an ownership-guarded upsert into ``kg_nodes`` with
    ``entity_type='FUNCTIONAL_LOCATION'`` and ``change_origin='sync'``.

    Returns the authored node's canonical label.
    """
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id
    label = fl_label(namespace_slug, *path_parts)
    await _upsert_fl_node(conn, ns_uuid, label, source_id)
    return label


async def upsert_fl_edge(
    conn: asyncpg.Connection | None,  # type: ignore[type-arg]
    namespace_id: str | UUID,
    *,
    subject: str,
    predicate: str,
    obj: str,
    confidence: float = _STRUCTURAL_CONFIDENCE,
    source_id: str | None = None,
) -> None:
    """Upsert a single kg_edge for functional-location topology or metadata."""
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id
    await _upsert_edge(conn, ns_uuid, subject, predicate, obj, confidence, source_id)


# ---------------------------------------------------------------------------
# Room Category & Responsible Graph Edge Operations (Wave C-2)
# ---------------------------------------------------------------------------


async def set_fl_category_edge(
    conn: asyncpg.Connection | None,  # type: ignore[type-arg]
    namespace_id: str | UUID,
    *,
    fl_label: str,
    category_id: str,
) -> None:
    """Set the room category for a functional location via a has_category kg_edge.

    Replaces any previous has_category edge for this functional location.
    """
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id
    category_label = f"ROOM_CATEGORY:{category_id.upper()}"

    if conn is not None and hasattr(conn, "execute"):
        await conn.execute(
            """
            DELETE FROM kg_edges
            WHERE namespace_id = $1::uuid
              AND subject_label = $2
              AND predicate = $3
            """,
            str(ns_uuid),
            fl_label,
            _PRED_HAS_CATEGORY,
        )
    else:
        from nce.vertical_modules.system_design.fl_tree import _MEM_EDGES

        edges = _MEM_EDGES.setdefault(str(ns_uuid), [])
        _MEM_EDGES[str(ns_uuid)] = [
            e
            for e in edges
            if not (e.get("subject_label") == fl_label and e.get("predicate") == _PRED_HAS_CATEGORY)
        ]

    await _upsert_edge(
        conn,
        ns_uuid,
        subject=fl_label,
        predicate=_PRED_HAS_CATEGORY,
        obj=category_label,
        confidence=_STRUCTURAL_CONFIDENCE,
        source_id=None,
    )


async def get_fl_category_edge(
    conn: asyncpg.Connection | None,  # type: ignore[type-arg]
    namespace_id: str | UUID,
    *,
    fl_label: str,
) -> str | None:
    """Retrieve the room category ID assigned to a functional location, or None."""
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id

    if conn is not None and hasattr(conn, "fetchrow"):
        row = await conn.fetchrow(
            """
            SELECT object_label
            FROM kg_edges
            WHERE namespace_id = $1::uuid
              AND subject_label = $2
              AND predicate = $3
            LIMIT 1
            """,
            str(ns_uuid),
            fl_label,
            _PRED_HAS_CATEGORY,
        )
        if row is not None:
            obj_label = str(row["object_label"])
            return obj_label.removeprefix("ROOM_CATEGORY:")
        return None

    from nce.vertical_modules.system_design.fl_tree import _MEM_EDGES

    edges = _MEM_EDGES.get(str(ns_uuid), [])
    for e in edges:
        if e.get("subject_label") == fl_label and e.get("predicate") == _PRED_HAS_CATEGORY:
            obj_label = str(e.get("object_label", ""))
            return obj_label.removeprefix("ROOM_CATEGORY:")
    return None


async def assign_fl_responsible_edge(
    conn: asyncpg.Connection | None,  # type: ignore[type-arg]
    namespace_id: str | UUID,
    *,
    fl_label: str,
    employee_id: str,
    role: str = "primary",
) -> None:
    """Link an employee to a functional location via a responsible_for edge."""
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id
    subject_label = f"EMPLOYEE:{employee_id}"
    source_id = f"role:{role}"

    await _upsert_edge(
        conn,
        ns_uuid,
        subject=subject_label,
        predicate=_PRED_RESPONSIBLE_FOR,
        obj=fl_label,
        confidence=_STRUCTURAL_CONFIDENCE,
        source_id=source_id,
    )


async def unassign_fl_responsible_edge(
    conn: asyncpg.Connection | None,  # type: ignore[type-arg]
    namespace_id: str | UUID,
    *,
    fl_label: str,
    employee_id: str,
) -> bool:
    """Remove a responsible_for edge between employee and functional location.

    Returns True if an edge was found and removed, False otherwise.
    """
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id
    subject_label = f"EMPLOYEE:{employee_id}"

    if conn is not None and hasattr(conn, "execute"):
        status = await conn.execute(
            """
            DELETE FROM kg_edges
            WHERE namespace_id = $1::uuid
              AND subject_label = $2
              AND predicate = $3
              AND object_label = $4
            """,
            str(ns_uuid),
            subject_label,
            _PRED_RESPONSIBLE_FOR,
            fl_label,
        )
        # asyncpg returns status string e.g. "DELETE 1"
        return status != "DELETE 0"

    from nce.vertical_modules.system_design.fl_tree import _MEM_EDGES

    edges = _MEM_EDGES.get(str(ns_uuid), [])
    initial_len = len(edges)
    _MEM_EDGES[str(ns_uuid)] = [
        e
        for e in edges
        if not (
            e.get("subject_label") == subject_label
            and e.get("predicate") == _PRED_RESPONSIBLE_FOR
            and e.get("object_label") == fl_label
        )
    ]
    return len(_MEM_EDGES[str(ns_uuid)]) < initial_len


async def list_fl_responsible_edges(
    conn: asyncpg.Connection | None,  # type: ignore[type-arg]
    namespace_id: str | UUID,
    *,
    fl_label: str,
) -> list[dict[str, Any]]:
    """List all employees responsible for a given functional location."""
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id

    if conn is not None and hasattr(conn, "fetch"):
        rows = await conn.fetch(
            """
            SELECT subject_label, system_design_source_id, created_at, updated_at
            FROM kg_edges
            WHERE namespace_id = $1::uuid
              AND predicate = $2
              AND object_label = $3
            ORDER BY subject_label ASC
            """,
            str(ns_uuid),
            _PRED_RESPONSIBLE_FOR,
            fl_label,
        )
        results = []
        for r in rows:
            subj = str(r["subject_label"])
            emp_id = subj.removeprefix("EMPLOYEE:")
            source_id = str(r["system_design_source_id"] or "")
            role = source_id.removeprefix("role:") if source_id.startswith("role:") else "primary"
            results.append(
                {
                    "employee_id": emp_id,
                    "role": role,
                    "fl_label": fl_label,
                    "created_at": str(r["created_at"]),
                    "updated_at": str(r["updated_at"]),
                }
            )
        return results

    from nce.vertical_modules.system_design.fl_tree import _MEM_EDGES

    edges = _MEM_EDGES.get(str(ns_uuid), [])
    results = []
    for e in edges:
        if e.get("predicate") == _PRED_RESPONSIBLE_FOR and e.get("object_label") == fl_label:
            subj = str(e.get("subject_label", ""))
            emp_id = subj.removeprefix("EMPLOYEE:")
            source_id = str(e.get("system_design_source_id") or "")
            role = source_id.removeprefix("role:") if source_id.startswith("role:") else "primary"
            results.append(
                {
                    "employee_id": emp_id,
                    "role": role,
                    "fl_label": fl_label,
                    "created_at": str(e.get("created_at", "")),
                    "updated_at": str(e.get("updated_at", "")),
                }
            )
    results.sort(key=lambda x: x["employee_id"])
    return results


async def list_responsible_fl_edges_for_employee(
    conn: asyncpg.Connection | None,  # type: ignore[type-arg]
    namespace_id: str | UUID,
    *,
    employee_id: str,
) -> list[dict[str, Any]]:
    """List all functional locations assigned to a given employee."""
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id
    subject_label = f"EMPLOYEE:{employee_id}"

    if conn is not None and hasattr(conn, "fetch"):
        rows = await conn.fetch(
            """
            SELECT object_label, system_design_source_id, created_at, updated_at
            FROM kg_edges
            WHERE namespace_id = $1::uuid
              AND predicate = $2
              AND subject_label = $3
            ORDER BY object_label ASC
            """,
            str(ns_uuid),
            _PRED_RESPONSIBLE_FOR,
            subject_label,
        )
        results = []
        for r in rows:
            obj_lbl = str(r["object_label"])
            source_id = str(r["system_design_source_id"] or "")
            role = source_id.removeprefix("role:") if source_id.startswith("role:") else "primary"
            results.append(
                {
                    "fl_label": obj_lbl,
                    "role": role,
                    "employee_id": employee_id,
                    "created_at": str(r["created_at"]),
                    "updated_at": str(r["updated_at"]),
                }
            )
        return results

    from nce.vertical_modules.system_design.fl_tree import _MEM_EDGES

    edges = _MEM_EDGES.get(str(ns_uuid), [])
    results = []
    for e in edges:
        if e.get("predicate") == _PRED_RESPONSIBLE_FOR and e.get("subject_label") == subject_label:
            obj_lbl = str(e.get("object_label", ""))
            source_id = str(e.get("system_design_source_id") or "")
            role = source_id.removeprefix("role:") if source_id.startswith("role:") else "primary"
            results.append(
                {
                    "fl_label": obj_lbl,
                    "role": role,
                    "employee_id": employee_id,
                    "created_at": str(e.get("created_at", "")),
                    "updated_at": str(e.get("updated_at", "")),
                }
            )
    results.sort(key=lambda x: x["fl_label"])
    return results


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
    site_lbl = await upsert_fl_path(
        conn, ns_uuid, namespace_slug=namespace_slug, path_parts=[site_name], source_id=source_id
    )
    node_count += 1

    # DESIGN -[contains]-> SITE
    await upsert_fl_edge(
        conn,
        ns_uuid,
        subject=design_lbl,
        predicate=_PRED_CONTAINS,
        obj=site_lbl,
        confidence=_STRUCTURAL_CONFIDENCE,
        source_id=source_id,
    )
    edge_count += 1

    # ------------------------------------------------------------------
    # 3. BUILDING > FLOOR > ROOM > POSITION tree
    # ------------------------------------------------------------------
    for building in buildings:
        bld_name: str = building["name"]
        bld_lbl = await upsert_fl_path(
            conn,
            ns_uuid,
            namespace_slug=namespace_slug,
            path_parts=[site_name, bld_name],
            source_id=source_id,
        )
        node_count += 1

        # SITE -[parent_of]-> BUILDING
        await upsert_fl_edge(
            conn,
            ns_uuid,
            subject=site_lbl,
            predicate=_PRED_PARENT_OF,
            obj=bld_lbl,
            confidence=_STRUCTURAL_CONFIDENCE,
            source_id=source_id,
        )
        edge_count += 1

        for floor in building.get("floors", []):
            flr_name: str = floor["name"]
            flr_lbl = await upsert_fl_path(
                conn,
                ns_uuid,
                namespace_slug=namespace_slug,
                path_parts=[site_name, bld_name, flr_name],
                source_id=source_id,
            )
            node_count += 1

            # BUILDING -[parent_of]-> FLOOR
            await upsert_fl_edge(
                conn,
                ns_uuid,
                subject=bld_lbl,
                predicate=_PRED_PARENT_OF,
                obj=flr_lbl,
                confidence=_STRUCTURAL_CONFIDENCE,
                source_id=source_id,
            )
            edge_count += 1

            for room in floor.get("rooms", []):
                room_name: str = room["name"]
                room_lbl = await upsert_fl_path(
                    conn,
                    ns_uuid,
                    namespace_slug=namespace_slug,
                    path_parts=[site_name, bld_name, flr_name, room_name],
                    source_id=source_id,
                )
                node_count += 1

                # FLOOR -[parent_of]-> ROOM
                await upsert_fl_edge(
                    conn,
                    ns_uuid,
                    subject=flr_lbl,
                    predicate=_PRED_PARENT_OF,
                    obj=room_lbl,
                    confidence=_STRUCTURAL_CONFIDENCE,
                    source_id=source_id,
                )
                edge_count += 1

                for pos_name in room.get("positions", []):
                    pos_lbl = await upsert_fl_path(
                        conn,
                        ns_uuid,
                        namespace_slug=namespace_slug,
                        path_parts=[site_name, bld_name, flr_name, room_name, pos_name],
                        source_id=source_id,
                    )
                    node_count += 1

                    # ROOM -[parent_of]-> POSITION
                    await upsert_fl_edge(
                        conn,
                        ns_uuid,
                        subject=room_lbl,
                        predicate=_PRED_PARENT_OF,
                        obj=pos_lbl,
                        confidence=_STRUCTURAL_CONFIDENCE,
                        source_id=source_id,
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
