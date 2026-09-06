"""
nce/vertical_modules/procurement/po_line.py
===========================================
PO_LINE entity, status state machine, and Contract-A graph writer (Wave PR-2).

Responsibilities:
  - Definitive status state machine for purchase order lines:
      DRAFT -> ORDERED -> RECEIVED
      DRAFT -> CANCELLED
      ORDERED -> CANCELLED
  - Contract-A ownership: Procurement is sole writer of PO_LINE nodes in kg_nodes.
  - Relational content store synchronization (procurement_po_lines table).
  - Emits C4 reactive event bus graph write:
      node_type="PO_LINE", op="status_changed"
    with the exact payload contract expected by project/automation.py.
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Any
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]

from nce.entity_resolution.ownership import assert_owner
from nce.events.bus import publish

log = logging.getLogger("nce.vertical_modules.procurement.po_line")

# ---------------------------------------------------------------------------
# Node type and engine constants
# ---------------------------------------------------------------------------
NODE_TYPE_PO_LINE: str = "PO_LINE"
_PROCUREMENT_ENGINE: str = "procurement"
_CHANGE_ORIGIN: str = "agent"


# ---------------------------------------------------------------------------
# Status state machine
# ---------------------------------------------------------------------------
class POLineStatus(str, Enum):
    """Lifecycle status states for a PO line."""

    DRAFT = "DRAFT"
    ORDERED = "ORDERED"
    RECEIVED = "RECEIVED"
    CANCELLED = "CANCELLED"


ALLOWED_TRANSITIONS: dict[POLineStatus, frozenset[POLineStatus]] = {
    POLineStatus.DRAFT: frozenset({POLineStatus.ORDERED, POLineStatus.CANCELLED}),
    POLineStatus.ORDERED: frozenset({POLineStatus.RECEIVED, POLineStatus.CANCELLED}),
    POLineStatus.RECEIVED: frozenset(),
    POLineStatus.CANCELLED: frozenset(),
}


def validate_status_transition(
    current_status: str | POLineStatus,
    new_status: str | POLineStatus,
) -> None:
    """Validate that transition from current_status to new_status is permitted.

    Raises ValueError if transition is invalid or unknown.
    """
    try:
        curr = (
            current_status
            if isinstance(current_status, POLineStatus)
            else POLineStatus(str(current_status).upper())
        )
    except ValueError:
        raise ValueError(f"Unknown current PO_LINE status: {current_status!r}")

    try:
        nxt = (
            new_status
            if isinstance(new_status, POLineStatus)
            else POLineStatus(str(new_status).upper())
        )
    except ValueError:
        raise ValueError(f"Unknown target PO_LINE status: {new_status!r}")

    if curr == nxt:
        return

    allowed = ALLOWED_TRANSITIONS.get(curr, frozenset())
    if nxt not in allowed:
        valid_targets = sorted(s.value for s in allowed)
        raise ValueError(
            f"Invalid PO_LINE status transition: {curr.value} -> {nxt.value}. "
            f"Allowed target states from {curr.value}: {valid_targets}"
        )


def po_line_label(po_number: str, line_ref: str) -> str:
    """Canonical kg_nodes label for a PO_LINE node: PO_LINE:<PO_NUMBER>:<LINE_REF>."""
    return f"PO_LINE:{po_number.upper()}:{line_ref.upper()}"


def _po_label(po_number: str) -> str:
    return f"PO:{po_number.upper()}"


# ---------------------------------------------------------------------------
# Graph & Relational Writers
# ---------------------------------------------------------------------------
async def upsert_po_line_node(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: str | UUID,
    *,
    po_number: str,
    line_ref: str,
    bom_line_label: str | None = None,
    project_id: str | None = None,
    artnr: str | None = None,
    description: str | None = None,
    quantity: float = 1.0,
    unit_price: float | None = None,
    line_total: float | None = None,
    currency: str = "NOK",
    status: str | POLineStatus = POLineStatus.DRAFT,
    source_id: str | None = None,
) -> dict[str, Any]:
    """Upsert a PO_LINE node into kg_nodes and record in procurement_po_lines.

    Guarded by assert_owner against Contract-A ownership registry.
    Connects PO -[contains]-> PO_LINE and PO_LINE -[fulfills]-> BOM_LINE.
    """
    ns_uuid = namespace_id if isinstance(namespace_id, UUID) else UUID(str(namespace_id))
    status_enum = status if isinstance(status, POLineStatus) else POLineStatus(str(status).upper())
    status_str = status_enum.value
    transition = f"status:{status_str.lower()}"

    await assert_owner(conn, ns_uuid, NODE_TYPE_PO_LINE, _PROCUREMENT_ENGINE, transition)

    lbl = po_line_label(po_number, line_ref)
    calculated_total = (
        line_total
        if line_total is not None
        else ((unit_price * quantity) if unit_price is not None else None)
    )

    # Upsert kg_nodes
    await conn.execute(
        """
        INSERT INTO kg_nodes (
            namespace_id,
            entity_type,
            label,
            procurement_source_id,
            change_origin,
            created_at,
            updated_at
        )
        VALUES ($1, $2, $3, $4, $5, now(), now())
        ON CONFLICT (namespace_id, label) DO UPDATE
        SET updated_at = now(),
            procurement_source_id = COALESCE(EXCLUDED.procurement_source_id, kg_nodes.procurement_source_id)
        """,
        ns_uuid,
        NODE_TYPE_PO_LINE,
        lbl,
        source_id,
        _CHANGE_ORIGIN,
    )

    # Edge PO -[contains]-> PO_LINE
    parent_po_lbl = _po_label(po_number)
    await conn.execute(
        """
        INSERT INTO kg_edges (
            subject_label,
            predicate,
            object_label,
            confidence,
            namespace_id,
            change_origin
        )
        VALUES ($1, 'contains', $2, 1.0, $3, 'agent')
        ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO NOTHING
        """,
        parent_po_lbl,
        lbl,
        ns_uuid,
    )

    # Edge PO_LINE -[fulfills]-> BOM_LINE if linked
    if bom_line_label:
        await conn.execute(
            """
            INSERT INTO kg_edges (
                subject_label,
                predicate,
                object_label,
                confidence,
                namespace_id,
                change_origin
            )
            VALUES ($1, 'fulfills', $2, 1.0, $3, 'agent')
            ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO NOTHING
            """,
            lbl,
            bom_line_label,
            ns_uuid,
        )

    # Upsert procurement_po_lines relational store
    await conn.execute(
        """
        INSERT INTO procurement_po_lines (
            namespace_id,
            po_number,
            line_ref,
            project_id,
            bom_line_label,
            artnr,
            description,
            quantity,
            unit_price,
            line_total,
            currency,
            status,
            status_changed_at,
            created_at,
            updated_at
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, now(), now(), now())
        ON CONFLICT (namespace_id, po_number, line_ref) DO UPDATE
        SET project_id = COALESCE(EXCLUDED.project_id, procurement_po_lines.project_id),
            bom_line_label = COALESCE(EXCLUDED.bom_line_label, procurement_po_lines.bom_line_label),
            artnr = COALESCE(EXCLUDED.artnr, procurement_po_lines.artnr),
            description = COALESCE(EXCLUDED.description, procurement_po_lines.description),
            quantity = EXCLUDED.quantity,
            unit_price = EXCLUDED.unit_price,
            line_total = EXCLUDED.line_total,
            currency = EXCLUDED.currency,
            updated_at = now()
        """,
        ns_uuid,
        po_number,
        line_ref,
        project_id,
        bom_line_label,
        artnr,
        description,
        quantity,
        unit_price,
        calculated_total,
        currency,
        status_str,
    )

    return {
        "po_number": po_number,
        "line_ref": line_ref,
        "po_line_label": lbl,
        "status": status_str,
        "bom_line_label": bom_line_label,
        "project_id": project_id,
    }


async def update_po_line_status(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: str | UUID,
    *,
    po_number: str,
    line_ref: str,
    new_status: str | POLineStatus,
    current_status: str | POLineStatus | None = None,
    project_id: str | None = None,
    bom_line_label: str | None = None,
    project_value: float | None = None,
) -> dict[str, Any]:
    """Advance PO_LINE status, update relational & graph state, and emit status_changed event.

    Guarded by assert_owner against Contract-A ownership registry.
    Emits C4 graph write event with payload contract matching project/automation.py.
    """
    ns_uuid = namespace_id if isinstance(namespace_id, UUID) else UUID(str(namespace_id))

    # Read current status from relational store if not provided
    effective_current = current_status
    effective_project_id = project_id
    effective_bom_line_label = bom_line_label

    row = await conn.fetchrow(
        """
        SELECT status, project_id, bom_line_label
        FROM procurement_po_lines
        WHERE namespace_id = $1 AND po_number = $2 AND line_ref = $3
        """,
        ns_uuid,
        po_number,
        line_ref,
    )
    if row is not None:
        if effective_current is None:
            effective_current = row["status"]
        if effective_project_id is None:
            effective_project_id = row["project_id"]
        if effective_bom_line_label is None:
            effective_bom_line_label = row["bom_line_label"]

    if effective_current is not None:
        validate_status_transition(effective_current, new_status)

    nxt_enum = (
        new_status
        if isinstance(new_status, POLineStatus)
        else POLineStatus(str(new_status).upper())
    )
    nxt_str = nxt_enum.value
    transition = f"status:{nxt_str.lower()}"

    await assert_owner(conn, ns_uuid, NODE_TYPE_PO_LINE, _PROCUREMENT_ENGINE, transition)

    lbl = po_line_label(po_number, line_ref)

    # Update relational table
    await conn.execute(
        """
        UPDATE procurement_po_lines
        SET status = $4,
            status_changed_at = now(),
            updated_at = now(),
            project_id = COALESCE($5, project_id),
            bom_line_label = COALESCE($6, bom_line_label)
        WHERE namespace_id = $1 AND po_number = $2 AND line_ref = $3
        """,
        ns_uuid,
        po_number,
        line_ref,
        nxt_str,
        effective_project_id,
        effective_bom_line_label,
    )

    # Emit C4 reactive outbox event
    payload = {
        "namespace_id": str(ns_uuid),
        "po_number": po_number,
        "line_ref": line_ref,
        "project_id": effective_project_id or "",
        "bom_line_label": effective_bom_line_label or "",
        "id": effective_bom_line_label or "",  # backward-compatibility with project/automation.py
        "status": nxt_str,
        "project_value": project_value,
    }

    await publish(
        conn,
        namespace_id=ns_uuid,
        node_type=NODE_TYPE_PO_LINE,
        op="status_changed",
        aggregate_id=lbl,
        payload=payload,
    )

    log.info(
        "[po_line.status_changed] po=%s line=%s status=%s ns=%s",
        po_number,
        line_ref,
        nxt_str,
        str(ns_uuid)[:8],
    )

    return {
        "po_number": po_number,
        "line_ref": line_ref,
        "po_line_label": lbl,
        "status": nxt_str,
        "project_id": effective_project_id,
        "bom_line_label": effective_bom_line_label,
        "project_value": project_value,
    }
