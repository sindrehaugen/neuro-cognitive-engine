"""
nce/decision_feedback.py
========================
C10 Decision-Feedback Service:
Stores the ground-truth human decision signal across vertical engines:
  - product: BOM_LINE -> PRODUCT match decisions (accept / override)
  - procurement: supplier ranking decisions (accept / override)
  - economy: invoice match decisions (accept / override)
  - resources: allocation outcomes (held / deviated)
  - field_tech: work order execution outcomes (succeeded / rework)

Rows carry the proposal, the human's decision, the delta, the actor,
the engine, context metadata, and the tenant namespace.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID

from nce.db_utils import scoped_pg_session
from nce.mcp_args import require_namespace_id
from nce.mcp_errors import mcp_handler

if TYPE_CHECKING:
    import asyncpg

    from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.decision_feedback")


def _extract_pool(engine_or_pool: Any) -> asyncpg.Pool:
    """Extract an asyncpg.Pool from either an NCEEngine instance or a bare Pool."""
    if hasattr(engine_or_pool, "pg_pool"):
        # If it's a bare mock pool where pg_pool was not explicitly set or defined on the class,
        # accessing .pg_pool would auto-generate an unconfigured nested mock.
        if (
            hasattr(engine_or_pool, "__dict__")
            and "pg_pool" not in engine_or_pool.__dict__
            and not hasattr(type(engine_or_pool), "pg_pool")
        ):
            return engine_or_pool
        val = getattr(engine_or_pool, "pg_pool", None)
        if val is not None:
            return val
    return engine_or_pool


async def record_decision_feedback(
    pool_or_engine: Any,
    namespace_id: str | UUID,
    *,
    engine: str,
    proposal: dict[str, Any] | None = None,
    decision: str,
    delta: dict[str, Any] | None = None,
    actor: str = "human",
    context_id: str | None = None,
) -> dict[str, Any]:
    """Insert one row into ``decision_feedback`` under tenant RLS.

    Parameters
    ----------
    pool_or_engine:
        Live NCEEngine instance or asyncpg.Pool.
    namespace_id:
        Tenant namespace UUID string or UUID instance.
    engine:
        Source engine identifier (e.g. 'product', 'procurement', 'economy', 'resources', 'field_tech').
    proposal:
        The proposal / recommendation produced by the engine before human interaction.
    decision:
        The human's decision (e.g. 'accept', 'override', 'held', 'deviated', 'succeeded', 'rework').
    delta:
        Difference between proposal and final decision (e.g. chosen/rejected keys, score adjustments).
    actor:
        Actor identifier or role that made the decision (default 'human').
    context_id:
        Optional entity / correlation identifier (e.g. bom_line, supplier_id, invoice_id, work_order_id).

    Returns
    -------
    dict with status, id, engine, decision, context_id, created_at.
    """
    if not engine or not isinstance(engine, str):
        raise ValueError("engine must be a non-empty string")
    if not decision or not isinstance(decision, str):
        raise ValueError("decision must be a non-empty string")

    ns_uuid = UUID(str(namespace_id))
    pool = _extract_pool(pool_or_engine)

    proposal_json = json.dumps(proposal or {})
    delta_json = json.dumps(delta or {})

    async with scoped_pg_session(pool, ns_uuid) as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO decision_feedback (
                namespace_id, engine, context_id, proposal, decision, delta, actor
            ) VALUES (
                $1, $2, $3, $4::jsonb, $5, $6::jsonb, $7
            )
            RETURNING id, created_at
            """,
            ns_uuid,
            engine.strip(),
            str(context_id).strip() if context_id is not None else None,
            proposal_json,
            decision.strip(),
            delta_json,
            str(actor or "human").strip(),
        )

    feedback_id = None
    created_at = None
    if row:
        raw_id = row.get("id") if hasattr(row, "get") else (row["id"] if "id" in row else None)
        feedback_id = str(raw_id) if raw_id is not None else None
        raw_created = (
            row.get("created_at")
            if hasattr(row, "get")
            else (row["created_at"] if "created_at" in row else None)
        )
        if raw_created is not None:
            created_at = (
                raw_created.isoformat() if hasattr(raw_created, "isoformat") else str(raw_created)
            )

    log.info(
        "record_decision_feedback: namespace=%s engine=%s context=%s decision=%s id=%s",
        ns_uuid,
        engine,
        context_id,
        decision,
        feedback_id,
    )

    return {
        "status": "recorded",
        "id": feedback_id,
        "engine": engine,
        "decision": decision,
        "context_id": context_id,
        "created_at": created_at,
    }


# ---------------------------------------------------------------------------
# MCP handler
# ---------------------------------------------------------------------------


@mcp_handler
async def handle_record_decision_feedback(
    engine: NCEEngine,
    arguments: dict[str, Any],
) -> str:
    """MCP tool handler: record a human decision or outcome in decision_feedback."""
    namespace_id = require_namespace_id(arguments)
    engine_name = str(arguments.get("engine") or "").strip()
    if not engine_name:
        raise ValueError("Field 'engine' is required and cannot be empty")

    decision = str(arguments.get("decision") or "").strip()
    if not decision:
        raise ValueError("Field 'decision' is required and cannot be empty")

    proposal = arguments.get("proposal")
    if proposal is not None and not isinstance(proposal, dict):
        raise ValueError("Field 'proposal' must be an object if provided")

    delta = arguments.get("delta")
    if delta is not None and not isinstance(delta, dict):
        raise ValueError("Field 'delta' must be an object if provided")

    actor = str(arguments.get("actor") or "human").strip()
    context_id = arguments.get("context_id")
    if context_id is not None:
        context_id = str(context_id).strip()

    result = await record_decision_feedback(
        engine,
        namespace_id,
        engine=engine_name,
        proposal=proposal,
        decision=decision,
        delta=delta,
        actor=actor,
        context_id=context_id,
    )
    return json.dumps(result)
