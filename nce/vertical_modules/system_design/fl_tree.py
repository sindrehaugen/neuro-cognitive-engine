"""nce.vertical_modules.system_design.fl_tree — Functional Location tree service.

Phase C Wave C-1:
Unified graph traversal, tree navigation, re-parenting, deduplication merge queue,
and lifecycle operations over FUNCTIONAL_LOCATION nodes in the cognitive graph.

Architecture & Governance (Charter §13 & §14 K-C1):
- Backed strictly by kg_nodes and kg_edges (no synthetic attribute tables invented).
- Hierarchy structured via _PRED_PARENT_OF ('parent_of') edges.
- Kind derivation (site, building, floor, room, desk, vessel) inferred from depth and naming.
- Cycle detection on move/re-parenting operations.
- Reversible and child-auditable merge operations.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID, uuid4

from nce.vertical_modules.system_design.fold_rules import (
    DEFAULT_FOLD_RULES,
    FoldRules,
    evaluate_fl_match,
)

log = logging.getLogger("nce.vertical_modules.system_design.fl_tree")

_NODE_TYPE_FL = "FUNCTIONAL_LOCATION"
_PRED_PARENT_OF = "parent_of"
_PRED_CONTAINS = "contains"
_PRED_NEEDS = "needs"
_STRUCTURAL_CONFIDENCE = 1.0


class FLTreeError(ValueError):
    """Base exception for Functional Location tree operations."""


class FLNodeNotFoundError(FLTreeError, KeyError):
    """Raised when a specified functional location node does not exist."""


class CycleDetectedError(FLTreeError, ValueError):
    """Raised when moving a functional location would create a circular reference."""


class InvalidMoveError(FLTreeError, ValueError):
    """Raised when a move operation is invalid (e.g. self-parenting)."""


class MergeConflictError(FLTreeError, ValueError):
    """Raised when two nodes cannot be merged."""


# In-memory mock store used when pg_pool is absent (e.g. unit testing)
_MEM_NODES: dict[str, dict[str, dict[str, Any]]] = {}
_MEM_EDGES: dict[str, list[dict[str, Any]]] = {}
_MEM_MERGE_AUDIT: dict[str, list[dict[str, Any]]] = {}


def _clear_mem_store() -> None:
    """Clear in-memory mock store (for test cleanup)."""
    _MEM_NODES.clear()
    _MEM_EDGES.clear()
    _MEM_MERGE_AUDIT.clear()


def derive_fl_kind(label: str, depth: int | None = None) -> str:
    """Derive functional location kind from label and depth in hierarchy.

    Kinds: site | building | floor | room | desk | vessel
    """
    upper = label.upper()
    if any(k in upper for k in (":VESSEL:", ":SKIP:", ":BOAT:", "VESSEL", "MS ", "SS ")):
        return "vessel"
    if any(k in upper for k in (":DESK:", ":POS:", ":POSITION:", "DESK", "POS-")):
        return "desk"

    parts = [p for p in label.split(":") if p and p != "FL"]
    # If namespace slug is present: FL:<NS>:<PARTS...>
    # Effective depth is number of parts minus namespace slug if >= 2
    part_count = len(parts) - 1 if len(parts) > 1 else len(parts)
    effective_depth = depth if depth is not None else part_count

    if effective_depth <= 1:
        return "site"
    if effective_depth == 2:
        return "building"
    if effective_depth == 3:
        return "floor"
    if effective_depth == 4:
        return "room"
    return "desk"


def _format_node_dict(row: dict[str, Any], depth: int | None = None) -> dict[str, Any]:
    """Format a node row into standard FL representation."""
    label = str(row.get("label", ""))
    kind = row.get("kind") or derive_fl_kind(label, depth)
    change_origin = str(row.get("change_origin", "sync"))
    as_built = change_origin in ("operator", "field_tech", "as_built")

    parts = [p for p in label.split(":") if p and p != "FL"]
    name = parts[-1] if parts else label

    return {
        "id": str(row.get("id", "")),
        "label": label,
        "name": name,
        "kind": kind,
        "entity_type": _NODE_TYPE_FL,
        "namespace_id": str(row.get("namespace_id", "")),
        "as_built": as_built,
        "change_origin": change_origin,
        "source_id": row.get("system_design_source_id"),
        "created_at": str(row.get("created_at", "")),
        "updated_at": str(row.get("updated_at", "")),
    }


# ---------------------------------------------------------------------------
# Node Retrieval & Search
# ---------------------------------------------------------------------------


async def get_fl_node(
    conn: Any | None,
    namespace_id: str | UUID,
    node_id_or_label: str,
) -> dict[str, Any]:
    """Fetch a single FUNCTIONAL_LOCATION node by UUID or label."""
    ns_str = str(namespace_id)

    # 1. Live database path
    if conn is not None and hasattr(conn, "fetchrow"):
        try:
            row = await conn.fetchrow(
                """
                SELECT id, label, entity_type, namespace_id, change_origin,
                       system_design_source_id, created_at, updated_at
                FROM kg_nodes
                WHERE entity_type = $1
                  AND namespace_id = $2::uuid
                  AND (label = $3 OR id::text = $3)
                LIMIT 1
                """,
                _NODE_TYPE_FL,
                ns_str,
                node_id_or_label,
            )
            if row is not None:
                return _format_node_dict(dict(row))
        except Exception as exc:
            log.warning("get_fl_node query failed on connection: %s", exc)

    # 2. In-memory fallback
    bucket = _MEM_NODES.get(ns_str, {})
    for node in bucket.values():
        if node.get("label") == node_id_or_label or str(node.get("id")) == node_id_or_label:
            return _format_node_dict(node)

    raise FLNodeNotFoundError(
        f"Functional location node {node_id_or_label!r} not found in namespace {ns_str}"
    )


async def search_fl_nodes(
    conn: Any | None,
    namespace_id: str | UUID,
    q: str | None = None,
    kind: str | None = None,
    as_built: bool | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Search or list FUNCTIONAL_LOCATION nodes with optional filters."""
    ns_str = str(namespace_id)
    items: list[dict[str, Any]] = []

    if conn is not None and hasattr(conn, "fetch"):
        try:
            rows = await conn.fetch(
                """
                SELECT id, label, entity_type, namespace_id, change_origin,
                       system_design_source_id, created_at, updated_at
                FROM kg_nodes
                WHERE entity_type = $1
                  AND namespace_id = $2::uuid
                  AND ($3::text IS NULL OR label ILIKE '%' || $3 || '%')
                ORDER BY label ASC
                LIMIT $4
                """,
                _NODE_TYPE_FL,
                ns_str,
                q,
                limit * 2 if (kind or as_built is not None) else limit,
            )
            for r in rows:
                formatted = _format_node_dict(dict(r))
                if kind and formatted["kind"] != kind.lower():
                    continue
                if as_built is not None and formatted["as_built"] != as_built:
                    continue
                items.append(formatted)
                if len(items) >= limit:
                    break
            return items
        except Exception as exc:
            log.warning("search_fl_nodes live query failed: %s", exc)

    # In-memory fallback
    bucket = _MEM_NODES.get(ns_str, {})
    q_lower = q.lower() if q else None
    for node in bucket.values():
        formatted = _format_node_dict(node)
        if (
            q_lower
            and q_lower not in formatted["label"].lower()
            and q_lower not in formatted["name"].lower()
        ):
            continue
        if kind and formatted["kind"] != kind.lower():
            continue
        if as_built is not None and formatted["as_built"] != as_built:
            continue
        items.append(formatted)
        if len(items) >= limit:
            break

    return items


# ---------------------------------------------------------------------------
# Hierarchy Queries (Children, Ancestors, Path)
# ---------------------------------------------------------------------------


async def get_fl_children(
    conn: Any | None,
    namespace_id: str | UUID,
    node_id_or_label: str,
    recursive: bool = False,
) -> list[dict[str, Any]]:
    """Retrieve immediate or recursive children of a functional location."""
    parent = await get_fl_node(conn, namespace_id, node_id_or_label)
    parent_label = parent["label"]
    ns_str = str(namespace_id)

    if conn is not None and hasattr(conn, "fetch"):
        try:
            if not recursive:
                rows = await conn.fetch(
                    """
                    SELECT n.id, n.label, n.entity_type, n.namespace_id, n.change_origin,
                           n.system_design_source_id, n.created_at, n.updated_at
                    FROM kg_edges e
                    JOIN kg_nodes n ON n.label = e.object_label AND n.namespace_id = e.namespace_id
                    WHERE e.subject_label = $1
                      AND e.predicate = $2
                      AND e.namespace_id = $3::uuid
                    ORDER BY n.label ASC
                    """,
                    parent_label,
                    _PRED_PARENT_OF,
                    ns_str,
                )
                return [_format_node_dict(dict(r)) for r in rows]
            else:
                rows = await conn.fetch(
                    """
                    WITH RECURSIVE descendants AS (
                        SELECT e.object_label, 1 as depth
                        FROM kg_edges e
                        WHERE e.subject_label = $1
                          AND e.predicate = $2
                          AND e.namespace_id = $3::uuid
                        UNION ALL
                        SELECT e.object_label, d.depth + 1
                        FROM kg_edges e
                        JOIN descendants d ON e.subject_label = d.object_label
                        WHERE e.predicate = $2
                          AND e.namespace_id = $3::uuid
                          AND d.depth < 20
                    )
                    SELECT DISTINCT n.id, n.label, n.entity_type, n.namespace_id, n.change_origin,
                           n.system_design_source_id, n.created_at, n.updated_at, d.depth
                    FROM descendants d
                    JOIN kg_nodes n ON n.label = d.object_label AND n.namespace_id = $3::uuid
                    ORDER BY d.depth ASC, n.label ASC
                    """,
                    parent_label,
                    _PRED_PARENT_OF,
                    ns_str,
                )
                return [_format_node_dict(dict(r), depth=r.get("depth")) for r in rows]
        except Exception as exc:
            log.warning("get_fl_children live query failed: %s", exc)

    # In-memory fallback
    edges = _MEM_EDGES.get(ns_str, [])
    children_labels: list[str] = []
    to_visit = [parent_label]
    visited = set(to_visit)

    while to_visit:
        curr = to_visit.pop(0)
        curr_children = [
            e["object_label"]
            for e in edges
            if e["subject_label"] == curr and e["predicate"] == _PRED_PARENT_OF
        ]
        for child_lbl in curr_children:
            if child_lbl not in visited:
                visited.add(child_lbl)
                children_labels.append(child_lbl)
                if recursive:
                    to_visit.append(child_lbl)

    bucket = _MEM_NODES.get(ns_str, {})
    return [_format_node_dict(bucket[lbl]) for lbl in children_labels if lbl in bucket]


async def get_fl_ancestors(
    conn: Any | None,
    namespace_id: str | UUID,
    node_id_or_label: str,
) -> list[dict[str, Any]]:
    """Retrieve ancestor chain from immediate parent up to the root."""
    target = await get_fl_node(conn, namespace_id, node_id_or_label)
    target_label = target["label"]
    ns_str = str(namespace_id)

    if conn is not None and hasattr(conn, "fetch"):
        try:
            rows = await conn.fetch(
                """
                WITH RECURSIVE ancestors AS (
                    SELECT e.subject_label, 1 as depth
                    FROM kg_edges e
                    WHERE e.object_label = $1
                      AND e.predicate = $2
                      AND e.namespace_id = $3::uuid
                    UNION ALL
                    SELECT e.subject_label, a.depth + 1
                    FROM kg_edges e
                    JOIN ancestors a ON e.object_label = a.subject_label
                    WHERE e.predicate = $2
                      AND e.namespace_id = $3::uuid
                      AND a.depth < 20
                )
                SELECT n.id, n.label, n.entity_type, n.namespace_id, n.change_origin,
                       n.system_design_source_id, n.created_at, n.updated_at, a.depth
                FROM ancestors a
                JOIN kg_nodes n ON n.label = a.subject_label AND n.namespace_id = $3::uuid
                ORDER BY a.depth ASC
                """,
                target_label,
                _PRED_PARENT_OF,
                ns_str,
            )
            return [_format_node_dict(dict(r)) for r in rows]
        except Exception as exc:
            log.warning("get_fl_ancestors live query failed: %s", exc)

    # In-memory fallback
    edges = _MEM_EDGES.get(ns_str, [])
    ancestors: list[dict[str, Any]] = []
    curr = target_label
    bucket = _MEM_NODES.get(ns_str, {})

    while True:
        parent_lbl = next(
            (
                e["subject_label"]
                for e in edges
                if e["object_label"] == curr and e["predicate"] == _PRED_PARENT_OF
            ),
            None,
        )
        if not parent_lbl or parent_lbl == curr:
            break
        if parent_lbl in bucket:
            ancestors.append(_format_node_dict(bucket[parent_lbl]))
        curr = parent_lbl

    return ancestors


async def get_fl_path(
    conn: Any | None,
    namespace_id: str | UUID,
    node_id_or_label: str,
) -> dict[str, Any]:
    """Return ordered hierarchy path [root, ..., node] and path string."""
    target = await get_fl_node(conn, namespace_id, node_id_or_label)
    ancestors = await get_fl_ancestors(conn, namespace_id, node_id_or_label)

    # Ancestors are immediate parent first; reverse to get root-to-parent
    path_nodes = list(reversed(ancestors)) + [target]
    path_str = " > ".join(n["name"] for n in path_nodes)

    return {
        "node": target,
        "path_nodes": path_nodes,
        "path_string": path_str,
        "depth": len(path_nodes),
    }


# ---------------------------------------------------------------------------
# Mutations: Move, Merge, Promote
# ---------------------------------------------------------------------------


async def move_fl_node(
    conn: Any | None,
    namespace_id: str | UUID,
    node_id_or_label: str,
    new_parent_id_or_label: str,
    actor: str | None = None,
) -> dict[str, Any]:
    """Move a functional location node under a new parent with cycle detection."""
    target = await get_fl_node(conn, namespace_id, node_id_or_label)
    new_parent = await get_fl_node(conn, namespace_id, new_parent_id_or_label)

    target_label = target["label"]
    new_parent_label = new_parent["label"]
    ns_str = str(namespace_id)

    if target_label == new_parent_label:
        raise InvalidMoveError(f"Cannot move node {target_label!r} under itself")

    # Cycle Detection: verify new_parent is not a descendant of target
    descendants = await get_fl_children(conn, namespace_id, target_label, recursive=True)
    descendant_labels = {d["label"] for d in descendants}
    if new_parent_label in descendant_labels:
        raise CycleDetectedError(
            f"Cannot move {target_label!r} under {new_parent_label!r}: creates a cycle"
        )

    # 1. Live database update
    if conn is not None and hasattr(conn, "execute"):
        try:
            # Remove old parent_of edge
            await conn.execute(
                """
                DELETE FROM kg_edges
                WHERE object_label = $1
                  AND predicate = $2
                  AND namespace_id = $3::uuid
                """,
                target_label,
                _PRED_PARENT_OF,
                ns_str,
            )
            # Insert new parent_of edge
            await conn.execute(
                """
                INSERT INTO kg_edges
                    (subject_label, predicate, object_label, namespace_id, confidence)
                VALUES ($1, $2, $3, $4::uuid, $5)
                ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO NOTHING
                """,
                new_parent_label,
                _PRED_PARENT_OF,
                target_label,
                ns_str,
                _STRUCTURAL_CONFIDENCE,
            )
            # Touch target node updated_at
            await conn.execute(
                """
                UPDATE kg_nodes
                SET updated_at = NOW()
                WHERE label = $1 AND namespace_id = $2::uuid
                """,
                target_label,
                ns_str,
            )
        except Exception as exc:
            log.error("move_fl_node DB operation failed: %s", exc)
            raise

    # 2. In-memory update
    edges = _MEM_EDGES.setdefault(ns_str, [])
    # Filter out old parent_of edge
    _MEM_EDGES[ns_str] = [
        e
        for e in edges
        if not (e["object_label"] == target_label and e["predicate"] == _PRED_PARENT_OF)
    ]
    _MEM_EDGES[ns_str].append(
        {
            "subject_label": new_parent_label,
            "predicate": _PRED_PARENT_OF,
            "object_label": target_label,
            "namespace_id": ns_str,
            "confidence": _STRUCTURAL_CONFIDENCE,
        }
    )

    log.info("Moved FL node %s under new parent %s by %s", target_label, new_parent_label, actor)
    return {
        "node": target,
        "new_parent": new_parent,
        "moved": True,
        "actor": actor,
    }


async def merge_fl_nodes(
    conn: Any | None,
    namespace_id: str | UUID,
    survivor_id_or_label: str,
    absorbed_id_or_label: str,
    reversible: bool = True,
    fold_rules: FoldRules = DEFAULT_FOLD_RULES,
    actor: str | None = None,
) -> dict[str, Any]:
    """Merge an absorbed FL node into a survivor FL node.

    Re-parents all child nodes of absorbed to survivor.
    Maintains an audit record for reversibility.
    """
    survivor = await get_fl_node(conn, namespace_id, survivor_id_or_label)
    absorbed = await get_fl_node(conn, namespace_id, absorbed_id_or_label)

    survivor_label = survivor["label"]
    absorbed_label = absorbed["label"]
    ns_str = str(namespace_id)

    if survivor_label == absorbed_label:
        raise MergeConflictError(f"Cannot merge node {survivor_label!r} into itself")

    # Evaluate compatibility with fold rules
    is_match, reason = evaluate_fl_match(
        survivor["name"],
        absorbed["name"],
        survivor["kind"],
        absorbed["kind"],
        rules=fold_rules,
    )

    # Get absorbed's children before reparenting (for audit and execution)
    children = await get_fl_children(conn, namespace_id, absorbed_label, recursive=False)
    children_labels = [c["label"] for c in children]

    audit_entry = {
        "merge_id": str(uuid4()),
        "namespace_id": ns_str,
        "survivor_label": survivor_label,
        "absorbed_label": absorbed_label,
        "reparented_children": children_labels,
        "reversible": reversible,
        "match_reason": reason,
        "actor": actor,
    }

    # 1. Live database updates
    if conn is not None and hasattr(conn, "execute"):
        try:
            # Reparent children: absorbed -[parent_of]-> child  -->  survivor -[parent_of]-> child
            await conn.execute(
                """
                UPDATE kg_edges
                SET subject_label = $1,
                    updated_at = NOW()
                WHERE subject_label = $2
                  AND predicate = $3
                  AND namespace_id = $4::uuid
                """,
                survivor_label,
                absorbed_label,
                _PRED_PARENT_OF,
                ns_str,
            )
            # Remove parent_of edge pointing to absorbed
            await conn.execute(
                """
                DELETE FROM kg_edges
                WHERE object_label = $1
                  AND predicate = $2
                  AND namespace_id = $3::uuid
                """,
                absorbed_label,
                _PRED_PARENT_OF,
                ns_str,
            )
            # Reparent needs edges (DESIGN_LINE associations)
            await conn.execute(
                """
                UPDATE kg_edges
                SET subject_label = $1,
                    updated_at = NOW()
                WHERE subject_label = $2
                  AND predicate = $3
                  AND namespace_id = $4::uuid
                """,
                survivor_label,
                absorbed_label,
                _PRED_NEEDS,
                ns_str,
            )
            # Mark absorbed node as merged
            await conn.execute(
                """
                UPDATE kg_nodes
                SET change_origin = 'merged',
                    updated_at = NOW()
                WHERE label = $1 AND namespace_id = $2::uuid
                """,
                absorbed_label,
                ns_str,
            )
        except Exception as exc:
            log.error("merge_fl_nodes DB execution failed: %s", exc)
            raise

    # 2. In-memory updates
    edges = _MEM_EDGES.setdefault(ns_str, [])
    for e in edges:
        if e["subject_label"] == absorbed_label and e["predicate"] == _PRED_PARENT_OF:
            e["subject_label"] = survivor_label
        elif e["subject_label"] == absorbed_label and e["predicate"] == _PRED_NEEDS:
            e["subject_label"] = survivor_label

    _MEM_EDGES[ns_str] = [
        e
        for e in edges
        if not (e["object_label"] == absorbed_label and e["predicate"] == _PRED_PARENT_OF)
    ]

    bucket = _MEM_NODES.get(ns_str, {})
    if absorbed_label in bucket:
        bucket[absorbed_label]["change_origin"] = "merged"

    _MEM_MERGE_AUDIT.setdefault(ns_str, []).append(audit_entry)

    log.info(
        "Merged FL node %s into %s (%d children reparented, match: %s)",
        absorbed_label,
        survivor_label,
        len(children_labels),
        reason,
    )

    return {
        "survivor": survivor,
        "absorbed": absorbed,
        "reparented_children": children_labels,
        "match_evaluation": {"matched": is_match, "reason": reason},
        "audit": audit_entry,
    }


async def promote_fl_node(
    conn: Any | None,
    namespace_id: str | UUID,
    node_id_or_label: str,
    actor: str | None = None,
) -> dict[str, Any]:
    """Promote a functional location from design-intent to as-built."""
    node = await get_fl_node(conn, namespace_id, node_id_or_label)
    label = node["label"]
    ns_str = str(namespace_id)

    if conn is not None and hasattr(conn, "execute"):
        try:
            await conn.execute(
                """
                UPDATE kg_nodes
                SET change_origin = 'operator',
                    updated_at = NOW()
                WHERE label = $1 AND namespace_id = $2::uuid
                """,
                label,
                ns_str,
            )
        except Exception as exc:
            log.error("promote_fl_node DB execution failed: %s", exc)
            raise

    bucket = _MEM_NODES.get(ns_str, {})
    if label in bucket:
        bucket[label]["change_origin"] = "operator"

    updated = await get_fl_node(conn, namespace_id, label)
    log.info("Promoted FL node %s to as-built by %s", label, actor)
    return {
        "node": updated,
        "as_built": True,
        "actor": actor,
    }


def seed_mem_node(
    namespace_id: str | UUID,
    label: str,
    kind: str | None = None,
    change_origin: str = "sync",
    parent_label: str | None = None,
) -> dict[str, Any]:
    """Seed node into in-memory store for testing."""
    ns_str = str(namespace_id)
    bucket = _MEM_NODES.setdefault(ns_str, {})
    node_id = str(uuid4())
    node_data = {
        "id": node_id,
        "label": label,
        "entity_type": _NODE_TYPE_FL,
        "namespace_id": ns_str,
        "kind": kind or derive_fl_kind(label),
        "change_origin": change_origin,
        "created_at": "2026-09-18T12:00:00Z",
        "updated_at": "2026-09-18T12:00:00Z",
    }
    bucket[label] = node_data

    if parent_label:
        edges = _MEM_EDGES.setdefault(ns_str, [])
        edges.append(
            {
                "subject_label": parent_label,
                "predicate": _PRED_PARENT_OF,
                "object_label": label,
                "namespace_id": ns_str,
                "confidence": _STRUCTURAL_CONFIDENCE,
            }
        )

    return _format_node_dict(node_data)
