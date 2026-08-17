"""
nce/vertical_modules/agreements/mcp_handlers.py
================================================
MCP tool handlers for the Agreements vertical module (M3.W5: coverage-surface).

Public entry-points:
  ``handle_agreements_lookup_terms`` — term lookup for one or many agreements
  ("what are our payment terms with X").

Read-only Advisor tool (cacheable=True, admin_only=False, mutation=False).
No new logic — a thin, namespace-scoped read over ``agreement_review_queue``.
Rows are returned WITH their per-field confidence and review status so callers
can judge trust themselves (§9.3 calibration) — unconfirmed rows are included,
never silently filtered.

Registered in ``nce/tool_registry.py`` via ``_h(agreements_mcp_handlers, "handle_*")``.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID

from nce.db_utils import scoped_pg_session
from nce.mcp_args import require_namespace_id
from nce.mcp_errors import MCP_SCOPE_FORBIDDEN, McpError, mcp_handler
from nce.vertical_modules.agreements._guard import (
    AgreementsDisabledError,
    require_agreements_enabled,
)

if TYPE_CHECKING:
    from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.vertical_modules.agreements.mcp_handlers")


# ---------------------------------------------------------------------------
# Shared opt-in guard — applied at handler boundary (not inside do_* cores)
# ---------------------------------------------------------------------------

_MCP_AGREEMENTS_DISABLED_CODE: int = MCP_SCOPE_FORBIDDEN


async def _check_agreements_enabled(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """Check namespace opt-in; raise McpError(-32005) if not enabled.

    Returns the canonical namespace_id string on success.
    """
    namespace_id = require_namespace_id(arguments)
    try:
        await require_agreements_enabled(engine.pg_pool, namespace_id)
    except AgreementsDisabledError as exc:
        raise McpError(
            _MCP_AGREEMENTS_DISABLED_CODE,
            "Agreements vertical is not enabled for this namespace",
            data={"reason": "agreements_disabled", "detail": str(exc)},
        ) from exc
    return namespace_id


# ---------------------------------------------------------------------------
# Query construction — parameterized SQL only, explicit namespace predicate
# ---------------------------------------------------------------------------

# Maximum rows returned from a lookup (guard against large result dumps).
_LOOKUP_MAX_ROWS: int = 50

_LOOKUP_BASE_SQL: str = """
    SELECT agreement_id, source_doc_ref, review_status, extraction_confidence,
           extracted, flagged_at
    FROM   agreement_review_queue
    WHERE  namespace_id = $1::uuid
"""


def _build_lookup_query(
    namespace_id: str,
    agreement_id: str | None,
    supplier: str | None,
) -> tuple[str, list[Any]]:
    """Build the review-queue lookup SQL + parameter list.

    The ``namespace_id = $1::uuid`` predicate is ALWAYS present — never rely
    on RLS alone (repo lesson: owner-pool test roles bypass FORCE RLS).

    The supplier filter matches the ``supplierId`` term stored in ``extracted``
    (per-field shape ``{value, extractionConfidence, reviewStatus}`` — see
    ``extract.py``; the extraction schema has no supplierName field).  The
    COALESCE fallback to ``extracted->>'supplierId'`` also matches rows whose
    ``extracted`` was overwritten with FLAT ``corrected_terms`` via the review
    path (``review.py`` stores reviewer input verbatim, shape-unvalidated).
    The ILIKE arm gives substring matching; the equality arm keeps literal
    lookups working when the caller's value contains LIKE wildcard characters.
    """
    params: list[Any] = [UUID(namespace_id)]
    sql = _LOOKUP_BASE_SQL
    if agreement_id:
        sql += "      AND agreement_id = $2\n"
        params.append(UUID(str(agreement_id)))
    elif supplier:
        sql += (
            "      AND (COALESCE(extracted->'supplierId'->>'value',\n"
            "                    extracted->>'supplierId') ILIKE '%'||$2||'%'\n"
            "           OR COALESCE(extracted->'supplierId'->>'value',\n"
            "                       extracted->>'supplierId') = $2)\n"
        )
        params.append(supplier)
    sql += f"    ORDER BY flagged_at DESC LIMIT {_LOOKUP_MAX_ROWS}"
    return sql, params


# ---------------------------------------------------------------------------
# Row shaping — pure helpers, zero DB
# ---------------------------------------------------------------------------


def _unwrap_terms(extracted: dict[str, Any]) -> dict[str, Any]:
    """Unwrap each extracted field into ``{value, confidence, review_status}``.

    Mirrors the per-field JSONB shape written by ``extract.py``
    (``{value, extractionConfidence, reviewStatus}``); tolerates flat scalar
    values the same way ``coverage._unwrap_field`` does.
    """
    terms: dict[str, Any] = {}
    for field, raw in extracted.items():
        if isinstance(raw, dict):
            terms[field] = {
                "value": raw.get("value"),
                "confidence": raw.get("extractionConfidence"),
                "review_status": raw.get("reviewStatus"),
            }
        else:
            terms[field] = {"value": raw, "confidence": None, "review_status": None}
    return terms


def _serialize_lookup_row(row: Any) -> dict[str, Any]:
    """Shape one ``agreement_review_queue`` row for the tool response."""
    extracted = row["extracted"]
    if isinstance(extracted, str):
        extracted = json.loads(extracted)
    elif extracted is None:
        extracted = {}
    return {
        "agreement_id": str(row["agreement_id"]),
        "source_doc_ref": row["source_doc_ref"],
        "review_status": row["review_status"],
        "extraction_confidence": row["extraction_confidence"],
        "flagged_at": row["flagged_at"],
        "terms": _unwrap_terms(extracted),
    }


# ---------------------------------------------------------------------------
# MCP handler
# ---------------------------------------------------------------------------


@mcp_handler
async def handle_agreements_lookup_terms(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: agreements_lookup_terms — READ-ONLY agreement term lookup.

    Required arguments:
        namespace_id (str, UUID)
    Optional arguments:
        agreement_id (str, UUID) — return the single matching agreement.
        supplier     (str)       — filter on the extracted ``supplierId`` term.

    Without a filter, returns the 50 most recently flagged agreements.
    Every row carries ``review_status`` plus per-field confidence/review state
    so callers judge trust — unconfirmed rows are NOT silently filtered.
    """
    namespace_id = await _check_agreements_enabled(engine, arguments)
    agreement_id = arguments.get("agreement_id")
    supplier = str(arguments.get("supplier") or "").strip() or None

    sql, params = _build_lookup_query(namespace_id, agreement_id, supplier)
    async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
        rows = await conn.fetch(sql, *params)

    agreements = [_serialize_lookup_row(row) for row in rows]
    return json.dumps(
        {"status": "ok", "count": len(agreements), "agreements": agreements},
        default=str,
    )
