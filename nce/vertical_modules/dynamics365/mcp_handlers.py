"""
nce/vertical_modules/dynamics365/mcp_handlers.py
=================================================
MCP tool handlers for the Dynamics 365 vertical module.

Registered in ``nce/tool_registry.py`` following the standard
``async (engine, arguments) -> str`` signature pattern.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.vertical_modules.dynamics365.mcp_handlers")


async def handle_d365_query_case(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """
    Fetch a Dynamics 365 case from Dataverse and enrich it with NCE graph context.

    Arguments
    ---------
    namespace_id : str
    case_id      : str   — Dataverse incident GUID
    include_notes : bool — also fetch linked annotations (default True)
    include_activities : bool — also fetch activity timeline (default False)
    """
    namespace_id = arguments.get("namespace_id", "")
    case_id = arguments.get("case_id", "")
    include_notes = bool(arguments.get("include_notes", True))
    include_activities = bool(arguments.get("include_activities", False))

    if not case_id:
        return json.dumps({"error": "case_id is required"})

    from nce.config import cfg
    from nce.vertical_modules.dynamics365.auth import DataverseTokenManager
    from nce.vertical_modules.dynamics365.client import DataverseClient

    try:
        import redis.asyncio as aioredis

        redis_client = aioredis.from_url(cfg.REDIS_URL)
        token_mgr = DataverseTokenManager(redis_client)
        token = await token_mgr.get_access_token()
        client = DataverseClient(cfg.NCE_D365_ORG_URL, token)

        case = await client.get_entity(
            "incidents",
            case_id,
            select=[
                "incidentid",
                "ticketnumber",
                "title",
                "description",
                "prioritycode",
                "statuscode",
                "_customerid_value",
                "_ownerid_value",
            ],
        )

        result: dict[str, Any] = {"case": case}

        if include_notes:
            notes = []
            async for annotation in client.paginate(
                "annotations",
                filter_expr=f"_objectid_value eq {case_id}",
                select=["annotationid", "notetext", "subject", "createdon"],
                page_size=50,
            ):
                notes.append(annotation)
            result["notes"] = notes

        if include_activities:
            activities = []
            async for act in client.paginate(
                "activitypointers",
                filter_expr=f"_regardingobjectid_value eq {case_id}",
                select=["activityid", "activitytypecode", "subject", "createdon"],
                page_size=50,
            ):
                activities.append(act)
            result["activities"] = activities

        # Enrich with NCE graph context
        ticket = case.get("ticketnumber") or case_id
        from nce.db_utils import scoped_pg_session

        async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
            edges = await conn.fetch(
                """
                SELECT subject_label, predicate, object_label, confidence
                FROM kg_edges
                WHERE namespace_id = $1::uuid
                  AND (subject_label LIKE $2 OR object_label LIKE $2)
                ORDER BY confidence DESC
                LIMIT 20
                """,
                namespace_id,
                f"Incident:{ticket}%",
            )
        result["graph_context"] = [dict(e) for e in edges]
        return json.dumps(result, default=str)

    except Exception as exc:
        log.exception("handle_d365_query_case failed case_id=%s", case_id)
        return json.dumps({"error": str(exc)})


async def handle_d365_sync_now(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """
    Trigger an immediate Dataverse entity sync for a namespace.

    Arguments
    ---------
    namespace_id  : str
    entity_types  : list[str] | None  — subset to sync; omit for all
    """
    namespace_id = arguments.get("namespace_id", "")
    entity_types = arguments.get("entity_types")

    if not namespace_id:
        return json.dumps({"error": "namespace_id is required"})

    from nce.config import cfg
    from nce.db_utils import scoped_pg_session
    from nce.vertical_modules.dynamics365.auth import DataverseTokenManager
    from nce.vertical_modules.dynamics365.client import DataverseClient
    from nce.vertical_modules.dynamics365.sync import DataverseSyncEngine

    try:
        import uuid

        import redis.asyncio as aioredis

        redis_client = aioredis.from_url(cfg.REDIS_URL)
        token_mgr = DataverseTokenManager(redis_client)
        token = await token_mgr.get_access_token()
        client = DataverseClient(cfg.NCE_D365_ORG_URL, token)

        async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
            sync_engine = DataverseSyncEngine(conn, uuid.UUID(namespace_id), client)
            stats = await sync_engine.run_full_sync(entity_types)

        log.info("d365_sync_now completed namespace=%s stats=%s", namespace_id, stats)
        return json.dumps({"status": "completed", "stats": stats})

    except Exception as exc:
        log.exception("handle_d365_sync_now failed namespace=%s", namespace_id)
        return json.dumps({"error": str(exc)})


async def handle_d365_case_stress_report(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """
    Empathic Tensor frustration/stress report for cases linked to an account.

    Queries ``v3_cognitive_ledger`` for case-linked memories, analyses the
    frustration trend using ``StressTracker``, and returns the burnout signal.

    Arguments
    ---------
    namespace_id  : str
    account_name  : str
    lookback_days : int  — default 30
    """
    namespace_id = arguments.get("namespace_id", "")
    account_name = arguments.get("account_name", "")
    lookback_days = int(arguments.get("lookback_days", 30))

    if not namespace_id or not account_name:
        return json.dumps({"error": "namespace_id and account_name are required"})

    try:
        from nce.db_utils import scoped_pg_session

        async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
            # Find incident memories linked to this account via kg_edges
            incident_edges = await conn.fetch(
                """
                SELECT subject_label
                FROM kg_edges
                WHERE namespace_id = $1::uuid
                  AND predicate = 'REPORTED_BY'
                  AND object_label = $2
                LIMIT 100
                """,
                namespace_id,
                f"Account:{account_name}",
            )
            incident_labels = [r["subject_label"] for r in incident_edges]

            # Pull Empathic Tensor data for linked memories
            if not incident_labels:
                return json.dumps(
                    {
                        "account_name": account_name,
                        "incident_count": 0,
                        "frustration_trend": [],
                        "burnout_alert": False,
                        "message": "No incidents found for this account in the graph.",
                    }
                )

            tensor_rows = await conn.fetch(
                f"""
                SELECT cl.empathic_tensor, cl.created_at
                FROM v3_cognitive_ledger cl
                JOIN kg_edges ke ON ke.object_label LIKE 'Annotation:%'
                    AND ke.predicate = 'HAS_NOTE'
                    AND ke.subject_label = ANY($1::text[])
                    AND ke.namespace_id = $2::uuid
                JOIN memories m ON m.payload_ref = substring(ke.object_label FROM 13)
                    AND m.namespace_id = $2::uuid
                    AND m.id = cl.memory_id
                WHERE cl.namespace_id = $2::uuid
                  AND cl.created_at >= NOW() - INTERVAL '{int(lookback_days)} days'
                ORDER BY cl.created_at ASC
                LIMIT 200
                """,
                incident_labels,
                namespace_id,
            )

        frustration_trend = [
            float(row["empathic_tensor"][5]) if row["empathic_tensor"] else 0.0
            for row in tensor_rows
        ]

        # Burnout alert: last 5 readings all above threshold
        burnout_threshold = 7.0
        recent = frustration_trend[-5:]
        burnout_alert = len(recent) >= 3 and all(f > burnout_threshold for f in recent)

        avg_frustration = (
            round(sum(frustration_trend) / len(frustration_trend), 2) if frustration_trend else 0.0
        )

        return json.dumps(
            {
                "account_name": account_name,
                "incident_count": len(incident_labels),
                "lookback_days": lookback_days,
                "note_readings": len(frustration_trend),
                "frustration_trend": [round(f, 2) for f in frustration_trend],
                "avg_frustration": avg_frustration,
                "burnout_alert": burnout_alert,
                "burnout_threshold": burnout_threshold,
            }
        )

    except Exception as exc:
        log.exception(
            "handle_d365_case_stress_report failed namespace=%s account=%s",
            namespace_id,
            account_name,
        )
        return json.dumps({"error": str(exc)})


async def handle_d365_list_sla_breaches(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """
    List SLA breach events from the WORM ``event_log``.

    Arguments
    ---------
    namespace_id : str
    since        : str — ISO-8601 datetime
    limit        : int — default 50, max 500
    """
    namespace_id = arguments.get("namespace_id", "")
    since = arguments.get("since", "")
    limit = min(int(arguments.get("limit", 50)), 500)

    if not namespace_id or not since:
        return json.dumps({"error": "namespace_id and since are required"})

    try:
        from nce.db_utils import scoped_pg_session

        async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
            rows = await conn.fetch(
                """
                SELECT id, agent_id, event_type, params, created_at
                FROM event_log
                WHERE namespace_id = $1::uuid
                  AND event_type = 'd365_sla_breach'
                  AND created_at >= $2::timestamptz
                ORDER BY created_at DESC
                LIMIT $3
                """,
                namespace_id,
                since,
                limit,
            )

        breaches = [
            {
                "event_id": str(r["id"]),
                "agent_id": r["agent_id"],
                "params": r["params"] if isinstance(r["params"], dict) else {},
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ]

        return json.dumps(
            {
                "namespace_id": namespace_id,
                "since": since,
                "count": len(breaches),
                "breaches": breaches,
            }
        )

    except Exception as exc:
        log.exception("handle_d365_list_sla_breaches failed namespace=%s", namespace_id)
        return json.dumps({"error": str(exc)})


async def handle_d365_netbox_mappings(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """
    Query the D365 ↔ NetBox cross-reference mapping table.

    Returns confirmed and inferred identity links between D365 Accounts/Functional
    Locations and NetBox Tenants/Sites for a namespace.

    Arguments
    ---------
    namespace_id  : str — required
    entity_type   : str — filter: 'account' | 'functional_location' | 'all' (default 'all')
    confirmed_only: bool — only return human-confirmed mappings (default False)
    limit         : int — max rows (default 100)
    """
    namespace_id = arguments.get("namespace_id", "")
    entity_type = arguments.get("entity_type", "all")
    confirmed_only = bool(arguments.get("confirmed_only", False))
    limit = min(int(arguments.get("limit", 100)), 500)

    if not namespace_id:
        return json.dumps({"error": "namespace_id is required"})

    try:
        where = ["namespace_id = $1::uuid"]
        args: list[Any] = [namespace_id]
        i = 2

        if entity_type != "all" and entity_type in ("account", "functional_location"):
            where.append(f"d365_entity_type = ${i}")
            args.append(entity_type)
            i += 1
        if confirmed_only:
            where.append("confirmed = TRUE")

        where_sql = "WHERE " + " AND ".join(where)

        async with engine.pg_pool.acquire(timeout=10.0) as conn:
            rows = await conn.fetch(
                f"""
                SELECT id, d365_entity_type, d365_entity_name, d365_entity_id,
                       nb_entity_type, nb_entity_name, nb_entity_slug, nb_entity_id,
                       match_method, match_confidence, confirmed, created_at, updated_at
                FROM d365_netbox_mappings
                {where_sql}
                ORDER BY match_confidence DESC, d365_entity_name
                LIMIT ${i}
                """,
                *args,
                limit,
            )

        mappings = [
            {
                "id": str(r["id"]),
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
                "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
            }
            for r in rows
        ]

        return json.dumps(
            {
                "namespace_id": namespace_id,
                "count": len(mappings),
                "entity_type_filter": entity_type,
                "confirmed_only": confirmed_only,
                "mappings": mappings,
            },
            default=str,
        )

    except Exception as exc:
        log.exception("handle_d365_netbox_mappings failed namespace=%s", namespace_id)
        return json.dumps({"error": str(exc)})


async def handle_d365_sync_status(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """
    Return recent Dynamics 365 sync-run audit rows for the namespace.

    Arguments
    ---------
    namespace_id : str
    limit        : int — max rows to return (default 50, capped at 500)
    """
    namespace_id = arguments.get("namespace_id", "")
    if not namespace_id:
        return json.dumps({"error": "namespace_id is required"})

    try:
        limit = int(arguments.get("limit", 50) or 50)
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, min(limit, 500))

    try:
        from nce.db_utils import scoped_pg_session

        async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
            rows = await conn.fetch(
                """
                SELECT run_id, entity, upserted, incremental, status, error,
                       started_at, finished_at
                FROM d365_sync_runs
                WHERE namespace_id = $1::uuid
                ORDER BY started_at DESC
                LIMIT $2
                """,
                namespace_id,
                limit,
            )
        runs = [dict(r) for r in rows]
        return json.dumps(
            {"namespace_id": namespace_id, "count": len(runs), "runs": runs},
            default=str,
        )
    except Exception as exc:
        log.exception("handle_d365_sync_status failed namespace=%s", namespace_id)
        return json.dumps({"error": str(exc)})
