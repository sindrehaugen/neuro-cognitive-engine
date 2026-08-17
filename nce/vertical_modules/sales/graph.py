"""
nce/vertical_modules/sales/graph.py
======================================
Cognitive-graph upserts for the Sales vertical module (Wave 6: pipeline-nodes).

Responsibilities:
  - Upsert CUSTOMER, LEAD, OPPORTUNITY, DEAL, QUOTE, and SIGNED_BASELINE nodes.
  - Assert ownership (Contract A) via ownership registry checks.
  - Upsert linking edges with confidence:
    - CUSTOMER -[has]-> LEAD
    - LEAD -[qualifies_into]-> OPPORTUNITY
    - OPPORTUNITY -[becomes]-> DEAL
    - DEAL -[priced_as]-> QUOTE
    - QUOTE -[freezes]-> SIGNED_BASELINE
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]

from nce.entity_resolution.ownership import assert_owner
from nce.events.emit import emit_graph_write

log = logging.getLogger("nce.vertical_modules.sales.graph")

_SALES_ENGINE: str = "sales"

# Node Types
_NODE_TYPE_CUSTOMER: str = "CUSTOMER"
_NODE_TYPE_LEAD: str = "LEAD"
_NODE_TYPE_OPPORTUNITY: str = "OPPORTUNITY"
_NODE_TYPE_DEAL: str = "DEAL"
_NODE_TYPE_QUOTE: str = "QUOTE"
_NODE_TYPE_SIGNED_BASELINE: str = "SIGNED_BASELINE"

# Predicates
_PRED_HAS: str = "has"
_PRED_QUALIFIES_INTO: str = "qualifies_into"
_PRED_BECOMES: str = "becomes"
_PRED_PRICED_AS: str = "priced_as"
_PRED_FREEZES: str = "freezes"


# ---------------------------------------------------------------------------
# Label helpers
# ---------------------------------------------------------------------------


def _customer_label(customer_id: str) -> str:
    return f"CUSTOMER:{customer_id.upper()}"


def _lead_label(lead_id: str) -> str:
    return f"LEAD:{lead_id.upper()}"


def _opportunity_label(opportunity_id: str) -> str:
    return f"OPPORTUNITY:{opportunity_id.upper()}"


def _deal_label(deal_id: str) -> str:
    return f"DEAL:{deal_id.upper()}"


def _quote_label(quote_id: str) -> str:
    return f"QUOTE:{quote_id.upper()}"


def _signed_baseline_label(baseline_id: str) -> str:
    return f"SIGNED_BASELINE:{baseline_id.upper()}"


# ---------------------------------------------------------------------------
# Private upsert helpers
# ---------------------------------------------------------------------------


async def _upsert_node(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: UUID,
    label: str,
    entity_type: str,
    source_id: str | None = None,
) -> None:
    """Upsert a single kg_node row and emit a transactional outbox event.

    Guarded by assert_owner to ensure write auth.
    """
    await assert_owner(conn, namespace_id, entity_type, _SALES_ENGINE)

    await conn.execute(
        """
        INSERT INTO kg_nodes (label, entity_type, namespace_id, change_origin, d365_source_id)
        VALUES ($1, $2, $3::uuid, 'agent', $4)
        ON CONFLICT (label, namespace_id) DO UPDATE
            SET entity_type = EXCLUDED.entity_type,
                change_origin = EXCLUDED.change_origin,
                d365_source_id = COALESCE(EXCLUDED.d365_source_id, kg_nodes.d365_source_id),
                updated_at = NOW()
        """,
        label,
        entity_type,
        str(namespace_id),
        source_id,
    )

    await emit_graph_write(
        conn,
        namespace_id=namespace_id,
        node_type=entity_type,
        op="upserted",
        node_id=label,
    )


async def _upsert_edge(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: UUID,
    subject_label: str,
    predicate: str,
    object_label: str,
    confidence: float,
    source_id: str | None = None,
) -> None:
    """Upsert a single kg_edges row.

    Confidence is stored on the edge only.
    """
    await conn.execute(
        """
        INSERT INTO kg_edges (subject_label, predicate, object_label, confidence, namespace_id, change_origin, d365_source_id)
        VALUES ($1, $2, $3, $4, $5::uuid, 'agent', $6)
        ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO UPDATE
            SET confidence = EXCLUDED.confidence,
                change_origin = EXCLUDED.change_origin,
                d365_source_id = COALESCE(EXCLUDED.d365_source_id, kg_edges.d365_source_id),
                updated_at = NOW()
        """,
        subject_label,
        predicate,
        object_label,
        float(confidence),
        str(namespace_id),
        source_id,
    )


# ---------------------------------------------------------------------------
# Public Actions
# ---------------------------------------------------------------------------


async def do_create_deal(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: str | UUID,
    *,
    deal_id: str,
    customer_id: str,
    quote_id: str,
    opportunity_id: str | None = None,
    lead_id: str | None = None,
    confidence: float = 1.0,
    source_id: str | None = None,
) -> dict[str, Any]:
    """Transactional action to natively create a deal in the graph.

    Upserts CUSTOMER, DEAL, and QUOTE nodes, plus any intermediate LEAD/OPPORTUNITY nodes.
    Writes the linking edges with the specified confidence.
    """
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id

    # 1. Upsert nodes
    cust_lbl = _customer_label(customer_id)
    await _upsert_node(conn, ns_uuid, cust_lbl, _NODE_TYPE_CUSTOMER, source_id)

    deal_lbl = _deal_label(deal_id)
    await _upsert_node(conn, ns_uuid, deal_lbl, _NODE_TYPE_DEAL, source_id)

    quote_lbl = _quote_label(quote_id)
    await _upsert_node(conn, ns_uuid, quote_lbl, _NODE_TYPE_QUOTE, source_id)

    lead_lbl = None
    if lead_id:
        lead_lbl = _lead_label(lead_id)
        await _upsert_node(conn, ns_uuid, lead_lbl, _NODE_TYPE_LEAD, source_id)

    opp_lbl = None
    if opportunity_id:
        opp_lbl = _opportunity_label(opportunity_id)
        await _upsert_node(conn, ns_uuid, opp_lbl, _NODE_TYPE_OPPORTUNITY, source_id)

    # 2. Upsert edges
    if lead_lbl:
        await _upsert_edge(conn, ns_uuid, cust_lbl, _PRED_HAS, lead_lbl, confidence, source_id)
        if opp_lbl:
            await _upsert_edge(
                conn, ns_uuid, lead_lbl, _PRED_QUALIFIES_INTO, opp_lbl, confidence, source_id
            )

    if opp_lbl:
        await _upsert_edge(conn, ns_uuid, opp_lbl, _PRED_BECOMES, deal_lbl, confidence, source_id)

    await _upsert_edge(conn, ns_uuid, deal_lbl, _PRED_PRICED_AS, quote_lbl, confidence, source_id)

    return {"ok": True, "deal_id": deal_id, "quote_id": quote_id}


async def do_edit_deal(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: str | UUID,
    *,
    deal_id: str,
    confidence: float | None = None,
    source_id: str | None = None,
) -> dict[str, Any]:
    """Transactional action to natively edit a deal in the graph.

    Updates the DEAL node itself and optionally adjusts the confidence of its outgoing edge.
    """
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id

    deal_lbl = _deal_label(deal_id)
    await _upsert_node(conn, ns_uuid, deal_lbl, _NODE_TYPE_DEAL, source_id)

    if confidence is not None:
        # Update any outgoing edges (e.g. priced_as quote)
        rows = await conn.fetch(
            """
            SELECT predicate, object_label FROM kg_edges
            WHERE subject_label = $1 AND namespace_id = $2
            """,
            deal_lbl,
            ns_uuid,
        )
        for r in rows:
            await _upsert_edge(
                conn,
                ns_uuid,
                deal_lbl,
                r["predicate"],
                r["object_label"],
                confidence,
                source_id,
            )

    return {"ok": True, "deal_id": deal_id}
