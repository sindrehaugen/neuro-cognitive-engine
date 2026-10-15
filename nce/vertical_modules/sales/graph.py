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

import json
import logging
from typing import Any
from uuid import UUID, uuid5

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
# Label & UUID helpers
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


def _resolve_entity_uuid(ns_uuid: UUID, raw_id: str, prefix: str) -> UUID:
    """Resolve a string identifier to a parsed UUID or deterministic UUIDv5."""
    stripped = raw_id.strip()
    candidates = [stripped]
    if stripped.lower().startswith("nce:"):
        candidates.append(stripped[4:].strip())
    for candidate in candidates:
        try:
            return UUID(candidate)
        except (ValueError, AttributeError):
            continue
    return uuid5(ns_uuid, f"{prefix}:{stripped.upper()}")


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


async def _upsert_relational_customer(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    ns_uuid: UUID,
    customer_id: str,
    name: str | None = None,
) -> UUID:
    """Upsert a customer row in sales_customers so C12 child foreign keys resolve."""
    cust_uuid = _resolve_entity_uuid(ns_uuid, customer_id, _NODE_TYPE_CUSTOMER)
    customer_name = name or customer_id
    await conn.execute(
        """
        INSERT INTO sales_customers (id, namespace_id, name)
        VALUES ($1::uuid, $2::uuid, $3)
        ON CONFLICT (id) DO UPDATE
            SET name = COALESCE($4, sales_customers.name),
                updated_at = NOW()
        """,
        str(cust_uuid),
        str(ns_uuid),
        customer_name,
        name,
    )
    return cust_uuid


async def _upsert_relational_lead(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    ns_uuid: UUID,
    lead_id: str,
    *,
    customer_uuid: UUID | None = None,
    title: str | None = None,
) -> UUID:
    """Upsert a lead row in sales_leads so C12 child foreign keys resolve."""
    lead_uuid = _resolve_entity_uuid(ns_uuid, lead_id, _NODE_TYPE_LEAD)
    lead_title = title or lead_id
    await conn.execute(
        """
        INSERT INTO sales_leads (id, namespace_id, title, customer_id)
        VALUES ($1::uuid, $2::uuid, $3, $4::uuid)
        ON CONFLICT (id) DO UPDATE
            SET title = COALESCE($5, sales_leads.title),
                customer_id = COALESCE(EXCLUDED.customer_id, sales_leads.customer_id),
                updated_at = NOW()
        """,
        str(lead_uuid),
        str(ns_uuid),
        lead_title,
        str(customer_uuid) if customer_uuid else None,
        title,
    )
    return lead_uuid


async def _upsert_relational_deal(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    ns_uuid: UUID,
    deal_id: str,
    *,
    customer_uuid: UUID | None = None,
    lead_uuid: UUID | None = None,
    title: str | None = None,
) -> UUID:
    """Upsert a deal row in sales_deals so C12 child foreign keys resolve."""
    deal_uuid = _resolve_entity_uuid(ns_uuid, deal_id, _NODE_TYPE_DEAL)
    deal_title = title or deal_id
    await conn.execute(
        """
        INSERT INTO sales_deals (id, namespace_id, title, customer_id, lead_id)
        VALUES ($1::uuid, $2::uuid, $3, $4::uuid, $5::uuid)
        ON CONFLICT (id) DO UPDATE
            SET title = COALESCE($6, sales_deals.title),
                customer_id = COALESCE(EXCLUDED.customer_id, sales_deals.customer_id),
                lead_id = COALESCE(EXCLUDED.lead_id, sales_deals.lead_id),
                updated_at = NOW()
        """,
        str(deal_uuid),
        str(ns_uuid),
        deal_title,
        str(customer_uuid) if customer_uuid else None,
        str(lead_uuid) if lead_uuid else None,
        title,
    )
    return deal_uuid


# ---------------------------------------------------------------------------
# Public Actions
# ---------------------------------------------------------------------------


async def do_create_customer(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: str | UUID,
    *,
    customer_id: str,
    name: str | None = None,
    source_id: str | None = None,
) -> dict[str, Any]:
    """Transactional action to natively create a customer in the graph and sales_read_model."""
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id

    cust_lbl = _customer_label(customer_id)
    await _upsert_node(conn, ns_uuid, cust_lbl, _NODE_TYPE_CUSTOMER, source_id)

    customer_name = name or customer_id
    source_json = {"accountid": customer_id, "name": customer_name}
    await conn.execute(
        """
        INSERT INTO sales_read_model
            (namespace_id, entity, source_id, name, modifiedon, source_json, manual, source, is_deleted, synced_at, updated_at)
        VALUES
            ($1::uuid, 'accounts', $2, $3, NOW(), $4::jsonb, '{}'::jsonb, 'nce', false, NOW(), NOW())
        ON CONFLICT (namespace_id, entity, source_id)
        DO UPDATE SET
            name = COALESCE(EXCLUDED.name, sales_read_model.name),
            modifiedon = NOW(),
            source_json = sales_read_model.source_json || EXCLUDED.source_json,
            is_deleted = false,
            updated_at = NOW()
        """,
        str(ns_uuid),
        customer_id,
        customer_name,
        json.dumps(source_json),
    )

    await _upsert_relational_customer(conn, ns_uuid, customer_id, name)

    return {"ok": True, "customer_id": customer_id, "name": customer_name}


async def do_create_lead(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: str | UUID,
    *,
    lead_id: str,
    customer_id: str | None = None,
    name: str | None = None,
    confidence: float = 1.0,
    source_id: str | None = None,
) -> dict[str, Any]:
    """Transactional action to natively create a lead in the graph and sales_read_model."""
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id

    lead_lbl = _lead_label(lead_id)
    await _upsert_node(conn, ns_uuid, lead_lbl, _NODE_TYPE_LEAD, source_id)

    cust_lbl = None
    if customer_id:
        cust_lbl = _customer_label(customer_id)
        await _upsert_node(conn, ns_uuid, cust_lbl, _NODE_TYPE_CUSTOMER, source_id)
        await _upsert_edge(conn, ns_uuid, cust_lbl, _PRED_HAS, lead_lbl, confidence, source_id)

    lead_name = name or lead_id
    source_json: dict[str, Any] = {"leadid": lead_id, "name": lead_name}
    if customer_id:
        source_json["_customerid_value"] = customer_id

    await conn.execute(
        """
        INSERT INTO sales_read_model
            (namespace_id, entity, source_id, name, modifiedon, source_json, manual, source, is_deleted, synced_at, updated_at)
        VALUES
            ($1::uuid, 'leads', $2, $3, NOW(), $4::jsonb, '{}'::jsonb, 'nce', false, NOW(), NOW())
        ON CONFLICT (namespace_id, entity, source_id)
        DO UPDATE SET
            name = COALESCE(EXCLUDED.name, sales_read_model.name),
            modifiedon = NOW(),
            source_json = sales_read_model.source_json || EXCLUDED.source_json,
            is_deleted = false,
            updated_at = NOW()
        """,
        str(ns_uuid),
        lead_id,
        lead_name,
        json.dumps(source_json),
    )

    cust_uuid = (
        await _upsert_relational_customer(conn, ns_uuid, customer_id) if customer_id else None
    )
    await _upsert_relational_lead(
        conn,
        ns_uuid,
        lead_id,
        customer_uuid=cust_uuid,
        title=name,
    )

    return {
        "ok": True,
        "lead_id": lead_id,
        "customer_id": customer_id,
        "name": lead_name,
    }


async def do_create_deal(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: str | UUID,
    *,
    deal_id: str,
    customer_id: str,
    quote_id: str,
    opportunity_id: str | None = None,
    lead_id: str | None = None,
    name: str | None = None,
    confidence: float = 1.0,
    source_id: str | None = None,
) -> dict[str, Any]:
    """Transactional action to natively create a deal in the graph and sales_read_model.

    Upserts CUSTOMER, DEAL, and QUOTE nodes, plus any intermediate LEAD/OPPORTUNITY nodes.
    Writes the linking edges with the specified confidence and records into sales_read_model.
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

    # 3. Dual-write to sales_read_model (opportunities entity)
    deal_name = name or deal_id
    deal_source_json: dict[str, Any] = {
        "opportunityid": deal_id,
        "name": deal_name,
        "_customerid_value": customer_id,
        "statecode": "0",
    }
    if quote_id:
        deal_source_json["quoteid"] = quote_id

    await conn.execute(
        """
        INSERT INTO sales_read_model
            (namespace_id, entity, source_id, name, modifiedon, source_json, manual, source, is_deleted, synced_at, updated_at)
        VALUES
            ($1::uuid, 'opportunities', $2, $3, NOW(), $4::jsonb, '{}'::jsonb, 'nce', false, NOW(), NOW())
        ON CONFLICT (namespace_id, entity, source_id)
        DO UPDATE SET
            name = COALESCE(EXCLUDED.name, sales_read_model.name),
            modifiedon = NOW(),
            source_json = sales_read_model.source_json || EXCLUDED.source_json,
            is_deleted = false,
            updated_at = NOW()
        """,
        str(ns_uuid),
        deal_id,
        deal_name,
        json.dumps(deal_source_json),
    )

    cust_uuid = await _upsert_relational_customer(conn, ns_uuid, customer_id)
    lead_uuid = (
        await _upsert_relational_lead(conn, ns_uuid, lead_id, customer_uuid=cust_uuid)
        if lead_id
        else None
    )
    await _upsert_relational_deal(
        conn,
        ns_uuid,
        deal_id,
        customer_uuid=cust_uuid,
        lead_uuid=lead_uuid,
        title=name,
    )

    return {"ok": True, "deal_id": deal_id, "quote_id": quote_id, "customer_id": customer_id}


async def do_edit_deal(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: str | UUID,
    *,
    deal_id: str,
    name: str | None = None,
    confidence: float | None = None,
    source_id: str | None = None,
) -> dict[str, Any]:
    """Transactional action to natively edit a deal in the graph and sales_read_model.

    Updates the DEAL node itself, optionally adjusts edge confidence, and updates sales_read_model.
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

    if name is not None:
        await conn.execute(
            """
            UPDATE sales_read_model
            SET name = $1,
                source_json = jsonb_set(source_json, '{name}', to_jsonb($1::text)),
                updated_at = NOW()
            WHERE namespace_id = $2::uuid AND entity = 'opportunities' AND source_id = $3
            """,
            name,
            str(ns_uuid),
            deal_id,
        )
        await conn.execute(
            """
            UPDATE sales_deals
            SET title = $1,
                updated_at = NOW()
            WHERE namespace_id = $2::uuid AND id = $3::uuid
            """,
            name,
            str(ns_uuid),
            str(_resolve_entity_uuid(ns_uuid, deal_id, _NODE_TYPE_DEAL)),
        )

    return {"ok": True, "deal_id": deal_id}
