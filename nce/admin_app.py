"""Starlette admin application factory (routes, middleware, lifespan)."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route

from nce import admin_http_handlers as h
from nce import admin_state
from nce.admin_handlers import agreements as agreements_handlers
from nce.admin_handlers import assets as assets_handlers
from nce.admin_handlers import economy as economy_handlers
from nce.admin_handlers import entity_resolution as entity_resolution_handlers
from nce.admin_handlers import inventory as inventory_handlers
from nce.admin_handlers import pricing as pricing_handlers
from nce.admin_handlers import procurement as procurement_handlers
from nce.admin_handlers import product as product_handlers
from nce.admin_handlers import project as project_handlers
from nce.admin_handlers import sales as sales_handlers
from nce.admin_handlers import sales_public as sales_public_handlers
from nce.admin_handlers import system_design as system_design_handlers
from nce.admin_handlers import vendors as vendors_handlers
from nce.auth import (
    AdminHTTPRateLimitMiddleware,
    BasicAuthMiddleware,
    HMACAuthMiddleware,
    optional_hmac_nonce_store,
)
from nce.config import cfg
from nce.mtls import MTLSAuthMiddleware
from nce.notifications import dispatcher
from nce.observability import OpenTelemetryTraceMiddleware
from nce.orchestrator import NCEEngine

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
    """Unauthenticated liveness probe for load balancers / orchestrators."""
    return JSONResponse({"status": "ok"})


def build_admin_middleware() -> list[Middleware]:
    return [
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
            excluded_prefixes=("/api/", "/healthz", "/public-api/"),
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


def build_admin_routes() -> list[Route]:
    return [
        Route("/healthz", endpoint=get_healthz, methods=["GET"]),
        Route(
            "/public-api/sales/quotes/{id}",
            endpoint=sales_public_handlers.api_sales_quote_public,
            methods=["GET"],
        ),
        Route("/", endpoint=h.serve_index),
        Route("/styles.css", endpoint=h.serve_styles),
        Route("/api/health", endpoint=h.get_health, methods=["GET"]),
        Route("/api/health/v1", endpoint=h.get_health_v1, methods=["GET"]),
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
        # ------------------------------------------------------------------
        # Dynamics 365 / Dataverse admin endpoints
        # ------------------------------------------------------------------
        Route(
            "/api/admin/d365/config",
            endpoint=h.api_admin_d365_config,
            methods=["GET"],
        ),
        Route(
            "/api/admin/d365/integrations",
            endpoint=h.api_admin_d365_integrations,
            methods=["GET"],
        ),
        Route(
            "/api/admin/d365/sync",
            endpoint=h.api_admin_d365_sync_now,
            methods=["POST"],
        ),
        Route(
            "/api/admin/d365/sla-breaches",
            endpoint=h.api_admin_d365_sla_breaches,
            methods=["GET"],
        ),
        Route(
            "/api/admin/d365/namespace/{ns_id}/d365-enabled",
            endpoint=h.api_admin_d365_namespace_update,
            methods=["POST"],
        ),
        Route(
            "/api/admin/d365/netbox-mappings",
            endpoint=h.api_admin_d365_netbox_mappings,
            methods=["GET"],
        ),
        Route(
            "/api/admin/d365/netbox-mappings/{mapping_id}/confirm",
            endpoint=h.api_admin_d365_netbox_mapping_confirm,
            methods=["POST"],
        ),
        Route(
            "/api/admin/d365/netbox-bridge/sync",
            endpoint=h.api_admin_d365_netbox_bridge_sync,
            methods=["POST"],
        ),
        # ------------------------------------------------------------------
        # Sales source-mode admin endpoints
        # ------------------------------------------------------------------
        Route(
            "/api/admin/sales/source-mode",
            endpoint=sales_handlers.api_sales_source_mode_get,
            methods=["GET"],
        ),
        Route(
            "/api/admin/sales/source-mode",
            endpoint=sales_handlers.api_sales_source_mode_put,
            methods=["PUT"],
        ),
        # ------------------------------------------------------------------
        # Sales read-model & targets endpoints (M5.W5)
        # ------------------------------------------------------------------
        Route(
            "/api/sales/customers",
            endpoint=sales_handlers.api_admin_sales_customers,
            methods=["GET"],
        ),
        Route(
            "/api/sales/customers/{id}",
            endpoint=sales_handlers.api_admin_sales_customer_profile,
            methods=["GET"],
        ),
        Route(
            "/api/sales/overview",
            endpoint=sales_handlers.api_admin_sales_overview,
            methods=["GET"],
        ),
        Route(
            "/api/sales/seller-detail/{user}",
            endpoint=sales_handlers.api_admin_sales_seller_detail,
            methods=["GET"],
        ),
        Route(
            "/api/sales/dashboard",
            endpoint=sales_handlers.api_admin_sales_dashboard,
            methods=["GET"],
        ),
        Route(
            "/api/sales/stats",
            endpoint=sales_handlers.api_admin_sales_stats,
            methods=["GET"],
        ),
        Route(
            "/api/sales/manager",
            endpoint=sales_handlers.api_admin_sales_manager,
            methods=["GET"],
        ),
        Route(
            "/api/sales/agreements",
            endpoint=sales_handlers.api_admin_sales_agreements,
            methods=["GET"],
        ),
        Route(
            "/api/sales/agreements/{id}",
            endpoint=sales_handlers.api_admin_sales_agreement_detail,
            methods=["GET"],
        ),
        Route(
            "/api/sales/quotes/{id}",
            endpoint=sales_handlers.api_admin_sales_quote_detail,
            methods=["GET"],
        ),
        Route(
            "/api/sales/targets",
            endpoint=sales_handlers.api_admin_sales_targets_get,
            methods=["GET"],
        ),
        Route(
            "/api/sales/targets",
            endpoint=sales_handlers.api_admin_sales_targets_put,
            methods=["PUT"],
        ),
        # ------------------------------------------------------------------
        # Entity resolution admin endpoints
        # ------------------------------------------------------------------
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
        # ------------------------------------------------------------------
        # Pricing admin endpoints
        # ------------------------------------------------------------------
        Route(
            "/api/admin/pricing/resolve",
            endpoint=pricing_handlers.api_pricing_resolve,
            methods=["POST"],
        ),
        # ------------------------------------------------------------------
        # Product vertical module endpoints (M2.W3 / M2.W8)
        # ------------------------------------------------------------------
        Route(
            "/api/product/search",
            endpoint=product_handlers.api_product_search,
            methods=["GET"],
        ),
        # W8: enrichment review queue — must be declared before /{id} so the
        # literal path segment "enrichment" is not captured as an {id} param.
        Route(
            "/api/product/enrichment/review",
            endpoint=product_handlers.api_product_enrichment_review,
            methods=["GET"],
        ),
        Route(
            "/api/product/{id}",
            endpoint=product_handlers.api_product_get,
            methods=["GET"],
        ),
        # ------------------------------------------------------------------
        # Procurement vertical module endpoints (M1.W4 / M1.W7)
        # ------------------------------------------------------------------
        Route(
            "/api/procurement/tco",
            endpoint=procurement_handlers.api_procurement_calculate_tco,
            methods=["POST"],
        ),
        Route(
            "/api/procurement/rank",
            endpoint=procurement_handlers.api_procurement_rank_suppliers,
            methods=["POST"],
        ),
        Route(
            "/api/procurement/match",
            endpoint=procurement_handlers.api_procurement_evaluate_match,
            methods=["POST"],
        ),
        # W7: projection-consumer operator surface
        Route(
            "/api/procurement/sync",
            endpoint=procurement_handlers.api_procurement_sync_now,
            methods=["POST"],
        ),
        Route(
            "/api/procurement/sync/status",
            endpoint=procurement_handlers.api_procurement_sync_status,
            methods=["GET"],
        ),
        # W12: Frontier Advisor routes — read-only
        Route(
            "/api/procurement/frontier/forecast-rebate",
            endpoint=procurement_handlers.api_procurement_forecast_rebate,
            methods=["POST"],
        ),
        Route(
            "/api/procurement/frontier/recommend-move-spend",
            endpoint=procurement_handlers.api_procurement_recommend_move_spend,
            methods=["POST"],
        ),
        Route(
            "/api/procurement/frontier/whatif-spend",
            endpoint=procurement_handlers.api_procurement_whatif_spend,
            methods=["POST"],
        ),
        # ------------------------------------------------------------------
        # System Design vertical module endpoints (M6.W11) — Lucid export
        # ------------------------------------------------------------------
        Route(
            "/api/system-design/publish-design-docs",
            endpoint=system_design_handlers.api_system_design_publish_design_docs,
            methods=["POST"],
        ),
        # System Design vertical module endpoints (M6.W13a) — read-only topology
        Route(
            "/api/system-design/topology",
            endpoint=system_design_handlers.api_system_design_get_topology,
            methods=["GET"],
        ),
        # System Design vertical module endpoints (M6.W13b) — authoring (writes).
        # The POST shares its path with the W13a GET above: Starlette records a
        # path-but-not-method hit as a PARTIAL match and keeps scanning, so the
        # method-specific route below still wins for POST. Two entries, not one
        # merged `methods=["GET", "POST"]`, because they are two endpoints.
        Route(
            "/api/system-design/topology",
            endpoint=system_design_handlers.api_system_design_author_topology,
            methods=["POST"],
        ),
        Route(
            "/api/system-design/functional-location",
            endpoint=system_design_handlers.api_system_design_author_functional_location,
            methods=["POST"],
        ),
        # System Design vertical module endpoints (M6.W13c) — the design-graph
        # validator. POST, but a pure read: it writes nothing and therefore does
        # not bump the MCP cache generation.
        Route(
            "/api/system-design/validate",
            endpoint=system_design_handlers.api_system_design_validate_design_graph,
            methods=["POST"],
        ),
        # System Design vertical module endpoints (M6.W17) — retire planned
        # nodes.  THE FIRST DELETE PATH IN THIS CODEBASE.
        #
        # 🔴 The VERB IS A DELIBERATE MISMATCH WITH THE DEFAULT BEHAVIOUR.
        # ``DELETE`` and this path are Copper's pinned contract row, so neither
        # may change — but the default is a SOFT RETIRE (a lifecycle status
        # change plus a salience floor) and nothing is removed without an
        # explicit ``permanent: true`` in the body, which additionally requires
        # ``actor``.  Stated in the handler's first docstring line.
        Route(
            "/api/system-design/planned",
            endpoint=system_design_handlers.api_system_design_delete_planned,
            methods=["DELETE"],
        ),
        # ------------------------------------------------------------------
        # Vendors vertical module endpoints (M4.W3)
        # ------------------------------------------------------------------
        Route(
            "/api/vendors/scorecard",
            endpoint=vendors_handlers.api_vendors_scorecard,
            methods=["GET"],
        ),
        Route(
            "/api/vendors/{id}",
            endpoint=vendors_handlers.api_vendors_get_vendor,
            methods=["GET"],
        ),
        # ------------------------------------------------------------------
        # Project vertical module endpoints (M7.W5) — phase-routes
        # ------------------------------------------------------------------
        Route(
            "/api/project/convert-signed-quote",
            endpoint=project_handlers.api_project_convert_signed_quote,
            methods=["POST"],
        ),
        # NOTE: literal path must be declared before /{id}/phase to avoid
        # Starlette routing conflicts — no conflict here (no other /api/project/
        # literals clash with {id}), but the convert route is above as belt-and-
        # suspenders against any future literal additions.
        Route(
            "/api/project/{id}/phase",
            endpoint=project_handlers.api_project_get_phase,
            methods=["GET"],
        ),
        Route(
            "/api/project/{id}/phase",
            endpoint=project_handlers.api_project_advance_phase,
            methods=["POST"],
        ),
        Route(
            "/api/project/my-day",
            endpoint=project_handlers.api_admin_project_my_day,
            methods=["GET"],
        ),
        Route(
            "/api/project/capacity",
            endpoint=project_handlers.api_admin_project_capacity,
            methods=["GET"],
        ),
        Route(
            "/api/project/{id}/scope-creep",
            endpoint=project_handlers.api_admin_project_scope_creep,
            methods=["GET"],
        ),
        Route(
            "/api/project/{id}/status-report",
            endpoint=project_handlers.api_admin_project_status_report,
            methods=["GET"],
        ),
        # Agreements vertical module endpoints (Batch 107)
        Route(
            "/api/agreements",
            endpoint=agreements_handlers.api_agreements_list,
            methods=["GET"],
        ),
        # Coverage dashboard (Batch 109) — MUST precede /api/agreements/{id}:
        # Starlette matches in order, so a later mount would capture "coverage"
        # as the {id} path parameter.
        Route(
            "/api/agreements/coverage",
            endpoint=agreements_handlers.api_agreements_coverage,
            methods=["GET"],
        ),
        Route(
            "/api/agreements/{id}",
            endpoint=agreements_handlers.api_agreements_detail,
            methods=["GET"],
        ),
        Route(
            "/api/agreements/extract",
            endpoint=agreements_handlers.api_agreements_extract,
            methods=["POST"],
        ),
        Route(
            "/api/agreements/review",
            endpoint=agreements_handlers.api_agreements_review,
            methods=["POST"],
        ),
        # ------------------------------------------------------------------
        # Economy vertical module endpoints (M8.W4) — cores-surface
        # ------------------------------------------------------------------
        Route(
            "/api/economy/match-invoice",
            endpoint=economy_handlers.api_economy_match_invoice,
            methods=["POST"],
        ),
        Route(
            "/api/economy/periodisering",
            endpoint=economy_handlers.api_economy_periodisering,
            methods=["POST"],
        ),
        Route(
            "/api/economy/emit-event",
            endpoint=economy_handlers.api_economy_emit_event,
            methods=["POST"],
        ),
        # ------------------------------------------------------------------
        # Inventory vertical module endpoints (Batch 131, M11.W3) — stock-surface
        # ------------------------------------------------------------------
        Route(
            "/api/inventory/stock-levels",
            endpoint=inventory_handlers.api_inventory_stock_levels,
            methods=["GET"],
        ),
        Route(
            "/api/inventory/transfer-stock",
            endpoint=inventory_handlers.api_inventory_transfer_stock,
            methods=["POST"],
        ),
        Route(
            "/api/inventory/record-consumption",
            endpoint=inventory_handlers.api_inventory_record_consumption,
            methods=["POST"],
        ),
        # ------------------------------------------------------------------
        # Inventory vertical module endpoints (Batch 138a, M11.W10a) — surface
        # completion: the cores Batch 131's single surface wave predated.
        # ------------------------------------------------------------------
        Route(
            "/api/inventory/record-goods-receipt",
            endpoint=inventory_handlers.api_inventory_record_goods_receipt,
            methods=["POST"],
        ),
        Route(
            "/api/inventory/recommend-restock",
            endpoint=inventory_handlers.api_inventory_recommend_restock,
            methods=["POST"],
        ),
        Route(
            "/api/inventory/forecast-demand",
            endpoint=inventory_handlers.api_inventory_forecast_demand,
            methods=["POST"],
        ),
        Route(
            "/api/inventory/reserve-stock",
            endpoint=inventory_handlers.api_inventory_reserve_stock,
            methods=["POST"],
        ),
        Route(
            "/api/inventory/release-stock",
            endpoint=inventory_handlers.api_inventory_release_stock,
            methods=["POST"],
        ),
        Route(
            "/api/inventory/record-rma",
            endpoint=inventory_handlers.api_inventory_record_rma,
            methods=["POST"],
        ),
        Route(
            "/api/inventory/valuation",
            endpoint=inventory_handlers.api_inventory_valuation,
            methods=["GET"],
        ),
        Route(
            "/api/inventory/record-goods-receipt-and-match",
            endpoint=inventory_handlers.api_inventory_record_goods_receipt_and_match,
            methods=["POST"],
        ),
        Route(
            "/api/inventory/reconcile-dead-stock",
            endpoint=inventory_handlers.api_inventory_reconcile_dead_stock,
            methods=["POST"],
        ),
        Route(
            "/api/inventory/restock-from-rma",
            endpoint=inventory_handlers.api_inventory_restock_from_rma,
            methods=["POST"],
        ),
        Route(
            "/api/inventory/dispose-rma-weee",
            endpoint=inventory_handlers.api_inventory_dispose_rma_weee,
            methods=["POST"],
        ),
        # ------------------------------------------------------------------
        # Assets vertical module endpoints (Batch 143, M9.W3) — assets-surface
        # ------------------------------------------------------------------
        # No literal path under /api/assets/ besides {id} and {id}/lifecycle,
        # so there is no Starlette literal-vs-{id} ordering hazard here (unlike
        # /api/product/enrichment/review vs /api/product/{id}).
        Route(
            "/api/assets",
            endpoint=assets_handlers.api_assets_list,
            methods=["GET"],
        ),
        Route(
            "/api/assets/{id}",
            endpoint=assets_handlers.api_assets_get,
            methods=["GET"],
        ),
        Route(
            "/api/assets/{id}/lifecycle",
            endpoint=assets_handlers.api_assets_advance_lifecycle,
            methods=["POST"],
        ),
    ]


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
    return Starlette(
        debug=False,
        lifespan=admin_lifespan,
        middleware=list(extra_middleware) + build_admin_middleware(),
        routes=list(extra_routes) + build_admin_routes(),
    )


def create_admin_app() -> Starlette:
    return build_app()


app = create_admin_app()
