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
from typing import Any
from uuid import UUID

from starlette.responses import JSONResponse

from nce.admin_handlers._shared import (
    _json_safe,
    admin_error_response,
    admin_state,
    bump_mcp_cache_generation,
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
from nce.vertical_modules.agreements.authoring import (
    do_add_comment,
    do_create_agreement,
    do_suggest_revision,
)
from nce.vertical_modules.agreements.compliance import (
    do_run_compliance_audit,
    do_suggest_terms,
)
from nce.vertical_modules.agreements.coverage import do_coverage_matrix
from nce.vertical_modules.agreements.extract import do_extract_agreement
from nce.vertical_modules.agreements.graph import (
    do_upsert_agreement,
    write_agreement_to_graph_and_memories,
)
from nce.vertical_modules.agreements.kickback import do_reconcile_kickback
from nce.vertical_modules.agreements.review import do_review_extraction
from nce.vertical_modules.agreements.signing import (
    do_record_signature,
    do_request_signature,
)
from nce.vertical_modules.agreements.sla import do_set_sla_coverage

__all__ = [
    "AgreementsDisabledError",
    "require_agreements_enabled",
    "api_agreements_list",
    "api_agreements_detail",
    "api_agreements_extract",
    "api_agreements_review",
    "api_agreements_coverage",
    "api_agreements_reconcile",
    "api_agreements_create",
    "api_agreements_suggest_revision",
    "api_agreements_comment",
    "api_agreements_request_signature",
    "api_agreements_record_signature",
    "api_agreements_compliance_audit",
    "api_agreements_suggest_terms",
    "api_agreements_sla_coverage",
    "api_agreements_upsert",
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

        await bump_mcp_cache_generation(admin_state.engine, route="api_agreements_extract")
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

        await bump_mcp_cache_generation(admin_state.engine, route="api_agreements_review")
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


async def _extract_request_data(request) -> tuple[dict[str, Any], JSONResponse | None]:
    """Extract request payload from JSON body or query params."""
    data: dict[str, Any] = {}
    if hasattr(request, "json") and callable(request.json):
        try:
            body = await request.json()
            if isinstance(body, dict):
                data.update(body)
        except Exception:
            content_type = getattr(request, "headers", {}).get("content-type", "")
            if "application/json" in content_type:
                return {}, JSONResponse({"error": "Invalid JSON body"}, status_code=422)
    if hasattr(request, "query_params"):
        for k, v in request.query_params.items():
            if k not in data:
                data[k] = v
    return data, None


async def _resolve_namespace_id(data: dict[str, Any]) -> tuple[str | None, JSONResponse | None]:
    """Validate namespace_id and check if agreements vertical is enabled."""
    namespace_id_raw = data.get("namespace_id")
    if not namespace_id_raw:
        return None, JSONResponse({"error": "Missing namespace_id"}, status_code=422)
    namespace_id = validate_agent_id(str(namespace_id_raw).strip())
    try:
        UUID(namespace_id)
    except ValueError as exc:
        return None, JSONResponse({"error": f"Invalid namespace_id: {exc}"}, status_code=422)
    disabled = await _check_agreements_enabled_rest(namespace_id)
    if disabled is not None:
        return None, disabled
    return namespace_id, None


async def api_agreements_reconcile(request) -> JSONResponse:
    """POST /api/agreements/reconcile — compute supplier kickback reconciliation."""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)
    data, err = await _extract_request_data(request)
    if err:
        return err
    namespace_id, err = await _resolve_namespace_id(data)
    if err:
        return err
    data["namespace_id"] = namespace_id
    try:
        res = await do_reconcile_kickback(admin_state.engine, data)
        return JSONResponse({"status": "ok", **_json_safe(res)})
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Agreements reconcile error", exc, status_code=500, log_event="api_agreements_reconcile"
        )


async def api_agreements_create(request) -> JSONResponse:
    """POST /api/agreements/create — author a new agreement."""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)
    data, err = await _extract_request_data(request)
    if err:
        return err
    namespace_id, err = await _resolve_namespace_id(data)
    if err:
        return err
    data["namespace_id"] = namespace_id
    try:
        res = await do_create_agreement(admin_state.engine, data)
        await bump_mcp_cache_generation(admin_state.engine, route="api_agreements_create")
        return JSONResponse({"status": "ok", **_json_safe(res)})
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Agreements create error", exc, status_code=500, log_event="api_agreements_create"
        )


async def api_agreements_suggest_revision(request) -> JSONResponse:
    """POST /api/agreements/suggest-revision — propose revision to agreement clause."""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)
    data, err = await _extract_request_data(request)
    if err:
        return err
    namespace_id, err = await _resolve_namespace_id(data)
    if err:
        return err
    data["namespace_id"] = namespace_id
    try:
        res = await do_suggest_revision(admin_state.engine, data)
        await bump_mcp_cache_generation(admin_state.engine, route="api_agreements_suggest_revision")
        return JSONResponse({"status": "ok", **_json_safe(res)})
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Agreements suggest revision error",
            exc,
            status_code=500,
            log_event="api_agreements_suggest_revision",
        )


async def api_agreements_comment(request) -> JSONResponse:
    """POST /api/agreements/comment — record negotiation comment."""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)
    data, err = await _extract_request_data(request)
    if err:
        return err
    namespace_id, err = await _resolve_namespace_id(data)
    if err:
        return err
    data["namespace_id"] = namespace_id
    try:
        res = await do_add_comment(admin_state.engine, data)
        await bump_mcp_cache_generation(admin_state.engine, route="api_agreements_comment")
        return JSONResponse({"status": "ok", **_json_safe(res)})
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Agreements comment error", exc, status_code=500, log_event="api_agreements_comment"
        )


async def api_agreements_request_signature(request) -> JSONResponse:
    """POST /api/agreements/request-signature — dispatch signature request."""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)
    data, err = await _extract_request_data(request)
    if err:
        return err
    namespace_id, err = await _resolve_namespace_id(data)
    if err:
        return err
    data["namespace_id"] = namespace_id
    try:
        res = await do_request_signature(admin_state.engine, data)
        await bump_mcp_cache_generation(
            admin_state.engine, route="api_agreements_request_signature"
        )
        return JSONResponse({"status": "ok", **_json_safe(res)})
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Agreements request signature error",
            exc,
            status_code=500,
            log_event="api_agreements_request_signature",
        )


async def api_agreements_record_signature(request) -> JSONResponse:
    """POST /api/agreements/record-signature — record external signature."""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)
    data, err = await _extract_request_data(request)
    if err:
        return err
    namespace_id, err = await _resolve_namespace_id(data)
    if err:
        return err
    data["namespace_id"] = namespace_id
    try:
        res = await do_record_signature(admin_state.engine, data)
        await bump_mcp_cache_generation(admin_state.engine, route="api_agreements_record_signature")
        return JSONResponse({"status": "ok", **_json_safe(res)})
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Agreements record signature error",
            exc,
            status_code=500,
            log_event="api_agreements_record_signature",
        )


async def api_agreements_compliance_audit(request) -> JSONResponse:
    """POST /api/agreements/compliance-audit — execute compliance audit gate."""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)
    data, err = await _extract_request_data(request)
    if err:
        return err
    namespace_id, err = await _resolve_namespace_id(data)
    if err:
        return err
    data["namespace_id"] = namespace_id
    try:
        res = await do_run_compliance_audit(admin_state.engine, data)
        return JSONResponse({"status": "ok", **_json_safe(res)})
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Agreements compliance audit error",
            exc,
            status_code=500,
            log_event="api_agreements_compliance_audit",
        )


async def api_agreements_suggest_terms(request) -> JSONResponse:
    """POST /api/agreements/suggest-terms — suggest term adjustments versus benchmark."""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)
    data, err = await _extract_request_data(request)
    if err:
        return err
    namespace_id, err = await _resolve_namespace_id(data)
    if err:
        return err
    data["namespace_id"] = namespace_id
    try:
        res = await do_suggest_terms(admin_state.engine, data)
        await bump_mcp_cache_generation(admin_state.engine, route="api_agreements_suggest_terms")
        return JSONResponse({"status": "ok", **_json_safe(res)})
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Agreements suggest terms error",
            exc,
            status_code=500,
            log_event="api_agreements_suggest_terms",
        )


async def api_agreements_sla_coverage(request) -> JSONResponse:
    """POST /api/agreements/sla-coverage — set SLA coverage terms and functional location edge."""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)
    data, err = await _extract_request_data(request)
    if err:
        return err
    namespace_id, err = await _resolve_namespace_id(data)
    if err:
        return err
    data["namespace_id"] = namespace_id
    try:
        res = await do_set_sla_coverage(admin_state.engine, data)
        await bump_mcp_cache_generation(admin_state.engine, route="api_agreements_sla_coverage")
        return JSONResponse({"status": "ok", **_json_safe(res)})
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Agreements SLA coverage error",
            exc,
            status_code=500,
            log_event="api_agreements_sla_coverage",
        )


async def api_agreements_upsert(request) -> JSONResponse:
    """POST /api/agreements/upsert — upsert agreement graph node and memories."""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)
    data, err = await _extract_request_data(request)
    if err:
        return err
    namespace_id, err = await _resolve_namespace_id(data)
    if err:
        return err
    data["namespace_id"] = namespace_id
    try:
        res = await do_upsert_agreement(admin_state.engine, data)
        await bump_mcp_cache_generation(admin_state.engine, route="api_agreements_upsert")
        return JSONResponse({"status": "ok", **_json_safe(res)})
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Agreements upsert error", exc, status_code=500, log_event="api_agreements_upsert"
        )
