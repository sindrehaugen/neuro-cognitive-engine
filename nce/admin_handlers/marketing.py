"""
nce/admin_handlers/marketing.py
===============================
Admin HTTP handlers for Module 14: Marketing Engine (ML14-B6, marketing-advisor-aeo).

Exports:
  api_marketing_candidates          — GET  /api/marketing/candidates
  api_marketing_draft_case_study    — POST /api/marketing/draft
  api_marketing_testimonials        — GET  /api/marketing/testimonials
  api_marketing_capture_testimonial — POST /api/marketing/testimonials/capture
  api_marketing_suggest_content     — POST /api/marketing/suggest-content
  api_marketing_audit_seo           — POST /api/marketing/audit-seo
  api_marketing_approve_content     — POST /api/marketing/approve
  api_marketing_assets              — GET  /api/marketing/assets
  api_marketing_publish_content     — POST /api/marketing/publish

All mutating routes bump the MCP response cache via ``bump_mcp_cache_generation``.
All queries enforce explicit tenant isolation: WHERE namespace_id = $1.
All exception handlers strictly pass (message, exc) to ``admin_error_response``.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID, uuid4

from nce.admin_handlers._shared import (
    _MISSING_NAMESPACE_QUERY_PARAM,
    JSONResponse,
    _json_safe,
    _require_namespace_id,
    admin_error_response,
    admin_state,
    bump_mcp_cache_generation,
)
from nce.vertical_modules.marketing._guard import (
    MarketingConsentMissingError,
    MarketingDisabledError,
    MarketingLowHealthTriggerError,
    MarketingSensitiveDataLeakError,
    MarketingUnapprovedPublishError,
    MarketingUngroundedClaimError,
    require_marketing_enabled,
)
from nce.vertical_modules.marketing.advisor import (
    do_audit_seo,
    do_suggest_content,
)
from nce.vertical_modules.marketing.approval import do_approve_content
from nce.vertical_modules.marketing.candidates import do_find_case_study_candidates
from nce.vertical_modules.marketing.drafting import do_draft_case_study
from nce.vertical_modules.marketing.publish import do_publish_content
from nce.vertical_modules.marketing.testimonials import do_capture_testimonial

log = logging.getLogger("nce.admin_handlers.marketing")


def _extract_pool(engine: Any) -> Any:
    """Extract an asyncpg pool from the engine context."""
    if hasattr(engine, "pg_pool") and (
        "pg_pool" in getattr(engine, "__dict__", {}) or hasattr(type(engine), "pg_pool")
    ):
        return engine.pg_pool
    return engine


# ---------------------------------------------------------------------------
# GET /api/marketing/candidates
# ---------------------------------------------------------------------------


async def api_marketing_candidates(request: Any) -> JSONResponse:
    """GET /api/marketing/candidates — find case study worthy projects."""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    ns, err = _require_namespace_id(
        request.query_params.get("namespace_id"),
        missing_error=_MISSING_NAMESPACE_QUERY_PARAM,
    )
    if err is not None:
        return err

    pool = _extract_pool(admin_state.engine)
    try:
        await require_marketing_enabled(pool, ns)
    except MarketingDisabledError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)

    params: dict[str, Any] = {"namespace_id": ns}
    if "lookback_days" in request.query_params:
        try:
            params["lookback_days"] = int(request.query_params["lookback_days"])
        except ValueError:
            return JSONResponse({"error": "lookback_days must be an integer"}, status_code=422)

    if "min_outcome_score" in request.query_params:
        try:
            params["min_outcome_score"] = float(request.query_params["min_outcome_score"])
        except ValueError:
            return JSONResponse({"error": "min_outcome_score must be a float"}, status_code=422)

    if "limit" in request.query_params:
        try:
            params["limit"] = int(request.query_params["limit"])
        except ValueError:
            return JSONResponse({"error": "limit must be an integer"}, status_code=422)

    try:
        result = await do_find_case_study_candidates(admin_state.engine, params)
        return JSONResponse(_json_safe(result), status_code=200)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response("Failed to find case study candidates", exc)


# ---------------------------------------------------------------------------
# POST /api/marketing/draft
# ---------------------------------------------------------------------------


async def api_marketing_draft_case_study(request: Any) -> JSONResponse:
    """POST /api/marketing/draft — generate a case study draft from a project."""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=422)

    if not isinstance(body, dict):
        return JSONResponse({"error": "Request body must be a JSON object"}, status_code=422)

    ns, err = _require_namespace_id(body.get("namespace_id"))
    if err is not None:
        return err

    pool = _extract_pool(admin_state.engine)
    try:
        await require_marketing_enabled(pool, ns)
    except MarketingDisabledError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)

    project_id = str(body.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse({"error": "project_id is required"}, status_code=422)

    anonymize = bool(body.get("anonymize", True))

    try:
        draft = do_draft_case_study(body, anonymize=anonymize)

        # Persist draft to case_studies table if database is connected
        if pool is not None:
            try:
                study_id = uuid4()
                async with pool.acquire() as conn:
                    await conn.execute(
                        """
                        INSERT INTO case_studies (
                            id, namespace_id, project_id, title, body,
                            status, anonymized, marketing_source_id, raw
                        ) VALUES (
                            $1::uuid, $2::uuid, $3, $4, $5,
                            'draft', $6, $7, $8::jsonb
                        )
                        """,
                        study_id,
                        UUID(ns),
                        project_id,
                        draft.get("title", "Draft Case Study"),
                        json.dumps(draft),
                        anonymize,
                        draft.get("marketing_source_id"),
                        json.dumps(draft),
                    )
                draft["id"] = str(study_id)
            except Exception as exc:
                log.warning("api_marketing_draft_case_study DB save warning: %s", exc)

        from nce.vertical_modules.marketing.events import (
            EVENT_MARKETING_CASE_STUDY_DRAFTED,
            emit_marketing_event,
        )

        await emit_marketing_event(
            admin_state.engine,
            ns,
            EVENT_MARKETING_CASE_STUDY_DRAFTED,
            {"project_id": project_id},
        )

        await bump_mcp_cache_generation(admin_state.engine, route="api_marketing_draft_case_study")
        return JSONResponse(_json_safe(draft), status_code=200)
    except (
        MarketingSensitiveDataLeakError,
        MarketingUngroundedClaimError,
        ValueError,
    ) as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response("Failed to generate case study draft", exc)


# ---------------------------------------------------------------------------
# GET /api/marketing/testimonials
# ---------------------------------------------------------------------------


async def api_marketing_testimonials(request: Any) -> JSONResponse:
    """GET /api/marketing/testimonials — list customer testimonials."""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    ns, err = _require_namespace_id(
        request.query_params.get("namespace_id"),
        missing_error=_MISSING_NAMESPACE_QUERY_PARAM,
    )
    if err is not None:
        return err

    pool = _extract_pool(admin_state.engine)
    try:
        await require_marketing_enabled(pool, ns)
    except MarketingDisabledError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)

    status_filter = request.query_params.get("status")
    customer_id = request.query_params.get("customer_id")
    limit = min(max(int(request.query_params.get("limit") or 50), 1), 200)
    offset = max(int(request.query_params.get("offset") or 0), 0)

    items: list[dict[str, Any]] = []
    total = 0

    if pool is not None:
        try:
            async with pool.acquire() as conn:
                conditions = ["namespace_id = $1::uuid"]
                binds: list[Any] = [UUID(ns)]

                if status_filter:
                    binds.append(status_filter)
                    conditions.append(f"status = ${len(binds)}")

                if customer_id:
                    binds.append(customer_id)
                    conditions.append(f"customer_id = ${len(binds)}")

                where_sql = " AND ".join(conditions)

                count_row = await conn.fetchrow(
                    f"SELECT count(*) AS total FROM testimonials WHERE {where_sql}",
                    *binds,
                )
                total = count_row["total"] if count_row else 0

                binds.append(limit)
                limit_idx = len(binds)
                binds.append(offset)
                offset_idx = len(binds)

                rows = await conn.fetch(
                    f"""
                    SELECT id, namespace_id, customer_id, project_id, quote,
                           status, consent, consent_tier, consent_scope,
                           consent_recorded_at, nps_at_capture, marketing_source_id,
                           created_at, updated_at
                    FROM   testimonials
                    WHERE  {where_sql}
                    ORDER  BY created_at DESC
                    LIMIT  ${limit_idx} OFFSET ${offset_idx}
                    """,
                    *binds,
                )
                items = [dict(r) for r in rows]
        except Exception as exc:
            return admin_error_response("Failed to query testimonials", exc)

    return JSONResponse(
        _json_safe({"ok": True, "items": items, "total": total, "limit": limit, "offset": offset}),
        status_code=200,
    )


# ---------------------------------------------------------------------------
# POST /api/marketing/testimonials/capture
# ---------------------------------------------------------------------------


async def api_marketing_capture_testimonial(request: Any) -> JSONResponse:
    """POST /api/marketing/testimonials/capture — record customer quote with consent."""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=422)

    if not isinstance(body, dict):
        return JSONResponse({"error": "Request body must be a JSON object"}, status_code=422)

    ns, err = _require_namespace_id(body.get("namespace_id"))
    if err is not None:
        return err

    pool = _extract_pool(admin_state.engine)
    try:
        await require_marketing_enabled(pool, ns)
    except MarketingDisabledError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)

    try:
        result = await do_capture_testimonial(admin_state.engine, body)
        await bump_mcp_cache_generation(
            admin_state.engine, route="api_marketing_capture_testimonial"
        )
        return JSONResponse(_json_safe(result), status_code=200)
    except MarketingConsentMissingError as exc:
        return JSONResponse({"error": str(exc)}, status_code=403)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response("Failed to capture testimonial", exc)


# ---------------------------------------------------------------------------
# POST /api/marketing/suggest-content
# ---------------------------------------------------------------------------


async def api_marketing_suggest_content(request: Any) -> JSONResponse:
    """POST /api/marketing/suggest-content — suggest thought-leadership / drip ideas."""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=422)

    if not isinstance(body, dict):
        return JSONResponse({"error": "Request body must be a JSON object"}, status_code=422)

    ns, err = _require_namespace_id(body.get("namespace_id"))
    if err is not None:
        return err

    pool = _extract_pool(admin_state.engine)
    try:
        await require_marketing_enabled(pool, ns)
    except MarketingDisabledError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)

    try:
        result = await do_suggest_content(admin_state.engine, body)
        return JSONResponse(_json_safe(result), status_code=200)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response("Failed to suggest marketing content", exc)


# ---------------------------------------------------------------------------
# POST /api/marketing/audit-seo
# ---------------------------------------------------------------------------


async def api_marketing_audit_seo(request: Any) -> JSONResponse:
    """POST /api/marketing/audit-seo — AEO/GEO audit and Schema.org JSON-LD."""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=422)

    if not isinstance(body, dict):
        return JSONResponse({"error": "Request body must be a JSON object"}, status_code=422)

    ns, err = _require_namespace_id(body.get("namespace_id"))
    if err is not None:
        return err

    pool = _extract_pool(admin_state.engine)
    try:
        await require_marketing_enabled(pool, ns)
    except MarketingDisabledError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)

    try:
        result = await do_audit_seo(admin_state.engine, body)
        if body.get("asset_id"):
            await bump_mcp_cache_generation(admin_state.engine, route="api_marketing_audit_seo")
        return JSONResponse(_json_safe(result), status_code=200)
    except MarketingSensitiveDataLeakError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response("Failed to audit content SEO", exc)


# ---------------------------------------------------------------------------
# POST /api/marketing/approve
# ---------------------------------------------------------------------------


async def api_marketing_approve_content(request: Any) -> JSONResponse:
    """POST /api/marketing/approve — human approval gate for marketing content."""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=422)

    if not isinstance(body, dict):
        return JSONResponse({"error": "Request body must be a JSON object"}, status_code=422)

    ns, err = _require_namespace_id(body.get("namespace_id"))
    if err is not None:
        return err

    pool = _extract_pool(admin_state.engine)
    try:
        await require_marketing_enabled(pool, ns)
    except MarketingDisabledError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)

    try:
        result = await do_approve_content(admin_state.engine, body)
        await bump_mcp_cache_generation(admin_state.engine, route="api_marketing_approve_content")
        return JSONResponse(_json_safe(result), status_code=200)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response("Failed to approve marketing content", exc)


# ---------------------------------------------------------------------------
# GET /api/marketing/assets
# ---------------------------------------------------------------------------


async def api_marketing_assets(request: Any) -> JSONResponse:
    """GET /api/marketing/assets — brand asset and content library listing."""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    ns, err = _require_namespace_id(
        request.query_params.get("namespace_id"),
        missing_error=_MISSING_NAMESPACE_QUERY_PARAM,
    )
    if err is not None:
        return err

    pool = _extract_pool(admin_state.engine)
    try:
        await require_marketing_enabled(pool, ns)
    except MarketingDisabledError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)

    kind_filter = request.query_params.get("kind")
    status_filter = request.query_params.get("status")
    limit = min(max(int(request.query_params.get("limit") or 50), 1), 200)
    offset = max(int(request.query_params.get("offset") or 0), 0)

    items: list[dict[str, Any]] = []
    total = 0

    if pool is not None:
        try:
            async with pool.acquire() as conn:
                conditions = ["namespace_id = $1::uuid"]
                binds: list[Any] = [UUID(ns)]

                if kind_filter:
                    binds.append(kind_filter)
                    conditions.append(f"kind = ${len(binds)}")

                if status_filter:
                    binds.append(status_filter)
                    conditions.append(f"status = ${len(binds)}")

                where_sql = " AND ".join(conditions)

                count_row = await conn.fetchrow(
                    f"SELECT count(*) AS total FROM content_assets WHERE {where_sql}",
                    *binds,
                )
                total = count_row["total"] if count_row else 0

                binds.append(limit)
                limit_idx = len(binds)
                binds.append(offset)
                offset_idx = len(binds)

                rows = await conn.fetch(
                    f"""
                    SELECT id, namespace_id, kind, ref_id, title,
                           seo, storage_uri, status, marketing_source_id,
                           created_at, updated_at
                    FROM   content_assets
                    WHERE  {where_sql}
                    ORDER  BY created_at DESC
                    LIMIT  ${limit_idx} OFFSET ${offset_idx}
                    """,
                    *binds,
                )
                items = [dict(r) for r in rows]
        except Exception as exc:
            return admin_error_response("Failed to query marketing assets", exc)

    return JSONResponse(
        _json_safe({"ok": True, "items": items, "total": total, "limit": limit, "offset": offset}),
        status_code=200,
    )


# ---------------------------------------------------------------------------
# POST /api/marketing/publish
# ---------------------------------------------------------------------------


async def api_marketing_publish_content(request: Any) -> JSONResponse:
    """POST /api/marketing/publish — publish approved marketing content."""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=422)

    if not isinstance(body, dict):
        return JSONResponse({"error": "Request body must be a JSON object"}, status_code=422)

    ns, err = _require_namespace_id(body.get("namespace_id"))
    if err is not None:
        return err

    pool = _extract_pool(admin_state.engine)
    try:
        await require_marketing_enabled(pool, ns)
    except MarketingDisabledError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)

    try:
        result = await do_publish_content(admin_state.engine, body)
        await bump_mcp_cache_generation(admin_state.engine, route="api_marketing_publish_content")
        return JSONResponse(_json_safe(result), status_code=200)
    except MarketingUnapprovedPublishError as exc:
        return JSONResponse({"error": str(exc)}, status_code=403)
    except MarketingConsentMissingError as exc:
        return JSONResponse({"error": str(exc)}, status_code=403)
    except MarketingLowHealthTriggerError as exc:
        return JSONResponse({"error": str(exc)}, status_code=403)
    except MarketingSensitiveDataLeakError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except NotImplementedError as exc:
        return JSONResponse({"error": str(exc)}, status_code=501)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response("Failed to publish marketing content", exc)
