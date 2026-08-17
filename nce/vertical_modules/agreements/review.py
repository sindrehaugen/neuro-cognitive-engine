"""
nce/vertical_modules/agreements/review.py
==========================================
Agreement OCR extraction review logic — Module 3.Wave 2.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from nce.db_utils import scoped_pg_session
from nce.mcp_args import require_namespace_id

if TYPE_CHECKING:
    from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.vertical_modules.agreements.review")


async def do_review_extraction(
    engine: NCEEngine,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Human review (accept/reject/correct) of an extracted agreement.

    Updates the review status and optionally updates the extracted terms.

    Parameters
    ----------
    engine:
        NCEEngine instance providing connection pools.
    params:
        dict containing:
          - namespace_id: str/UUID (required)
          - agreement_id: str/UUID (required)
          - decision: str, 'confirm' or 'reject' (required)
          - reviewed_by: str (required)
          - corrected_terms: dict (optional)

    Returns
    -------
    dict:
        The updated review queue row dict.
    """
    namespace_id = require_namespace_id(params)
    agreement_id_str = params.get("agreement_id")
    if not agreement_id_str:
        raise ValueError("agreement_id is required")

    decision = params.get("decision")
    if decision not in ("confirm", "reject"):
        raise ValueError("decision must be 'confirm' or 'reject'")

    reviewed_by = params.get("reviewed_by")
    if not reviewed_by:
        raise ValueError("reviewed_by is required")

    corrected_terms = params.get("corrected_terms")
    if corrected_terms is not None and not isinstance(corrected_terms, dict):
        raise ValueError("corrected_terms must be a dictionary")

    agreement_id = uuid.UUID(str(agreement_id_str))

    # Map decision to review_status:
    # - 'confirm' maps to 'auto_green'
    # - 'reject' maps to 'manual_red'
    new_status = "auto_green" if decision == "confirm" else "manual_red"
    now = datetime.now(timezone.utc)

    async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
        # Check if the row exists
        existing = await conn.fetchrow(
            """
            SELECT agreement_id, source_doc_ref, extraction_confidence, review_status, extracted
            FROM agreement_review_queue
            WHERE agreement_id = $1 AND namespace_id = $2
            """,
            agreement_id,
            uuid.UUID(str(namespace_id)),
        )
        if not existing:
            raise ValueError(f"Agreement review queue row not found for ID: {agreement_id}")

        # Update the row
        if corrected_terms is not None:
            extracted_json = json.dumps(corrected_terms)
        else:
            # If existing["extracted"] is already a dict (from asyncpg jsonb decoding),
            # serialize it back or let asyncpg handle it. Passing a dict directly
            # to $2 when the SQL type is jsonb works perfectly in asyncpg.
            extracted_json = existing["extracted"]

        updated_row = await conn.fetchrow(
            """
            UPDATE agreement_review_queue
            SET review_status = $1,
                extracted = $2::jsonb,
                reviewed_by = $3,
                reviewed_at = $4
            WHERE agreement_id = $5 AND namespace_id = $6
            RETURNING agreement_id, source_doc_ref, extraction_confidence, review_status,
                      extracted, flagged_at, reviewed_by, reviewed_at
            """,
            new_status,
            extracted_json,
            reviewed_by,
            now,
            agreement_id,
            uuid.UUID(str(namespace_id)),
        )

        if not updated_row:
            raise RuntimeError("Failed to update agreement review queue row")

        # Parse extracted back to dict/object cleanly
        raw_extracted = updated_row["extracted"]
        if isinstance(raw_extracted, str):
            extracted_data = json.loads(raw_extracted)
        else:
            extracted_data = raw_extracted

        result = {
            "agreement_id": str(updated_row["agreement_id"]),
            "source_doc_ref": updated_row["source_doc_ref"],
            "extraction_confidence": float(updated_row["extraction_confidence"]),
            "review_status": updated_row["review_status"],
            "extracted": extracted_data,
            "flagged_at": updated_row["flagged_at"].isoformat()
            if updated_row["flagged_at"]
            else None,
            "reviewed_by": updated_row["reviewed_by"],
            "reviewed_at": updated_row["reviewed_at"].isoformat()
            if updated_row["reviewed_at"]
            else None,
        }
        return result
