"""
nce/vertical_modules/business_insights/provenance.py
====================================================
Cognitive graph provenance and cognitive ledger audit helpers for Module 16.

Graph contract:
  - Node types: BUSINESS_INSIGHTS_BRIEFING, BUSINESS_INSIGHTS_FINDING,
                BUSINESS_INSIGHTS_SCENARIO, BUSINESS_INSIGHTS_KPI_SNAPSHOT
  - Edges:
      BRIEFING -[surfaces]-> FINDING
      FINDING  -[derived_from]-> {PROJECT|INVOICE|TICKET|QUOTE|...}
      SCENARIO -[projects]-> {PIPELINE|CAPACITY|CASHFLOW}
      KPI_SNAPSHOT -[rolls_up]-> {ENGINE}

Every claim resolves to derived_from edges.
All accesses/generations write an audit row to v3_cognitive_ledger.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

log = logging.getLogger("nce.vertical_modules.business_insights.provenance")


def make_briefing_node(
    namespace_id: str | UUID,
    briefing_date: str,
    headline: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Construct a BRIEFING cognitive graph node dictionary."""
    node_id = str(uuid4())
    return {
        "id": node_id,
        "namespace_id": str(namespace_id),
        "label": f"briefing:{briefing_date}",
        "entity_type": "BUSINESS_INSIGHTS_BRIEFING",
        "headline": headline,
        "date": briefing_date,
        "business_insights_source_id": f"bi:briefing:{briefing_date}",
        "metadata": metadata or {},
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def make_finding_node(
    namespace_id: str | UUID,
    finding_type: str,
    title: str,
    rationale: str,
    provenance_node_ids: list[str],
    coverage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Construct a BUSINESS_INSIGHT finding node dictionary."""
    node_id = str(uuid4())
    return {
        "id": node_id,
        "namespace_id": str(namespace_id),
        "label": f"insight:{finding_type}:{node_id[:8]}",
        "entity_type": "BUSINESS_INSIGHTS_FINDING",
        "finding_type": finding_type,
        "title": title,
        "rationale": rationale,
        "provenance_node_ids": provenance_node_ids,
        "coverage": coverage or {},
        "business_insights_source_id": f"bi:finding:{finding_type}:{node_id[:8]}",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def make_edge(
    namespace_id: str | UUID,
    source_id: str,
    target_id: str,
    edge_type: str,
    properties: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Construct an advisory/provenance graph edge."""
    return {
        "id": str(uuid4()),
        "namespace_id": str(namespace_id),
        "source_id": source_id,
        "target_id": target_id,
        "edge_type": edge_type,
        "properties": properties or {},
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def make_scenario_node(
    namespace_id: str | UUID,
    name: str,
    assumptions: dict[str, Any],
    results: dict[str, Any],
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Construct a SCENARIO cognitive graph node dictionary."""
    node_id = str(uuid4())
    return {
        "id": node_id,
        "namespace_id": str(namespace_id),
        "label": f"scenario:{node_id[:8]}",
        "entity_type": "BUSINESS_INSIGHTS_SCENARIO",
        "name": name,
        "assumptions": assumptions,
        "results": results,
        "business_insights_source_id": f"bi:scenario:{node_id[:8]}",
        "metadata": metadata or {},
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


async def record_ledger_audit(
    conn: Any,
    namespace_id: str | UUID,
    actor: str,
    action: str,
    referenced_nodes: list[str],
    details: dict[str, Any] | None = None,
) -> None:
    """Record auditable access or generation event in v3_cognitive_ledger."""
    if conn is None:
        return
    ns_uuid = UUID(str(namespace_id))
    now = datetime.now(timezone.utc)
    entry_id = uuid4()
    payload = {
        "actor": actor,
        "action": action,
        "referenced_nodes": referenced_nodes,
        "details": details or {},
        "recorded_at": now.isoformat(),
    }
    try:
        await conn.execute(
            """
            INSERT INTO v3_cognitive_ledger (
                id, namespace_id, entity_id, entity_type, change_type,
                author, metadata, created_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            entry_id,
            ns_uuid,
            str(entry_id),
            "BUSINESS_INSIGHTS_AUDIT",
            action,
            actor,
            json.dumps(payload),
            now,
        )
    except Exception as exc:
        log.warning("Failed to record access audit in v3_cognitive_ledger: %s", exc)
