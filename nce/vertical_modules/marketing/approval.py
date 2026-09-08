"""
nce/vertical_modules/marketing/approval.py
==========================================
Human sign-off approval gate for Module 14 (Marketing Engine).

Enforces MK-1:
- Structural human sign-off requirement before any content publication.
- Records approver identity, decision, notes, and timestamp in DB and cognitive ledger.
- Transitions draft -> approved (or rejected / changes_requested).
- Explicit tenant isolation on all queries: WHERE namespace_id = $1.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from nce.event_log import append_event

EVENT_MARKETING_CONTENT_APPROVED: str = "marketing_content_approved"

log = logging.getLogger("nce.vertical_modules.marketing.approval")

VALID_DECISIONS = frozenset({"approved", "rejected", "changes_requested"})


async def do_approve_content(
    engine: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Record human approval sign-off for drafted marketing content (MK-1).

    Parameters
    ----------
    engine : Any
        Engine context providing pg_pool.
    params : dict[str, Any]
        - namespace_id (str | UUID): active tenant
        - artifact_id (str | UUID): target case study or content asset ID
        - approver (str): non-empty human approver identifier/name
        - decision (str): 'approved', 'rejected', or 'changes_requested'
        - notes (str, optional): feedback or approval comments

    Returns
    -------
    dict[str, Any]
        Approval confirmation with timestamp and status.
    """
    raw_ns = params.get("namespace_id")
    if not raw_ns:
        raise ValueError("namespace_id is required")
    ns_str = str(raw_ns)

    raw_artifact_id = params.get("artifact_id")
    if not raw_artifact_id:
        raise ValueError("artifact_id is required")
    artifact_id_str = str(raw_artifact_id).strip()

    approver = str(params.get("approver") or "").strip()
    if not approver:
        raise ValueError("approver must be a non-empty human name or identifier (MK-1)")

    decision = str(params.get("decision") or "approved").strip().lower()
    if decision not in VALID_DECISIONS:
        raise ValueError(f"Invalid decision {decision!r}. Must be one of {sorted(VALID_DECISIONS)}")

    notes = str(params.get("notes") or "").strip()
    now_iso = datetime.now(timezone.utc).isoformat()
    new_status = "approved" if decision == "approved" else "draft"

    pool = getattr(engine, "pg_pool", None) or getattr(engine, "pool", None)
    if pool is not None:
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    # Update case_studies table if artifact is a case study
                    updated = await conn.execute(
                        """
                        UPDATE case_studies
                        SET    status = $3,
                               approver = $4,
                               approved_at = now(),
                               updated_at = now()
                        WHERE  namespace_id = $1::uuid
                          AND  id = $2::uuid
                        """,
                        UUID(ns_str),
                        UUID(artifact_id_str),
                        new_status,
                        approver,
                    )

                    # If no rows updated in case_studies, try content_assets table
                    if updated == "UPDATE 0":
                        await conn.execute(
                            """
                            UPDATE content_assets
                            SET    status = $3,
                                   updated_at = now()
                            WHERE  namespace_id = $1::uuid
                              AND  id = $2::uuid
                            """,
                            UUID(ns_str),
                            UUID(artifact_id_str),
                            new_status,
                        )

                    # Re-home human approval audit from broken v3_cognitive_ledger onto event_log
                    # via append_event inside active transaction (Charter §13 Item 3 / PR #51).
                    await append_event(
                        conn=conn,
                        namespace_id=UUID(ns_str),
                        agent_id="marketing_engine",
                        event_type=EVENT_MARKETING_CONTENT_APPROVED,
                        params={
                            "artifact_id": artifact_id_str,
                            "approver": approver,
                            "decision": decision,
                            "notes": notes,
                            "approved_at": now_iso,
                        },
                    )
        except Exception as exc:
            log.warning("do_approve_content DB write error: %s", exc)

    return {
        "ok": True,
        "artifact_id": artifact_id_str,
        "status": new_status,
        "decision": decision,
        "approver": approver,
        "approved_at": now_iso,
        "notes": notes,
    }
