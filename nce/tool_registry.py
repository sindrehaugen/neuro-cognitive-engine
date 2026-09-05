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
    graph_mcp_handlers,
    memory_mcp_handlers,
    migration_mcp_handlers,
    replay_mcp_handlers,
    snapshot_mcp_handlers,
)
from nce.admin_handlers import settings as settings_mcp_handlers
from nce.entity_resolution import mcp_handlers as entity_resolution_mcp_handlers
from nce.pricing import mcp_handlers as pricing_mcp_handlers
from nce.vertical_modules.agreements import mcp_handlers as agreements_mcp_handlers
from nce.vertical_modules.assets import mcp_handlers as assets_mcp_handlers
from nce.vertical_modules.diagnostics import mcp_handlers as diag_mcp_handlers
from nce.vertical_modules.dynamics365 import mcp_handlers as d365_mcp_handlers
from nce.vertical_modules.economy import mcp_handlers as economy_mcp_handlers
from nce.vertical_modules.field_tech import mcp_handlers as field_tech_mcp_handlers
from nce.vertical_modules.hr import mcp_handlers as hr_mcp_handlers
from nce.vertical_modules.inventory import mcp_handlers as inventory_mcp_handlers
from nce.vertical_modules.netbox import circuits as netbox_circuits
from nce.vertical_modules.procurement import mcp_handlers as procurement_mcp_handlers
from nce.vertical_modules.product import mcp_handlers as product_mcp_handlers
from nce.vertical_modules.project import mcp_handlers as project_mcp_handlers
from nce.vertical_modules.sales import mcp_handlers as sales_mcp_handlers
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
    # ------------------------------------------------------------------
    # Procurement vertical module tools (M1.W4) — Advisor: read-only
    # ------------------------------------------------------------------
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
    # ------------------------------------------------------------------
    # Agreements vertical module tools (Batch 109) — Advisor: read-only
    # ------------------------------------------------------------------
    "agreements_lookup_terms": ToolSpec(
        _h(agreements_mcp_handlers, "handle_agreements_lookup_terms"),
        cacheable=True,
        admin_only=False,
        mutation=False,
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
}

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
