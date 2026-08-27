from __future__ import annotations

import re
from typing import Any

from nce.admin_handlers import _shared
from nce.admin_handlers._shared import *  # noqa: F403


async def api_admin_events(request):
    """GET /api/admin/events

    Query params:
      namespace_id?, event_type?, agent_id?, from?, to?,
      event_seq_gte?, event_seq_lte?,
      include_details=1 (optional: params, result_summary, llm_payload_uri),
      page=1, limit=50
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        qp = request.query_params
        namespace_id = parse_optional_uuid(qp.get("namespace_id"))
        event_type_raw, et_err = sanitize_event_type_filter(qp.get("event_type"))
        if et_err:
            return JSONResponse({"error": et_err}, status_code=422)
        agent_id, agent_err = sanitize_optional_agent_filter(qp.get("agent_id"))
        if agent_err:
            return JSONResponse({"error": agent_err}, status_code=422)
        from_dt = parse_as_of(qp.get("from"))
        to_dt = parse_as_of(qp.get("to"))
        seq_gte, sg_err = parse_optional_bigint_bounds(
            qp.get("event_seq_gte"), label="event_seq_gte"
        )
        if sg_err:
            return JSONResponse({"error": sg_err}, status_code=422)
        seq_lte, sl_err = parse_optional_bigint_bounds(
            qp.get("event_seq_lte"), label="event_seq_lte"
        )
        if sl_err:
            return JSONResponse({"error": sl_err}, status_code=422)
        page, limit = parse_page_limit_common(qp)
        offset = offset_from_page_limit(page, limit)
    except ValueError as exc:
        return admin_validation_error(exc, status_code=422)

    event_type = event_type_raw

    where = []
    args: list[object] = []
    i = 1
    if namespace_id:
        where.append(f"namespace_id = ${i}")
        args.append(namespace_id)
        i += 1
    if event_type:
        where.append(f"event_type = ${i}")
        args.append(event_type)
        i += 1
    if agent_id:
        where.append(f"agent_id = ${i}")
        args.append(agent_id)
        i += 1
    if from_dt:
        where.append(f"occurred_at >= ${i}")
        args.append(from_dt)
        i += 1
    if to_dt:
        where.append(f"occurred_at <= ${i}")
        args.append(to_dt)
        i += 1
    if seq_gte is not None:
        where.append(f"event_seq >= ${i}")
        args.append(seq_gte)
        i += 1
    if seq_lte is not None:
        where.append(f"event_seq <= ${i}")
        args.append(seq_lte)
        i += 1

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    count_sql = f"SELECT COUNT(*)::bigint AS total FROM event_log {where_sql}"
    include_details = (qp.get("include_details") or "").lower() in (
        "1",
        "true",
        "yes",
    )
    extra_cols = ""
    if include_details:
        extra_cols = ", params, result_summary, llm_payload_uri"

    items_sql = f"""
        SELECT id, namespace_id, agent_id, event_type, event_seq, occurred_at, parent_event_id
        {extra_cols}
        FROM event_log
        {where_sql}
        ORDER BY occurred_at DESC
        LIMIT ${i} OFFSET ${i + 1}
    """

    try:
        async with admin_state.engine.pg_pool.acquire(timeout=10.0) as conn:
            count_row = await conn.fetchrow(count_sql, *args)
            rows = await conn.fetch(items_sql, *args, limit, offset)
    except Exception as exc:
        return admin_error_response("Failed to query events", exc, status_code=500)

    def _jsonish(val: Any) -> Any:
        if val is None:
            return None
        if isinstance(val, (dict, list)):
            return val
        if isinstance(val, str):
            try:
                return json.loads(val)
            except (json.JSONDecodeError, TypeError):
                return val
        return val

    items = []
    for r in rows:
        row_dict: dict[str, Any] = {
            "id": str(r["id"]),
            "namespace_id": str(r["namespace_id"]),
            "agent_id": r["agent_id"],
            "event_type": r["event_type"],
            "event_seq": r["event_seq"],
            "occurred_at": r["occurred_at"].astimezone(UTC).isoformat(),
            "parent_event_id": (str(r["parent_event_id"]) if r["parent_event_id"] else None),
        }
        if include_details:
            row_dict["params"] = _jsonish(r["params"])
            row_dict["result_summary"] = _jsonish(r["result_summary"])
            uri = r["llm_payload_uri"]
            row_dict["llm_payload_uri"] = str(uri) if uri else None
        items.append(row_dict)
    return JSONResponse(
        {
            "items": items,
            "page": page,
            "limit": limit,
            "total": int(count_row["total"]) if count_row else 0,
        }
    )


async def api_admin_events_summary(request):
    """GET /api/admin/events/summary

    Query params:
      namespace_id?, from?, to?
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        namespace_id = parse_optional_uuid(request.query_params.get("namespace_id"))
        from_dt = parse_as_of(request.query_params.get("from"))
        to_dt = parse_as_of(request.query_params.get("to"))
    except ValueError as exc:
        return admin_validation_error(exc, status_code=422)

    where = []
    args: list[object] = []
    i = 1
    if namespace_id:
        where.append(f"namespace_id = ${i}")
        args.append(namespace_id)
        i += 1
    if from_dt:
        where.append(f"occurred_at >= ${i}")
        args.append(from_dt)
        i += 1
    if to_dt:
        where.append(f"occurred_at <= ${i}")
        args.append(to_dt)
        i += 1
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""

    try:
        async with admin_state.engine.pg_pool.acquire(timeout=10.0) as conn:
            total_row = await conn.fetchrow(
                f"SELECT COUNT(*)::bigint AS total, MAX(occurred_at) AS latest FROM event_log {where_sql}",
                *args,
            )
            by_type_rows = await conn.fetch(
                f"SELECT event_type, COUNT(*)::bigint AS c FROM event_log {where_sql} GROUP BY event_type ORDER BY c DESC",
                *args,
            )
            by_ns_rows = await conn.fetch(
                f"SELECT namespace_id, COUNT(*)::bigint AS c FROM event_log {where_sql} GROUP BY namespace_id ORDER BY c DESC LIMIT 20",
                *args,
            )
            replay_failed = await conn.fetchval(
                "SELECT COUNT(*)::bigint FROM replay_runs WHERE status = 'failed'"
            )
    except Exception as exc:
        return admin_error_response("Failed to summarize events", exc, status_code=500)

    return JSONResponse(
        {
            "total_events": int(total_row["total"]) if total_row else 0,
            "latest_occurred_at": (
                total_row["latest"].astimezone(UTC).isoformat()
                if total_row and total_row["latest"] is not None
                else None
            ),
            "replay_failed_runs": int(replay_failed or 0),
            "by_event_type": {r["event_type"]: int(r["c"]) for r in by_type_rows},
            "by_namespace": {str(r["namespace_id"]): int(r["c"]) for r in by_ns_rows},
        }
    )


async def api_admin_verify_chain(request):
    """GET /api/admin/verify-chain/{namespace_id}

    Verify the Merkle hash chain for the given namespace.
    Recomputes chain_hash for every event in sequence order and
    reports the first break (if any).
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    raw_ns = request.path_params.get("namespace_id")
    try:
        namespace_id = uuid.UUID(raw_ns) if raw_ns else None
    except ValueError:
        return JSONResponse({"error": "Invalid namespace_id"}, status_code=422)

    if namespace_id is None:
        return JSONResponse({"error": "namespace_id required"}, status_code=422)

    try:
        async with admin_state.engine.pg_pool.acquire(timeout=10.0) as conn:
            result = await verify_merkle_chain(conn, namespace_id=namespace_id)
    except Exception as exc:
        return admin_error_response(
            "Failed to verify chain",
            exc,
            log_event="api_admin_verify_chain failed",
        )

    valid = bool(result.get("valid"))
    MERKLE_CHAIN_VALID.set(1 if valid else 0)

    return JSONResponse(
        {
            "valid": valid,
            "checked": result.get("checked", 0),
            "first_break": result.get("first_break"),
            "last_verified_seq": result.get("last_verified_seq", 0),
        }
    )


async def api_admin_a2a_grants(request):
    """GET /api/admin/a2a/grants

    Query params:
      owner_namespace_id?, status?, target_namespace_id?, page=1, limit=50
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        owner_namespace_id = parse_optional_uuid(request.query_params.get("owner_namespace_id"))
        target_namespace_id = parse_optional_uuid(request.query_params.get("target_namespace_id"))
        status = request.query_params.get("status")
        if status and status not in ("active", "revoked", "expired"):
            return JSONResponse({"error": "status must be active|revoked|expired"}, status_code=422)
        qp = request.query_params
        page, limit = parse_page_limit_common(qp)
        offset = offset_from_page_limit(page, limit)
    except ValueError as exc:
        return admin_validation_error(exc, status_code=422)

    where = []
    args: list[object] = []
    i = 1
    if owner_namespace_id:
        where.append(f"owner_namespace_id = ${i}")
        args.append(owner_namespace_id)
        i += 1
    if status:
        where.append(f"status = ${i}")
        args.append(status)
        i += 1
    if target_namespace_id:
        where.append(f"target_namespace_id = ${i}")
        args.append(target_namespace_id)
        i += 1
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""

    offset = (page - 1) * limit
    count_sql = f"SELECT COUNT(*)::bigint AS total FROM a2a_grants {where_sql}"
    items_sql = f"""
        SELECT id, owner_namespace_id, owner_agent_id, target_namespace_id,
               target_agent_id, scopes, status, expires_at, created_at
        FROM a2a_grants
        {where_sql}
        ORDER BY created_at DESC
        LIMIT ${i} OFFSET ${i + 1}
    """

    try:
        async with admin_state.engine.pg_pool.acquire(timeout=10.0) as conn:
            count_row = await conn.fetchrow(count_sql, *args)
            rows = await conn.fetch(items_sql, *args, limit, offset)
    except Exception as exc:
        return admin_error_response("Failed to query A2A grants", exc, status_code=500)

    items = [
        {
            "grant_id": str(r["id"]),
            "owner_namespace_id": str(r["owner_namespace_id"]),
            "owner_agent_id": r["owner_agent_id"],
            "target_namespace_id": (
                str(r["target_namespace_id"]) if r["target_namespace_id"] else None
            ),
            "target_agent_id": r["target_agent_id"],
            "scopes": (json.loads(r["scopes"]) if isinstance(r["scopes"], str) else r["scopes"]),
            "status": r["status"],
            "expires_at": r["expires_at"].astimezone(UTC).isoformat(),
            "created_at": r["created_at"].astimezone(UTC).isoformat(),
        }
        for r in rows
    ]
    return JSONResponse(
        {
            "items": items,
            "page": page,
            "limit": limit,
            "total": int(count_row["total"]) if count_row else 0,
        }
    )


async def api_admin_a2a_grants_summary(request):
    """GET /api/admin/a2a/grants/summary

    Query params:
      owner_namespace_id?
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        owner_namespace_id = parse_optional_uuid(request.query_params.get("owner_namespace_id"))
    except ValueError as exc:
        return admin_validation_error(exc, status_code=422)

    where_sql = "WHERE owner_namespace_id = $1" if owner_namespace_id else ""
    args: list[object] = [owner_namespace_id] if owner_namespace_id else []
    expiring_where = (
        "WHERE owner_namespace_id = $1 AND status = 'active' AND expires_at <= now() + interval '24 hours'"
        if owner_namespace_id
        else "WHERE status = 'active' AND expires_at <= now() + interval '24 hours'"
    )

    try:
        async with admin_state.engine.pg_pool.acquire(timeout=10.0) as conn:
            rows = await conn.fetch(
                f"SELECT status, COUNT(*)::bigint AS c FROM a2a_grants {where_sql} GROUP BY status",
                *args,
            )
            expiring_24h = await conn.fetchval(
                f"SELECT COUNT(*)::bigint FROM a2a_grants {expiring_where}",
                *args,
            )
    except Exception as exc:
        return admin_error_response(
            "Failed to summarize A2A grants",
            exc,
            log_event="api_admin_a2a_grants_summary failed",
        )

    status_counts = {r["status"]: int(r["c"]) for r in rows}
    return JSONResponse(
        {
            "active": status_counts.get("active", 0),
            "revoked": status_counts.get("revoked", 0),
            "expired": status_counts.get("expired", 0),
            "expiring_24h": int(expiring_24h or 0),
        }
    )


async def api_admin_a2a_revoke_grant(request):
    """POST /api/admin/a2a/grants/{grant_id}/revoke

    Admin revoke endpoint. Requires owner namespace context in request body.
    """
    return await api_a2a_revoke_grant(request)


async def api_admin_quotas(request):
    """GET /api/admin/quotas

    Query params:
      namespace_id?, resource_type?, window=day,
      page=1, limit=50
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    qp = request.query_params
    try:
        namespace_id = parse_optional_uuid(qp.get("namespace_id"))
    except ValueError as exc:
        return admin_validation_error(exc, status_code=422)

    res_type_raw, rt_err = sanitize_resource_type_filter(qp.get("resource_type"))
    if rt_err:
        return JSONResponse({"error": rt_err}, status_code=422)

    try:
        page, limit = parse_page_limit_common(qp)
        offset = offset_from_page_limit(page, limit)
    except ValueError as exc:
        return admin_validation_error(exc, status_code=422)

    window = qp.get("window", "day")
    if window not in ("hour", "day", "month"):
        return JSONResponse({"error": "window must be hour|day|month"}, status_code=422)

    where_fragments: list[str] = []
    args: list[object] = []
    idx = 1
    if namespace_id:
        where_fragments.append(f"namespace_id = ${idx}")
        args.append(namespace_id)
        idx += 1
    if res_type_raw:
        where_fragments.append(f"resource_type = ${idx}")
        args.append(res_type_raw)
        idx += 1
    where_sql = f"WHERE {' AND '.join(where_fragments)}" if where_fragments else ""

    now = datetime.now(UTC)
    if window == "hour":
        cutoff = now.replace(minute=0, second=0, microsecond=0)
    elif window == "day":
        cutoff = now.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        cutoff = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    count_sql = f"SELECT COUNT(*)::bigint AS total FROM resource_quotas {where_sql}"
    sums_sql = f"""
        SELECT COALESCE(SUM(used_amount), 0)::bigint AS used_sum,
               COALESCE(SUM(limit_amount), 0)::bigint AS limit_sum
        FROM resource_quotas
        {where_sql}
    """
    items_sql = f"""
        SELECT namespace_id, agent_id, resource_type, limit_amount, used_amount, reset_at, updated_at
        FROM resource_quotas
        {where_sql}
        ORDER BY namespace_id, agent_id NULLS FIRST, resource_type
        LIMIT ${idx} OFFSET ${idx + 1}
    """

    try:
        async with admin_state.engine.pg_pool.acquire(timeout=10.0) as conn:
            count_row = await conn.fetchrow(count_sql, *args)
            sums_row = await conn.fetchrow(sums_sql, *args)
            rows = await conn.fetch(items_sql, *args, limit, offset)
    except Exception as exc:
        return admin_error_response("Failed to query quotas", exc, status_code=500)

    total_used = int(sums_row["used_sum"]) if sums_row else 0
    total_limit = int(sums_row["limit_sum"]) if sums_row else 0

    tools = []
    for r in rows:
        used = int(r["used_amount"])
        limit_amount = int(r["limit_amount"])
        remaining = max(0, limit_amount - used)
        tools.append(
            {
                "namespace_id": str(r["namespace_id"]),
                "agent_id": r["agent_id"],
                "resource_type": r["resource_type"],
                "used": used,
                "limit": limit_amount,
                "remaining": remaining,
                "reset_at": (r["reset_at"].astimezone(UTC).isoformat() if r["reset_at"] else None),
                "updated_at": (
                    r["updated_at"].astimezone(UTC).isoformat() if r["updated_at"] else None
                ),
                "window_start": cutoff.isoformat(),
            }
        )

    return JSONResponse(
        {
            "tools": tools,
            "page": page,
            "limit": limit,
            "total": int(count_row["total"]) if count_row else 0,
            "totals": {
                "used": total_used,
                "limit": total_limit,
                "utilization_pct": (
                    round((total_used / total_limit * 100.0), 2) if total_limit > 0 else 0.0
                ),
            },
        }
    )


async def api_admin_quotas_summary(request):
    """GET /api/admin/quotas/summary

    Query params:
      namespace_id?
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        namespace_id = parse_optional_uuid(request.query_params.get("namespace_id"))
    except ValueError as exc:
        return admin_validation_error(exc, status_code=422)

    where_sql = "WHERE namespace_id = $1" if namespace_id else ""
    args: list[object] = [namespace_id] if namespace_id else []

    try:
        async with admin_state.engine.pg_pool.acquire(timeout=10.0) as conn:
            top_rows = await conn.fetch(
                f"""
                SELECT namespace_id, resource_type, used_amount, limit_amount
                FROM resource_quotas
                {where_sql}
                ORDER BY (CASE WHEN limit_amount = 0 THEN 0 ELSE used_amount::float / limit_amount END) DESC
                LIMIT 20
                """,
                *args,
            )
    except Exception as exc:
        return admin_error_response("Failed to summarize quotas", exc, status_code=500)

    near_limit = []
    for r in top_rows:
        limit_amount = int(r["limit_amount"])
        used = int(r["used_amount"])
        utilization = (used / limit_amount * 100.0) if limit_amount > 0 else 0.0
        if utilization >= 80.0:
            near_limit.append(
                {
                    "namespace_id": str(r["namespace_id"]),
                    "resource_type": r["resource_type"],
                    "used": used,
                    "limit": limit_amount,
                    "utilization_pct": round(utilization, 2),
                }
            )

    return JSONResponse({"near_limit": near_limit, "total": len(near_limit)})


async def api_admin_graph_explore(request):
    """POST /api/admin/graph/explore

    Body (JSON):
      namespace_id (required), query (required), max_depth?=2, anchor_top_k?=3, as_of?
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError, TypeError):
        return JSONResponse({"error": "Request body must be valid JSON"}, status_code=400)

    missing = [f for f in ("namespace_id", "query") if not body.get(f)]
    if missing:
        return JSONResponse(
            {"error": f"Missing required fields: {', '.join(missing)}"}, status_code=422
        )

    from pydantic import ValidationError

    from nce.models import GraphSearchRequest

    try:
        as_of_dt = parse_as_of(body.get("as_of"))
        payload = GraphSearchRequest(
            namespace_id=body["namespace_id"],
            query=body["query"],
            max_depth=body.get("max_depth", 2),
            anchor_top_k=body.get("anchor_top_k", 3),
            as_of=as_of_dt,
        )
    except (ValueError, ValidationError) as exc:
        return admin_validation_error(exc, status_code=422)

    try:
        result = await admin_state.engine.graph_search(payload)
    except Exception as exc:
        return admin_error_response("Graph exploration failed", exc, status_code=500)

    return JSONResponse(result)


async def api_admin_embedding_models(request):
    """GET /api/admin/embedding-models — list embedding model registry rows."""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)
    try:
        async with admin_state.engine.pg_pool.acquire(timeout=10.0) as conn:
            rows = await conn.fetch("""
                SELECT id, name, dimension, status, created_at, retired_at
                FROM embedding_models
                ORDER BY created_at DESC
                """)
    except Exception as exc:
        return admin_error_response("Failed to list models", exc, status_code=500)
    return JSONResponse({"models": [serialize_pg_row(r) for r in rows]})


async def api_admin_embedding_migration_start(request):
    """POST /api/admin/embedding-migrations/start — body { \"target_model_id\": uuid }."""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError, TypeError):
        return JSONResponse({"error": "Request body must be valid JSON"}, status_code=400)
    tid = body.get("target_model_id")
    if not tid:
        return JSONResponse({"error": "target_model_id is required"}, status_code=422)
    try:
        # Routes through the engine chokepoint — the dimension preflight + pre-flight
        # WORM audit (in NCEEngine.start_migration) apply to this admin HTTP path too,
        # so a dimension-incompatible migration is refused here with HTTP 409.
        out = await admin_state.engine.start_migration(
            str(tid),
            admin_identity=getattr(request.state, "admin_identity", None),
        )
    except ValueError as exc:
        return admin_validation_error(exc, status_code=409)
    except Exception as exc:
        return admin_error_response("start_migration failed", exc, status_code=500)
    # Mirror the MCP dispatch loop: start_migration is mutation=True.
    await bump_mcp_cache_generation(admin_state.engine, route="api_admin_embedding_migration_start")
    return JSONResponse(out)


async def api_admin_embedding_migration_status(request):
    """GET /api/admin/embedding-migrations/{migration_id}/status"""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)
    mid = request.path_params.get("migration_id")
    try:
        out = await admin_state.engine.migration_status(mid)
    except ValueError as exc:
        return admin_validation_error(exc, status_code=404)
    except Exception as exc:
        return admin_error_response(
            "Internal server error", exc, log_event="migration_status failed"
        )
    return JSONResponse(serialize_pg_row(out))


async def api_admin_embedding_migration_validate(request):
    """POST /api/admin/embedding-migrations/{migration_id}/validate"""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)
    mid = request.path_params.get("migration_id")
    try:
        out = await admin_state.engine.validate_migration(mid)
    except ValueError as exc:
        return admin_validation_error(exc, status_code=400)
    except Exception as exc:
        return admin_error_response(
            "Internal server error", exc, log_event="validate_migration failed"
        )
    return JSONResponse(out)


def _parse_force_flag(value: Any) -> bool:
    """Coerce a force flag from a JSON body value or a query-string value."""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


async def api_admin_embedding_migration_commit(request):
    """POST /api/admin/embedding-migrations/{migration_id}/commit

    Routes through ``engine.commit_migration`` — the SAME engine-layer chokepoint
    as the MCP tool — so the neighbor-overlap quality gate, the audited ``force``
    escape, and the pre-flight WORM audit all apply to this admin HTTP path.
    A below-threshold (or degenerate-sample) gate is refused with HTTP 409 unless
    ``force`` is set (query ``?force=true`` or JSON body ``{"force": true}``).
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)
    mid = request.path_params.get("migration_id")

    # force defaults to False; accept it from the JSON body or the query string.
    force = _parse_force_flag(request.query_params.get("force"))
    if not force:
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError, TypeError):
            body = None
        if isinstance(body, dict):
            force = _parse_force_flag(body.get("force"))

    try:
        out = await admin_state.engine.commit_migration(
            mid,
            force=force,
            admin_identity=getattr(request.state, "admin_identity", None),
        )
    except ValueError as exc:
        # Gate refusal / invalid state — 409 so a refused gate is distinguishable
        # from a generic bad request and is not silently swallowed.
        return admin_validation_error(exc, status_code=409)
    except Exception as exc:
        return admin_error_response(
            "Internal server error", exc, log_event="commit_migration failed"
        )
    # Mirror the MCP dispatch loop: commit_migration is mutation=True. Committing
    # retires the old vector space, so cached searches must not keep answering
    # from it.
    await bump_mcp_cache_generation(
        admin_state.engine, route="api_admin_embedding_migration_commit"
    )
    return JSONResponse(out)


async def api_admin_embedding_migration_abort(request):
    """POST /api/admin/embedding-migrations/{migration_id}/abort"""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)
    mid = request.path_params.get("migration_id")
    try:
        out = await admin_state.engine.abort_migration(mid)
    except ValueError as exc:
        return admin_validation_error(exc, status_code=404)
    except Exception as exc:
        return admin_error_response(
            "Internal server error", exc, log_event="abort_migration failed"
        )
    # Mirror the MCP dispatch loop: abort_migration is mutation=True.
    await bump_mcp_cache_generation(admin_state.engine, route="api_admin_embedding_migration_abort")
    return JSONResponse(out)


async def api_admin_schema(request):
    """GET /api/admin/schema — JSON Schema for all public NCE Pydantic models."""
    from pydantic.json_schema import models_json_schema

    from nce.models import (
        ForgetMemoryRequest,
        GetRecentContextRequest,
        GraphSearchRequest,
        IndexCodeFileRequest,
        KGEdge,
        KGNode,
        ManageQuotasRequest,
        MediaPayload,
        MemoryRecord,
        NamespaceCognitiveConfig,
        NamespaceCreate,
        NamespaceMetadata,
        NamespaceMetadataPatch,
        NamespacePIIConfig,
        NamespaceRecord,
        SemanticSearchRequest,
        SemanticSearchResult,
        StoreMemoryRequest,
        UnredactMemoryRequest,
    )

    _models = [
        NamespaceCreate,
        NamespaceRecord,
        NamespaceMetadata,
        NamespaceMetadataPatch,
        NamespaceCognitiveConfig,
        NamespacePIIConfig,
        ManageQuotasRequest,
        StoreMemoryRequest,
        MemoryRecord,
        ForgetMemoryRequest,
        UnredactMemoryRequest,
        GetRecentContextRequest,
        SemanticSearchRequest,
        SemanticSearchResult,
        GraphSearchRequest,
        IndexCodeFileRequest,
        KGNode,
        KGEdge,
        MediaPayload,
    ]
    _, schema = models_json_schema(
        [(m, "validation") for m in _models],
        title="NCE API Schema",
    )
    return JSONResponse(schema)


async def api_admin_dlq_list(request):
    """GET /api/admin/dlq

    Query params: task_name?, status?, page?, limit?,
    or legacy: limit=50, offset=0 (used when ``page`` is omitted).
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    from nce.dead_letter_queue import count_dead_letters, list_dead_letters

    qp = request.query_params
    task_name, tn_err = sanitize_task_name_filter(qp.get("task_name"))
    if tn_err:
        return JSONResponse({"error": tn_err}, status_code=422)
    dlq_status, st_err = validate_dlq_status(qp.get("status"))
    if st_err:
        return JSONResponse({"error": st_err}, status_code=422)

    page: int
    offset: int
    try:
        if qp.get("page") not in (None, ""):
            page, limit = parse_page_limit_common(qp)
            offset = offset_from_page_limit(page, limit)
        else:
            limit = clamp_bounded_int(
                qp.get("limit"),
                default=50,
                min_value=1,
                max_value=ADMIN_MAX_LIST_LIMIT,
            )
            offset = clamp_bounded_int(
                qp.get("offset"),
                default=0,
                min_value=0,
                max_value=ADMIN_MAX_ROWS_SKIP,
            )
            page = (offset // limit) + 1 if limit > 0 else 1
    except ValueError as exc:
        return admin_validation_error(exc, status_code=422)

    try:
        total = await count_dead_letters(
            admin_state.engine.pg_pool,
            task_name=task_name,
            status=dlq_status,
        )
        entries = await list_dead_letters(
            admin_state.engine.pg_pool,
            task_name=task_name,
            status=dlq_status,
            limit=limit,
            offset=offset,
        )
    except Exception as exc:
        return admin_error_response(
            "Internal server error", exc, log_event="api_admin_dlq_list failed"
        )
    return JSONResponse(
        {
            "entries": entries,
            "count": len(entries),
            "total": total,
            "page": page,
            "limit": limit,
            "offset": offset,
        }
    )


async def api_admin_dlq_replay(request):
    """POST /api/admin/dlq/{dlq_id}/replay"""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    from nce.dead_letter_queue import replay_dead_letter

    dlq_id = request.path_params["dlq_id"]
    try:
        result = await replay_dead_letter(admin_state.engine.pg_pool, dlq_id)
    except ValueError as exc:
        return admin_validation_error(exc, status_code=404)
    except Exception as exc:
        return admin_error_response(
            "Internal server error", exc, log_event="api_admin_dlq_replay failed"
        )
    # Mirror the MCP dispatch loop: the same core backs a mutation=True tool.
    await bump_mcp_cache_generation(admin_state.engine, route="api_admin_dlq_replay")
    return JSONResponse(result)


async def api_admin_dlq_purge(request):
    """POST /api/admin/dlq/{dlq_id}/purge"""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    from nce.dead_letter_queue import purge_dead_letter

    dlq_id = request.path_params["dlq_id"]
    try:
        await purge_dead_letter(admin_state.engine.pg_pool, dlq_id)
    except ValueError as exc:
        return admin_validation_error(exc, status_code=404)
    except Exception as exc:
        return admin_error_response(
            "Internal server error", exc, log_event="api_admin_dlq_purge failed"
        )
    # Mirror the MCP dispatch loop: the same core backs a mutation=True tool.
    await bump_mcp_cache_generation(admin_state.engine, route="api_admin_dlq_purge")
    return JSONResponse({"status": "ok", "id": dlq_id})


async def api_admin_db_postgres_status(request):
    """GET /api/admin/db/postgres/status"""
    if not admin_state.engine or not admin_state.engine.pg_pool:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)
    try:
        async with admin_state.engine.pg_pool.acquire() as conn:
            # Query sizes and estimate row counts for core tables
            tables_query = """
                SELECT 
                    c.relname AS name,
                    c.reltuples::bigint AS row_count_estimate,
                    pg_total_relation_size(c.oid) AS table_size_bytes,
                    pg_relation_size(c.oid) AS relation_size_bytes
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public' 
                  AND c.relkind = 'r'
                  AND c.relname IN ('memories', 'kg_nodes', 'kg_edges', 'event_log', 'outbox_events')
            """
            rows = await conn.fetch(tables_query)
            tables = [dict(r) for r in rows]

            # Estimate partition runway months
            partitions_query = """
                SELECT count(*) AS cnt
                FROM pg_inherits i
                JOIN pg_class c ON c.oid = i.inhrelid
                WHERE i.inhparent = 'event_log'::regclass
                  AND c.relname LIKE 'event_log_%'
                  AND c.relname >= 'event_log_' || to_char(now(), 'YYYY_MM')
            """
            part_row = await conn.fetchrow(partitions_query)
            runway_months = part_row["cnt"] if part_row else 0

        return JSONResponse(
            {"tables": tables, "partition_status": {"runway_months": runway_months}}
        )
    except Exception as exc:
        return admin_error_response(
            "Internal server error", exc, log_event="api_admin_db_postgres_status failed"
        )


async def api_admin_db_mongo_status(request):
    """GET /api/admin/db/mongo/status"""
    if not admin_state.engine or not admin_state.engine.mongo_client:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)
    try:
        db = admin_state.engine.mongo_client.get_database("memory_archive")
        collections_list = await db.list_collection_names()
        collections = []
        for col_name in collections_list:
            if col_name.startswith("system."):
                continue
            stats = await db.command("collStats", col_name)
            collections.append(
                {
                    "name": col_name,
                    "document_count": stats.get("count", 0),
                    "storage_size_bytes": stats.get("storageSize", 0),
                    "indexes": list(stats.get("indexSizes", {}).keys()),
                }
            )
        return JSONResponse({"collections": collections})
    except Exception as exc:
        return admin_error_response(
            "Internal server error", exc, log_event="api_admin_db_mongo_status failed"
        )


async def api_admin_db_redis_status(request):
    """GET /api/admin/db/redis/status"""
    if not admin_state.engine or not admin_state.engine.redis_client:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)
    try:
        info = await admin_state.engine.redis_client.info()
        db_stats = info.get("db0", {})
        keys_count = db_stats.get("keys", 0)

        keys_cache_count = 0
        keys_lock_count = 0
        try:
            # Safe non-blocking partial keyspace scan
            cursor, keys = await admin_state.engine.redis_client.scan(
                cursor=0, match="nce:*", count=500
            )
            for k in keys:
                k_str = k.decode("utf-8") if isinstance(k, bytes) else str(k)
                if ":lock:" in k_str:
                    keys_lock_count += 1
                else:
                    keys_cache_count += 1
        except Exception:
            pass

        return JSONResponse(
            {
                "info": {
                    "used_memory_human": info.get("used_memory_human", "0B"),
                    "connected_clients": info.get("connected_clients", 0),
                    "instantaneous_ops_per_sec": info.get("instantaneous_ops_per_sec", 0),
                },
                "keyspaces": [
                    {"pattern": "nce:cache:*", "count": keys_cache_count},
                    {"pattern": "nce:lock:*", "count": keys_lock_count},
                    {"pattern": "all_keys", "count": keys_count},
                ],
            }
        )
    except Exception as exc:
        return admin_error_response(
            "Internal server error", exc, log_event="api_admin_db_redis_status failed"
        )


async def api_admin_db_minio_status(request):
    """GET /api/admin/db/minio/status"""
    if not admin_state.engine or not admin_state.engine.minio_client:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)
    try:
        import asyncio

        def _get_buckets():
            bucket_list = admin_state.engine.minio_client.list_buckets()
            res = []
            for b in bucket_list:
                obj_count = 0
                total_size = 0
                try:
                    objects = admin_state.engine.minio_client.list_objects(b.name, recursive=True)
                    for i, o in enumerate(objects):
                        if i >= 100:  # Limit safety scan depth
                            break
                        obj_count += 1
                        total_size += o.size
                except Exception:
                    pass
                res.append(
                    {"name": b.name, "object_count": obj_count, "total_size_bytes": total_size}
                )
            return res

        buckets = await asyncio.to_thread(_get_buckets)
        return JSONResponse({"buckets": buckets})
    except Exception as exc:
        return admin_error_response(
            "Internal server error", exc, log_event="api_admin_db_minio_status failed"
        )


async def api_admin_connectors_status(request):
    """GET /api/admin/connectors/status"""
    try:
        sharepoint_cfg = {
            "enabled": bool(_shared.cfg.AZURE_CLIENT_ID),
            "has_client_id": bool(_shared.cfg.AZURE_CLIENT_ID),
            "token_status": "active" if _shared.cfg.GRAPH_BRIDGE_TOKEN else "missing",
            "sync_interval_mins": _shared.cfg.BRIDGE_CRON_INTERVAL_MINUTES,
            "mcp_connect_provider": "sharepoint",
        }
        bridges = {
            "sharepoint": sharepoint_cfg,
            "google_drive": {
                "enabled": bool(_shared.cfg.GDRIVE_OAUTH_CLIENT_ID),
                "has_client_id": bool(_shared.cfg.GDRIVE_OAUTH_CLIENT_ID),
                "token_status": "active" if _shared.cfg.GDRIVE_BRIDGE_TOKEN else "missing",
                "sync_interval_mins": _shared.cfg.BRIDGE_CRON_INTERVAL_MINUTES,
                "mcp_connect_provider": "gdrive",
            },
            "dropbox": {
                "enabled": bool(_shared.cfg.DROPBOX_OAUTH_CLIENT_ID),
                "has_client_id": bool(_shared.cfg.DROPBOX_OAUTH_CLIENT_ID),
                "token_status": "active" if _shared.cfg.DROPBOX_BRIDGE_TOKEN else "missing",
                "sync_interval_mins": _shared.cfg.BRIDGE_CRON_INTERVAL_MINUTES,
                "mcp_connect_provider": "dropbox",
            },
            # Legacy admin UI key — same Microsoft Graph credentials as sharepoint.
            "onedrive": {
                **sharepoint_cfg,
                "deprecated_ui_key": True,
            },
        }

        cognitive_online = False
        if _shared.cfg.NCE_COGNITIVE_BASE_URL:
            import httpx

            try:
                async with httpx.AsyncClient(timeout=1.0) as client:
                    resp = await client.get(f"{_shared.cfg.NCE_COGNITIVE_BASE_URL}/health")
                    cognitive_online = resp.status_code == 200
            except Exception:
                cognitive_online = False

        external_apis = {
            "openai_compatible_cognitive": {
                "endpoint": _shared.cfg.NCE_COGNITIVE_BASE_URL or "not_configured",
                "configured": bool(_shared.cfg.NCE_COGNITIVE_BASE_URL),
                "online": cognitive_online,
            },
            "nli_deberta": {
                "model_id": _shared.cfg.NLI_MODEL_ID or "not_configured",
                "loaded": bool(_shared.cfg.NLI_MODEL_ID),
            },
        }

        return JSONResponse({"bridges": bridges, "external_apis": external_apis})
    except Exception as exc:
        return admin_error_response(
            "Internal server error", exc, log_event="api_admin_connectors_status failed"
        )


async def api_admin_connectors_save(request):
    """POST /api/admin/connectors/save"""
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError, TypeError):
        return JSONResponse({"error": "Request body must be valid JSON"}, status_code=400)

    updates = {}

    def reconstruct_secret(new_val: str, old_val: str) -> str:
        if new_val == "••••••••":
            return old_val
        return new_val

    # Google Drive
    gd = body.get("google_drive", {})
    if "client_id" in gd:
        _shared.cfg.GDRIVE_OAUTH_CLIENT_ID = gd["client_id"]
        updates["GDRIVE_OAUTH_CLIENT_ID"] = gd["client_id"]
    if "client_secret" in gd:
        val = reconstruct_secret(gd["client_secret"], _shared.cfg.GDRIVE_OAUTH_CLIENT_SECRET)
        _shared.cfg.GDRIVE_OAUTH_CLIENT_SECRET = val
        updates["GDRIVE_OAUTH_CLIENT_SECRET"] = val
    if "token" in gd:
        val = reconstruct_secret(gd["token"], _shared.cfg.GDRIVE_BRIDGE_TOKEN)
        _shared.cfg.GDRIVE_BRIDGE_TOKEN = val
        updates["GDRIVE_BRIDGE_TOKEN"] = val

    # Dropbox
    dbx = body.get("dropbox", {})
    if "client_id" in dbx:
        _shared.cfg.DROPBOX_OAUTH_CLIENT_ID = dbx["client_id"]
        updates["DROPBOX_OAUTH_CLIENT_ID"] = dbx["client_id"]
    if "token" in dbx:
        val = reconstruct_secret(dbx["token"], _shared.cfg.DROPBOX_BRIDGE_TOKEN)
        _shared.cfg.DROPBOX_BRIDGE_TOKEN = val
        updates["DROPBOX_BRIDGE_TOKEN"] = val

    # OneDrive
    od = body.get("onedrive", {})
    if "client_id" in od:
        _shared.cfg.AZURE_CLIENT_ID = od["client_id"]
        updates["AZURE_CLIENT_ID"] = od["client_id"]
    if "client_secret" in od:
        val = reconstruct_secret(od["client_secret"], _shared.cfg.AZURE_CLIENT_SECRET)
        _shared.cfg.AZURE_CLIENT_SECRET = val
        updates["AZURE_CLIENT_SECRET"] = val
    if "tenant_id" in od:
        _shared.cfg.AZURE_TENANT_ID = od["tenant_id"]
        updates["AZURE_TENANT_ID"] = od["tenant_id"]
    if "token" in od:
        val = reconstruct_secret(od["token"], _shared.cfg.GRAPH_BRIDGE_TOKEN)
        _shared.cfg.GRAPH_BRIDGE_TOKEN = val
        updates["GRAPH_BRIDGE_TOKEN"] = val

    # Common
    common = body.get("common", {})
    if "cron_interval_mins" in common:
        try:
            val = int(common["cron_interval_mins"])
            _shared.cfg.BRIDGE_CRON_INTERVAL_MINUTES = val
            updates["BRIDGE_CRON_INTERVAL_MINUTES"] = str(val)
        except ValueError:
            pass

    try:
        update_dotenv(updates)
    except RuntimeError as exc:
        return admin_validation_error(exc, status_code=403)
    except Exception as exc:
        return admin_error_response(
            "Failed to persist connector configuration",
            exc,
            log_event="Failed to write connectors configuration to .env",
        )

    return JSONResponse(
        {"status": "success", "message": "Connector configurations successfully updated."}
    )


async def api_admin_datastores_status(request):
    """GET /api/admin/datastores/status
    Retrieves masked connection credentials and pools config for active datastores.
    """
    try:
        postgres = {
            "pg_dsn": mask_uri_password(_shared.cfg.PG_DSN),
            "db_read_url": mask_uri_password(_shared.cfg.DB_READ_URL),
            "db_write_url": mask_uri_password(_shared.cfg.DB_WRITE_URL),
            "pg_min_pool": _shared.cfg.PG_MIN_POOL,
            "pg_max_pool": _shared.cfg.PG_MAX_POOL,
        }
        mongodb = {
            "mongo_uri": mask_uri_password(_shared.cfg.MONGO_URI),
        }
        redis = {
            "redis_url": mask_uri_password(_shared.cfg.REDIS_URL),
            "redis_ttl": _shared.cfg.REDIS_TTL,
            "redis_max_connections": _shared.cfg.REDIS_MAX_CONNECTIONS,
        }
        minio = {
            "minio_endpoint": _shared.cfg.MINIO_ENDPOINT,
            "minio_access_key": _shared.cfg.MINIO_ACCESS_KEY,
            "has_secret_key": bool(_shared.cfg.MINIO_SECRET_KEY),
            "minio_secure": _shared.cfg.MINIO_SECURE,
        }
        return JSONResponse(
            {
                "postgres": postgres,
                "mongodb": mongodb,
                "redis": redis,
                "minio": minio,
            }
        )
    except Exception as exc:
        return admin_error_response(
            "Internal server error", exc, log_event="api_admin_datastores_status failed"
        )


async def api_admin_datastores_save(request):
    """POST /api/admin/datastores/save

    Persists datastore settings to ``.env`` only. Connection strings are not applied to the
    running process; restart the admin server after saving DSN/URI changes.
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    if not _shared.cfg.NCE_ALLOW_ADMIN_DOTENV_PERSIST:
        return JSONResponse(
            {
                "error": "Persisting datastore configuration to .env is disabled. "
                "Set NCE_ALLOW_ADMIN_DOTENV_PERSIST=true for local development only."
            },
            status_code=403,
        )

    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError, TypeError):
        return JSONResponse({"error": "Request body must be valid JSON"}, status_code=400)

    from urllib.parse import urlparse, urlunparse

    def reconstruct_uri(new_uri: str, old_uri: str) -> str:
        if not new_uri:
            return ""
        if "••••••••" not in new_uri:
            return new_uri
        try:
            old_parsed = urlparse(old_uri)
            old_password = old_parsed.password or ""
            new_parsed = urlparse(new_uri)
            netloc = new_parsed.hostname or ""
            if new_parsed.port:
                netloc = f"{netloc}:{new_parsed.port}"
            if new_parsed.username:
                netloc = f"{new_parsed.username}:{old_password}@{netloc}"
            else:
                netloc = f":{old_password}@{netloc}"
            return urlunparse(new_parsed._replace(netloc=netloc))
        except Exception:
            return new_uri

    updates = {}

    # 1. PostgreSQL
    pg = body.get("postgres", {})
    if "pg_dsn" in pg and pg["pg_dsn"]:
        updates["PG_DSN"] = reconstruct_uri(pg["pg_dsn"], _shared.cfg.PG_DSN)
    if "db_read_url" in pg and pg["db_read_url"]:
        updates["DB_READ_URL"] = reconstruct_uri(pg["db_read_url"], _shared.cfg.DB_READ_URL)
    if "db_write_url" in pg and pg["db_write_url"]:
        updates["DB_WRITE_URL"] = reconstruct_uri(pg["db_write_url"], _shared.cfg.DB_WRITE_URL)
    if "pg_min_pool" in pg:
        try:
            updates["PG_MIN_POOL"] = str(int(pg["pg_min_pool"]))
        except ValueError:
            pass
    if "pg_max_pool" in pg:
        try:
            updates["PG_MAX_POOL"] = str(int(pg["pg_max_pool"]))
        except ValueError:
            pass

    # 2. MongoDB
    mongo = body.get("mongodb", {})
    if "mongo_uri" in mongo and mongo["mongo_uri"]:
        updates["MONGO_URI"] = reconstruct_uri(mongo["mongo_uri"], _shared.cfg.MONGO_URI)

    # 3. Redis
    redis_data = body.get("redis", {})
    if "redis_url" in redis_data and redis_data["redis_url"]:
        updates["REDIS_URL"] = reconstruct_uri(redis_data["redis_url"], _shared.cfg.REDIS_URL)
    if "redis_ttl" in redis_data:
        try:
            updates["REDIS_TTL"] = str(int(redis_data["redis_ttl"]))
        except ValueError:
            pass
    if "redis_max_connections" in redis_data:
        try:
            updates["REDIS_MAX_CONNECTIONS"] = str(int(redis_data["redis_max_connections"]))
        except ValueError:
            pass

    # 4. MinIO S3
    minio = body.get("minio", {})
    if "minio_endpoint" in minio:
        updates["MINIO_ENDPOINT"] = minio["minio_endpoint"]
    if "minio_access_key" in minio:
        updates["MINIO_ACCESS_KEY"] = minio["minio_access_key"]
    if "minio_secret_key" in minio:
        secret = minio["minio_secret_key"]
        if secret and secret != "••••••••":
            updates["MINIO_SECRET_KEY"] = secret
    if "minio_secure" in minio:
        secure_val = bool(minio["minio_secure"])
        updates["MINIO_SECURE"] = "true" if secure_val else "false"

    # Save updates back to active .env file on disk
    try:
        update_dotenv(updates)
    except RuntimeError as exc:
        return admin_validation_error(exc, status_code=403)
    except Exception as exc:
        return admin_error_response(
            "Failed to persist datastore configuration",
            exc,
            log_event="Failed to write datastores configuration to .env",
        )

    client_ip = request.client.host if request.client else "unknown"
    _shared.logger.warning(
        "AUDIT datastores_save keys=%s client_ip=%s",
        sorted(updates.keys()),
        client_ip,
    )

    return JSONResponse(
        {
            "status": "success",
            "message": "Datastore configuration persisted to .env.",
            "restart_required": True,
            "updated_keys": sorted(updates.keys()),
        }
    )


async def api_admin_signing_status(request):
    """GET /api/admin/signing/status — non-secret signing key rotation summary."""
    if not admin_state.engine or not admin_state.engine.pg_pool:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        async with admin_state.engine.pg_pool.acquire(timeout=10.0) as conn:
            payload = await admin_signing_keys_status(conn)
    except Exception as exc:
        return admin_error_response(
            "Failed to load signing keys status",
            exc,
            log_event="api_admin_signing_status failed",
        )
    return JSONResponse(payload)


async def api_admin_pii_redactions_list(request):
    """GET /api/admin/pii-redactions — paginated vault rows (no ciphertext)."""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)
    qp = request.query_params
    try:
        namespace_id = parse_optional_uuid(qp.get("namespace_id"))
        page, limit = parse_page_limit_common(qp)
        offset = offset_from_page_limit(page, limit)
    except ValueError as exc:
        return admin_validation_error(exc, status_code=422)

    where = "WHERE ($1::uuid IS NULL OR namespace_id = $1)"
    count_sql = f"SELECT COUNT(*)::bigint AS total FROM pii_redactions {where}"
    items_sql = f"""
        SELECT memory_id, namespace_id, entity_type, token, created_at
        FROM pii_redactions
        {where}
        ORDER BY created_at DESC
        LIMIT $2 OFFSET $3
    """

    try:
        async with admin_state.engine.pg_pool.acquire(timeout=10.0) as conn:
            total_row = await conn.fetchrow(count_sql, namespace_id)
            rows = await conn.fetch(items_sql, namespace_id, limit, offset)
    except Exception as exc:
        return admin_error_response(
            "Failed to list PII redactions",
            exc,
            log_event="api_admin_pii_redactions_list failed",
        )

    items = [
        {
            "memory_id": str(r["memory_id"]),
            "namespace_id": str(r["namespace_id"]),
            "entity_type": r["entity_type"],
            "token": r["token"][:16] + "…" if len(r["token"]) > 16 else r["token"],
            "created_at": r["created_at"].astimezone(UTC).isoformat(),
        }
        for r in rows
    ]
    return JSONResponse(
        {
            "items": items,
            "page": page,
            "limit": limit,
            "total": int(total_row["total"]) if total_row else 0,
        }
    )


async def api_admin_security_event_seq_gaps(request):
    """GET /api/admin/security/event-seq-gaps/{namespace_id}"""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)
    raw_ns = request.path_params.get("namespace_id")
    try:
        namespace_id = uuid.UUID(raw_ns) if raw_ns else None
    except ValueError:
        return JSONResponse({"error": "Invalid namespace_id"}, status_code=422)
    if namespace_id is None:
        return JSONResponse({"error": "namespace_id required"}, status_code=422)

    gap_sql = """
        WITH s AS (
            SELECT event_seq,
                   LAG(event_seq) OVER (ORDER BY event_seq) AS prev
            FROM event_log
            WHERE namespace_id = $1
        )
        SELECT prev::bigint AS after_seq, event_seq::bigint AS before_seq
        FROM s
        WHERE prev IS NOT NULL AND event_seq > prev + 1
        ORDER BY event_seq
        LIMIT 200
    """

    try:
        async with admin_state.engine.pg_pool.acquire(timeout=10.0) as conn:
            min_seq = await conn.fetchval(
                "SELECT MIN(event_seq)::bigint FROM event_log WHERE namespace_id = $1",
                namespace_id,
            )
            max_seq = await conn.fetchval(
                "SELECT MAX(event_seq)::bigint FROM event_log WHERE namespace_id = $1",
                namespace_id,
            )
            gap_rows = await conn.fetch(gap_sql, namespace_id)
    except Exception as exc:
        return admin_error_response(
            "Failed to compute sequence gaps",
            exc,
            log_event="api_admin_security_event_seq_gaps failed",
        )

    gaps: list[dict[str, int]] = []
    if min_seq is not None and min_seq > 1:
        gaps.append({"after_seq": 0, "before_seq": int(min_seq)})
    for r in gap_rows:
        gaps.append({"after_seq": int(r["after_seq"]), "before_seq": int(r["before_seq"])})

    return JSONResponse(
        {
            "namespace_id": str(namespace_id),
            "min_seq": int(min_seq) if min_seq is not None else None,
            "max_seq": int(max_seq) if max_seq is not None else None,
            "gaps": gaps,
        }
    )


async def api_admin_security_verify_memory_sample(request):
    """POST /api/admin/security/verify-memory-sample

    Body: {"namespace_id"?: uuid, "sample_size"?: int 1–30}
    """
    if not admin_state.engine or admin_state.engine.memory is None:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        namespace_id = parse_optional_uuid(body.get("namespace_id"))
        raw_sz = body.get("sample_size")
        sample_size = clamp_bounded_int(
            None if raw_sz is None or raw_sz == "" else str(raw_sz),
            default=10,
            min_value=1,
            max_value=30,
        )
    except ValueError as exc:
        return admin_validation_error(exc, status_code=422)

    sample_sql = """
        SELECT id FROM memories
        WHERE valid_to IS NULL
          AND ($1::uuid IS NULL OR namespace_id = $1)
        ORDER BY random()
        LIMIT $2
    """

    try:
        async with admin_state.engine.pg_pool.acquire(timeout=10.0) as conn:
            mem_rows = await conn.fetch(sample_sql, namespace_id, sample_size)
    except Exception as exc:
        return admin_error_response(
            "Failed to sample memories",
            exc,
            log_event="api_admin_security_verify_memory_sample fetch failed",
        )

    results: list[dict[str, object]] = []
    invalid_count = 0
    for r in mem_rows:
        mid = str(r["id"])
        try:
            vr = await admin_state.engine.memory.verify_memory(mid)
        except Exception as exc:
            results.append(
                {
                    "memory_id": mid,
                    "valid": False,
                    "reason": sanitize_admin_reason(exc),
                    "key_id": None,
                }
            )
            invalid_count += 1
            continue
        ok = bool(vr.get("valid"))
        if not ok:
            invalid_count += 1
        results.append(
            {
                "memory_id": mid,
                "valid": ok,
                "reason": vr.get("reason"),
                "key_id": vr.get("key_id"),
            }
        )

    return JSONResponse(
        {
            "sampled": len(results),
            "invalid_count": invalid_count,
            "namespace_filter": str(namespace_id) if namespace_id else None,
            "results": results,
        }
    )


async def api_admin_security_test_rls_isolation(request):
    """POST /api/admin/security/test-rls-isolation

    Body: {"namespace_id": uuid, "probe_namespace_id": uuid}
    Sets SET LOCAL nce.namespace_id to *namespace_id* and counts rows in
    *probe_namespace_id* — should be 0 when RLS enforces tenant isolation.
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        ns_a = uuid.UUID(str(body["namespace_id"]))
        ns_b = uuid.UUID(str(body["probe_namespace_id"]))
    except (KeyError, ValueError):
        return JSONResponse(
            {"error": "namespace_id and probe_namespace_id (UUID) required"},
            status_code=422,
        )

    steps: list[str] = [
        "BEGIN;",
        f"SELECT set_config('nce.namespace_id', '{ns_a}', true);",
        f"-- COUNT(*) FROM memories WHERE namespace_id = '{ns_b}' (cross-tenant probe)",
        f"-- COUNT(*) FROM memories WHERE namespace_id = '{ns_a}' (same-tenant check)",
        "COMMIT;",
    ]

    try:
        async with admin_state.engine.pg_pool.acquire(timeout=10.0) as conn:
            async with conn.transaction():
                await set_namespace_context(conn, ns_a)
                cross_count = await conn.fetchval(
                    "SELECT COUNT(*)::bigint FROM memories WHERE namespace_id = $1",
                    ns_b,
                )
                own_count = await conn.fetchval(
                    "SELECT COUNT(*)::bigint FROM memories WHERE namespace_id = $1",
                    ns_a,
                )
    except Exception as exc:
        return admin_error_response(
            "RLS isolation probe failed",
            exc,
            log_event="api_admin_security_test_rls_isolation failed",
            extra={"steps": steps},
        )

    isolation_ok = cross_count == 0
    return JSONResponse(
        {
            "scoped_namespace_id": str(ns_a),
            "probe_namespace_id": str(ns_b),
            "cross_tenant_rows_visible": int(cross_count or 0),
            "same_tenant_rows_visible": int(own_count or 0),
            "isolation_ok": isolation_ok,
            "policy_name": "namespace_isolation_policy",
            "steps": steps,
        }
    )


async def api_admin_namespaces_list(request):
    """GET /api/admin/namespaces

    Query params: slug_prefix?, page=1, limit=500

    Always includes ``namespaces`` (compat with admin UI).
    Pagination metadata: ``page``, ``limit``, ``total``, ``items`` (same slice as ``namespaces``).
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)
    qp = request.query_params
    prefix, pref_err = sanitize_slug_prefix_filter(qp.get("slug_prefix"))
    if pref_err:
        return JSONResponse({"error": pref_err}, status_code=422)

    try:
        page, limit = parse_page_limit_common(
            qp,
            default_limit=ADMIN_NAMESPACES_DEFAULT_LIMIT,
            max_limit=500,
        )
        offset = offset_from_page_limit(page, limit)
    except ValueError as exc:
        return admin_validation_error(exc, status_code=422)

    clauses: list[str] = []
    args: list[object] = []
    bind = 1
    if prefix is not None:
        clauses.append(f"slug ILIKE ${bind}")
        args.append(prefix + "%")
        bind += 1
    where_sql = f"WHERE {' AND '.join(clauses)} " if clauses else ""

    count_sql = f"SELECT COUNT(*)::bigint AS total FROM namespaces {where_sql}"
    items_sql = f"""
        SELECT id, slug, parent_id, created_at, metadata
        FROM namespaces
        {where_sql}
        ORDER BY slug
        LIMIT ${bind} OFFSET ${bind + 1}
    """

    try:
        async with admin_state.engine.pg_pool.acquire(timeout=10.0) as conn:
            total_row = await conn.fetchrow(count_sql, *args)
            rows = await conn.fetch(items_sql, *args, limit, offset)
        namespaces: list[dict] = []
        for r in rows:
            namespaces.append(
                {
                    "id": str(r["id"]),
                    "slug": r["slug"],
                    "parent_id": str(r["parent_id"]) if r["parent_id"] else None,
                    "created_at": (
                        r["created_at"].astimezone(UTC).isoformat() if r["created_at"] else None
                    ),
                    "metadata": json.loads(r["metadata"]) if r["metadata"] else {},
                }
            )
        total = int(total_row["total"]) if total_row else 0
        return JSONResponse(
            {
                "namespaces": namespaces,
                "items": namespaces,
                "page": page,
                "limit": limit,
                "total": total,
            }
        )
    except Exception as exc:
        return admin_error_response(
            "Internal server error", exc, log_event="api_admin_namespaces_list failed"
        )


async def api_admin_namespaces_get(request):
    """GET /api/admin/namespaces/{namespace_id}
    Retrieves metadata and info for a specific namespace.
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)
    try:
        ns_id = uuid.UUID(request.path_params["namespace_id"])
    except ValueError:
        return JSONResponse({"error": "Invalid namespace_id UUID"}, status_code=400)

    try:
        async with admin_state.engine.pg_pool.acquire(timeout=10.0) as conn:
            row = await conn.fetchrow(
                "SELECT id, slug, parent_id, created_at, metadata FROM namespaces WHERE id = $1",
                ns_id,
            )
        if not row:
            return JSONResponse({"error": "Namespace not found"}, status_code=404)
        return JSONResponse(
            {
                "id": str(row["id"]),
                "slug": row["slug"],
                "parent_id": str(row["parent_id"]) if row["parent_id"] else None,
                "created_at": row["created_at"].astimezone(UTC).isoformat()
                if row["created_at"]
                else None,
                "metadata": json.loads(row["metadata"]) if row["metadata"] else {},
            }
        )
    except Exception as exc:
        return admin_error_response(
            "Internal server error", exc, log_event="api_admin_namespaces_get failed"
        )


async def api_admin_namespaces_update_metadata(request):
    """POST /api/admin/namespaces/{namespace_id}/metadata
    Saves/updates a namespace's metadata, routing through admin_state.engine.manage_namespace.
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)
    try:
        ns_id = uuid.UUID(request.path_params["namespace_id"])
    except ValueError:
        return JSONResponse({"error": "Invalid namespace_id UUID"}, status_code=400)

    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError, TypeError):
        return JSONResponse({"error": "Request body must be valid JSON"}, status_code=400)

    try:
        from pydantic import ValidationError

        from nce.models import ManageNamespaceRequest, NamespaceMetadataPatch

        patch = NamespaceMetadataPatch.model_validate(body)
        payload = ManageNamespaceRequest(
            command="update_metadata", namespace_id=ns_id, metadata_patch=patch
        )

        res = await admin_state.engine.manage_namespace(payload, admin_identity="admin_webportal")
        # Mirror the MCP dispatch loop: manage_namespace is mutation=True. The
        # orchestrator's own bump (nce/orchestrators/namespace.py) fires on the
        # DELETE branch only, so update_metadata would otherwise never invalidate.
        await bump_mcp_cache_generation(
            admin_state.engine, route="api_admin_namespaces_update_metadata"
        )
        return JSONResponse(res)
    except ValidationError as exc:
        return admin_validation_error(exc, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Internal server error", exc, log_event="api_admin_namespaces_update_metadata failed"
        )


async def api_admin_memory_boost(request):
    """POST /api/admin/memory/boost — salience reinforce via CognitiveOrchestrator."""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError, TypeError):
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    try:
        ns = parse_optional_uuid(body.get("namespace_id"))
        mem = parse_optional_uuid(body.get("memory_id"))
    except ValueError:
        return JSONResponse({"error": "Invalid UUID"}, status_code=400)

    if ns is None or mem is None:
        return JSONResponse({"error": "namespace_id and memory_id are required"}, status_code=422)

    agent_id = validate_agent_id(str(body.get("agent_id") or ""))

    try:
        factor = float(body.get("factor")) if body.get("factor") is not None else 0.2
    except (TypeError, ValueError):
        return JSONResponse({"error": "factor must be a number"}, status_code=422)

    try:
        res = await admin_state.engine.boost_memory(
            memory_id=str(mem),
            agent_id=agent_id,
            namespace_id=str(ns),
            factor=factor,
        )
    except Exception as exc:
        return admin_error_response(
            "Internal server error", exc, log_event="api_admin_memory_boost failed"
        )

    # Mirror the MCP dispatch loop: boost_memory is mutation=True. Salience feeds
    # the ranking of cacheable searches, so their cached results are now stale.
    await bump_mcp_cache_generation(admin_state.engine, route="api_admin_memory_boost")

    return JSONResponse(res)


async def api_admin_salience_map(request):
    """GET /api/admin/salience-map

    Query params: ``namespace_id`` (required), ``agent_id?``, ``top_k?``, ``half_life_days?``
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    qp = request.query_params
    try:
        ns = parse_optional_uuid(qp.get("namespace_id"))
        if ns is None:
            return JSONResponse({"error": "namespace_id is required"}, status_code=422)
    except ValueError:
        return JSONResponse({"error": "Invalid namespace_id UUID"}, status_code=400)

    top_k, tk_err = parse_salience_top_k(qp.get("top_k"))
    if tk_err:
        return JSONResponse({"error": tk_err}, status_code=422)
    agent_filter, ag_err = sanitize_optional_agent_filter(qp.get("agent_id"))
    if ag_err:
        return JSONResponse({"error": ag_err}, status_code=422)

    hl_default = float(_shared.cfg.CONSOLIDATION_HALF_LIFE_DAYS)
    half_life, hl_err = parse_optional_half_life_days(qp.get("half_life_days"), default=hl_default)
    if hl_err:
        return JSONResponse({"error": hl_err}, status_code=422)

    try:
        async with admin_state.engine.pg_pool.acquire(timeout=10.0) as conn:
            points = await fetch_salience_map_points(
                conn,
                namespace_id=ns,
                agent_id=agent_filter,
                top_k=top_k,
                half_life_days=half_life,
            )
    except Exception as exc:
        _shared.logger.exception("api_admin_salience_map failed ns=%s", ns)
        return admin_error_response("Internal server error", exc, status_code=500)

    return JSONResponse(
        {
            "namespace_id": str(ns),
            "half_life_days": half_life,
            "top_k": top_k,
            "points": points,
            "total_returned": len(points),
        }
    )


async def api_admin_llm_payload(request):
    """GET /api/admin/llm-payload

    Query params: ``namespace_id``, ``event_id`` — fetches consolidated LLM artifact JSON from MinIO.
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    qp = request.query_params
    try:
        ns = parse_optional_uuid(qp.get("namespace_id"))
        evt_raw = qp.get("event_id")
        evt = uuid.UUID(evt_raw) if evt_raw else None
    except ValueError:
        return JSONResponse({"error": "Invalid UUID in namespace_id/event_id"}, status_code=400)

    if ns is None or evt is None:
        return JSONResponse({"error": "namespace_id and event_id are required"}, status_code=422)

    try:
        from nce import salience as _salience

        async with admin_state.engine.pg_pool.acquire(timeout=10.0) as conn:
            uri, uri_err = await fetch_event_llm_payload_uri(conn, namespace_id=ns, event_id=evt)
        if uri_err:
            return JSONResponse({"error": uri_err}, status_code=404)
        assert uri is not None
        payload = await _salience.fetch_llm_payload(uri)
    except Exception as exc:
        return admin_error_response(
            "Internal server error", exc, log_event="api_admin_llm_payload failed"
        )

    return JSONResponse({"llm_payload_uri": uri, "payload": payload})


async def api_admin_fleet_overview(request):
    """GET /api/admin/fleet-overview — namespace-scoped rollup for fleet monitoring."""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    qp = request.query_params
    prefix, pref_err = sanitize_slug_prefix_filter(qp.get("slug_prefix"))
    if pref_err:
        return JSONResponse({"error": pref_err}, status_code=422)

    try:
        page, limit = parse_page_limit_common(qp)
    except ValueError as exc:
        return admin_validation_error(exc, status_code=422)

    hl_default = float(_shared.cfg.CONSOLIDATION_HALF_LIFE_DAYS)
    half_life, hl_err = parse_optional_half_life_days(qp.get("half_life_days"), default=hl_default)
    if hl_err:
        return JSONResponse({"error": hl_err}, status_code=422)

    try:
        async with admin_state.engine.pg_pool.acquire(timeout=10.0) as conn:
            rls = await fetch_pg_rls_snapshot(conn)
            items, total = await fetch_fleet_overview_page(
                conn,
                slug_prefix=prefix,
                page=page,
                limit=limit,
                half_life_days=half_life,
            )
    except Exception as exc:
        return admin_error_response(
            "Internal server error", exc, log_event="api_admin_fleet_overview failed"
        )

    return JSONResponse(
        {
            "items": items,
            "page": page,
            "limit": limit,
            "total": total,
            "half_life_days": half_life,
            "rls_tenant_tables": rls,
        }
    )


async def api_admin_contradictions_recent(request):
    """GET /api/admin/contradictions/recent — Fleet contradiction feed."""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)
    try:
        limit = int(request.query_params.get("limit") or "5")
    except ValueError:
        return JSONResponse({"error": "Invalid limit"}, status_code=422)
    limit = max(1, min(limit, 50))
    try:
        async with admin_state.engine.pg_pool.acquire(timeout=10.0) as conn:
            items = await fetch_recent_open_contradictions(conn, limit=limit)
    except Exception as exc:
        return admin_error_response(
            "Internal server error", exc, log_event="api_admin_contradictions_recent failed"
        )
    return JSONResponse({"items": items, "limit": limit})


async def api_admin_namespace_bridges(request):
    """GET /api/admin/namespaces/{namespace_id}/bridges — integration cards."""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)
    try:
        ns_uuid = uuid.UUID(request.path_params["namespace_id"])
    except ValueError:
        return JSONResponse({"error": "Invalid namespace_id UUID"}, status_code=400)
    try:
        async with admin_state.engine.pg_pool.acquire(timeout=10.0) as conn:
            items = await fetch_namespace_bridge_subscriptions(conn, ns_uuid)
    except Exception as exc:
        return admin_error_response(
            "Internal server error", exc, log_event="api_admin_namespace_bridges failed"
        )
    return JSONResponse({"items": items, "namespace_id": str(ns_uuid)})


async def detect_causal_cycles(
    pool: Any,
    namespace_id: uuid.UUID,
    *,
    depth_cap: int = 50,
) -> dict[str, Any]:
    """Detect cycles in the ``event_parents`` causal DAG for *namespace_id*.

    Uses a recursive CTE capped at *depth_cap* levels.  Returns a dict with:

    ``has_cycle`` — True if a cycle was found.
    ``cycle_paths`` — list of ``(event_id, parent_event_id)`` edge tuples on a
                      cycle (up to 20 examples).
    ``depth_cap`` — the cap used.
    """
    # A cycle exists when a recursive path through event_parents visits the same
    # event_id twice.  The CTE tracks the current path as a text array so we can
    # detect the revisit and surface the offending edges.
    sql = """
        WITH RECURSIVE dag(event_id, parent_event_id, path, cycle) AS (
            SELECT
                ep.event_id,
                ep.parent_event_id,
                ARRAY[ep.event_id::text, ep.parent_event_id::text],
                ep.event_id = ep.parent_event_id
            FROM event_parents ep
            WHERE ep.namespace_id = $1

          UNION ALL

            SELECT
                ep.event_id,
                ep.parent_event_id,
                dag.path || ep.parent_event_id::text,
                ep.parent_event_id::text = ANY(dag.path)
            FROM event_parents ep
            JOIN dag ON ep.event_id = dag.parent_event_id
            WHERE NOT dag.cycle
              AND array_length(dag.path, 1) < $2
        )
        SELECT event_id, parent_event_id
        FROM dag
        WHERE cycle
        LIMIT 20
    """
    async with pool.acquire(timeout=30.0) as conn:
        rows = await conn.fetch(sql, namespace_id, depth_cap)

    cycle_paths = [
        {"event_id": str(r["event_id"]), "parent_event_id": str(r["parent_event_id"])} for r in rows
    ]
    return {
        "has_cycle": bool(cycle_paths),
        "cycle_paths": cycle_paths,
        "depth_cap": depth_cap,
    }


async def api_admin_bridge_renew(request):
    """POST /api/admin/bridges/{bridge_id}/renew

    Forces a webhook subscription refresh for SharePoint/Google Drive integrations.
    Optional query ``namespace_id`` scopes the call when the caller wants an extra guardrail.
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        bridge_uuid = uuid.UUID(request.path_params["bridge_id"])
    except ValueError:
        return JSONResponse({"error": "Invalid bridge_id UUID"}, status_code=400)

    try:
        ns_guard = parse_optional_uuid(request.query_params.get("namespace_id"))
    except ValueError:
        return JSONResponse({"error": "Invalid namespace_id UUID"}, status_code=400)

    from nce.bridge_renewal import renew_dropbox, renew_gdrive, renew_sharepoint

    try:
        async with admin_state.engine.pg_pool.acquire(timeout=10.0) as conn:
            row = await conn.fetchrow(
                "SELECT * FROM bridge_subscriptions WHERE id = $1",
                bridge_uuid,
            )
        if row is None:
            return JSONResponse({"error": "bridge subscription not found"}, status_code=404)
        if (
            ns_guard is not None
            and row["namespace_id"] is not None
            and row["namespace_id"] != ns_guard
        ):
            return JSONResponse({"error": "namespace_id does not own this bridge"}, status_code=403)
    except Exception as exc:
        _shared.logger.exception("api_admin_bridge_renew prefetch failed bridge_id=%s", bridge_uuid)
        return admin_error_response("Internal server error", exc, status_code=500)

    prov = row["provider"]
    _shared.logger.info(
        "audit bridge_admin_renew_requested bridge_id=%s provider=%s namespace_id=%s",
        bridge_uuid,
        prov,
        row["namespace_id"],
    )

    try:
        if prov == "sharepoint":
            await renew_sharepoint(admin_state.engine.pg_pool, row)
            action = "renewed_sharepoint"
        elif prov == "gdrive":
            await renew_gdrive(admin_state.engine.pg_pool, row)
            action = "renewed_gdrive"
        elif prov == "dropbox":
            await renew_dropbox(admin_state.engine.pg_pool, row)
            action = "noop_dropbox"
        else:
            return JSONResponse(
                {"error": f"Unsupported provider for renewal: {prov}"}, status_code=422
            )
    except Exception as exc:
        _shared.logger.exception("audit bridge_admin_renew_failed bridge_id=%s", bridge_uuid)
        return admin_error_response("Internal server error", exc, status_code=500)

    _shared.logger.info(
        "audit bridge_admin_renew_succeeded bridge_id=%s provider=%s action=%s",
        bridge_uuid,
        prov,
        action,
    )
    return JSONResponse({"status": "ok", "action": action, "bridge_id": str(bridge_uuid)})


async def api_admin_anchor_status(request):
    """GET /api/admin/anchor/status — last anchored seq/hash per namespace.

    Returns the most-recent anchor blob stored in the WORM MinIO bucket
    (``NCE_ANCHOR_BUCKET``) for each namespace, or an error when the bucket
    or client is unavailable.

    Response schema::

        {
          "bucket": "<bucket-name>",
          "namespaces": [
            {
              "namespace_id": "<uuid>",
              "max_seq": <int>,
              "chain_hash": "<hex>",
              "anchored_at": "<iso8601>"
            },
            ...
          ]
        }
    """
    if not admin_state.engine or not admin_state.engine.minio_client:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    import asyncio
    import json as _json

    minio_client = admin_state.engine.minio_client
    bucket = _shared.cfg.NCE_ANCHOR_BUCKET

    def _read_anchor_status() -> list[dict[str, Any]]:
        from minio.error import S3Error

        results: list[dict[str, Any]] = []
        try:
            objects = minio_client.list_objects(bucket, recursive=False)
        except S3Error:
            # Bucket may not exist yet (no anchors written).
            return results

        # Each namespace is a "directory" prefix <namespace_id>/
        ns_prefixes: set[str] = set()
        for obj in objects:
            # list_objects with recursive=False returns pseudo-dirs with is_dir=True
            if obj.is_dir and obj.object_name:
                ns_prefixes.add(obj.object_name.rstrip("/"))

        for prefix in sorted(ns_prefixes):
            # Find the object with the highest numeric basename (= max_seq)
            try:
                children = list(
                    minio_client.list_objects(bucket, prefix=f"{prefix}/", recursive=False)
                )
            except S3Error:
                continue

            best_seq: int | None = None
            best_name: str | None = None
            for child in children:
                name = child.object_name or ""
                basename = name.rsplit("/", 1)[-1].replace(".json", "")
                try:
                    seq = int(basename)
                except ValueError:
                    continue
                if best_seq is None or seq > best_seq:
                    best_seq = seq
                    best_name = name

            if best_name is None:
                continue

            try:
                response = minio_client.get_object(bucket, best_name)
                try:
                    blob = response.read()
                finally:
                    response.close()
                    response.release_conn()
                data = _json.loads(blob.decode("utf-8"))
                results.append(
                    {
                        "namespace_id": data.get("namespace_id", prefix),
                        "max_seq": data.get("max_seq"),
                        "chain_hash": data.get("chain_hash"),
                        "anchored_at": data.get("anchored_at"),
                    }
                )
            except Exception:  # noqa: BLE001
                results.append(
                    {
                        "namespace_id": prefix,
                        "max_seq": best_seq,
                        "chain_hash": None,
                        "anchored_at": None,
                        "error": "failed to read anchor blob",
                    }
                )

        return results

    try:
        items = await asyncio.to_thread(_read_anchor_status)
    except Exception as exc:
        return admin_error_response(
            "Internal server error", exc, log_event="api_admin_anchor_status failed"
        )

    return JSONResponse({"bucket": bucket, "namespaces": items})


# ---------------------------------------------------------------------------
# Batch 121: DLQ circuit-breaker close endpoint
# ---------------------------------------------------------------------------

_TASK_NAME_RE = re.compile(r"^[a-zA-Z0-9_./-]{1,128}$")


async def api_admin_dlq_circuit_close(request):
    """POST /api/admin/dlq/circuit/{task_name}/close

    Admin endpoint: clears the Redis circuit-open flag for *task_name* so new
    enqueues of that task type are allowed again.  The operator must verify the
    underlying deterministic failure has been resolved before closing the circuit.

    Path params:
      task_name — the RQ task function name (e.g. ``process_code_indexing``)
    """
    task_name = request.path_params.get("task_name", "")
    if not task_name or not _TASK_NAME_RE.fullmatch(task_name):
        return JSONResponse(
            {"error": "task_name path parameter is missing or contains invalid characters"},
            status_code=422,
        )

    try:
        from redis import Redis

        from nce.dead_letter_queue import close_circuit, is_circuit_open

        redis_client = Redis.from_url(_shared.cfg.REDIS_URL)
        was_open = is_circuit_open(redis_client, task_name)
        close_circuit(redis_client, task_name)
        redis_client.close()
    except Exception as exc:
        return admin_error_response(
            "Internal server error",
            exc,
            log_event="api_admin_dlq_circuit_close failed",
        )

    _shared.logger.warning(
        "audit dlq_circuit_close task_name=%s was_open=%s",
        task_name,
        was_open,
    )
    return JSONResponse(
        {
            "status": "ok",
            "task_name": task_name,
            "was_open": was_open,
            "message": (
                f"Circuit for '{task_name}' closed — enqueues are re-enabled."
                if was_open
                else f"Circuit for '{task_name}' was already closed (no-op)."
            ),
        }
    )
