"""Admin HTTP handlers for the Dynamics 365 / Dataverse vertical module."""

from __future__ import annotations

import json
import uuid

from starlette.responses import JSONResponse

from nce import admin_state
from nce.admin_handlers._shared import (
    admin_error_response,
    admin_validation_error,
    cfg,
    logger,
    offset_from_page_limit,
    parse_optional_uuid,
    parse_page_limit_common,
)


async def api_admin_d365_config(request):
    """GET /api/admin/d365/config — return current D365 configuration (no secrets)."""
    return JSONResponse(
        {
            "enabled": cfg.NCE_D365_ENABLED,
            "org_url": cfg.NCE_D365_ORG_URL or None,
            "api_version": cfg.NCE_D365_API_VERSION,
            "sync_interval_minutes": cfg.NCE_D365_SYNC_INTERVAL_MINUTES,
            "sync_page_size": cfg.NCE_D365_SYNC_PAGE_SIZE,
            "high_priority_salience_boost": cfg.NCE_D365_HIGH_PRIORITY_SALIENCE_BOOST,
            "webhook_secret_set": bool(cfg.NCE_D365_WEBHOOK_SECRET),
            "empathic_urgency_keywords": cfg.NCE_D365_EMPATHIC_URGENCY_KEYWORDS,
            "empathic_frustration_keywords": cfg.NCE_D365_EMPATHIC_FRUSTRATION_KEYWORDS,
        }
    )


async def api_admin_d365_integrations(request):
    """GET /api/admin/d365/integrations — list d365_integrations rows."""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        qp = request.query_params
        page, limit = parse_page_limit_common(qp)
        offset = offset_from_page_limit(page, limit)
    except ValueError as exc:
        return admin_validation_error(exc, status_code=422)

    try:
        async with admin_state.engine.pg_pool.acquire(timeout=10.0) as conn:
            count_row = await conn.fetchrow(
                "SELECT COUNT(*)::bigint AS total FROM d365_integrations"
            )
            rows = await conn.fetch(
                """
                SELECT
                    i.id,
                    i.namespace_id,
                    n.slug AS namespace_slug,
                    i.org_url,
                    i.status,
                    i.last_sync_at,
                    i.last_sync_stats,
                    i.created_at,
                    i.updated_at,
                    (n.metadata->'d365'->>'enabled')::boolean AS d365_enabled
                FROM d365_integrations i
                JOIN namespaces n ON n.id = i.namespace_id
                ORDER BY i.created_at DESC
                LIMIT $1 OFFSET $2
                """,
                limit,
                offset,
            )
    except Exception as exc:
        return admin_error_response("Failed to fetch D365 integrations", exc, status_code=500)

    items = [
        {
            "id": str(r["id"]),
            "namespace_id": str(r["namespace_id"]),
            "namespace_slug": r["namespace_slug"],
            "org_url": r["org_url"],
            "status": r["status"],
            "last_sync_at": r["last_sync_at"].isoformat() if r["last_sync_at"] else None,
            "last_sync_stats": r["last_sync_stats"],
            "created_at": r["created_at"].isoformat(),
            "updated_at": r["updated_at"].isoformat(),
            "d365_enabled": bool(r["d365_enabled"]),
        }
        for r in rows
    ]

    return JSONResponse({"total": count_row["total"], "items": items})


async def api_admin_d365_sync_now(request):
    """POST /api/admin/d365/sync — trigger an immediate D365 sync for a namespace.

    Body (JSON):
        namespace_id   str (UUID, required)
        entity_types   list[str] optional — subset of accounts/contacts/opportunities/incidents
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    if not cfg.NCE_D365_ENABLED:
        return JSONResponse({"error": "NCE_D365_ENABLED is false"}, status_code=409)

    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError, TypeError):
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    try:
        ns_id = uuid.UUID(body["namespace_id"])
    except (KeyError, ValueError) as exc:
        return JSONResponse({"error": f"namespace_id: {exc}"}, status_code=422)

    entity_types = body.get("entity_types") or None

    try:
        import asyncio

        from nce.db_utils import scoped_pg_session
        from nce.vertical_modules.dynamics365.auth import DataverseTokenManager
        from nce.vertical_modules.dynamics365.client import DataverseClient
        from nce.vertical_modules.dynamics365.sync import DataverseSyncEngine

        async def _run_sync():
            token_mgr = DataverseTokenManager(admin_state.engine.redis_client)
            token = await token_mgr.get_access_token()
            client = DataverseClient(
                org_url=cfg.NCE_D365_ORG_URL,
                access_token=token,
                api_version=cfg.NCE_D365_API_VERSION,
                page_size=cfg.NCE_D365_SYNC_PAGE_SIZE,
            )
            # Tenant-scoped transaction (mirrors cron.py:433 and tasks.py:651) so
            # the kg-write, its transactional-outbox emit, and the status UPDATE
            # all commit or roll back together under RLS — never split-brain.
            async with scoped_pg_session(admin_state.engine.pg_pool, str(ns_id)) as conn:
                engine = DataverseSyncEngine(conn=conn, namespace_id=ns_id, client=client)
                stats = await engine.run_full_sync(entity_types=entity_types)
                await conn.execute(
                    """
                    UPDATE d365_integrations
                    SET last_sync_at = NOW(),
                        last_sync_stats = $1::jsonb,
                        updated_at = NOW()
                    WHERE namespace_id = $2
                    """,
                    json.dumps(stats),
                    ns_id,
                )
                return stats

        stats = await asyncio.wait_for(_run_sync(), timeout=300.0)
        return JSONResponse({"status": "ok", "stats": stats})

    except asyncio.TimeoutError:
        return JSONResponse({"error": "Sync timed out (300s)"}, status_code=504)
    except Exception as exc:
        return admin_error_response("D365 sync failed", exc, status_code=500)


async def api_admin_d365_sla_breaches(request):
    """GET /api/admin/d365/sla-breaches — WORM event_log entries for d365_sla_breach."""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        qp = request.query_params
        namespace_id = parse_optional_uuid(qp.get("namespace_id"))
        page, limit = parse_page_limit_common(qp)
        offset = offset_from_page_limit(page, limit)
    except ValueError as exc:
        return admin_validation_error(exc, status_code=422)

    where = ["event_type = 'd365_sla_breach'"]
    args: list = []
    i = 1
    if namespace_id:
        where.append(f"namespace_id = ${i}")
        args.append(namespace_id)
        i += 1

    where_sql = "WHERE " + " AND ".join(where)

    try:
        async with admin_state.engine.pg_pool.acquire(timeout=10.0) as conn:
            count_row = await conn.fetchrow(
                f"SELECT COUNT(*)::bigint AS total FROM event_log {where_sql}", *args
            )
            rows = await conn.fetch(
                f"""
                SELECT id, namespace_id, agent_id, event_type, event_seq,
                       occurred_at, params, result_summary
                FROM event_log
                {where_sql}
                ORDER BY occurred_at DESC
                LIMIT ${i} OFFSET ${i + 1}
                """,
                *args,
                limit,
                offset,
            )
    except Exception as exc:
        return admin_error_response("Failed to fetch SLA breaches", exc, status_code=500)

    items = [
        {
            "id": str(r["id"]),
            "namespace_id": str(r["namespace_id"]),
            "agent_id": r["agent_id"],
            "event_seq": r["event_seq"],
            "occurred_at": r["occurred_at"].isoformat(),
            "params": r["params"],
            "result_summary": r["result_summary"],
        }
        for r in rows
    ]

    return JSONResponse({"total": count_row["total"], "items": items})


async def api_admin_d365_netbox_mappings(request):
    """GET /api/admin/d365/netbox-mappings — list cross-reference mapping rows.

    Query params:
      namespace_id?, d365_entity_type?, confirmed?, page=1, limit=50
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        qp = request.query_params
        namespace_id = parse_optional_uuid(qp.get("namespace_id"))
        entity_type = qp.get("d365_entity_type", "all")
        confirmed_filter = qp.get("confirmed")
        page, limit = parse_page_limit_common(qp)
        offset = offset_from_page_limit(page, limit)
    except ValueError as exc:
        return admin_validation_error(exc, status_code=422)

    where = []
    args: list = []
    i = 1
    if namespace_id:
        where.append(f"namespace_id = ${i}")
        args.append(namespace_id)
        i += 1
    if entity_type and entity_type != "all":
        if entity_type in ("account", "functional_location"):
            where.append(f"d365_entity_type = ${i}")
            args.append(entity_type)
            i += 1
    if confirmed_filter is not None:
        confirmed_bool = confirmed_filter.lower() in ("1", "true", "yes")
        where.append(f"confirmed = ${i}")
        args.append(confirmed_bool)
        i += 1

    where_sql = "WHERE " + " AND ".join(where) if where else ""

    try:
        async with admin_state.engine.pg_pool.acquire(timeout=10.0) as conn:
            count_row = await conn.fetchrow(
                f"SELECT COUNT(*)::bigint AS total FROM d365_netbox_mappings {where_sql}",
                *args,
            )
            rows = await conn.fetch(
                f"""
                SELECT id, namespace_id, d365_entity_type, d365_entity_name, d365_entity_id,
                       nb_entity_type, nb_entity_name, nb_entity_slug, nb_entity_id,
                       match_method, match_confidence, confirmed, created_at, updated_at
                FROM d365_netbox_mappings
                {where_sql}
                ORDER BY match_confidence DESC, d365_entity_name
                LIMIT ${i} OFFSET ${i + 1}
                """,
                *args,
                limit,
                offset,
            )
    except Exception as exc:
        return admin_error_response("Failed to fetch D365↔NetBox mappings", exc, status_code=500)

    items = [
        {
            "id": str(r["id"]),
            "namespace_id": str(r["namespace_id"]),
            "d365_entity_type": r["d365_entity_type"],
            "d365_entity_name": r["d365_entity_name"],
            "d365_entity_id": r["d365_entity_id"],
            "nb_entity_type": r["nb_entity_type"],
            "nb_entity_name": r["nb_entity_name"],
            "nb_entity_slug": r["nb_entity_slug"],
            "nb_entity_id": r["nb_entity_id"],
            "match_method": r["match_method"],
            "match_confidence": round(float(r["match_confidence"]), 4),
            "confirmed": bool(r["confirmed"]),
            "created_at": r["created_at"].isoformat(),
            "updated_at": r["updated_at"].isoformat(),
        }
        for r in rows
    ]
    return JSONResponse({"total": count_row["total"], "items": items})


async def api_admin_d365_netbox_mapping_confirm(request):
    """POST /api/admin/d365/netbox-mappings/{mapping_id}/confirm

    Body (JSON):
        confirmed   bool (required) — set to true/false
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    mapping_id_str = request.path_params.get("mapping_id", "")
    try:
        mapping_id = uuid.UUID(mapping_id_str)
    except ValueError:
        return JSONResponse({"error": "mapping_id is not a valid UUID"}, status_code=422)

    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError, TypeError):
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    confirmed = body.get("confirmed")
    if not isinstance(confirmed, bool):
        return JSONResponse({"error": "'confirmed' must be a boolean"}, status_code=422)

    try:
        async with admin_state.engine.pg_pool.acquire(timeout=10.0) as conn:
            result = await conn.fetchrow(
                """
                UPDATE d365_netbox_mappings
                SET confirmed = $1, updated_at = NOW()
                WHERE id = $2
                RETURNING id, d365_entity_name, nb_entity_name, confirmed
                """,
                confirmed,
                mapping_id,
            )
            if not result:
                return JSONResponse({"error": "Mapping not found"}, status_code=404)

        logger.info(
            "D365↔NetBox mapping %s confirmed=%s (%s → %s)",
            mapping_id,
            confirmed,
            result["d365_entity_name"],
            result["nb_entity_name"],
        )
        return JSONResponse(
            {
                "id": str(result["id"]),
                "d365_entity_name": result["d365_entity_name"],
                "nb_entity_name": result["nb_entity_name"],
                "confirmed": bool(result["confirmed"]),
            }
        )

    except Exception as exc:
        return admin_error_response("Failed to update mapping confirmation", exc, status_code=500)


async def api_admin_d365_netbox_bridge_sync(request):
    """POST /api/admin/d365/netbox-bridge/sync — trigger an immediate bridge sync.

    Body (JSON):
        namespace_id   str (UUID, required)
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    if not cfg.NCE_D365_ENABLED:
        return JSONResponse({"error": "NCE_D365_ENABLED is false"}, status_code=409)
    if not cfg.NCE_NETBOX_URL or not cfg.NCE_NETBOX_TOKEN:
        return JSONResponse(
            {"error": "NCE_NETBOX_URL or NCE_NETBOX_TOKEN not configured"}, status_code=409
        )

    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError, TypeError):
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    try:
        ns_id = uuid.UUID(body["namespace_id"])
    except (KeyError, ValueError) as exc:
        return JSONResponse({"error": f"namespace_id: {exc}"}, status_code=422)

    try:
        import asyncio

        from nce.vertical_modules.dynamics365.auth import DataverseTokenManager
        from nce.vertical_modules.dynamics365.client import DataverseClient
        from nce.vertical_modules.dynamics365.netbox_bridge import (
            D365NetBoxBridge,
            NetBoxBridgeClient,
        )

        async def _run_bridge():
            token_mgr = DataverseTokenManager(admin_state.engine.redis_client)
            token = await token_mgr.get_access_token()
            d365_client = DataverseClient(
                org_url=cfg.NCE_D365_ORG_URL,
                access_token=token,
                api_version=cfg.NCE_D365_API_VERSION,
                page_size=cfg.NCE_D365_SYNC_PAGE_SIZE,
            )
            nb_client = NetBoxBridgeClient(
                base_url=cfg.NCE_NETBOX_URL,
                token=cfg.NCE_NETBOX_TOKEN,
            )
            async with admin_state.engine.pg_pool.acquire(timeout=300.0) as conn:
                bridge = D365NetBoxBridge(conn, ns_id, d365_client, nb_client)
                return await bridge.run_full_bridge_sync()

        stats = await asyncio.wait_for(_run_bridge(), timeout=300.0)
        return JSONResponse({"status": "ok", "stats": stats})

    except asyncio.TimeoutError:
        return JSONResponse({"error": "Bridge sync timed out (300s)"}, status_code=504)
    except Exception as exc:
        return admin_error_response("D365↔NetBox bridge sync failed", exc, status_code=500)


async def api_admin_d365_namespace_update(request):
    """POST /api/admin/d365/namespace/{ns_id}/d365-enabled

    Body (JSON):
        enabled   bool (required)

    Merges `{"d365": {"enabled": <bool>}}` into the namespace metadata column.
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    ns_id_str = request.path_params.get("ns_id", "")
    try:
        ns_id = uuid.UUID(ns_id_str)
    except ValueError:
        return JSONResponse({"error": "ns_id is not a valid UUID"}, status_code=422)

    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError, TypeError):
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        return JSONResponse({"error": "'enabled' must be a boolean"}, status_code=422)

    try:
        async with admin_state.engine.pg_pool.acquire(timeout=10.0) as conn:
            row = await conn.fetchrow("SELECT id, metadata FROM namespaces WHERE id = $1", ns_id)
            if not row:
                return JSONResponse({"error": "Namespace not found"}, status_code=404)

            existing = row["metadata"] or {}
            if isinstance(existing, str):
                existing = json.loads(existing)

            d365_block = existing.get("d365") or {}
            d365_block["enabled"] = enabled
            existing["d365"] = d365_block

            await conn.execute(
                "UPDATE namespaces SET metadata = $1::jsonb, updated_at = NOW() WHERE id = $2",
                json.dumps(existing),
                ns_id,
            )

        logger.info("D365 namespace update: ns=%s enabled=%s", ns_id, enabled)
        return JSONResponse({"namespace_id": str(ns_id), "d365_enabled": enabled})

    except Exception as exc:
        return admin_error_response("Failed to update namespace D365 config", exc, status_code=500)
