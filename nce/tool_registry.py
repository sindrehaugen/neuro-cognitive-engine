"""Declarative MCP tool registry — single source of truth for dispatch metadata.

Replaces the 54-branch ``if name ==`` ladder in ``mcp_stdio_dispatch.py``
with a ``ToolSpec`` → ``TOOL_REGISTRY`` lookup table.

Each entry records:
  * which handler coroutine to call
  * whether the tool requires admin credentials (``admin_only``)
  * whether successful responses may be cached in Redis (``cacheable``)
  * whether the tool mutates state and should bump the cache generation counter
    (``mutation``)
  * whether the tool is gated by ``NCE_DISABLE_MIGRATION_MCP`` (``migration``)

Derived frozensets (``MUTATION_TOOLS``, ``CACHEABLE_TOOLS``, etc.) are computed
once at import time from the registry — no duplicated inline sets elsewhere.
"""

from __future__ import annotations

import types
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from nce import (
    a2a_mcp_handlers,
    admin_mcp_handlers,
    bridge_mcp_handlers,
    catalog_mcp_handlers,
    code_mcp_handlers,
    contradiction_mcp_handlers,
    decision_feedback,
    graph_mcp_handlers,
    memory_mcp_handlers,
    migration_mcp_handlers,
    replay_mcp_handlers,
    snapshot_mcp_handlers,
    trust_dial,
)
from nce.admin_handlers import settings as settings_mcp_handlers
from nce.entity_resolution import mcp_handlers as entity_resolution_mcp_handlers
from nce.pricing import mcp_handlers as pricing_mcp_handlers
from nce.resource_surface import get_all_resource_specs
from nce.resource_surface.mcp import build_mcp_tool_specs
from nce.vertical_modules.agreements import mcp_handlers as agreements_mcp_handlers
from nce.vertical_modules.assets import mcp_handlers as assets_mcp_handlers
from nce.vertical_modules.business_insights import (
    mcp_handlers as business_insights_mcp_handlers,
)
from nce.vertical_modules.customer_portal import (
    mcp_handlers as customer_portal_mcp_handlers,
)
from nce.vertical_modules.diagnostics import mcp_handlers as diag_mcp_handlers
from nce.vertical_modules.dynamics365 import mcp_handlers as d365_mcp_handlers
from nce.vertical_modules.economy import mcp_handlers as economy_mcp_handlers
from nce.vertical_modules.field_tech import mcp_handlers as field_tech_mcp_handlers
from nce.vertical_modules.geodata import mcp_handlers as geodata_mcp_handlers
from nce.vertical_modules.hr import mcp_handlers as hr_mcp_handlers
from nce.vertical_modules.inventory import mcp_handlers as inventory_mcp_handlers
from nce.vertical_modules.legal_entities import mcp_handlers as legal_entities_mcp_handlers
from nce.vertical_modules.marketing import mcp_handlers as marketing_mcp_handlers
from nce.vertical_modules.netbox import circuits as netbox_circuits
from nce.vertical_modules.procurement import mcp_handlers as procurement_mcp_handlers
from nce.vertical_modules.product import mcp_handlers as product_mcp_handlers
from nce.vertical_modules.project import mcp_handlers as project_mcp_handlers
from nce.vertical_modules.resources import mcp_handlers as resources_mcp_handlers
from nce.vertical_modules.sales import mcp_handlers as sales_mcp_handlers
from nce.vertical_modules.sites import mcp_handlers as sites_mcp_handlers
from nce.vertical_modules.support import mcp_handlers as support_mcp_handlers
from nce.vertical_modules.system_design import mcp_handlers as system_design_mcp_handlers
from nce.vertical_modules.vendors import mcp_handlers as vendors_mcp_handlers


def _h(module: types.ModuleType, attr: str) -> Callable[..., Any]:
    """Return an async wrapper that resolves ``module.attr`` at **call time**.

    Late-binding preserves ``unittest.mock.patch("pkg.module.handler", ...)``
    compatibility: patching the module attribute after import still affects
    the function actually invoked by the dispatch loop.  Direct references
    stored in a frozen dataclass at registry construction time would silently
    ignore any later patches.
    """

    async def _call(engine: Any, arguments: Any) -> Any:
        return await getattr(module, attr)(engine, arguments)

    # Preserve legible names in tracebacks and for iscoroutinefunction checks.
    _call.__name__ = attr
    _call.__qualname__ = f"{module.__name__}.{attr}"
    return _call


@dataclass(frozen=True)
class ToolSpec:
    """Immutable metadata for a single registered MCP tool.

    Attributes:
        handler:    The async coroutine that implements the tool.
                    Signature: ``async (engine, arguments) -> str``
        admin_only: When *True* the dispatch layer calls ``_check_admin``
                    before invoking the handler.
        cacheable:  When *True* the dispatch layer writes a successful
                    response into Redis with TTL = MCP_CACHE_TTL_S.
        mutation:   When *True* the dispatch layer increments the global
                    cache-generation counter before serving the request
                    (and before any cache lookup).
        migration:  When *True* the tool is gated by
                    ``cfg.NCE_DISABLE_MIGRATION_MCP``; a disabled gate
                    returns a human-readable message without calling the handler.
    """

    handler: Callable[..., Any]
    admin_only: bool = False
    cacheable: bool = False
    mutation: bool = False
    migration: bool = False
    engine: str | None = None
    engine_dependencies: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Registry — one entry per tool, grouped by domain
# ---------------------------------------------------------------------------

TOOL_REGISTRY: dict[str, ToolSpec] = {
    # ------------------------------------------------------------------
    # Memory tools
    # ------------------------------------------------------------------
    "store_memory": ToolSpec(
        _h(memory_mcp_handlers, "handle_store_memory"),
        mutation=True,
    ),
    "store_artifact": ToolSpec(
        _h(memory_mcp_handlers, "handle_store_artifact"),
        mutation=True,
    ),
    "store_media": ToolSpec(
        _h(memory_mcp_handlers, "handle_store_media"),
        mutation=True,
    ),
    "semantic_search": ToolSpec(
        _h(memory_mcp_handlers, "handle_semantic_search"),
        cacheable=True,
    ),
    "get_recent_context": ToolSpec(
        _h(memory_mcp_handlers, "handle_get_recent_context"),
    ),
    "boost_memory": ToolSpec(
        _h(memory_mcp_handlers, "handle_boost_memory"),
        mutation=True,
    ),
    "forget_memory": ToolSpec(
        _h(memory_mcp_handlers, "handle_forget_memory"),
        mutation=True,
    ),
    "unredact_memory": ToolSpec(
        _h(memory_mcp_handlers, "handle_unredact_memory"),
        admin_only=True,
        mutation=True,
    ),
    "shred_memory": ToolSpec(
        _h(memory_mcp_handlers, "handle_shred_memory"),
        admin_only=True,
        mutation=True,
    ),
    # ------------------------------------------------------------------
    # Code indexing tools
    # ------------------------------------------------------------------
    "index_code_file": ToolSpec(
        _h(code_mcp_handlers, "handle_index_code_file"),
        mutation=True,
    ),
    "check_indexing_status": ToolSpec(
        _h(code_mcp_handlers, "handle_check_indexing_status"),
    ),
    "search_codebase": ToolSpec(
        _h(code_mcp_handlers, "handle_search_codebase"),
        cacheable=True,
    ),
    # ------------------------------------------------------------------
    # Graph / GraphRAG tools
    # ------------------------------------------------------------------
    "graph_search": ToolSpec(
        _h(graph_mcp_handlers, "handle_graph_search"),
        cacheable=True,
    ),
    "neuromorphic_search": ToolSpec(
        _h(graph_mcp_handlers, "handle_neuromorphic_search"),
        cacheable=True,
    ),
    # ------------------------------------------------------------------
    # Bridge / integration tools
    # ------------------------------------------------------------------
    "connect_bridge": ToolSpec(
        _h(bridge_mcp_handlers, "connect_bridge"),
        mutation=True,
    ),
    "complete_bridge_auth": ToolSpec(
        _h(bridge_mcp_handlers, "complete_bridge_auth"),
        mutation=True,
    ),
    "list_bridges": ToolSpec(
        _h(bridge_mcp_handlers, "list_bridges"),
    ),
    "disconnect_bridge": ToolSpec(
        _h(bridge_mcp_handlers, "disconnect_bridge"),
        mutation=True,
    ),
    "force_resync_bridge": ToolSpec(
        _h(bridge_mcp_handlers, "force_resync_bridge"),
        mutation=True,
    ),
    "bridge_status": ToolSpec(
        _h(bridge_mcp_handlers, "bridge_status"),
    ),
    # ------------------------------------------------------------------
    # Contradiction tools
    # ------------------------------------------------------------------
    "list_contradictions": ToolSpec(
        _h(contradiction_mcp_handlers, "handle_list_contradictions"),
    ),
    "resolve_contradiction": ToolSpec(
        _h(contradiction_mcp_handlers, "handle_resolve_contradiction"),
        mutation=True,
    ),
    # ------------------------------------------------------------------
    # Migration tools  (gated by NCE_DISABLE_MIGRATION_MCP)
    # ------------------------------------------------------------------
    "start_migration": ToolSpec(
        _h(migration_mcp_handlers, "handle_start_migration"),
        mutation=True,
        migration=True,
    ),
    "migration_status": ToolSpec(
        _h(migration_mcp_handlers, "handle_migration_status"),
        migration=True,
    ),
    "validate_migration": ToolSpec(
        _h(migration_mcp_handlers, "handle_validate_migration"),
        migration=True,
    ),
    "commit_migration": ToolSpec(
        _h(migration_mcp_handlers, "handle_commit_migration"),
        mutation=True,
        migration=True,
    ),
    "abort_migration": ToolSpec(
        _h(migration_mcp_handlers, "handle_abort_migration"),
        mutation=True,
        migration=True,
    ),
    # ------------------------------------------------------------------
    # Replay / event-sourcing tools
    # ------------------------------------------------------------------
    "replay_observe": ToolSpec(
        _h(replay_mcp_handlers, "handle_replay_observe"),
        admin_only=True,
    ),
    "replay_reconstruct": ToolSpec(
        _h(replay_mcp_handlers, "handle_replay_reconstruct"),
        admin_only=True,
        mutation=True,
    ),
    "replay_fork": ToolSpec(
        _h(replay_mcp_handlers, "handle_replay_fork"),
        admin_only=True,
    ),
    "replay_status": ToolSpec(
        _h(replay_mcp_handlers, "handle_replay_status"),
        admin_only=True,
    ),
    "get_event_provenance": ToolSpec(
        _h(replay_mcp_handlers, "handle_get_event_provenance"),
    ),
    "explain_memory": ToolSpec(
        _h(replay_mcp_handlers, "handle_explain_memory"),
    ),
    "explain_past_decision": ToolSpec(
        _h(replay_mcp_handlers, "handle_explain_past_decision"),
        admin_only=True,
        mutation=True,
    ),
    "explain_config_change": ToolSpec(
        _h(settings_mcp_handlers, "handle_explain_config_change"),
        admin_only=True,
    ),
    "detect_causal_cycles": ToolSpec(
        _h(replay_mcp_handlers, "handle_detect_causal_cycles"),
        admin_only=True,
    ),
    # ------------------------------------------------------------------
    # Agent-to-Agent (A2A) grant tools
    # ------------------------------------------------------------------
    "a2a_create_grant": ToolSpec(
        _h(a2a_mcp_handlers, "handle_a2a_create_grant"),
        mutation=True,
    ),
    "a2a_revoke_grant": ToolSpec(
        _h(a2a_mcp_handlers, "handle_a2a_revoke_grant"),
        mutation=True,
    ),
    "a2a_list_grants": ToolSpec(
        _h(a2a_mcp_handlers, "handle_a2a_list_grants"),
    ),
    "a2a_query_shared": ToolSpec(
        _h(a2a_mcp_handlers, "handle_a2a_query_shared"),
    ),
    "a2a_verify_grant_status": ToolSpec(
        _h(a2a_mcp_handlers, "handle_a2a_verify_grant_status"),
    ),
    "a2a_update_grant_scopes": ToolSpec(
        _h(a2a_mcp_handlers, "handle_a2a_update_grant_scopes"),
        mutation=True,
    ),
    "a2a_inspect_grant": ToolSpec(
        _h(a2a_mcp_handlers, "handle_a2a_inspect_grant"),
    ),
    # ------------------------------------------------------------------
    # Pricing tools
    # ------------------------------------------------------------------
    "pricing_resolve": ToolSpec(
        _h(pricing_mcp_handlers, "handle_pricing_resolve"),
        cacheable=True,
    ),
    # FX rate feed (MLV16 Lane F, Wave F-15) — GLOBAL (migration 092), not
    # tenant data, so no `engine=` opt-in gate applies. Its internal
    # cache-warming write to `pricing_fx_rates` is not a "mutation" in the
    # cache-generation sense (see geodata_get_weather's sibling note).
    "pricing_get_fx_rates": ToolSpec(
        _h(pricing_mcp_handlers, "handle_pricing_get_fx_rates"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    # ------------------------------------------------------------------
    # Entity resolution tools (C1 dual surface)
    # ------------------------------------------------------------------
    "resolve": ToolSpec(
        _h(entity_resolution_mcp_handlers, "handle_resolve"),
        cacheable=True,
    ),
    "merge_queue_list": ToolSpec(
        _h(entity_resolution_mcp_handlers, "handle_merge_queue_list"),
        cacheable=True,
    ),
    "merge_queue_confirm": ToolSpec(
        _h(entity_resolution_mcp_handlers, "handle_merge_queue_confirm"),
        mutation=True,
        admin_only=True,
    ),
    "merge_queue_reject": ToolSpec(
        _h(entity_resolution_mcp_handlers, "handle_merge_queue_reject"),
        mutation=True,
        admin_only=True,
    ),
    # ------------------------------------------------------------------
    # Admin / operational tools
    # ------------------------------------------------------------------
    "manage_namespace": ToolSpec(
        _h(admin_mcp_handlers, "handle_manage_namespace"),
        mutation=True,
    ),
    "verify_memory": ToolSpec(
        _h(admin_mcp_handlers, "handle_verify_memory"),
    ),
    "trigger_consolidation": ToolSpec(
        _h(admin_mcp_handlers, "handle_trigger_consolidation"),
        mutation=True,
    ),
    "consolidation_status": ToolSpec(
        _h(admin_mcp_handlers, "handle_consolidation_status"),
    ),
    "manage_quotas": ToolSpec(
        _h(admin_mcp_handlers, "handle_manage_quotas"),
        mutation=True,
    ),
    "rotate_signing_key": ToolSpec(
        _h(admin_mcp_handlers, "handle_rotate_signing_key"),
        mutation=True,
    ),
    "get_health": ToolSpec(
        _h(admin_mcp_handlers, "handle_get_health"),
    ),
    "list_dlq": ToolSpec(
        _h(admin_mcp_handlers, "handle_list_dlq"),
    ),
    "replay_dlq": ToolSpec(
        _h(admin_mcp_handlers, "handle_replay_dlq"),
        mutation=True,  # writes to dead_letter_queue (marks entry as replayed)
    ),
    "purge_dlq": ToolSpec(
        _h(admin_mcp_handlers, "handle_purge_dlq"),
        mutation=True,  # deletes from dead_letter_queue
    ),
    # ------------------------------------------------------------------
    # Snapshot tools
    # ------------------------------------------------------------------
    "create_snapshot": ToolSpec(
        _h(snapshot_mcp_handlers, "handle_create_snapshot"),
        mutation=True,
    ),
    "list_snapshots": ToolSpec(
        _h(snapshot_mcp_handlers, "handle_list_snapshots"),
    ),
    "delete_snapshot": ToolSpec(
        _h(snapshot_mcp_handlers, "handle_delete_snapshot"),
        mutation=True,
    ),
    "compare_states": ToolSpec(
        _h(snapshot_mcp_handlers, "handle_compare_states"),
    ),
    "import_snapshot": ToolSpec(
        _h(snapshot_mcp_handlers, "handle_import_snapshot"),
        mutation=True,
    ),
    # ------------------------------------------------------------------
    # Query catalog tools
    # ------------------------------------------------------------------
    "suggest_queries": ToolSpec(
        _h(catalog_mcp_handlers, "handle_suggest_queries"),
    ),
    "execute_query_template": ToolSpec(
        _h(catalog_mcp_handlers, "handle_execute_query_template"),
    ),
    "describe_schema": ToolSpec(
        _h(catalog_mcp_handlers, "handle_describe_schema"),
    ),
    # ------------------------------------------------------------------
    # Decision feedback tools (C10 cross-engine feedback service)
    # ------------------------------------------------------------------
    "decision_feedback_record": ToolSpec(
        _h(decision_feedback, "handle_record_decision_feedback"),
        admin_only=True,
        mutation=True,
    ),
    # ------------------------------------------------------------------
    # Trust Dial tools (Wave T-1 roadmap capstone)
    # ------------------------------------------------------------------
    "trust_dial_get_status": ToolSpec(
        _h(trust_dial, "handle_trust_dial_get_status"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "trust_dial_set_tier": ToolSpec(
        _h(trust_dial, "handle_trust_dial_set_tier"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    # ------------------------------------------------------------------
    # Dynamics 365 / Dataverse vertical module tools
    # ------------------------------------------------------------------
    "d365_query_case": ToolSpec(
        _h(d365_mcp_handlers, "handle_d365_query_case"),
        cacheable=True,
    ),
    "d365_sync_now": ToolSpec(
        _h(d365_mcp_handlers, "handle_d365_sync_now"),
        admin_only=True,
        mutation=True,
    ),
    "d365_case_stress_report": ToolSpec(
        _h(d365_mcp_handlers, "handle_d365_case_stress_report"),
        cacheable=True,
    ),
    "d365_list_sla_breaches": ToolSpec(
        _h(d365_mcp_handlers, "handle_d365_list_sla_breaches"),
        admin_only=True,
    ),
    "d365_netbox_mappings": ToolSpec(
        _h(d365_mcp_handlers, "handle_d365_netbox_mappings"),
        cacheable=True,
    ),
    "d365_sync_status": ToolSpec(
        _h(d365_mcp_handlers, "handle_d365_sync_status"),
    ),
    "evaluate_circuit_impact": ToolSpec(
        _h(netbox_circuits, "handle_evaluate_circuit_impact"),
        cacheable=False,
    ),
    # ------------------------------------------------------------------
    # Product vertical module tools (M2.W3–W5)
    # ------------------------------------------------------------------
    "product_search": ToolSpec(
        _h(product_mcp_handlers, "handle_product_search"),
        cacheable=True,
    ),
    "product_get": ToolSpec(
        _h(product_mcp_handlers, "handle_product_get"),
        cacheable=True,
    ),
    "product_price": ToolSpec(
        _h(product_mcp_handlers, "handle_product_price"),
        cacheable=True,
    ),
    "product_related": ToolSpec(
        _h(product_mcp_handlers, "handle_product_related"),
        cacheable=True,
    ),
    # Product vertical module tools (M2.W6)
    "product_match_bom_line": ToolSpec(
        _h(product_mcp_handlers, "handle_product_match_bom_line"),
        cacheable=False,
        mutation=False,
        admin_only=False,
    ),
    # Product vertical module tools (M2.W7) — on-demand enrichment (mutation, governed)
    "product_enrich": ToolSpec(
        _h(product_mcp_handlers, "handle_product_enrich"),
        mutation=True,
        cacheable=False,
        admin_only=False,
    ),
    # Product vertical module tools (v1.5 Phase 2 Waves P-2 / P-3)
    "product_ingest_spec": ToolSpec(
        _h(product_mcp_handlers, "handle_product_ingest_spec"),
        mutation=True,
        cacheable=False,
        admin_only=False,
    ),
    "product_golden_record": ToolSpec(
        _h(product_mcp_handlers, "handle_product_golden_record"),
        cacheable=True,
        mutation=False,
        admin_only=False,
    ),
    # ------------------------------------------------------------------
    # Procurement vertical module tools (M1.W4 / PR-3 / PR-4) — Advisor / Watcher: read-only
    # ------------------------------------------------------------------
    "procurement_aggregate_savings": ToolSpec(
        _h(procurement_mcp_handlers, "handle_procurement_aggregate_savings"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "procurement_resolve_bids": ToolSpec(
        _h(procurement_mcp_handlers, "handle_procurement_resolve_bids"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "procurement_calculate_tco": ToolSpec(
        _h(procurement_mcp_handlers, "handle_procurement_calculate_tco"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "procurement_rank_suppliers": ToolSpec(
        _h(procurement_mcp_handlers, "handle_procurement_rank_suppliers"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "procurement_evaluate_match": ToolSpec(
        _h(procurement_mcp_handlers, "handle_procurement_evaluate_match"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    # ------------------------------------------------------------------
    # Procurement vertical module tools (M1.W12) — Frontier Advisor: read-only
    # ------------------------------------------------------------------
    "procurement_forecast_rebate": ToolSpec(
        _h(procurement_mcp_handlers, "handle_procurement_forecast_rebate"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "procurement_recommend_move_spend": ToolSpec(
        _h(procurement_mcp_handlers, "handle_procurement_recommend_move_spend"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "procurement_whatif_spend": ToolSpec(
        _h(procurement_mcp_handlers, "handle_procurement_whatif_spend"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    # ------------------------------------------------------------------
    # Procurement vertical module tools (v1.5 PR-1) — Actor: PO lifecycle
    # ------------------------------------------------------------------
    "procurement_generate_po": ToolSpec(
        _h(procurement_mcp_handlers, "handle_procurement_generate_po"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    "procurement_submit_po": ToolSpec(
        _h(procurement_mcp_handlers, "handle_procurement_submit_po"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    # ------------------------------------------------------------------
    # System Design vertical module tools (M6.W1) — skeleton ping
    # ------------------------------------------------------------------
    "system_design_ping": ToolSpec(
        _h(system_design_mcp_handlers, "handle_system_design_ping"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    # System Design vertical module tools (M6.W11) — Lucid export (mutation: external publish)
    "system_design_publish_design_docs": ToolSpec(
        _h(system_design_mcp_handlers, "handle_system_design_publish_design_docs"),
        mutation=True,
        cacheable=False,
        admin_only=False,
    ),
    # System Design vertical module tools (M6.W13a) — read-only topology surface.
    # These three flags are Copper's published contract — do not adjust them.
    "system_design_get_topology": ToolSpec(
        _h(system_design_mcp_handlers, "handle_system_design_get_topology"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    # System Design vertical module tools (Wave SD-5) — thin signal-flow inspection.
    # cacheable=False matching validate_design_graph: active canvas editing
    # must not serve stale inspector or path-tracing data.
    "system_design_inspect_signal_flow": ToolSpec(
        _h(system_design_mcp_handlers, "handle_system_design_inspect_signal_flow"),
        cacheable=False,
        admin_only=False,
        mutation=False,
    ),
    # System Design vertical module tools (Wave SD-6) — frozen design grouped by ranked supplier for PR-1.
    "system_design_procurement_view": ToolSpec(
        _h(system_design_mcp_handlers, "handle_system_design_procurement_view"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    # System Design vertical module tools (M6.W13b) — the authoring surface: the
    # first external WRITE path into the design graph.  These names and flags are
    # Copper's published contract — do not adjust them.  ``mutation=True`` is what
    # makes the dispatch loop bump the MCP cache generation, and that bump is the
    # only thing stopping the cacheable ``system_design_get_topology`` entry from
    # serving pre-write data for the full MCP_CACHE_TTL_S.
    "system_design_author_topology": ToolSpec(
        _h(system_design_mcp_handlers, "handle_system_design_author_topology"),
        cacheable=False,
        admin_only=False,
        mutation=True,
    ),
    "system_design_author_functional_location": ToolSpec(
        _h(system_design_mcp_handlers, "handle_system_design_author_functional_location"),
        cacheable=False,
        admin_only=False,
        mutation=True,
    ),
    # System Design vertical module tools (M6.W13c) — the design-graph validator,
    # row four of Copper's contract table.  Do not adjust the name or the flags.
    # ``cacheable=False`` on a ``mutation=False`` read is deliberate, not an
    # oversight: a design under active canvas editing must not be served a stale
    # verdict for the full MCP_CACHE_TTL_S, and unlike a write there is nothing
    # here whose cache-generation bump would refresh it.
    "system_design_validate_design_graph": ToolSpec(
        _h(system_design_mcp_handlers, "handle_system_design_validate_design_graph"),
        cacheable=False,
        admin_only=False,
        mutation=False,
    ),
    # System Design vertical module tools (M6.W26, Batch 230a) -- the COMMERCIAL
    # half of the design loop. Four cores that had no route and no tool.
    #
    # Flags come from reading each core's call graph, not from its name:
    #   from_quote           _upsert_edge + do_author_functional_location +
    #                        emit_graph_write   -> mutation=True
    #   to_quote             _upsert_edge                          -> mutation=True
    #   enrich_design_lines  _fire_product_enrichment (line 396) ->
    #                        enqueue_product_enrichment            -> mutation=True
    #                        It writes no graph row itself, but it QUEUES work, and
    #                        a caller must be able to tell that invoking it causes
    #                        something to happen.
    #   generate_sow         only _read_* helpers                  -> mutation=False
    #
    # cacheable=False on all four, for validate_design_graph's stated reason: a
    # design under active canvas editing must not be served a stale answer, and
    # for the read there is no write whose cache bump would refresh it.
    "system_design_from_quote": ToolSpec(
        _h(system_design_mcp_handlers, "handle_system_design_from_quote"),
        cacheable=False,
        admin_only=False,
        mutation=True,
    ),
    "system_design_to_quote": ToolSpec(
        _h(system_design_mcp_handlers, "handle_system_design_to_quote"),
        cacheable=False,
        admin_only=False,
        mutation=True,
    ),
    "system_design_generate_sow": ToolSpec(
        _h(system_design_mcp_handlers, "handle_system_design_generate_sow"),
        cacheable=False,
        admin_only=False,
        mutation=False,
    ),
    "system_design_enrich_design_lines": ToolSpec(
        _h(system_design_mcp_handlers, "handle_system_design_enrich_design_lines"),
        cacheable=False,
        admin_only=False,
        mutation=True,
    ),
    # M6.W27 (Batch 230a2) -- do_propose_design, exposed SEPARATELY because it is
    # the one core in this group with existing internal callers
    # (sales/commission.py:189, from_quote.py:231). mutation=False: it is
    # propose-only and authors nothing.
    "system_design_propose_design": ToolSpec(
        _h(system_design_mcp_handlers, "handle_system_design_propose_design"),
        cacheable=False,
        admin_only=False,
        mutation=False,
    ),
    # System Design vertical module tools (M6.W17) — retire planned nodes, and
    # THE FIRST DELETE PATH IN THIS CODEBASE.
    #
    # 🔴 The NAME IS A DELIBERATE MISMATCH WITH THE BEHAVIOUR. Copper's contract
    # pins ``system_design_delete_planned`` and ``DELETE /api/system-design/planned``,
    # so neither may be renamed — but the DEFAULT IS A SOFT RETIRE and nothing is
    # removed without an explicit ``permanent=true`` (which additionally requires
    # ``actor``). Every docstring on the path says so in its first line.
    #
    # ``admin_only=True`` is the one flag that differs from the two authoring
    # tools above, and it is the contract, not a preference: those add and
    # update, this is the only tool in the module that can take something away.
    # ``mutation=True`` bumps the MCP cache generation, which is the only thing
    # stopping the cacheable ``system_design_get_topology`` entry from serving
    # a deleted device back for the full MCP_CACHE_TTL_S.
    "system_design_delete_planned": ToolSpec(
        _h(system_design_mcp_handlers, "handle_system_design_delete_planned"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    # System Design vertical module tools (Wave C-5) — standards, signal-rules, and capability sync
    "system_design_get_standards": ToolSpec(
        _h(system_design_mcp_handlers, "handle_system_design_get_standards"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "system_design_get_signal_rules": ToolSpec(
        _h(system_design_mcp_handlers, "handle_system_design_get_signal_rules"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "system_design_sync_device_capabilities": ToolSpec(
        _h(system_design_mcp_handlers, "handle_system_design_sync_device_capabilities"),
        cacheable=False,
        admin_only=False,
        mutation=True,
    ),
    # System Design vertical module tools (Wave C-1) — FUNCTIONAL_LOCATION tree
    "system_design_list_functional_locations": ToolSpec(
        _h(system_design_mcp_handlers, "handle_system_design_list_functional_locations"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "system_design_get_functional_location": ToolSpec(
        _h(system_design_mcp_handlers, "handle_system_design_get_functional_location"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "system_design_get_fl_children": ToolSpec(
        _h(system_design_mcp_handlers, "handle_system_design_get_fl_children"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "system_design_get_fl_ancestors": ToolSpec(
        _h(system_design_mcp_handlers, "handle_system_design_get_fl_ancestors"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "system_design_get_fl_path": ToolSpec(
        _h(system_design_mcp_handlers, "handle_system_design_get_fl_path"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "system_design_move_functional_location": ToolSpec(
        _h(system_design_mcp_handlers, "handle_system_design_move_functional_location"),
        cacheable=False,
        admin_only=False,
        mutation=True,
    ),
    "system_design_merge_functional_locations": ToolSpec(
        _h(system_design_mcp_handlers, "handle_system_design_merge_functional_locations"),
        cacheable=False,
        admin_only=False,
        mutation=True,
    ),
    "system_design_promote_functional_location": ToolSpec(
        _h(system_design_mcp_handlers, "handle_system_design_promote_functional_location"),
        cacheable=False,
        admin_only=False,
        mutation=True,
    ),
    # System Design vertical module tools (Wave C-2) — Room Categories & FL Metadata
    "system_design_list_room_categories": ToolSpec(
        _h(system_design_mcp_handlers, "handle_system_design_list_room_categories"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "system_design_get_room_category": ToolSpec(
        _h(system_design_mcp_handlers, "handle_system_design_get_room_category"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "system_design_set_fl_room_category": ToolSpec(
        _h(system_design_mcp_handlers, "handle_system_design_set_fl_room_category"),
        cacheable=False,
        admin_only=False,
        mutation=True,
    ),
    "system_design_get_fl_room_category": ToolSpec(
        _h(system_design_mcp_handlers, "handle_system_design_get_fl_room_category"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "system_design_assign_fl_responsible": ToolSpec(
        _h(system_design_mcp_handlers, "handle_system_design_assign_fl_responsible"),
        cacheable=False,
        admin_only=False,
        mutation=True,
    ),
    "system_design_unassign_fl_responsible": ToolSpec(
        _h(system_design_mcp_handlers, "handle_system_design_unassign_fl_responsible"),
        cacheable=False,
        admin_only=False,
        mutation=True,
    ),
    "system_design_list_fl_responsible": ToolSpec(
        _h(system_design_mcp_handlers, "handle_system_design_list_fl_responsible"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "system_design_list_my_responsible_fls": ToolSpec(
        _h(system_design_mcp_handlers, "handle_system_design_list_my_responsible_fls"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    # ------------------------------------------------------------------
    # System Design vertical module tools (Wave C-3) — DESIGN Versions & Room Specifications
    # ------------------------------------------------------------------
    "system_design_list_designs": ToolSpec(
        _h(system_design_mcp_handlers, "handle_system_design_list_designs"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "system_design_get_design": ToolSpec(
        _h(system_design_mcp_handlers, "handle_system_design_get_design"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "system_design_create_design": ToolSpec(
        _h(system_design_mcp_handlers, "handle_system_design_create_design"),
        cacheable=False,
        admin_only=False,
        mutation=True,
    ),
    "system_design_update_design": ToolSpec(
        _h(system_design_mcp_handlers, "handle_system_design_update_design"),
        cacheable=False,
        admin_only=False,
        mutation=True,
    ),
    "system_design_set_active_design": ToolSpec(
        _h(system_design_mcp_handlers, "handle_system_design_set_active_design"),
        cacheable=False,
        admin_only=False,
        mutation=True,
    ),
    "system_design_get_active_design": ToolSpec(
        _h(system_design_mcp_handlers, "handle_system_design_get_active_design"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "system_design_get_room_spec": ToolSpec(
        _h(system_design_mcp_handlers, "handle_system_design_get_room_spec"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "system_design_set_room_spec": ToolSpec(
        _h(system_design_mcp_handlers, "handle_system_design_set_room_spec"),
        cacheable=False,
        admin_only=False,
        mutation=True,
    ),
    # Wave C-4 (DESIGN_REQUEST intake queue / losningsdesign-ko)
    "system_design_create_design_request": ToolSpec(
        _h(system_design_mcp_handlers, "handle_system_design_create_design_request"),
        cacheable=False,
        admin_only=False,
        mutation=True,
    ),
    "system_design_get_design_request": ToolSpec(
        _h(system_design_mcp_handlers, "handle_system_design_get_design_request"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "system_design_list_design_requests": ToolSpec(
        _h(system_design_mcp_handlers, "handle_system_design_list_design_requests"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "system_design_update_design_request": ToolSpec(
        _h(system_design_mcp_handlers, "handle_system_design_update_design_request"),
        cacheable=False,
        admin_only=False,
        mutation=True,
    ),
    "system_design_assign_design_request": ToolSpec(
        _h(system_design_mcp_handlers, "handle_system_design_assign_design_request"),
        cacheable=False,
        admin_only=False,
        mutation=True,
    ),
    "system_design_complete_design_request": ToolSpec(
        _h(system_design_mcp_handlers, "handle_system_design_complete_design_request"),
        cacheable=False,
        admin_only=False,
        mutation=True,
    ),
    "system_design_fulfill_request_from_quote": ToolSpec(
        _h(system_design_mcp_handlers, "handle_system_design_fulfill_request_from_quote"),
        cacheable=False,
        admin_only=False,
        mutation=True,
    ),
    # ------------------------------------------------------------------
    # Project vertical module tools (M7.W3) — phase-gate readiness check
    # ------------------------------------------------------------------
    "project_can_enter_phase": ToolSpec(
        _h(project_mcp_handlers, "handle_project_can_enter_phase"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    # Project vertical module tools (M7.W4) — Sales→Project bridge (Actor)
    "project_convert_signed_quote": ToolSpec(
        _h(project_mcp_handlers, "handle_project_convert_signed_quote"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    # Project vertical module tools (M7.W4a) — phase-transition Actor
    "project_advance_phase": ToolSpec(
        _h(project_mcp_handlers, "handle_project_advance_phase"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    # Project vertical module tools (M7.W11) — suggest PL Advisor (needs HR via A2A)
    "project_suggest_pl": ToolSpec(
        _h(project_mcp_handlers, "handle_project_suggest_pl"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    # Project vertical module tools (v1.5 Phase 2 Wave PJ-1) — G5 outcome recorder
    "project_record_outcome": ToolSpec(
        _h(project_mcp_handlers, "handle_project_record_outcome"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    # Project vertical module tools (Wave C-PJ2) — G6 terminal case-study edge generator
    "project_generate_case_study_edge": ToolSpec(
        _h(project_mcp_handlers, "handle_project_generate_case_study_edge"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    # Project vertical module tools (Wave PJ-3) — Recall similar past slipped projects
    "project_recall_similar": ToolSpec(
        _h(project_mcp_handlers, "handle_project_recall_similar"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    # Project vertical module tools (Wave PJ-4) — Promoted REST reads to cacheable tools
    "project_my_day": ToolSpec(
        _h(project_mcp_handlers, "handle_project_my_day"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "project_capacity": ToolSpec(
        _h(project_mcp_handlers, "handle_project_capacity"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "project_detect_scope_creep": ToolSpec(
        _h(project_mcp_handlers, "handle_project_detect_scope_creep"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "project_status_report": ToolSpec(
        _h(project_mcp_handlers, "handle_project_status_report"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    # ------------------------------------------------------------------
    # Diagnostic Log Digestion Engine vertical module tools (Batch 77)
    # ------------------------------------------------------------------
    "diag_ingest_bundle": ToolSpec(
        _h(diag_mcp_handlers, "handle_diag_ingest_bundle"),
        mutation=True,
    ),
    "diag_commit_bundle": ToolSpec(
        _h(diag_mcp_handlers, "handle_diag_commit_bundle"),
        mutation=True,
    ),
    "diag_digest_status": ToolSpec(
        _h(diag_mcp_handlers, "handle_diag_digest_status"),
        cacheable=True,
    ),
    "diag_device_health": ToolSpec(
        _h(diag_mcp_handlers, "handle_diag_device_health"),
        cacheable=True,
    ),
    "diag_list_anomalies": ToolSpec(
        _h(diag_mcp_handlers, "handle_diag_list_anomalies"),
        cacheable=True,
    ),
    # ------------------------------------------------------------------
    # Sales vertical module tools (Batch 080)
    # ------------------------------------------------------------------
    "sales_ping": ToolSpec(
        _h(sales_mcp_handlers, "handle_sales_ping"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    # Cross-engine A2A read seam consumed by Project (project.baseline).
    # Not cacheable: the freeze happens via the signing callback (do_on_signed_
    # callback), not a registered MCP mutation, so no generation bump would
    # invalidate a cached pre-freeze `null`. Read-fresh to avoid stale
    # "unavailable" for up to the cache TTL after signature.
    "sales_get_signed_baseline": ToolSpec(
        _h(sales_mcp_handlers, "handle_sales_get_signed_baseline"),
        cacheable=False,
        admin_only=False,
        mutation=False,
    ),
    # Sales vertical module tools (M5.W16, Batch 132f) -- the cross-engine
    # READ seam for a quote's BOM_LINE rows, consumed by System Design's
    # from_quote flow. cacheable=False is DELIBERATE and not an oversight:
    # quote lines change as lines are added, the read is a single indexed
    # equality query, and caching it would require reasoning about cache-
    # generation bumps on both the REST and MCP write paths. Cheap read, no
    # staleness question. mutation=False (it writes nothing) and
    # admin_only=False (a salesperson reads their own quote).
    "sales_get_quote_lines": ToolSpec(
        _h(sales_mcp_handlers, "handle_sales_get_quote_lines"),
        cacheable=False,
        admin_only=False,
        mutation=False,
    ),
    # Sales vertical module tools (M5.W15, Batch 132d) -- the MANUAL-PICK
    # origination path for BOM_LINE, and the first real caller of the guarded
    # store in nce/bom_lines.py. A tenant write: mutation=True (so the MCP
    # cache generation bumps and no cacheable reader serves a quote without
    # its newest line), admin_only=False (a salesperson picks articles), and
    # cacheable=False (it writes).
    "sales_add_quote_line": ToolSpec(
        _h(sales_mcp_handlers, "handle_sales_add_quote_line"),
        cacheable=False,
        admin_only=False,
        mutation=True,
    ),
    # Sales quote signing orchestration via C7 SignTransport (Wave S-2a).
    # Actor tool: admin_only=True, mutation=True, cacheable=False.
    "sales_request_signature": ToolSpec(
        _h(sales_mcp_handlers, "handle_sales_request_signature"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    # Sales native write path tools (Wave S-1)
    # Actor tools: admin_only=True, mutation=True, cacheable=False.
    "sales_create_customer": ToolSpec(
        _h(sales_mcp_handlers, "handle_sales_create_customer"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    "sales_create_lead": ToolSpec(
        _h(sales_mcp_handlers, "handle_sales_create_lead"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    "sales_create_deal": ToolSpec(
        _h(sales_mcp_handlers, "handle_sales_create_deal"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    "sales_edit_deal": ToolSpec(
        _h(sales_mcp_handlers, "handle_sales_edit_deal"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    # Sales commission calculation surface completion (Wave S-6)
    # Advisor tool: admin_only=False, mutation=False, cacheable=True.
    "sales_calculate_commission": ToolSpec(
        _h(sales_mcp_handlers, "handle_sales_calculate_commission"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    # Sales divergence log parity window reader (Wave S-7)
    # Advisor tool: admin_only=False, mutation=False, cacheable=True.
    "sales_divergence_log": ToolSpec(
        _h(sales_mcp_handlers, "handle_sales_divergence_log"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    # Sales morning brief slice reader (Wave S-4)
    # Advisor tool: admin_only=False, mutation=False, cacheable=True.
    "sales_morning_brief_slice": ToolSpec(
        _h(sales_mcp_handlers, "handle_sales_morning_brief_slice"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    # Sales lead score & quote draft advisors (Wave S-5)
    # Advisor tools: admin_only=False, mutation=False, cacheable=True.
    "sales_score_lead": ToolSpec(
        _h(sales_mcp_handlers, "handle_sales_score_lead"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "sales_draft_quote": ToolSpec(
        _h(sales_mcp_handlers, "handle_sales_draft_quote"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    # ------------------------------------------------------------------
    # Vendors vertical module tools (Batch 096)
    # ------------------------------------------------------------------
    "vendors_get_vendor": ToolSpec(
        _h(vendors_mcp_handlers, "handle_vendors_get_vendor"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "vendors_compute_scorecard": ToolSpec(
        _h(vendors_mcp_handlers, "handle_vendors_compute_scorecard"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "vendors_get_tier_status": ToolSpec(
        _h(vendors_mcp_handlers, "handle_vendors_get_tier_status"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "vendors_detect_reliability_degradation": ToolSpec(
        _h(vendors_mcp_handlers, "handle_vendors_detect_reliability_degradation"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "vendors_check_tier_at_risk": ToolSpec(
        _h(vendors_mcp_handlers, "handle_vendors_check_tier_at_risk"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "vendors_match_contractor": ToolSpec(
        _h(vendors_mcp_handlers, "handle_vendors_match_contractor"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "vendors_compute_performance": ToolSpec(
        _h(vendors_mcp_handlers, "handle_vendors_compute_performance"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "vendors_recall_similar_jobs": ToolSpec(
        _h(vendors_mcp_handlers, "handle_vendors_recall_similar_jobs"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "vendors_reliability_radar": ToolSpec(
        _h(vendors_mcp_handlers, "handle_vendors_reliability_radar"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "vendors_calibrate_weights": ToolSpec(
        _h(vendors_mcp_handlers, "handle_vendors_calibrate_weights"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "vendors_upsert_vendor": ToolSpec(
        _h(vendors_mcp_handlers, "handle_vendors_upsert_vendor"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    "vendors_upsert_contractor": ToolSpec(
        _h(vendors_mcp_handlers, "handle_vendors_upsert_contractor"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    "vendors_get_contractor": ToolSpec(
        _h(vendors_mcp_handlers, "handle_vendors_get_contractor"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "vendors_upsert_cert": ToolSpec(
        _h(vendors_mcp_handlers, "handle_vendors_upsert_cert"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    # ------------------------------------------------------------------
    # Agreements vertical module tools (Batch 109 & Wave AG-2/AG-3)
    # ------------------------------------------------------------------
    "agreements_lookup_terms": ToolSpec(
        _h(agreements_mcp_handlers, "handle_agreements_lookup_terms"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "agreements_coverage_matrix": ToolSpec(
        _h(agreements_mcp_handlers, "handle_agreements_coverage_matrix"),
        cacheable=True,
        admin_only=False,
        mutation=False,
        engine="agreements",
        engine_dependencies=("agreements", "economy"),
    ),
    "agreements_reconcile_kickback": ToolSpec(
        _h(agreements_mcp_handlers, "handle_agreements_reconcile_kickback"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "agreements_run_compliance_audit": ToolSpec(
        _h(agreements_mcp_handlers, "handle_agreements_run_compliance_audit"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "agreements_extract": ToolSpec(
        _h(agreements_mcp_handlers, "handle_agreements_extract"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    "agreements_create": ToolSpec(
        _h(agreements_mcp_handlers, "handle_agreements_create"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    "agreements_suggest_revision": ToolSpec(
        _h(agreements_mcp_handlers, "handle_agreements_suggest_revision"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    "agreements_request_signature": ToolSpec(
        _h(agreements_mcp_handlers, "handle_agreements_request_signature"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    "agreements_record_signature": ToolSpec(
        _h(agreements_mcp_handlers, "handle_agreements_record_signature"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    "agreements_review_extraction": ToolSpec(
        _h(agreements_mcp_handlers, "handle_agreements_review_extraction"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    # Wave B-10: Price Rules & Index Series (Config-as-IP)
    "agreements_get_index_series": ToolSpec(
        _h(agreements_mcp_handlers, "handle_agreements_get_index_series"),
        cacheable=True,
        admin_only=False,
        mutation=False,
        engine="agreements",
    ),
    "agreements_calculate_index_adjustment": ToolSpec(
        _h(agreements_mcp_handlers, "handle_agreements_calculate_index_adjustment"),
        cacheable=True,
        admin_only=False,
        mutation=False,
        engine="agreements",
    ),
    "agreements_get_price_rules": ToolSpec(
        _h(agreements_mcp_handlers, "handle_agreements_get_price_rules"),
        cacheable=True,
        admin_only=False,
        mutation=False,
        engine="agreements",
    ),
    "agreements_evaluate_price_rule": ToolSpec(
        _h(agreements_mcp_handlers, "handle_agreements_evaluate_price_rule"),
        cacheable=True,
        admin_only=False,
        mutation=False,
        engine="agreements",
    ),
    # ------------------------------------------------------------------
    # Economy vertical module tools (M8.W4) — Advisor: read-only
    # ------------------------------------------------------------------
    "economy_match_invoice": ToolSpec(
        _h(economy_mcp_handlers, "handle_economy_match_invoice"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "economy_compute_periodisering": ToolSpec(
        _h(economy_mcp_handlers, "handle_economy_compute_periodisering"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "economy_emit_event": ToolSpec(
        _h(economy_mcp_handlers, "handle_economy_emit_event"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "economy_forecast_cashflow": ToolSpec(
        _h(economy_mcp_handlers, "handle_economy_forecast_cashflow"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "economy_snapshot_mrr_arr_churn": ToolSpec(
        _h(economy_mcp_handlers, "handle_economy_snapshot_mrr_arr_churn"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "economy_compute_dunning": ToolSpec(
        _h(economy_mcp_handlers, "handle_economy_compute_dunning"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "economy_compute_recognition_schedule": ToolSpec(
        _h(economy_mcp_handlers, "handle_economy_compute_recognition_schedule"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "economy_gl_sync_status": ToolSpec(
        _h(economy_mcp_handlers, "handle_economy_gl_sync_status"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "economy_generate_close_narrative": ToolSpec(
        _h(economy_mcp_handlers, "handle_economy_generate_close_narrative"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "economy_approve_invoice": ToolSpec(
        _h(economy_mcp_handlers, "handle_economy_approve_invoice"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    # Economy vertical module PEPPOL and validation tools (MLV15D Wave E-2)
    "economy_generate_kid": ToolSpec(
        _h(economy_mcp_handlers, "handle_economy_generate_kid"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "economy_validate_kid": ToolSpec(
        _h(economy_mcp_handlers, "handle_economy_validate_kid"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "economy_generate_ehf": ToolSpec(
        _h(economy_mcp_handlers, "handle_economy_generate_ehf"),
        cacheable=False,
        admin_only=True,
        mutation=False,
    ),
    "economy_validate_contract": ToolSpec(
        _h(economy_mcp_handlers, "handle_economy_validate_contract"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    # Economy vertical module GL records tool (Wave B-AG1 / B-E2)
    "economy_get_gl_records": ToolSpec(
        _h(economy_mcp_handlers, "handle_economy_get_gl_records"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    # ------------------------------------------------------------------
    # Inventory vertical module tools (Batch 131, M11.W3) — stock-surface
    # ------------------------------------------------------------------
    "inventory_stock_levels": ToolSpec(
        _h(inventory_mcp_handlers, "handle_inventory_stock_levels"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "inventory_transfer_stock": ToolSpec(
        _h(inventory_mcp_handlers, "handle_inventory_transfer_stock"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    "inventory_record_consumption": ToolSpec(
        _h(inventory_mcp_handlers, "handle_inventory_record_consumption"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    # ------------------------------------------------------------------
    # Batch 138a, M11.W10a — surface completion. The Inventory cores that
    # did not exist when Batch 131 ran the module's single surface wave.
    # ------------------------------------------------------------------
    "inventory_record_goods_receipt": ToolSpec(
        _h(inventory_mcp_handlers, "handle_inventory_record_goods_receipt"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    "inventory_recommend_restock": ToolSpec(
        _h(inventory_mcp_handlers, "handle_inventory_recommend_restock"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "inventory_forecast_demand": ToolSpec(
        _h(inventory_mcp_handlers, "handle_inventory_forecast_demand"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "inventory_reserve_stock": ToolSpec(
        _h(inventory_mcp_handlers, "handle_inventory_reserve_stock"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    "inventory_release_stock": ToolSpec(
        _h(inventory_mcp_handlers, "handle_inventory_release_stock"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    "inventory_reserve_kit": ToolSpec(
        _h(inventory_mcp_handlers, "handle_inventory_reserve_kit"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    "inventory_release_kit": ToolSpec(
        _h(inventory_mcp_handlers, "handle_inventory_release_kit"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    "inventory_record_rma": ToolSpec(
        _h(inventory_mcp_handlers, "handle_inventory_record_rma"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    "inventory_valuation": ToolSpec(
        _h(inventory_mcp_handlers, "handle_inventory_valuation"),
        cacheable=False,
        admin_only=True,
        mutation=False,
    ),
    "inventory_record_goods_receipt_and_match": ToolSpec(
        _h(inventory_mcp_handlers, "handle_inventory_record_goods_receipt_and_match"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    "inventory_reconcile_dead_stock": ToolSpec(
        _h(inventory_mcp_handlers, "handle_inventory_reconcile_dead_stock"),
        cacheable=False,
        admin_only=True,
        mutation=False,
    ),
    "inventory_restock_from_rma": ToolSpec(
        _h(inventory_mcp_handlers, "handle_inventory_restock_from_rma"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    "inventory_dispose_rma_weee": ToolSpec(
        _h(inventory_mcp_handlers, "handle_inventory_dispose_rma_weee"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    "inventory_create_restock_po": ToolSpec(
        _h(inventory_mcp_handlers, "handle_inventory_create_restock_po"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    # ------------------------------------------------------------------
    # Assets vertical module tools (Batch 141, M9.W1) — skeleton ping
    # ------------------------------------------------------------------
    "assets_ping": ToolSpec(
        _h(assets_mcp_handlers, "handle_assets_ping"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    # ------------------------------------------------------------------
    # Assets vertical module tools (Batch 143, M9.W3) — assets-surface:
    # get/list (Watcher reads, cacheable) + advance-lifecycle (Actor,
    # mutation). Flags for assets_advance_lifecycle match the MCP tools
    # table in docs/vertical_engines/09-assets-engine.md exactly
    # (cacheable=N, admin_only=N, mutation=Y) — unlike Inventory/Project's
    # Actor tools, this one is NOT admin_only per that table.
    # ------------------------------------------------------------------
    "assets_get": ToolSpec(
        _h(assets_mcp_handlers, "handle_assets_get"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "assets_list": ToolSpec(
        _h(assets_mcp_handlers, "handle_assets_list"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "assets_advance_lifecycle": ToolSpec(
        _h(assets_mcp_handlers, "handle_assets_advance_lifecycle"),
        cacheable=False,
        admin_only=False,
        mutation=True,
    ),
    "assets_seed_from_bom": ToolSpec(
        _h(assets_mcp_handlers, "handle_assets_seed_from_bom"),
        cacheable=False,
        admin_only=False,
        mutation=True,
    ),
    "assets_pull_telemetry": ToolSpec(
        _h(assets_mcp_handlers, "handle_assets_pull_telemetry"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    "assets_attach_sla": ToolSpec(
        _h(assets_mcp_handlers, "handle_assets_attach_sla"),
        cacheable=False,
        admin_only=False,
        mutation=True,
    ),
    "assets_compute_health": ToolSpec(
        _h(assets_mcp_handlers, "handle_assets_compute_health"),
        cacheable=False,
        admin_only=False,
        mutation=True,
    ),
    "assets_check_warranty_eol": ToolSpec(
        _h(assets_mcp_handlers, "handle_assets_check_warranty_eol"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "assets_sync_netbox": ToolSpec(
        _h(assets_mcp_handlers, "handle_assets_sync_netbox"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    "assets_generate_qr": ToolSpec(
        _h(assets_mcp_handlers, "handle_assets_generate_qr"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "assets_record_failure_pattern": ToolSpec(
        _h(assets_mcp_handlers, "handle_assets_record_failure_pattern"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    "assets_service_history": ToolSpec(
        _h(assets_mcp_handlers, "handle_assets_service_history"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    # Assets vertical module person assignment & sub-components (Wave D-2)
    "assets_assign_person": ToolSpec(
        _h(assets_mcp_handlers, "handle_assets_assign_person"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    "assets_unassign_person": ToolSpec(
        _h(assets_mcp_handlers, "handle_assets_unassign_person"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    "assets_list_person_assets": ToolSpec(
        _h(assets_mcp_handlers, "handle_assets_list_person_assets"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "assets_link_subcomponent": ToolSpec(
        _h(assets_mcp_handlers, "handle_assets_link_subcomponent"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    "assets_unlink_subcomponent": ToolSpec(
        _h(assets_mcp_handlers, "handle_assets_unlink_subcomponent"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    "assets_list_subcomponents": ToolSpec(
        _h(assets_mcp_handlers, "handle_assets_list_subcomponents"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    # ------------------------------------------------------------------
    # Support vertical module tools (Module 10, Wave 5, ML10-B5)
    # ------------------------------------------------------------------
    "support_query_ticket": ToolSpec(
        _h(support_mcp_handlers, "handle_support_query_ticket"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "support_open_ticket": ToolSpec(
        _h(support_mcp_handlers, "handle_support_open_ticket"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    "support_sla_clock": ToolSpec(
        _h(support_mcp_handlers, "handle_support_sla_clock"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "support_health_score": ToolSpec(
        _h(support_mcp_handlers, "handle_support_health_score"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "support_troubleshoot": ToolSpec(
        _h(support_mcp_handlers, "handle_support_troubleshoot"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "support_resolve_ticket": ToolSpec(
        _h(support_mcp_handlers, "handle_support_resolve_ticket"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    "support_triage_ticket": ToolSpec(
        _h(support_mcp_handlers, "handle_support_triage_ticket"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "support_record_touchpoint": ToolSpec(
        _h(support_mcp_handlers, "handle_support_record_touchpoint"),
        cacheable=False,
        admin_only=False,
        mutation=True,
    ),
    "support_dispatch_work_order": ToolSpec(
        _h(support_mcp_handlers, "handle_support_dispatch_work_order"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    "support_sync_now": ToolSpec(
        _h(support_mcp_handlers, "handle_support_sync_now"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    # Support vertical module ecosystem feeds (Wave SU-3)
    "support_failure_pattern": ToolSpec(
        _h(support_mcp_handlers, "handle_support_failure_pattern"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    "support_upsell_signal": ToolSpec(
        _h(support_mcp_handlers, "handle_support_upsell_signal"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    "support_at_risk_aggregate": ToolSpec(
        _h(support_mcp_handlers, "handle_support_at_risk_aggregate"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "support_log_ticket_action": ToolSpec(
        _h(support_mcp_handlers, "handle_support_log_ticket_action"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    "support_ticket_timeline": ToolSpec(
        _h(support_mcp_handlers, "handle_support_ticket_timeline"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    # Support vertical module on-call rota and active responder routing (Wave D-7)
    "support_get_on_call": ToolSpec(
        _h(support_mcp_handlers, "handle_support_get_on_call"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    # Support vertical module ticket summary and links (Wave D-6)
    "support_summarise_ticket": ToolSpec(
        _h(support_mcp_handlers, "handle_support_summarise_ticket"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "support_get_ticket_links": ToolSpec(
        _h(support_mcp_handlers, "handle_support_get_ticket_links"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "support_link_ticket": ToolSpec(
        _h(support_mcp_handlers, "handle_support_link_ticket"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    # Field Tech vertical module tools (ML12-B5, M12.W5)
    "field_tech_dispatch": ToolSpec(
        _h(field_tech_mcp_handlers, "handle_field_tech_dispatch"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "field_tech_partner_view": ToolSpec(
        _h(field_tech_mcp_handlers, "handle_field_tech_partner_view"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "field_tech_create_work_order": ToolSpec(
        _h(field_tech_mcp_handlers, "handle_field_tech_create_work_order"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    "field_tech_assign": ToolSpec(
        _h(field_tech_mcp_handlers, "handle_field_tech_assign"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    "field_tech_complete_checklist": ToolSpec(
        _h(field_tech_mcp_handlers, "handle_field_tech_complete_checklist"),
        cacheable=False,
        admin_only=False,
        mutation=True,
    ),
    "field_tech_scan_serial": ToolSpec(
        _h(field_tech_mcp_handlers, "handle_field_tech_scan_serial"),
        cacheable=False,
        admin_only=False,
        mutation=True,
    ),
    "field_tech_log_time": ToolSpec(
        _h(field_tech_mcp_handlers, "handle_field_tech_log_time"),
        cacheable=False,
        admin_only=False,
        mutation=True,
    ),
    "field_tech_attach_photo": ToolSpec(
        _h(field_tech_mcp_handlers, "handle_field_tech_attach_photo"),
        cacheable=False,
        admin_only=False,
        mutation=True,
    ),
    "field_tech_sync": ToolSpec(
        _h(field_tech_mcp_handlers, "handle_field_tech_sync"),
        cacheable=False,
        admin_only=False,
        mutation=True,
    ),
    "field_tech_record_outcome": ToolSpec(
        _h(field_tech_mcp_handlers, "handle_field_tech_record_outcome"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    # -----------------------------------------------------------------------
    # Module 13 — HR Engine (ML13-B3)
    # -----------------------------------------------------------------------
    "hr_get_employee": ToolSpec(
        _h(hr_mcp_handlers, "handle_hr_get_employee"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "hr_match_skills": ToolSpec(
        _h(hr_mcp_handlers, "handle_hr_match_skills"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "hr_capacity": ToolSpec(
        _h(hr_mcp_handlers, "handle_hr_capacity"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "hr_cert_status": ToolSpec(
        _h(hr_mcp_handlers, "handle_hr_cert_status"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "hr_register_absence": ToolSpec(
        _h(hr_mcp_handlers, "handle_hr_register_absence"),
        cacheable=False,
        admin_only=False,
        mutation=True,
    ),
    "hr_build_onboarding_quest": ToolSpec(
        _h(hr_mcp_handlers, "handle_hr_build_onboarding_quest"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    "hr_log_one_on_one": ToolSpec(
        _h(hr_mcp_handlers, "handle_hr_log_one_on_one"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    "hr_coach": ToolSpec(
        _h(hr_mcp_handlers, "handle_hr_coach"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "hr_record_skill": ToolSpec(
        _h(hr_mcp_handlers, "handle_hr_record_skill"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    "hr_query_absences": ToolSpec(
        _h(hr_mcp_handlers, "handle_hr_query_absences"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "hr_compliance_deadlines": ToolSpec(
        _h(hr_mcp_handlers, "handle_hr_compliance_deadlines"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "hr_update_absence_compliance": ToolSpec(
        _h(hr_mcp_handlers, "handle_hr_update_absence_compliance"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    "hr_get_onboarding_progress": ToolSpec(
        _h(hr_mcp_handlers, "handle_hr_get_onboarding_progress"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    # -------------------------------------------------------------------
    # Module 14: Marketing Engine (ML14-B3, M14.W3)
    # -------------------------------------------------------------------
    "marketing_find_case_study_candidates": ToolSpec(
        _h(marketing_mcp_handlers, "handle_marketing_find_case_study_candidates"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "marketing_draft_case_study": ToolSpec(
        _h(marketing_mcp_handlers, "handle_marketing_draft_case_study"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    "marketing_request_testimonial": ToolSpec(
        _h(marketing_mcp_handlers, "handle_marketing_request_testimonial"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    "marketing_capture_testimonial": ToolSpec(
        _h(marketing_mcp_handlers, "handle_marketing_capture_testimonial"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    "marketing_suggest_content": ToolSpec(
        _h(marketing_mcp_handlers, "handle_marketing_suggest_content"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "marketing_audit_seo": ToolSpec(
        _h(marketing_mcp_handlers, "handle_marketing_audit_seo"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    "marketing_approve_content": ToolSpec(
        _h(marketing_mcp_handlers, "handle_marketing_approve_content"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    "marketing_publish_content": ToolSpec(
        _h(marketing_mcp_handlers, "handle_marketing_publish_content"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    "marketing_retract_testimonial": ToolSpec(
        _h(marketing_mcp_handlers, "handle_marketing_retract_testimonial"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    "resources_resolve_capacity": ToolSpec(
        _h(resources_mcp_handlers, "handle_resources_resolve_capacity"),
        cacheable=True,
    ),
    "resources_plan_allocation": ToolSpec(
        _h(resources_mcp_handlers, "handle_resources_plan_allocation"),
        cacheable=False,
    ),
    "resources_detect_conflicts": ToolSpec(
        _h(resources_mcp_handlers, "handle_resources_detect_conflicts"),
        cacheable=True,
    ),
    "resources_forecast_demand": ToolSpec(
        _h(resources_mcp_handlers, "handle_resources_forecast_demand"),
        cacheable=True,
    ),
    "resources_field_schedule": ToolSpec(
        _h(resources_mcp_handlers, "handle_resources_field_schedule"),
        cacheable=True,
    ),
    "resources_reserve": ToolSpec(
        _h(resources_mcp_handlers, "handle_resources_reserve"),
        cacheable=False,
        mutation=True,
    ),
    "resources_release": ToolSpec(
        _h(resources_mcp_handlers, "handle_resources_release"),
        cacheable=False,
        mutation=True,
    ),
    "resources_plan_material_flow": ToolSpec(
        _h(resources_mcp_handlers, "handle_resources_plan_material_flow"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    "resources_plan_travel": ToolSpec(
        _h(resources_mcp_handlers, "handle_resources_plan_travel"),
        cacheable=False,
        mutation=True,
    ),
    # Resources Engine — outcome recording (Wave RS-3)
    "resources_record_allocation_outcome": ToolSpec(
        _h(resources_mcp_handlers, "handle_resources_record_allocation_outcome"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    # Resources Engine — master-data surface completion (Wave RS-1)
    "resources_create": ToolSpec(
        _h(resources_mcp_handlers, "handle_resources_create"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    "resources_update": ToolSpec(
        _h(resources_mcp_handlers, "handle_resources_update"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    "resources_get_resource": ToolSpec(
        _h(resources_mcp_handlers, "handle_resources_get_resource"),
        cacheable=True,
    ),
    "resources_list_resources": ToolSpec(
        _h(resources_mcp_handlers, "handle_resources_list_resources"),
        cacheable=True,
    ),
    # ML17-B5 (M17.W5) -- Customer Portal Engine (9 tools)
    "customer_portal_room_tracker": ToolSpec(
        _h(customer_portal_mcp_handlers, "handle_customer_portal_room_tracker"),
        cacheable=True,
        engine="customer_portal",
        engine_dependencies=("customer_portal", "system_design", "inventory", "assets"),
        admin_only=True,
    ),
    "customer_portal_room_overview": ToolSpec(
        _h(customer_portal_mcp_handlers, "handle_customer_portal_room_overview"),
        cacheable=True,
        engine="customer_portal",
        engine_dependencies=("customer_portal", "system_design", "inventory", "assets"),
        admin_only=True,
    ),
    "customer_portal_asset_register": ToolSpec(
        _h(customer_portal_mcp_handlers, "handle_customer_portal_asset_register"),
        cacheable=True,
        engine="customer_portal",
        engine_dependencies=("customer_portal", "assets"),
        admin_only=True,
    ),
    "customer_portal_list_documents": ToolSpec(
        _h(customer_portal_mcp_handlers, "handle_customer_portal_list_documents"),
        cacheable=True,
        engine="customer_portal",
        admin_only=True,
    ),
    "customer_portal_sla_status": ToolSpec(
        _h(customer_portal_mcp_handlers, "handle_customer_portal_sla_status"),
        cacheable=True,
        engine="customer_portal",
        engine_dependencies=("customer_portal", "support", "agreements"),
        admin_only=True,
    ),
    "customer_portal_list_invoices": ToolSpec(
        _h(customer_portal_mcp_handlers, "handle_customer_portal_list_invoices"),
        cacheable=True,
        engine="customer_portal",
        engine_dependencies=("customer_portal", "economy"),
        admin_only=True,
    ),
    "customer_portal_advisor_answer": ToolSpec(
        _h(customer_portal_mcp_handlers, "handle_customer_portal_advisor_answer"),
        cacheable=False,
        engine="customer_portal",
        admin_only=True,
    ),
    "customer_portal_raise_service_request": ToolSpec(
        _h(customer_portal_mcp_handlers, "handle_customer_portal_raise_service_request"),
        cacheable=False,
        mutation=True,
        engine="customer_portal",
        admin_only=True,
    ),
    "customer_portal_register_expansion_interest": ToolSpec(
        _h(customer_portal_mcp_handlers, "handle_customer_portal_register_expansion_interest"),
        cacheable=False,
        mutation=True,
        engine="customer_portal",
        admin_only=True,
    ),
    # ML16 (Business Insights Engine) — Module 16 executive decision support
    "business_insights_morning_brief": ToolSpec(
        _h(business_insights_mcp_handlers, "handle_business_insights_morning_brief"),
        cacheable=True,
        admin_only=True,
        mutation=False,
        engine="business_insights",
        engine_dependencies=(
            "business_insights",
            "economy",
            "project",
            "support",
            "sales",
            "resources",
            "inventory",
        ),
    ),
    "business_insights_risk_radar": ToolSpec(
        _h(business_insights_mcp_handlers, "handle_business_insights_risk_radar"),
        cacheable=True,
        admin_only=True,
        mutation=False,
        engine="business_insights",
        engine_dependencies=(
            "business_insights",
            "economy",
            "project",
            "support",
            "sales",
            "resources",
            "inventory",
            "assets",
            "agreements",
        ),
    ),
    "business_insights_run_scenario": ToolSpec(
        _h(business_insights_mcp_handlers, "handle_business_insights_run_scenario"),
        cacheable=False,
        admin_only=True,
        mutation=False,
        engine="business_insights",
    ),
    "business_insights_generate_board_pack": ToolSpec(
        _h(business_insights_mcp_handlers, "handle_business_insights_generate_board_pack"),
        cacheable=False,
        admin_only=True,
        mutation=False,
        engine="business_insights",
    ),
    "business_insights_kpi_dashboard": ToolSpec(
        _h(business_insights_mcp_handlers, "handle_business_insights_kpi_dashboard"),
        cacheable=True,
        admin_only=True,
        mutation=False,
        engine="business_insights",
        engine_dependencies=(
            "business_insights",
            "economy",
            "project",
            "support",
            "sales",
            "resources",
            "inventory",
            "assets",
        ),
    ),
    "business_insights_ask_business": ToolSpec(
        _h(business_insights_mcp_handlers, "handle_business_insights_ask_business"),
        cacheable=False,
        admin_only=True,
        mutation=False,
        engine="business_insights",
    ),
    # C15 Legal-Entity Register national business registry feed (Wave F-8) —
    # an operator/cron pull against an external public registry, same
    # reasoning as assets_pull_telemetry: admin_only, not an ordinary Actor
    # action.
    "legal_entities_enrich_from_registry": ToolSpec(
        _h(legal_entities_mcp_handlers, "handle_legal_entities_enrich_from_registry"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    # C17 Site Master Data address registry feed (Wave F-9) — same
    # reasoning as legal_entities_enrich_from_registry above: an
    # operator/cron pull against an external public registry, admin_only,
    # not an ordinary Actor action. No `engine=` gate, matching that same
    # precedent (sites' own C12 list/get/upsert/archive tools carry
    # engine="sites" via their auto-mounted ResourceSpec; this hand-written
    # enrichment tool does not, same as legal_entities' enrichment tool).
    "sites_enrich_address_from_registry": ToolSpec(
        _h(sites_mcp_handlers, "handle_sites_enrich_address_from_registry"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    # Geodata FEED module (Wave F-11) — a GLOBAL table (migration 086), not
    # a tenant vertical engine, so no `engine=` opt-in gate applies.
    "geodata_import_osm_elements": ToolSpec(
        _h(geodata_mcp_handlers, "handle_geodata_import_osm_elements"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    "geodata_query_osm_elements": ToolSpec(
        _h(geodata_mcp_handlers, "handle_geodata_query_osm_elements"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    # Geodata FEED module (Wave F-12) — a GLOBAL table (migration 089), not
    # a tenant vertical engine, so no `engine=` opt-in gate applies.
    "geodata_import_n50_land_cover": ToolSpec(
        _h(geodata_mcp_handlers, "handle_geodata_import_n50_land_cover"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    "geodata_query_n50_land_cover": ToolSpec(
        _h(geodata_mcp_handlers, "handle_geodata_query_n50_land_cover"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    # Geodata FEED module (Wave F-13) — a GLOBAL table (migration 090), not
    # a tenant vertical engine, so no `engine=` opt-in gate applies.
    "geodata_import_place_names": ToolSpec(
        _h(geodata_mcp_handlers, "handle_geodata_import_place_names"),
        cacheable=False,
        admin_only=True,
        mutation=True,
    ),
    "geodata_query_nearest_place_name": ToolSpec(
        _h(geodata_mcp_handlers, "handle_geodata_query_nearest_place_name"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
    # Geodata FEED module (Wave F-15) — live read-through to MET Norway, no
    # local table, no `engine=` opt-in gate (same reasoning as its siblings
    # above). An internal cache-warming write to `pricing_fx_rates` (its
    # sibling below) does not make either tool a "mutation" in the
    # cache-generation sense: nothing else's cached response depends on
    # either table.
    "geodata_get_weather": ToolSpec(
        _h(geodata_mcp_handlers, "handle_geodata_get_weather"),
        cacheable=True,
        admin_only=False,
        mutation=False,
    ),
}

# ---------------------------------------------------------------------------
# C12 Resource Surface auto-mounted tools
# ---------------------------------------------------------------------------
for _res_spec in get_all_resource_specs():
    for _tool_name, _tool_spec in build_mcp_tool_specs(_res_spec).items():
        if _tool_name in TOOL_REGISTRY:
            _existing = TOOL_REGISTRY[_tool_name]
            raise RuntimeError(
                f"Collision in TOOL_REGISTRY: resource tool '{_tool_name}' generated by "
                f"ResourceSpec(engine='{_res_spec.engine}', node_type='{_res_spec.node_type}') "
                f"collides with an existing hand-written tool (engine='{_existing.engine}'). "
                "A generated ResourceSpec tool cannot overwrite a hand-written tool."
            )
        TOOL_REGISTRY[_tool_name] = _tool_spec


# ---------------------------------------------------------------------------
# Derived sets — computed once at import time
# ---------------------------------------------------------------------------

#: Tools that mutate state — the dispatch layer increments the global cache
#: generation counter before serving these.  Migration-mutation tools
#: (``mutation=True, migration=True``) are included here; the dispatch layer
#: applies the ``NCE_DISABLE_MIGRATION_MCP`` gate separately.
MUTATION_TOOLS: frozenset[str] = frozenset(
    name for name, spec in TOOL_REGISTRY.items() if spec.mutation
)

#: Tools whose successful responses are eligible for Redis caching.
CACHEABLE_TOOLS: frozenset[str] = frozenset(
    name for name, spec in TOOL_REGISTRY.items() if spec.cacheable
)

#: Tools that require admin credentials (``_check_admin`` must pass).
ADMIN_ONLY_TOOLS: frozenset[str] = frozenset(
    name for name, spec in TOOL_REGISTRY.items() if spec.admin_only
)

#: Tools gated by ``cfg.NCE_DISABLE_MIGRATION_MCP``.
MIGRATION_TOOLS: frozenset[str] = frozenset(
    name for name, spec in TOOL_REGISTRY.items() if spec.migration
)


# ---------------------------------------------------------------------------
# Host / extension registration (NCE-FE-2)
# ---------------------------------------------------------------------------


def _refresh_derived_sets() -> None:
    """Recompute the module-level derived frozensets after a registry mutation.

    The dispatch layer reads ``spec.admin_only``/``mutation``/``cacheable``
    directly off the :class:`ToolSpec`, so gating never depends on these sets;
    they are refreshed here for any code that queries them fresh. Callers that
    imported a set *by value* keep their original reference.
    """
    global MUTATION_TOOLS, CACHEABLE_TOOLS, ADMIN_ONLY_TOOLS, MIGRATION_TOOLS
    MUTATION_TOOLS = frozenset(n for n, s in TOOL_REGISTRY.items() if s.mutation)
    CACHEABLE_TOOLS = frozenset(n for n, s in TOOL_REGISTRY.items() if s.cacheable)
    ADMIN_ONLY_TOOLS = frozenset(n for n, s in TOOL_REGISTRY.items() if s.admin_only)
    MIGRATION_TOOLS = frozenset(n for n, s in TOOL_REGISTRY.items() if s.migration)


def register_tool(name: str, spec: ToolSpec, *, replace: bool = False) -> None:
    """Register a host/extension MCP tool at runtime (NCE-FE-2).

    Lets a host add custom MCP tools **without editing this module or the
    dispatch loop** — see ``docs/FRONTEND_READINESS.md`` (NCE-FE-2). A registered
    tool is subject to the SAME dispatch-time gating as built-in tools: the
    ``nce:tools:disabled`` toggle, admin/scope checks (``spec.admin_only``),
    cache-generation bumping (``spec.mutation``) and response caching
    (``spec.cacheable``).

    Args:
        name: MCP tool name (the dispatch key).
        spec: the :class:`ToolSpec` (handler + flags).
        replace: if *False* (default) a duplicate ``name`` raises
            :class:`ValueError`; pass *True* to intentionally override.

    Raises:
        ValueError: if ``name`` is empty, or already registered without ``replace``.
    """
    if not name:
        raise ValueError("tool name must be a non-empty string")
    if name in TOOL_REGISTRY and not replace:
        raise ValueError(f"tool {name!r} is already registered; pass replace=True to override")
    TOOL_REGISTRY[name] = spec
    _refresh_derived_sets()


def infer_tool_engine(spec: ToolSpec) -> str | None:
    """Infer the primary engine domain for a ToolSpec from its handler module."""
    if spec.engine:
        return spec.engine
    qualname = getattr(spec.handler, "__qualname__", "")
    module_name = qualname.rsplit(".", 1)[0] if "." in qualname else ""
    if not module_name:
        module_name = getattr(spec.handler, "__module__", "") or ""

    if module_name.startswith("nce.vertical_modules."):
        parts = module_name.split(".")
        return parts[2]
    if module_name.startswith("nce.entity_resolution"):
        return "entity_resolution"
    if module_name.startswith("nce.pricing"):
        return "pricing"
    return None


def get_tool_engine(tool_name: str) -> str | None:
    """Return the engine domain for a registered tool, or None if global/unscoped."""
    spec = TOOL_REGISTRY.get(tool_name)
    if spec is None:
        return None
    return infer_tool_engine(spec)


def get_tool_dependencies(tool_name: str) -> tuple[str, ...]:
    """Return the set of engine dependencies that invalidate this tool's cache."""
    spec = TOOL_REGISTRY.get(tool_name)
    if spec is None:
        return ()
    if spec.engine_dependencies:
        return spec.engine_dependencies
    primary = infer_tool_engine(spec)
    if primary:
        return (primary,)
    return ()
