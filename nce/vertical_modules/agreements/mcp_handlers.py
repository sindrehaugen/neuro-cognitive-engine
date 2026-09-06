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
import uuid
from typing import TYPE_CHECKING, Any
from uuid import UUID

from nce.db_utils import scoped_pg_session
from nce.mcp_args import require_namespace_id
from nce.mcp_errors import MCP_SCOPE_FORBIDDEN, McpError, mcp_handler
from nce.vertical_modules.agreements._guard import (
    AgreementsDisabledError,
    require_agreements_enabled,
)
from nce.vertical_modules.agreements.authoring import (
    do_create_agreement,
    do_suggest_revision,
)
from nce.vertical_modules.agreements.compliance import do_run_compliance_audit
from nce.vertical_modules.agreements.coverage import do_coverage_matrix
from nce.vertical_modules.agreements.extract import do_extract_agreement
from nce.vertical_modules.agreements.graph import write_agreement_to_graph_and_memories
from nce.vertical_modules.agreements.kickback import do_reconcile_kickback
from nce.vertical_modules.agreements.review import do_review_extraction
from nce.vertical_modules.agreements.signing import (
    do_record_signature,
    do_request_signature,
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


@mcp_handler
async def handle_agreements_coverage_matrix(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: agreements_coverage_matrix — READ-ONLY agreement coverage/gap matrix.

    Cross-joins agreements against Economy GL spend to detect leakage, expiring
    agreements, and low-confidence extractions in the review queue.

    Required arguments:
        namespace_id (str, UUID)
    Optional arguments:
        since_iso    (str, ISO timestamp) — lower bound for GL spend analysis.
    """
    namespace_id = await _check_agreements_enabled(engine, arguments)
    since_iso = arguments.get("since_iso")
    result = await do_coverage_matrix(
        engine,
        {"namespace_id": namespace_id, "since_iso": since_iso},
    )
    return json.dumps(result, default=str)


@mcp_handler
async def handle_agreements_reconcile_kickback(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: agreements_reconcile_kickback — READ-ONLY supplier kickback reconciliation.

    Reconciles an agreement's kickback tiers against real Economy GL spend.
    Enforces §9.3 sign-off gate (only auto_green agreements proceed).

    Required arguments:
        namespace_id  (str, UUID)
        agreement_id  (str, UUID)
    Optional arguments:
        since_iso              (str, ISO timestamp) — inclusive GL date lower bound.
        until_iso              (str, ISO timestamp) — inclusive GL date upper bound.
        projected_kickback_nok (float)              — optional forecast figure.
    """
    namespace_id = await _check_agreements_enabled(engine, arguments)
    agreement_id = str(arguments.get("agreement_id") or "").strip()
    if not agreement_id:
        raise ValueError("agreement_id is required")

    params: dict[str, Any] = {
        "namespace_id": namespace_id,
        "agreement_id": agreement_id,
    }
    if "since_iso" in arguments:
        params["since_iso"] = arguments["since_iso"]
    if "until_iso" in arguments:
        params["until_iso"] = arguments["until_iso"]
    if "projected_kickback_nok" in arguments and arguments["projected_kickback_nok"] is not None:
        params["projected_kickback_nok"] = float(arguments["projected_kickback_nok"])

    result = await do_reconcile_kickback(engine, params)
    return json.dumps(result, default=str)


@mcp_handler
async def handle_agreements_run_compliance_audit(
    engine: NCEEngine, arguments: dict[str, Any]
) -> str:
    """MCP tool: agreements_run_compliance_audit — rebate/kickback compliance audit gate.

    Verifies rebate overrides against human-signed agreements to prevent
    anti-competitive steering and fraud. Fails closed on any policy violation.

    Required arguments:
        namespace_id   (str, UUID)
        po_number      (str)
        supplier_id    (str)
        rebate_amount  (float, numeric)
    Optional arguments:
        agreement_id   (str, UUID) — disambiguates the governing agreement.
    """
    namespace_id = await _check_agreements_enabled(engine, arguments)
    po_number = str(arguments.get("po_number") or "").strip()
    if not po_number:
        raise ValueError("po_number is required")
    supplier_id = str(arguments.get("supplier_id") or "").strip()
    if not supplier_id:
        raise ValueError("supplier_id is required")
    rebate_raw = arguments.get("rebate_amount")
    if rebate_raw is None:
        raise ValueError("rebate_amount is required")
    try:
        rebate_amount = float(rebate_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"rebate_amount must be a valid number: {exc}") from exc

    params: dict[str, Any] = {
        "namespace_id": namespace_id,
        "po_number": po_number,
        "supplier_id": supplier_id,
        "rebate_amount": rebate_amount,
    }
    if "agreement_id" in arguments and arguments["agreement_id"]:
        params["agreement_id"] = str(arguments["agreement_id"]).strip()

    result = await do_run_compliance_audit(engine, params)
    return json.dumps(result, default=str)


@mcp_handler
async def handle_agreements_extract(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: agreements_extract — OCR extraction and confidence gating for agreement docs.

    Actor tool (mutation=True, admin_only=True).
    Runs OCR on a source document reference, stores run history and queue entries,
    and auto-promotes to graph/memories only if overall confidence meets threshold.

    Required arguments:
        namespace_id    (str, UUID)
        source_doc_ref  (str) — SharePoint or object store document reference.
    """
    namespace_id = await _check_agreements_enabled(engine, arguments)
    source_doc_ref = str(arguments.get("source_doc_ref") or "").strip()
    if not source_doc_ref:
        raise ValueError("source_doc_ref is required")

    extraction_res = await do_extract_agreement(
        engine,
        {"namespace_id": namespace_id, "source_doc_ref": source_doc_ref},
    )

    agreement_id = uuid.uuid4()
    run_id = uuid.uuid4()

    confidences = [
        v["extractionConfidence"]
        for v in extraction_res.values()
        if isinstance(v, dict) and "extractionConfidence" in v
    ]
    overall_confidence = sum(confidences) / len(confidences) if confidences else 100.0

    overall_status = "auto_green"
    if any(
        isinstance(v, dict) and v.get("reviewStatus") == "manual_red"
        for v in extraction_res.values()
    ):
        overall_status = "manual_red"
    elif any(
        isinstance(v, dict) and v.get("reviewStatus") == "needs_review_yellow"
        for v in extraction_res.values()
    ):
        overall_status = "needs_review_yellow"

    async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
        await conn.execute(
            """
            INSERT INTO agreement_extraction_runs (namespace_id, run_id, source_doc_ref, extraction_confidence, status)
            VALUES ($1, $2, $3, $4, 'ok')
            """,
            UUID(namespace_id),
            run_id,
            source_doc_ref,
            overall_confidence,
        )
        await conn.execute(
            """
            INSERT INTO agreement_review_queue (agreement_id, namespace_id, source_doc_ref, extraction_confidence, review_status, extracted)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb)
            """,
            agreement_id,
            UUID(namespace_id),
            source_doc_ref,
            overall_confidence,
            overall_status,
            json.dumps(extraction_res),
        )

    if overall_status == "auto_green":
        await write_agreement_to_graph_and_memories(
            engine.pg_pool,
            namespace_id,
            agreement_id=agreement_id,
            source_doc_ref=source_doc_ref,
            extracted_data=extraction_res,
        )

    return json.dumps(
        {
            "status": "ok",
            "agreement_id": str(agreement_id),
            "review_status": overall_status,
            "extraction_confidence": overall_confidence,
            "extracted": extraction_res,
        },
        default=str,
    )


@mcp_handler
async def handle_agreements_create(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: agreements_create — author a new agreement graph-natively.

    Actor tool (mutation=True, admin_only=True).
    Instantiates an agreement and term nodes in DRAFT state with confidence 1.0.

    Required arguments:
        namespace_id (str, UUID)
    Optional arguments:
        supplier_id  (str) — supplier identifier for Vendor -under-> edge.
        customer_id  (str) — customer identifier for Customer -under-> edge.
        terms        (dict) — flat dictionary of commercial terms.
        agreement_id (str, UUID) — optional explicit agreement ID.
    """
    namespace_id = await _check_agreements_enabled(engine, arguments)
    params: dict[str, Any] = {"namespace_id": namespace_id}
    if "supplier_id" in arguments and arguments["supplier_id"]:
        params["supplier_id"] = arguments["supplier_id"]
    if "customer_id" in arguments and arguments["customer_id"]:
        params["customer_id"] = arguments["customer_id"]
    if "terms" in arguments and isinstance(arguments["terms"], dict):
        params["terms"] = arguments["terms"]
    if "agreement_id" in arguments and arguments["agreement_id"]:
        params["agreement_id"] = arguments["agreement_id"]

    result = await do_create_agreement(engine, params)
    return json.dumps(result, default=str)


@mcp_handler
async def handle_agreements_suggest_revision(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: agreements_suggest_revision — record a proposed revision to an agreement clause.

    Actor tool (mutation=True, admin_only=True).
    Propose-only revision suggestion appended to v3_cognitive_ledger.

    Required arguments:
        namespace_id    (str, UUID)
        agreement_id    (str, UUID)
        field           (str) — term field to change.
        proposed_value  (any) — proposed new value.
    Optional arguments:
        rationale       (str) — reason for proposal.
        author          (str) — author of suggestion.
    """
    namespace_id = await _check_agreements_enabled(engine, arguments)
    agreement_id = str(arguments.get("agreement_id") or "").strip()
    if not agreement_id:
        raise ValueError("agreement_id is required")
    field = str(arguments.get("field") or "").strip()
    if not field:
        raise ValueError("field is required")
    if "proposed_value" not in arguments:
        raise ValueError("proposed_value is required")

    params: dict[str, Any] = {
        "namespace_id": namespace_id,
        "agreement_id": agreement_id,
        "field": field,
        "proposed_value": arguments["proposed_value"],
    }
    if "rationale" in arguments:
        params["rationale"] = arguments["rationale"]
    if "author" in arguments:
        params["author"] = arguments["author"]

    result = await do_suggest_revision(engine, params)
    return json.dumps(result, default=str)


@mcp_handler
async def handle_agreements_request_signature(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: agreements_request_signature — open a signing session for an agreement.

    Actor tool (mutation=True, admin_only=True).
    Initiates e-signature flow and records request session.

    Required arguments:
        namespace_id (str, UUID)
        agreement_id (str, UUID)
        document     (str) — contract content or document string.
        signer       (str) — signer identifier or email.
    """
    namespace_id = await _check_agreements_enabled(engine, arguments)
    agreement_id = str(arguments.get("agreement_id") or "").strip()
    if not agreement_id:
        raise ValueError("agreement_id is required")
    document = arguments.get("document")
    if document is None:
        raise ValueError("document is required")
    signer = str(arguments.get("signer") or "").strip()
    if not signer:
        raise ValueError("signer is required")

    params: dict[str, Any] = {
        "namespace_id": namespace_id,
        "agreement_id": agreement_id,
        "document": document,
        "signer": signer,
    }
    result = await do_request_signature(engine, params)
    return json.dumps(result, default=str)


@mcp_handler
async def handle_agreements_record_signature(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: agreements_record_signature — record a completed agreement signature.

    Actor tool (mutation=True, admin_only=True).
    Performs tamper check on signed document, updates agreement state to SIGNED.

    Required arguments:
        namespace_id    (str, UUID)
        agreement_id    (str, UUID)
        session_id      (str) — signing session ID from request_signature.
        signed_document (str) — returned signed document content.
        signer          (str) — signer identifier.
    """
    namespace_id = await _check_agreements_enabled(engine, arguments)
    agreement_id = str(arguments.get("agreement_id") or "").strip()
    if not agreement_id:
        raise ValueError("agreement_id is required")
    session_id = str(arguments.get("session_id") or "").strip()
    if not session_id:
        raise ValueError("session_id is required")
    signed_document = arguments.get("signed_document")
    if signed_document is None:
        raise ValueError("signed_document is required")
    signer = str(arguments.get("signer") or "").strip()
    if not signer:
        raise ValueError("signer is required")

    params: dict[str, Any] = {
        "namespace_id": namespace_id,
        "agreement_id": agreement_id,
        "session_id": session_id,
        "signed_document": signed_document,
        "signer": signer,
    }
    result = await do_record_signature(engine, params)
    return json.dumps(result, default=str)


@mcp_handler
async def handle_agreements_review_extraction(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: agreements_review_extraction — human review of an extracted agreement.

    Operator tool (mutation=True, admin_only=True).
    Accepts/rejects/corrects extracted terms in the review queue.
    When decision is 'confirm', writes verified terms to graph and memories.

    Required arguments:
        namespace_id    (str, UUID)
        agreement_id    (str, UUID)
        decision        (str) — 'confirm' or 'reject'.
        reviewed_by     (str) — reviewer username/identifier.
    Optional arguments:
        corrected_terms (dict) — reviewer-corrected terms mapping.
    """
    namespace_id = await _check_agreements_enabled(engine, arguments)
    agreement_id = str(arguments.get("agreement_id") or "").strip()
    if not agreement_id:
        raise ValueError("agreement_id is required")
    decision = str(arguments.get("decision") or "").strip()
    if decision not in ("confirm", "reject"):
        raise ValueError("decision must be 'confirm' or 'reject'")
    reviewed_by = str(arguments.get("reviewed_by") or "").strip()
    if not reviewed_by:
        raise ValueError("reviewed_by is required")

    review_params: dict[str, Any] = {
        "namespace_id": namespace_id,
        "agreement_id": agreement_id,
        "decision": decision,
        "reviewed_by": reviewed_by,
    }
    if "corrected_terms" in arguments and isinstance(arguments["corrected_terms"], dict):
        review_params["corrected_terms"] = arguments["corrected_terms"]

    review_res = await do_review_extraction(engine, review_params)

    if decision == "confirm":
        await write_agreement_to_graph_and_memories(
            engine.pg_pool,
            namespace_id,
            agreement_id=UUID(agreement_id),
            source_doc_ref=review_res.get("source_doc_ref", "unknown"),
            extracted_data=review_res.get("extracted", {}),
        )

    return json.dumps(
        {"status": "ok", "agreement": review_res},
        default=str,
    )
