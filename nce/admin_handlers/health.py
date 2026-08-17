from __future__ import annotations

from nce.admin_handlers import _shared
from nce.admin_handlers._shared import *  # noqa: F403


async def get_health(request):
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    health = await admin_state.engine.check_health()

    # Check nested database statuses for any "down" components
    db_health = health.get("databases", {})
    if any(status == "down" for status in db_health.values()):
        await dispatcher.dispatch_alert(
            "Database Health Alert", f"Current health: {json.dumps(health)}"
        )

    return JSONResponse(health)


async def get_health_v1(request):
    """GET /api/health/v1 — deprecated alias, returns same data as /api/health."""
    return await get_health(request)


async def trigger_gc(request):
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        result = await admin_state.engine.force_gc()
        return JSONResponse({"status": "success", "result": result})
    except Exception as e:
        return admin_error_response(
            "Garbage collection failed", e, status_code=500, log_event="trigger_gc"
        )


async def api_search(request):
    """POST /api/search — unified semantic search with optional temporal filter.

    Request body (JSON):
        namespace_id  str   required
        agent_id      str   required
        query         str   required
        top_k         int   optional, default 5
        as_of         str   optional ISO 8601 UTC timestamp for time-travel reads
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError, TypeError):
        return JSONResponse({"error": "Request body must be valid JSON"}, status_code=400)

    missing = [f for f in ("namespace_id", "agent_id", "query") if not body.get(f)]
    if missing:
        return JSONResponse(
            {"error": f"Missing required fields: {', '.join(missing)}"},
            status_code=422,
        )

    try:
        as_of_dt = parse_as_of(body.get("as_of"))
    except ValueError as exc:
        return admin_validation_error(exc, status_code=422)

    from nce import quotas as _quotas

    try:
        q_res = await _quotas.consume_for_tool(
            admin_state.engine.pg_pool,
            "api_semantic_search",
            body,
            redis_client=admin_state.engine.redis_client,
        )
    except _quotas.QuotaExceededError as exc:
        return admin_client_error(str(exc), status_code=429)

    try:
        results = await admin_state.engine.semantic_search(
            namespace_id=body["namespace_id"],
            agent_id=body["agent_id"],
            query=body["query"],
            limit=int(body.get("limit", body.get("top_k", 5))),
            offset=int(body.get("offset", 0)),
            as_of=as_of_dt,
        )
        return JSONResponse({"results": results})
    except Exception as exc:
        await q_res.rollback()
        _shared.logger.error("api_search failed: %s", exc)
        return admin_error_response("Search failed", exc, status_code=500)


async def serve_index(request):
    index_path = os.path.join(os.path.dirname(__file__), "admin", "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return HTMLResponse("Admin UI not found", status_code=404)


async def serve_styles(request):
    """GET /styles.css — serve the admin dashboard stylesheet."""
    styles_path = os.path.join(os.path.dirname(__file__), "admin", "styles.css")
    if os.path.exists(styles_path):
        return FileResponse(styles_path, media_type="text/css")
    return HTMLResponse("styles.css not found", status_code=404)
