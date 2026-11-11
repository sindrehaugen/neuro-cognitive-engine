"""Starlette admin application factory (routes, middleware, lifespan)."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from contextlib import asynccontextmanager
from typing import Any

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route

from nce import admin_http_handlers as h
from nce import admin_state
from nce.admin_handlers import entity_resolution as entity_resolution_handlers
from nce.admin_handlers import pricing as pricing_handlers
from nce.admin_plugins import (
    admin_plugins_api,
    admin_plugins_console,
    admin_plugins_mint_token,
    admin_plugins_toggle,
)
from nce.auth import (
    AdminHTTPRateLimitMiddleware,
    BasicAuthMiddleware,
    HMACAuthMiddleware,
    optional_hmac_nonce_store,
)
from nce.config import cfg
from nce.http_body_limit import BoundedBodyMiddleware
from nce.mtls import MTLSAuthMiddleware
from nce.notifications import dispatcher
from nce.observability import OpenTelemetryTraceMiddleware
from nce.orchestrator import NCEEngine
from nce.resource_surface import build_all_resource_routes

logger = logging.getLogger("nce-admin")

_hmac_nonce_store = optional_hmac_nonce_store()

# ---------------------------------------------------------------------------
# C3 principal-session wiring (Wave 23)
# ---------------------------------------------------------------------------
# admin_app is protected by HMAC + mTLS — only internal employee/agent principals
# can reach it.  External principals (contractor, external-customer) are never
# authenticated here.
#
# Contract: the nce.external_scope_id GUC is NEVER set on admin_app sessions.
# Leaving it unset is the correct employee behaviour: employee sessions use the
# existing tenant_isolation_policy (namespace_id only); unset external_scope_id
# means deny-when-unset on external_isolation_policy tables, which is correct —
# employees reach those tables through service-layer logic, not direct RLS paths.
#
# If a future flow inside admin_app needs to impersonate an external principal
# (e.g. a support-impersonation endpoint), it must explicitly call
#   await set_external_scope(conn, scope_id)
# inside the scoped_pg_session block for that request only.
ADMIN_PRINCIPAL_KIND: str = "employee"


@asynccontextmanager
async def admin_lifespan(app):
    from nce.config import assert_admin_override_not_in_production
    from nce.mtls import assert_server_mtls_or_acknowledged

    assert_admin_override_not_in_production()

    admin_state.engine = NCEEngine()
    await admin_state.engine.connect()
    app.state.redis_client = admin_state.engine.redis_client

    # Zero-trust transport boot guard (Batch 115): refuse to start a prod admin
    # surface with mTLS disabled unless explicitly acknowledged. Runs after the
    # engine is connected so the acknowledged path can write its WORM audit event.
    await assert_server_mtls_or_acknowledged(
        service="admin",
        mtls_enabled=cfg.NCE_ADMIN_MTLS_ENABLED,
        pg_pool=admin_state.engine.pg_pool,
    )

    await dispatcher.start_worker()
    logger.info("NCE Admin: engine connected, dispatcher started.")

    try:
        from nce.observability import EVENT_LOG_PARTITION_MONTHS_AHEAD

        async with admin_state.engine.pg_pool.acquire(timeout=10.0) as conn:
            await conn.execute(
                f"SELECT nce_ensure_event_log_monthly_partitions({cfg.NCE_PARTITION_LOOKAHEAD_MONTHS})"
            )
            row = await conn.fetchrow(
                """
                SELECT count(*) AS cnt
                FROM pg_inherits i
                JOIN pg_class c ON c.oid = i.inhrelid
                WHERE i.inhparent = 'event_log'::regclass
                  AND c.relname LIKE 'event_log_%'
                  AND c.relname >= 'event_log_' || to_char(now(), 'YYYY_MM')
                """
            )
            months_ahead = row["cnt"] if row else 0
            EVENT_LOG_PARTITION_MONTHS_AHEAD.set(months_ahead)
            if months_ahead < 2:
                logger.warning(
                    "event_log partition runway low: %s months ahead (need >= 2)",
                    months_ahead,
                )
            else:
                logger.info("event_log partition runway: %s months ahead", months_ahead)
    except Exception:
        logger.exception("event_log partition startup check failed")

    yield
    await dispatcher.stop_worker()
    await admin_state.engine.disconnect()
    logger.info("NCE Admin: shutdown complete.")


async def get_healthz(request):
    """Unauthenticated liveness probe that actually probes.

    Wave I-13. This returned a literal ``{"status": "ok"}`` regardless of the engine's
    real state, so the compose healthcheck it backs could not fail. It now reports the
    engine's verdict and its security block, and answers 503 when a degradation is
    blocking. Still unauthenticated, so it exposes only status strings -- never a key,
    a fingerprint, or a row.
    """
    engine = getattr(admin_state, "engine", None)
    if engine is None:
        return JSONResponse({"status": "down", "reason": "engine_not_initialised"}, status_code=503)
    try:
        res = await engine.check_health()
    except Exception:
        logger.exception("/healthz: check_health raised")
        return JSONResponse({"status": "down", "reason": "health_probe_raised"}, status_code=503)

    from nce.orchestrator import health_is_blocking

    body = {
        "status": res.get("status", "unknown"),
        "security": res.get("security", {}),
        "databases": res.get("databases", {}),
        "degraded_reasons": res.get("degraded_reasons", []),
    }
    return JSONResponse(body, status_code=503 if health_is_blocking(res) else 200)


def build_admin_middleware() -> list[Middleware]:
    return [
        Middleware(BoundedBodyMiddleware),
        Middleware(OpenTelemetryTraceMiddleware),
        Middleware(AdminHTTPRateLimitMiddleware),
        Middleware(
            MTLSAuthMiddleware,
            protected_prefix="/api/",
            enabled=cfg.NCE_ADMIN_MTLS_ENABLED,
            strict=cfg.NCE_ADMIN_MTLS_STRICT,
            trusted_proxy_hops=cfg.NCE_ADMIN_MTLS_TRUSTED_PROXY_HOP,
            allowed_sans=cfg.NCE_ADMIN_MTLS_ALLOWED_SANS,
            allowed_fingerprints=cfg.NCE_ADMIN_MTLS_ALLOWED_FINGERPRINTS,
        ),
        Middleware(
            BasicAuthMiddleware,
            protected_prefix="/",
            excluded_prefixes=("/api/", "/admin/plugins/api", "/healthz", "/public-api/"),
            username=cfg.NCE_ADMIN_USERNAME,
            password=cfg.NCE_ADMIN_PASSWORD,
            realm="NCE Admin",
        ),
        Middleware(
            HMACAuthMiddleware,
            protected_prefix="/api/",
            api_key=cfg.NCE_API_KEY,
            nonce_store=_hmac_nonce_store,
        ),
    ]


def _bind(handler: Any, ctx: Any = None) -> Any:
    """Bind an admin handler to context or request."""

    async def _endpoint(request: Any) -> Any:
        try:
            return await handler(request, ctx)
        except TypeError:
            return await handler(request)

    _endpoint.__name__ = getattr(handler, "__name__", "_endpoint")
    _endpoint.__doc__ = getattr(handler, "__doc__", None)
    return _endpoint


def build_admin_routes(ctx: Any = None) -> list[Route]:
    routes: list[Route] = [
        Route("/healthz", endpoint=get_healthz, methods=["GET"]),
        Route("/", endpoint=h.serve_index),
        Route("/styles.css", endpoint=h.serve_styles),
        Route("/api/health", endpoint=h.get_health, methods=["GET"]),
        Route("/api/health/v1", endpoint=h.get_health_v1, methods=["GET"]),
        Route(
            "/api/health/degradations",
            endpoint=h.get_degradations,
            methods=["GET"],
        ),
        Route("/api/gc/trigger", endpoint=h.trigger_gc, methods=["POST"]),
        Route("/api/search", endpoint=h.api_search, methods=["POST"]),
        Route("/api/replay/observe", endpoint=h.api_replay_observe, methods=["POST"]),
        Route("/api/replay/fork", endpoint=h.api_replay_fork, methods=["POST"]),
        Route("/api/replay/status/{run_id}", endpoint=h.api_replay_status, methods=["GET"]),
        Route(
            "/api/replay/provenance/{memory_id}",
            endpoint=h.api_event_provenance,
            methods=["GET"],
        ),
        Route("/api/snapshot/export", endpoint=h.api_snapshot_export, methods=["POST"]),
        Route("/api/a2a/grants/create", endpoint=h.api_a2a_create_grant, methods=["POST"]),
        Route(
            "/api/a2a/grants/{grant_id}/revoke",
            endpoint=h.api_a2a_revoke_grant,
            methods=["POST"],
        ),
        Route("/api/a2a/grants", endpoint=h.api_a2a_list_grants, methods=["GET"]),
        Route("/api/admin/events", endpoint=h.api_admin_events, methods=["GET"]),
        Route(
            "/api/admin/events/summary",
            endpoint=h.api_admin_events_summary,
            methods=["GET"],
        ),
        Route("/api/admin/tools", endpoint=h.api_admin_tools, methods=["GET"]),
        Route(
            "/api/admin/tools/toggle",
            endpoint=h.api_admin_tools_toggle,
            methods=["POST"],
        ),
        Route("/admin/plugins", _bind(admin_plugins_console, ctx), methods=["GET"]),
        Route("/admin/plugins/api", _bind(admin_plugins_api, ctx), methods=["GET"]),
        Route("/admin/plugins/api/toggle", _bind(admin_plugins_toggle, ctx), methods=["POST"]),
        Route(
            "/admin/plugins/api/mint-token", _bind(admin_plugins_mint_token, ctx), methods=["POST"]
        ),
        Route("/api/admin/plugins", _bind(admin_plugins_api, ctx), methods=["GET"]),
        Route("/api/admin/plugins/toggle", _bind(admin_plugins_toggle, ctx), methods=["POST"]),
        Route(
            "/api/admin/plugins/mint-token", _bind(admin_plugins_mint_token, ctx), methods=["POST"]
        ),
        Route("/api/admin/source-mode", endpoint=h.api_source_mode_get, methods=["GET"]),
        Route("/api/admin/source-mode", endpoint=h.api_source_mode_put, methods=["PUT"]),
        Route("/api/admin/a2a/grants", endpoint=h.api_admin_a2a_grants, methods=["GET"]),
        Route(
            "/api/admin/a2a/grants/summary",
            endpoint=h.api_admin_a2a_grants_summary,
            methods=["GET"],
        ),
        Route(
            "/api/admin/a2a/grants/{grant_id}/revoke",
            endpoint=h.api_admin_a2a_revoke_grant,
            methods=["POST"],
        ),
        Route("/api/admin/quotas", endpoint=h.api_admin_quotas, methods=["GET"]),
        Route("/api/admin/webhooks", endpoint=h.api_admin_webhooks_post, methods=["POST"]),
        Route("/api/admin/webhooks", endpoint=h.api_admin_webhooks_get, methods=["GET"]),
        Route("/api/admin/webhooks/{id}", endpoint=h.api_admin_webhooks_delete, methods=["DELETE"]),
        Route("/api/admin/settings", endpoint=h.api_admin_settings_list, methods=["GET"]),
        Route("/api/admin/settings", endpoint=h.api_admin_settings_patch, methods=["PATCH"]),
        Route(
            "/api/admin/settings/effective",
            endpoint=h.api_admin_settings_effective,
            methods=["GET"],
        ),
        Route(
            "/api/admin/settings/pending",
            endpoint=h.api_admin_settings_pending,
            methods=["GET"],
        ),
        Route(
            "/api/admin/settings/reset",
            endpoint=h.api_admin_settings_reset,
            methods=["POST"],
        ),
        Route(
            "/api/admin/settings/reload",
            endpoint=h.api_admin_settings_reload,
            methods=["POST"],
        ),
        Route(
            "/api/admin/settings/rollback",
            endpoint=h.api_admin_settings_rollback,
            methods=["POST"],
        ),
        Route(
            "/api/admin/settings/{key}",
            endpoint=h.api_admin_settings_get,
            methods=["GET"],
        ),
        Route(
            "/api/admin/signing/status",
            endpoint=h.api_admin_signing_status,
            methods=["GET"],
        ),
        Route(
            "/api/admin/signing/mark-signed",
            endpoint=h.api_admin_signing_mark_signed,
            methods=["POST"],
        ),
        Route(
            "/api/admin/signing-credentials/save",
            endpoint=h.api_admin_signing_credentials_save,
            methods=["POST"],
        ),
        Route(
            "/api/admin/signing-credentials/status",
            endpoint=h.api_admin_signing_credentials_status,
            methods=["GET"],
        ),
        Route(
            "/api/admin/signing-credentials",
            endpoint=h.api_admin_signing_credentials_delete,
            methods=["DELETE"],
        ),
        Route(
            "/api/admin/namespaces/{namespace_id}/signing-credentials",
            endpoint=h.api_admin_signing_credentials_save,
            methods=["POST"],
        ),
        Route(
            "/api/admin/namespaces/{namespace_id}/signing-credentials",
            endpoint=h.api_admin_signing_credentials_status,
            methods=["GET"],
        ),
        Route(
            "/api/admin/namespaces/{namespace_id}/signing-credentials/{provider}",
            endpoint=h.api_admin_signing_credentials_delete,
            methods=["DELETE"],
        ),
        Route(
            "/api/admin/pii-redactions",
            endpoint=h.api_admin_pii_redactions_list,
            methods=["GET"],
        ),
        Route(
            "/api/admin/security/event-seq-gaps/{namespace_id}",
            endpoint=h.api_admin_security_event_seq_gaps,
            methods=["GET"],
        ),
        Route(
            "/api/admin/security/verify-memory-sample",
            endpoint=h.api_admin_security_verify_memory_sample,
            methods=["POST"],
        ),
        Route(
            "/api/admin/security/test-rls-isolation",
            endpoint=h.api_admin_security_test_rls_isolation,
            methods=["POST"],
        ),
        Route(
            "/api/admin/quotas/summary",
            endpoint=h.api_admin_quotas_summary,
            methods=["GET"],
        ),
        Route(
            "/api/admin/graph/explore",
            endpoint=h.api_admin_graph_explore,
            methods=["POST"],
        ),
        Route(
            "/api/admin/graph/provenance/{memory_id}",
            endpoint=h.api_event_provenance,
            methods=["GET"],
        ),
        Route(
            "/api/admin/verify-chain/{namespace_id}",
            endpoint=h.api_admin_verify_chain,
            methods=["GET"],
        ),
        Route(
            "/api/admin/embedding-models",
            endpoint=h.api_admin_embedding_models,
            methods=["GET"],
        ),
        Route(
            "/api/admin/embedding-migrations/start",
            endpoint=h.api_admin_embedding_migration_start,
            methods=["POST"],
        ),
        Route(
            "/api/admin/embedding-migrations/{migration_id}/status",
            endpoint=h.api_admin_embedding_migration_status,
            methods=["GET"],
        ),
        Route(
            "/api/admin/embedding-migrations/{migration_id}/validate",
            endpoint=h.api_admin_embedding_migration_validate,
            methods=["POST"],
        ),
        Route(
            "/api/admin/embedding-migrations/{migration_id}/commit",
            endpoint=h.api_admin_embedding_migration_commit,
            methods=["POST"],
        ),
        Route(
            "/api/admin/embedding-migrations/{migration_id}/abort",
            endpoint=h.api_admin_embedding_migration_abort,
            methods=["POST"],
        ),
        Route("/api/admin/schema", endpoint=h.api_admin_schema, methods=["GET"]),
        Route("/api/admin/dlq", endpoint=h.api_admin_dlq_list, methods=["GET"]),
        Route(
            "/api/admin/dlq/{dlq_id}/replay",
            endpoint=h.api_admin_dlq_replay,
            methods=["POST"],
        ),
        Route(
            "/api/admin/dlq/{dlq_id}/purge",
            endpoint=h.api_admin_dlq_purge,
            methods=["POST"],
        ),
        Route(
            "/api/admin/db/postgres/status",
            endpoint=h.api_admin_db_postgres_status,
            methods=["GET"],
        ),
        Route(
            "/api/admin/db/mongo/status",
            endpoint=h.api_admin_db_mongo_status,
            methods=["GET"],
        ),
        Route(
            "/api/admin/db/redis/status",
            endpoint=h.api_admin_db_redis_status,
            methods=["GET"],
        ),
        Route(
            "/api/admin/db/minio/status",
            endpoint=h.api_admin_db_minio_status,
            methods=["GET"],
        ),
        Route(
            "/api/admin/connectors/status",
            endpoint=h.api_admin_connectors_status,
            methods=["GET"],
        ),
        Route(
            "/api/admin/connectors/save",
            endpoint=h.api_admin_connectors_save,
            methods=["POST"],
        ),
        Route(
            "/api/admin/datastores/status",
            endpoint=h.api_admin_datastores_status,
            methods=["GET"],
        ),
        Route(
            "/api/admin/datastores/save",
            endpoint=h.api_admin_datastores_save,
            methods=["POST"],
        ),
        Route(
            "/api/admin/namespaces",
            endpoint=h.api_admin_namespaces_list,
            methods=["GET"],
        ),
        Route(
            "/api/admin/namespaces/{namespace_id}",
            endpoint=h.api_admin_namespaces_get,
            methods=["GET"],
        ),
        Route(
            "/api/admin/namespaces/{namespace_id}/metadata",
            endpoint=h.api_admin_namespaces_update_metadata,
            methods=["POST"],
        ),
        Route(
            "/api/admin/memory/boost",
            endpoint=h.api_admin_memory_boost,
            methods=["POST"],
        ),
        Route(
            "/api/admin/salience-map",
            endpoint=h.api_admin_salience_map,
            methods=["GET"],
        ),
        Route(
            "/api/admin/llm-payload",
            endpoint=h.api_admin_llm_payload,
            methods=["GET"],
        ),
        Route(
            "/api/admin/fleet-overview",
            endpoint=h.api_admin_fleet_overview,
            methods=["GET"],
        ),
        Route(
            "/api/admin/actor-trust",
            endpoint=h.api_admin_actor_trust,
            methods=["GET"],
        ),
        Route(
            "/api/admin/approval-queue",
            endpoint=h.api_admin_approval_queue_list,
            methods=["GET"],
        ),
        Route(
            "/api/admin/approval-queue/{id}",
            endpoint=h.api_admin_approval_queue_get,
            methods=["GET"],
        ),
        Route(
            "/api/admin/approval-queue/{id}/approve",
            endpoint=h.api_admin_approval_queue_approve,
            methods=["POST"],
        ),
        Route(
            "/api/admin/approval-queue/{id}/reject",
            endpoint=h.api_admin_approval_queue_reject,
            methods=["POST"],
        ),
        Route(
            "/api/admin/contradictions/recent",
            endpoint=h.api_admin_contradictions_recent,
            methods=["GET"],
        ),
        Route(
            "/api/admin/namespaces/{namespace_id}/bridges",
            endpoint=h.api_admin_namespace_bridges,
            methods=["GET"],
        ),
        Route(
            "/api/admin/bridges/{bridge_id}/renew",
            endpoint=h.api_admin_bridge_renew,
            methods=["POST"],
        ),
        Route(
            "/api/admin/entity-resolution/resolve",
            endpoint=entity_resolution_handlers.api_entity_resolution_resolve,
            methods=["POST"],
        ),
        Route(
            "/api/admin/entity-resolution/queue",
            endpoint=entity_resolution_handlers.api_entity_resolution_queue_list,
            methods=["GET"],
        ),
        Route(
            "/api/admin/entity-resolution/queue/{queue_id}/confirm",
            endpoint=entity_resolution_handlers.api_entity_resolution_queue_confirm,
            methods=["POST"],
        ),
        Route(
            "/api/admin/entity-resolution/queue/{queue_id}/reject",
            endpoint=entity_resolution_handlers.api_entity_resolution_queue_reject,
            methods=["POST"],
        ),
        Route(
            "/api/admin/pricing/resolve",
            endpoint=pricing_handlers.api_pricing_resolve,
            methods=["POST"],
        ),
        *build_all_resource_routes(),
    ]
    try:
        from nce.plugins import register_all_plugin_routes

        register_all_plugin_routes(routes)
    except Exception as exc:
        logger.warning("Failed to register plugin routes: %s", exc)
    return routes


def build_app(
    *,
    extra_routes: Sequence[Route] = (),
    extra_middleware: Sequence[Middleware] = (),
) -> Starlette:
    """Compose the NCE admin app with host-supplied routes/middleware.

    This is the front-end-readiness composition seam (NCE-FE-1, see
    ``docs/FRONTEND_READINESS.md``): a host application mounts its own Starlette
    routes/middleware **without editing this module**.

    - ``extra_routes`` are placed *before* NCE's own routes so a host may add or
      shadow paths; NCE's routes still resolve for everything else.
    - ``extra_middleware`` is placed *outermost* (before NCE's auth/rate-limit/
      trace stack) so cross-cutting host middleware such as CORS can handle a
      request (e.g. a preflight ``OPTIONS``) ahead of NCE's authentication.

    Called with no arguments it is identical to the historical app: the route
    set equals :func:`build_admin_routes` and the middleware equals
    :func:`build_admin_middleware`.
    """
    app = Starlette(
        debug=False,
        lifespan=admin_lifespan,
        middleware=list(extra_middleware) + build_admin_middleware(),
        routes=list(extra_routes) + build_admin_routes(),
    )
    return app


def create_admin_app() -> Starlette:
    return build_app()


app = create_admin_app()
