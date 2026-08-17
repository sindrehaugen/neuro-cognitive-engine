"""
Admin HTTP handlers for the Agreements vertical module.
======================================================
Covers:
  - api_agreements_list     — GET  /api/agreements/list
  - api_agreements_detail   — GET  /api/agreements/{id}
  - api_agreements_extract  — POST /api/agreements/extract
  - api_agreements_review   — POST /api/agreements/review
  - api_agreements_coverage — GET  /api/agreements/coverage
"""

from __future__ import annotations

import json
import logging
import uuid
from uuid import UUID

from starlette.responses import JSONResponse

from nce.admin_handlers._shared import (
    admin_error_response,
    admin_state,
    serialize_pg_row,
)
from nce.auth import validate_agent_id
from nce.db_utils import scoped_pg_session

# Guard lives in the vertical (dependencies point inward — B43 product pattern);
# re-exported here so existing importers of the web layer keep working.
from nce.vertical_modules.agreements._guard import (
    AgreementsDisabledError,
    require_agreements_enabled,
)
from nce.vertical_modules.agreements.coverage import do_coverage_matrix
from nce.vertical_modules.agreements.extract import do_extract_agreement
from nce.vertical_modules.agreements.graph import write_agreement_to_graph_and_memories
from nce.vertical_modules.agreements.review import do_review_extraction

__all__ = [
    "AgreementsDisabledError",
    "require_agreements_enabled",
    "api_agreements_list",
    "api_agreements_detail",
    "api_agreements_extract",
    "api_agreements_review",
    "api_agreements_coverage",
]

log = logging.getLogger("nce.admin_handlers.agreements")


async def _check_agreements_enabled_rest(namespace_id: str) -> JSONResponse | None:
    try:
        if admin_state.engine is None:
            return JSONResponse({"error": "Engine not connected"}, status_code=503)
        await require_agreements_enabled(admin_state.engine.pg_pool, namespace_id)
        return None
    except AgreementsDisabledError as exc:
        return JSONResponse(
            {"error": "Agreements vertical is not enabled for this namespace", "detail": str(exc)},
            status_code=409,
        )


async def api_agreements_list(request) -> JSONResponse:
    """GET /api/agreements/list"""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    namespace_id = request.query_params.get("namespace_id", "").strip()
    if not namespace_id:
        return JSONResponse({"error": "Missing namespace_id"}, status_code=422)

    # Validate the UUID shape at the REST boundary, BEFORE the opt-in gate:
    # `validate_agent_id` only sanitizes free text and never raises (see
    # nce/auth.py), so it cannot catch a malformed namespace_id. Without this
    # explicit check, `_check_agreements_enabled_rest` -> `require_agreements_enabled`
    # would hand the raw string to asyncpg's `::uuid` cast, which raises
    # asyncpg.exceptions.DataError (not ValueError) and escapes uncaught.
    namespace_id = validate_agent_id(namespace_id)
    try:
        UUID(namespace_id)
    except ValueError as exc:
        return JSONResponse({"error": f"Invalid namespace_id: {exc}"}, status_code=422)

    disabled = await _check_agreements_enabled_rest(namespace_id)
    if disabled is not None:
        return disabled

    try:
        async with scoped_pg_session(admin_state.engine.pg_pool, namespace_id) as conn:
            rows = await conn.fetch(
                """
                SELECT agreement_id, source_doc_ref, extraction_confidence, review_status, flagged_at, reviewed_by, reviewed_at
                FROM agreement_review_queue
                WHERE namespace_id = $1::uuid
                ORDER BY flagged_at DESC
                """,
                UUID(namespace_id),
            )
            items = [serialize_pg_row(r) for r in rows]
            for item in items:
                item["id"] = item["agreement_id"]

            # Compute KPI counts
            total = len(items)
            auto_green = sum(1 for i in items if i["review_status"] == "auto_green")
            needs_review = sum(1 for i in items if i["review_status"] == "needs_review_yellow")
            manual_red = sum(1 for i in items if i["review_status"] == "manual_red")

            kpis = {
                "total": total,
                "auto_green": auto_green,
                "needs_review": needs_review,
                "manual_red": manual_red,
            }

        return JSONResponse({"status": "ok", "items": items, "agreements": items, "kpis": kpis})
    except Exception as exc:
        return admin_error_response(
            "Agreements list error", exc, status_code=500, log_event="api_agreements_list"
        )


async def api_agreements_detail(request) -> JSONResponse:
    """GET /api/agreements/{id} or GET /api/agreements/detail"""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    namespace_id = request.query_params.get("namespace_id", "").strip()
    agreement_id_str = (
        request.path_params.get("id") or request.query_params.get("agreement_id", "").strip()
    )

    if not namespace_id:
        return JSONResponse({"error": "Missing namespace_id"}, status_code=422)
    if not agreement_id_str:
        return JSONResponse({"error": "Missing agreement_id"}, status_code=422)

    # See api_agreements_list: namespace_id must be UUID-checked explicitly
    # (validate_agent_id never raises) before the opt-in gate runs.
    namespace_id = validate_agent_id(namespace_id)
    try:
        UUID(namespace_id)
        agreement_id = UUID(agreement_id_str)
    except ValueError as exc:
        return JSONResponse({"error": f"Invalid ID format: {exc}"}, status_code=422)

    disabled = await _check_agreements_enabled_rest(namespace_id)
    if disabled is not None:
        return disabled

    try:
        async with scoped_pg_session(admin_state.engine.pg_pool, namespace_id) as conn:
            row = await conn.fetchrow(
                """
                SELECT agreement_id, source_doc_ref, extraction_confidence, review_status, extracted, flagged_at, reviewed_by, reviewed_at
                FROM agreement_review_queue
                WHERE agreement_id = $1 AND namespace_id = $2
                """,
                agreement_id,
                UUID(namespace_id),
            )
            if not row:
                return JSONResponse({"error": "Agreement not found"}, status_code=404)

            # Get kg_edges related to this agreement node
            agreement_label = f"Agreement:{agreement_id}"
            edges = await conn.fetch(
                """
                SELECT subject_label, predicate, object_label, confidence, agreements_source_id
                FROM kg_edges
                WHERE (subject_label = $1 OR object_label = $1) AND namespace_id = $2
                """,
                agreement_label,
                UUID(namespace_id),
            )
            serialized_edges = [serialize_pg_row(e) for e in edges]

            result = serialize_pg_row(row)
            result["id"] = result["agreement_id"]
            if isinstance(result["extracted"], str):
                result["extracted"] = json.loads(result["extracted"])

            result["graph_edges"] = serialized_edges

            terms = {}
            for edge in edges:
                if edge["predicate"] == "has_term" and edge["subject_label"] == agreement_label:
                    obj_label = edge["object_label"]
                    parts = obj_label.split(":")
                    if len(parts) >= 3:
                        term_type = parts[-1].lower()
                        terms[term_type] = {"confidence": float(edge["confidence"])}
            result["terms"] = terms

        return JSONResponse({"status": "ok", "agreement": result})
    except Exception as exc:
        return admin_error_response(
            "Agreement detail error", exc, status_code=500, log_event="api_agreements_detail"
        )


async def api_agreements_extract(request) -> JSONResponse:
    """POST /api/agreements/extract"""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        body = {}

    namespace_id = body.get("namespace_id", request.query_params.get("namespace_id", "")).strip()
    source_doc_ref = body.get(
        "source_doc_ref", request.query_params.get("source_doc_ref", "")
    ).strip()

    if not namespace_id:
        return JSONResponse({"error": "Missing namespace_id"}, status_code=422)
    if not source_doc_ref:
        return JSONResponse({"error": "Missing source_doc_ref"}, status_code=422)

    # See api_agreements_list: namespace_id must be UUID-checked explicitly
    # (validate_agent_id never raises) before the opt-in gate runs.
    namespace_id = validate_agent_id(namespace_id)
    try:
        UUID(namespace_id)
    except ValueError as exc:
        return JSONResponse({"error": f"Invalid namespace_id: {exc}"}, status_code=422)

    disabled = await _check_agreements_enabled_rest(namespace_id)
    if disabled is not None:
        return disabled

    try:
        # Call the core extraction engine
        extraction_res = await do_extract_agreement(
            admin_state.engine,
            {"namespace_id": namespace_id, "source_doc_ref": source_doc_ref},
        )

        agreement_id = uuid.uuid4()
        run_id = uuid.uuid4()

        # Compute overall confidence (average of field confidences)
        confidences = [
            v["extractionConfidence"]
            for v in extraction_res.values()
            if "extractionConfidence" in v
        ]
        overall_confidence = sum(confidences) / len(confidences) if confidences else 100.0

        # Determine overall status
        overall_status = "auto_green"
        if any(v.get("reviewStatus") == "manual_red" for v in extraction_res.values()):
            overall_status = "manual_red"
        elif any(v.get("reviewStatus") == "needs_review_yellow" for v in extraction_res.values()):
            overall_status = "needs_review_yellow"

        # Record run history
        async with scoped_pg_session(admin_state.engine.pg_pool, namespace_id) as conn:
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

            # Insert into review queue
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

        # If overall status is auto_green, write to graph and memories!
        if overall_status == "auto_green":
            await write_agreement_to_graph_and_memories(
                admin_state.engine.pg_pool,
                namespace_id,
                agreement_id=agreement_id,
                source_doc_ref=source_doc_ref,
                extracted_data=extraction_res,
            )

        return JSONResponse(
            {
                "status": "ok",
                "agreement_id": str(agreement_id),
                "review_status": overall_status,
                "extraction_confidence": overall_confidence,
                "extracted": extraction_res,
            }
        )
    except Exception as exc:
        return admin_error_response(
            "Agreement extract error", exc, status_code=500, log_event="api_agreements_extract"
        )


async def api_agreements_review(request) -> JSONResponse:
    """POST /api/agreements/review"""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        body = {}

    namespace_id = body.get("namespace_id", request.query_params.get("namespace_id", "")).strip()
    agreement_id_str = body.get("agreement_id", "").strip()
    decision = body.get("decision", "").strip()
    reviewed_by = body.get("reviewed_by", "").strip()
    corrected_terms = body.get("corrected_terms")

    if not namespace_id:
        return JSONResponse({"error": "Missing namespace_id"}, status_code=422)
    if not agreement_id_str:
        return JSONResponse({"error": "Missing agreement_id"}, status_code=422)
    if not decision:
        return JSONResponse({"error": "Missing decision"}, status_code=422)
    if not reviewed_by:
        return JSONResponse({"error": "Missing reviewed_by"}, status_code=422)

    # See api_agreements_list: namespace_id must be UUID-checked explicitly
    # (validate_agent_id never raises) before the opt-in gate runs.
    namespace_id = validate_agent_id(namespace_id)
    try:
        UUID(namespace_id)
        agreement_id = UUID(agreement_id_str)
    except ValueError as exc:
        return JSONResponse({"error": f"Invalid ID format: {exc}"}, status_code=422)

    disabled = await _check_agreements_enabled_rest(namespace_id)
    if disabled is not None:
        return disabled

    try:
        review_params = {
            "namespace_id": namespace_id,
            "agreement_id": agreement_id_str,
            "decision": decision,
            "reviewed_by": reviewed_by,
        }
        if corrected_terms is not None:
            review_params["corrected_terms"] = corrected_terms

        # Run core review function
        review_res = await do_review_extraction(admin_state.engine, review_params)

        # If decision is confirm, write the terms to the graph and memories
        if decision == "confirm":
            await write_agreement_to_graph_and_memories(
                admin_state.engine.pg_pool,
                namespace_id,
                agreement_id=agreement_id,
                source_doc_ref=review_res["source_doc_ref"],
                extracted_data=review_res["extracted"],
            )

        return JSONResponse({"status": "ok", "agreement": review_res})
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Agreement review error", exc, status_code=500, log_event="api_agreements_review"
        )


async def api_agreements_coverage(request) -> JSONResponse:
    """GET /api/agreements/coverage — coverage-matrix dashboard (M3.W5).

    Delegates to ``do_coverage_matrix`` (return-only; gracefully degrades to
    ``status="gl_unavailable"`` when the Economy GL seam is not built) and
    summarises the flags into per-type KPI counts for the dashboard.
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    namespace_id = request.query_params.get("namespace_id", "").strip()
    if not namespace_id:
        return JSONResponse({"error": "Missing namespace_id"}, status_code=422)

    # See api_agreements_list: namespace_id must be UUID-checked explicitly
    # (validate_agent_id never raises) before the opt-in gate runs.
    namespace_id = validate_agent_id(namespace_id)
    try:
        UUID(namespace_id)
    except ValueError as exc:
        return JSONResponse({"error": f"Invalid namespace_id: {exc}"}, status_code=422)

    disabled = await _check_agreements_enabled_rest(namespace_id)
    if disabled is not None:
        return disabled

    since_iso = request.query_params.get("since_iso", "").strip() or None

    try:
        result = await do_coverage_matrix(
            admin_state.engine,
            {"namespace_id": namespace_id, "since_iso": since_iso},
        )

        kpis = {"leakage": 0, "expiry": 0, "review": 0}
        for flag in result.get("flags", []):
            flag_type = flag.get("flag_type")
            if flag_type in kpis:
                kpis[flag_type] += 1

        return JSONResponse({"status": "ok", "coverage": result, "kpis": kpis})
    except Exception as exc:
        return admin_error_response(
            "Agreements coverage error", exc, status_code=500, log_event="api_agreements_coverage"
        )
