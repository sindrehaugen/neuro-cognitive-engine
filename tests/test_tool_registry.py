"""Structural contract tests for nce.tool_registry.

These tests pin the exact shape of TOOL_REGISTRY and its derived sets so that
accidental additions, removals, or mis-classification of tool metadata are
caught before any dispatch refactor goes live.

Run this suite before and after Batch 2.2 (dispatch rewrite) to verify that
the registry exactly mirrors the behaviour encoded in the original if-ladder.
"""

from __future__ import annotations

import inspect

import pytest

from nce.tool_registry import (
    ADMIN_ONLY_TOOLS,
    CACHEABLE_TOOLS,
    MIGRATION_TOOLS,
    MUTATION_TOOLS,
    TOOL_REGISTRY,
)

# ---------------------------------------------------------------------------
# Cardinality
# ---------------------------------------------------------------------------

_EXPECTED_TOTAL = 174  # 158 previous + 8 HR Engine tools + 8 marketing tools.


def test_registry_has_expected_entries():
    assert len(TOOL_REGISTRY) == _EXPECTED_TOTAL, (
        f"Expected {_EXPECTED_TOTAL} tools, got {len(TOOL_REGISTRY)}. "
        f"Tools: {sorted(TOOL_REGISTRY)}"
    )


# ---------------------------------------------------------------------------
# Handler callability
# ---------------------------------------------------------------------------


def test_all_handlers_are_async_callables():
    """Every registered handler must be an awaitable (async def) callable."""
    bad = [
        name
        for name, spec in TOOL_REGISTRY.items()
        if not (callable(spec.handler) and inspect.iscoroutinefunction(spec.handler))
    ]
    assert not bad, f"Non-async handlers found: {bad}"


# ---------------------------------------------------------------------------
# MUTATION_TOOLS — exact match with the hardcoded set from the old dispatch
# ---------------------------------------------------------------------------

# Ground truth for MUTATION_TOOLS.
# Base set (22) was copied from the original dispatch's _base_mutation_tools.
# replay_dlq and purge_dlq were added (review finding #1) — both write to the
# dead_letter_queue table and were erroneously absent from the original set.
_EXPECTED_MUTATION_TOOLS: frozenset[str] = frozenset(
    {
        # base set (22) — from pre-refactor mcp_stdio_dispatch._base_mutation_tools
        "store_memory",
        "store_artifact",
        "store_media",
        "index_code_file",
        "connect_bridge",
        "complete_bridge_auth",
        "disconnect_bridge",
        "force_resync_bridge",
        "create_snapshot",
        "delete_snapshot",
        "manage_namespace",
        "manage_quotas",
        "rotate_signing_key",
        "trigger_consolidation",
        "resolve_contradiction",
        "boost_memory",
        "forget_memory",
        "a2a_create_grant",
        "a2a_revoke_grant",
        "a2a_update_grant_scopes",
        "unredact_memory",
        # Batch 47 — Part II.4 Provable Forgetting; full crypto-shred + cascade
        # delete across all stores is a mutation (and admin_only).
        "shred_memory",
        "replay_reconstruct",
        # Batch 43 — bi-temporal accountability; optional counterfactual fork writes
        # events into the target namespace, so the tool is a mutation.
        "explain_past_decision",
        # DLQ mutations (2) — pre-existing omission corrected in code review
        "replay_dlq",
        "purge_dlq",
        # migration mutations (3) — always present in the registry;
        # the dispatch gate (NCE_DISABLE_MIGRATION_MCP) is applied separately.
        "start_migration",
        "commit_migration",
        "abort_migration",
        # D365 mutations
        "d365_sync_now",
        "import_snapshot",
        # C1 merge-queue mutations (2) — confirm and reject mutate queue rows
        # (status, decided_by, decided_at only); they are admin_only and
        # preserve the never-auto-merge invariant (Wave 6 SCOPE LOCK).
        "merge_queue_confirm",
        "merge_queue_reject",
        # M2.W7 — on-demand product enrichment (governed confirm-only mutation)
        "product_enrich",
        # M6.W11 — Lucid export (external publish is a mutation)
        "system_design_publish_design_docs",
        # M7.W4 — Sales→Project bridge (Actor: mutation=True, admin_only=True)
        "project_convert_signed_quote",
        # M7.W4a — phase-transition Actor (mutation=True, admin_only=True)
        "project_advance_phase",
        # Batch 77 (rl) — Diagnostic Log Digestion Engine mutations (2):
        # ingest mints a presigned PUT + registers a PENDING row; commit enqueues
        # the bundle-processing task on the diag_ingest RQ lane.
        "diag_ingest_bundle",
        "diag_commit_bundle",
        # Batch 131 (M11.W3) — Inventory Actor mutations (2): transfer moves
        # stock between two locations, record_consumption decrements it at
        # one; both are admin_only.
        "inventory_transfer_stock",
        "inventory_record_consumption",
        # Batch 143 (M9.W3) — Assets Actor mutation: advance-lifecycle writes
        # assets.lifecycle_state on a legal transition. NOT admin_only — the
        # MCP tools table in docs/vertical_engines/09-assets-engine.md
        # specifies cacheable=N, admin_only=N, mutation=Y for this tool.
        "assets_advance_lifecycle",
        # Batch 067c (M6.W13b) — System Design authoring: the first external
        # write path into the design graph. mutation=True is what makes the
        # dispatch loop bump the MCP cache generation, which is the only thing
        # that keeps the cacheable system_design_get_topology entry from serving
        # pre-write data for MCP_CACHE_TTL_S. Neither is admin_only — Copper
        # calls them as a tenant, and the guard is assert_owner + the SQL
        # namespace predicate, not an admin key.
        "system_design_author_topology",
        "system_design_author_functional_location",
        # Batch 067h (M6.W17) — the System Design retire tool, and the module's
        # FIRST delete path. mutation=True for the same cache reason as the two
        # above, and more sharply: without the generation bump the cacheable
        # system_design_get_topology entry keeps serving a device the caller
        # just removed. Unlike the two above it is ALSO admin_only — see
        # _EXPECTED_ADMIN_ONLY.
        "system_design_delete_planned",
        # Batch 138a (M11.W10a) — Inventory surface completion (7 mutations).
        # The Actor cores Batch 131's single surface wave predated: a goods
        # receipt and the receipt+three-way-match composition (two contracts,
        # not one with a wrapper), both reservation legs, the RMA record and
        # its two settlement legs. All seven are admin_only too — see
        # _EXPECTED_ADMIN_ONLY. inventory_valuation and
        # inventory_reconcile_dead_stock are NOT here: both only read.
        "inventory_record_goods_receipt",
        "inventory_record_goods_receipt_and_match",
        "inventory_reserve_stock",
        "inventory_release_stock",
        "inventory_record_rma",
        "inventory_restock_from_rma",
        "inventory_dispose_rma_weee",
        # System Design commercial surface (Batch 230a, M6.W26). Three of the
        # four are mutating; system_design_generate_sow is NOT here because it
        # only reads. enrich_design_lines writes no graph row itself but QUEUES
        # enrichment work, and a caller must be able to tell that invoking it
        # causes something to happen.
        "system_design_from_quote",
        "system_design_to_quote",
        "system_design_enrich_design_lines",
        # M5.W15 (Batch 132d) -- manual-pick BOM_LINE origination
        "sales_add_quote_line",
        # ML10-B5 (M10.W5) -- Support Engine mutations (Actor, admin_only)
        "support_open_ticket",
        "support_resolve_ticket",
        # ML12-B5 (M12.W5) -- Field Tech Engine mutations (8 tools)
        "field_tech_create_work_order",
        "field_tech_assign",
        "field_tech_complete_checklist",
        "field_tech_scan_serial",
        "field_tech_log_time",
        "field_tech_attach_photo",
        "field_tech_sync",
        "field_tech_record_outcome",
        # ML13-B3 (M13.W3) -- HR Engine mutations (3 tools)
        "hr_register_absence",
        "hr_build_onboarding_quest",
        "hr_log_one_on_one",
        # ML14-B3 (M14.W3) -- Marketing Engine mutations (5 tools)
        "marketing_draft_case_study",
        "marketing_request_testimonial",
        "marketing_capture_testimonial",
        "marketing_approve_content",
        "marketing_publish_content",
    }
)


def test_mutation_tools_exact_match():
    assert MUTATION_TOOLS == _EXPECTED_MUTATION_TOOLS, (
        f"Extra: {MUTATION_TOOLS - _EXPECTED_MUTATION_TOOLS}  "
        f"Missing: {_EXPECTED_MUTATION_TOOLS - MUTATION_TOOLS}"
    )


def test_mutation_tools_count():
    assert len(MUTATION_TOOLS) == 74  # 66 previous + 3 hr tools + 5 marketing tools
    # system_design_author_functional_location) from Batch 067c, M6.W13b
    # + 1 system_design retire tool (system_design_delete_planned) from
    # Batch 067h, M6.W17
    # + 7 Inventory Actor tools from Batch 138a, M11.W10a (surface completion):
    # inventory_record_goods_receipt, inventory_record_goods_receipt_and_match,
    # inventory_reserve_stock, inventory_release_stock, inventory_record_rma,
    # inventory_restock_from_rma, inventory_dispose_rma_weee. The same batch's
    # inventory_valuation and inventory_reconcile_dead_stock are read-only and
    # do NOT count here, which is why this moves by 7 and not by 11.


# ---------------------------------------------------------------------------
# CACHEABLE_TOOLS
# ---------------------------------------------------------------------------

_EXPECTED_CACHEABLE: frozenset[str] = frozenset(
    {
        "vendors_get_vendor",
        "vendors_compute_scorecard",
        "vendors_get_tier_status",
        "vendors_detect_reliability_degradation",
        "vendors_check_tier_at_risk",
        "vendors_match_contractor",
        "vendors_compute_performance",
        "vendors_recall_similar_jobs",
        "vendors_reliability_radar",
        "vendors_calibrate_weights",
        "semantic_search",
        "search_codebase",
        "graph_search",
        "neuromorphic_search",
        "d365_query_case",
        "d365_case_stress_report",
        "d365_netbox_mappings",
        "pricing_resolve",
        "resolve",
        "merge_queue_list",
        # Product vertical module (M2.W3) — advisor reads, cacheable
        "product_search",
        "product_get",
        # Product vertical module (M2.W4) — pricing advisor read, cacheable
        "product_price",
        # Product vertical module (M2.W5) — related-products advisor read, cacheable
        "product_related",
        # Procurement vertical module (M1.W4) — advisor reads, cacheable
        "procurement_calculate_tco",
        "procurement_rank_suppliers",
        "procurement_evaluate_match",
        # Procurement vertical module (M1.W12) — frontier advisor reads, cacheable
        "procurement_forecast_rebate",
        "procurement_recommend_move_spend",
        "procurement_whatif_spend",
        # System Design vertical module (M6.W1) — skeleton ping, cacheable
        "system_design_ping",
        # System Design vertical module (M6.W13a) — topology read, cacheable
        "system_design_get_topology",
        # Sales vertical module (Batch 080) — skeleton ping, cacheable
        "sales_ping",
        # Project vertical module (M7.W3) — phase-gate readiness check, cacheable
        "project_can_enter_phase",
        # Project vertical module (M7.W11) — suggest PL Advisor (needs HR via A2A)
        "project_suggest_pl",
        # Batch 77 (rl) — Diagnostic Log Digestion Engine read-only tools (3).
        "diag_digest_status",
        "diag_device_health",
        "diag_list_anomalies",
        # Agreements vertical module (Batch 109) — term-lookup advisor read, cacheable
        "agreements_lookup_terms",
        # Economy vertical module (Batch 119, M8.W4) — Advisor reads, cacheable
        "economy_match_invoice",
        "economy_compute_periodisering",
        "economy_emit_event",
        # Inventory vertical module (Batch 131, M11.W3) — Watcher read, cacheable
        "inventory_stock_levels",
        # Assets vertical module (Batch 141, M9.W1) — skeleton ping, cacheable
        "assets_ping",
        # Assets vertical module (Batch 143, M9.W3) — Watcher reads, cacheable
        "assets_get",
        "assets_list",
        # Inventory vertical module (Batch 138a, M11.W10a) — the only two
        # cacheable tools of the eleven the surface-completion wave added. Both
        # cores write nothing and derive from data that does not change per
        # call. inventory_valuation is deliberately NOT cacheable: it is derived
        # from the append-only inventory_transactions ledger and changes on
        # every movement — a stale quantity is a nuisance, a stale money
        # figure is a wrong number in someone's accounts.
        "inventory_recommend_restock",
        "inventory_forecast_demand",
        # ML10-B5 (M10.W5) -- Support Engine Watcher reads (cacheable)
        "support_query_ticket",
        "support_sla_clock",
        "support_health_score",
        "support_troubleshoot",
        # ML12-B5 (M12.W5) -- Field Tech Engine Advisor reads (cacheable)
        "field_tech_dispatch",
        "field_tech_partner_view",
        # ML13-B3 (M13.W3) -- HR Engine Advisor/Watcher reads (5 cacheable tools)
        "hr_get_employee",
        "hr_match_skills",
        "hr_capacity",
        "hr_cert_status",
        "hr_coach",
        # ML14-B3 (M14.W3) -- Marketing Engine cacheable reads (3 tools)
        "marketing_find_case_study_candidates",
        "marketing_suggest_content",
        "marketing_audit_seo",
    }
)


def test_cacheable_tools_exact_match():
    assert CACHEABLE_TOOLS == _EXPECTED_CACHEABLE, (
        f"Extra: {CACHEABLE_TOOLS - _EXPECTED_CACHEABLE}  "
        f"Missing: {_EXPECTED_CACHEABLE - CACHEABLE_TOOLS}"
    )


def test_cacheable_tools_count():
    assert len(CACHEABLE_TOOLS) == 62  # 54 previous + 5 hr tools + 3 marketing tools


# ---------------------------------------------------------------------------
# ADMIN_ONLY_TOOLS
# ---------------------------------------------------------------------------

_EXPECTED_ADMIN_ONLY: frozenset[str] = frozenset(
    {
        "unredact_memory",
        "replay_observe",
        "replay_reconstruct",
        "replay_fork",
        "replay_status",
        "explain_past_decision",
        # Batch 47 — Part II.4 Provable Forgetting; shred is destructive + admin-only.
        "shred_memory",
        "d365_sync_now",
        "d365_list_sla_breaches",
        # Batch 54 — V.6 config time-travel audit; admin-only read of the
        # config_changed/config_reset WORM history for a key.
        "explain_config_change",
        # C1 merge-queue admin tools (2) — confirm and reject are admin-only;
        # only humans/authorized services can decide on merge candidates.
        "merge_queue_confirm",
        "merge_queue_reject",
        # M7.W4 — Sales→Project bridge is Actor / admin-only (autonomous-by-tier).
        "project_convert_signed_quote",
        # M7.W4a — phase-transition Actor is admin-only.
        "project_advance_phase",
        # Batch 120 (rl) — causal-dag admin tool for cycle detection.
        "detect_causal_cycles",
        # Batch 131 (M11.W3) — Inventory Actor mutations are admin_only.
        "inventory_transfer_stock",
        "inventory_record_consumption",
        # Batch 067h (M6.W17) — System Design retire. THE ONLY TOOL IN THE
        # MODULE THAT CAN REMOVE ANYTHING, and the codebase's first delete
        # path. The two W13b authoring tools are deliberately NOT admin_only —
        # Copper calls them as a tenant — and this one deliberately IS: adding
        # and updating is a canvas operation, taking away is not.
        "system_design_delete_planned",
        # Batch 138a (M11.W10a) — Inventory surface completion. NINE of the
        # eleven tools are admin_only: the seven Actor mutations, plus two
        # read-only tools that are admin_only for their DATA rather than their
        # effect — inventory_valuation returns the money value of stock (cost
        # data is never a general-audience field) and inventory_reconcile_dead_stock
        # exposes the whole dead-stock position against the ledger.
        # inventory_recommend_restock and inventory_forecast_demand are the two
        # that are NOT admin_only: both are Watcher advisor reads.
        "inventory_record_goods_receipt",
        "inventory_record_goods_receipt_and_match",
        "inventory_reserve_stock",
        "inventory_release_stock",
        "inventory_record_rma",
        "inventory_restock_from_rma",
        "inventory_dispose_rma_weee",
        "inventory_valuation",
        "inventory_reconcile_dead_stock",
        # ML10-B5 (M10.W5) -- Support Engine mutations (admin_only)
        "support_open_ticket",
        "support_resolve_ticket",
        # ML12-B5 (M12.W5) -- Field Tech Engine admin_only tools (3 tools)
        "field_tech_create_work_order",
        "field_tech_assign",
        "field_tech_record_outcome",
        # ML13-B3 (M13.W3) -- HR Engine admin_only tools (2 tools)
        "hr_build_onboarding_quest",
        "hr_log_one_on_one",
        # ML14-B3 (M14.W3) -- Marketing Engine admin_only tools (5 tools)
        "marketing_draft_case_study",
        "marketing_request_testimonial",
        "marketing_capture_testimonial",
        "marketing_approve_content",
        "marketing_publish_content",
    }
)


def test_admin_only_tools_exact_match():
    assert ADMIN_ONLY_TOOLS == _EXPECTED_ADMIN_ONLY, (
        f"Extra: {ADMIN_ONLY_TOOLS - _EXPECTED_ADMIN_ONLY}  "
        f"Missing: {_EXPECTED_ADMIN_ONLY - ADMIN_ONLY_TOOLS}"
    )


def test_admin_only_tools_count():
    assert len(ADMIN_ONLY_TOOLS) == 39  # 32 previous + 2 hr tools + 5 marketing tools


# ---------------------------------------------------------------------------
# MIGRATION_TOOLS
# ---------------------------------------------------------------------------

_EXPECTED_MIGRATION: frozenset[str] = frozenset(
    {
        "start_migration",
        "migration_status",
        "validate_migration",
        "commit_migration",
        "abort_migration",
    }
)


def test_migration_tools_exact_match():
    assert MIGRATION_TOOLS == _EXPECTED_MIGRATION, (
        f"Extra: {MIGRATION_TOOLS - _EXPECTED_MIGRATION}  "
        f"Missing: {_EXPECTED_MIGRATION - MIGRATION_TOOLS}"
    )


def test_migration_tools_count():
    assert len(MIGRATION_TOOLS) == 5


# ---------------------------------------------------------------------------
# Derived-set consistency
# ---------------------------------------------------------------------------


def test_mutation_tools_subset_of_registry():
    assert MUTATION_TOOLS <= TOOL_REGISTRY.keys()


def test_cacheable_tools_subset_of_registry():
    assert CACHEABLE_TOOLS <= TOOL_REGISTRY.keys()


def test_admin_only_tools_subset_of_registry():
    assert ADMIN_ONLY_TOOLS <= TOOL_REGISTRY.keys()


def test_migration_tools_subset_of_registry():
    assert MIGRATION_TOOLS <= TOOL_REGISTRY.keys()


def test_migration_mutations_are_in_mutation_tools():
    """All migration tools marked mutation=True must appear in MUTATION_TOOLS."""
    migration_mutations = {
        name for name, spec in TOOL_REGISTRY.items() if spec.migration and spec.mutation
    }
    assert migration_mutations <= MUTATION_TOOLS


def test_no_tool_is_cacheable_and_mutation():
    """Cacheable and mutation are logically exclusive — a write should not be cached."""
    overlap = CACHEABLE_TOOLS & MUTATION_TOOLS
    assert not overlap, f"Tools are both cacheable and mutation: {overlap}"


# ---------------------------------------------------------------------------
# ToolSpec frozen-ness
# ---------------------------------------------------------------------------


def test_toolspec_is_frozen():
    spec = TOOL_REGISTRY["get_health"]
    with pytest.raises((AttributeError, TypeError)):
        spec.admin_only = True  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Spot-checks for a representative sample of each domain
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool_name,expected_flags",
    [
        # memory
        (
            "store_memory",
            {"mutation": True, "cacheable": False, "admin_only": False, "migration": False},
        ),
        (
            "semantic_search",
            {"mutation": False, "cacheable": True, "admin_only": False, "migration": False},
        ),
        (
            "unredact_memory",
            {"mutation": True, "cacheable": False, "admin_only": True, "migration": False},
        ),
        (
            "shred_memory",
            {"mutation": True, "cacheable": False, "admin_only": True, "migration": False},
        ),
        # code
        (
            "index_code_file",
            {"mutation": True, "cacheable": False, "admin_only": False, "migration": False},
        ),
        (
            "search_codebase",
            {"mutation": False, "cacheable": True, "admin_only": False, "migration": False},
        ),
        # graph
        (
            "graph_search",
            {"mutation": False, "cacheable": True, "admin_only": False, "migration": False},
        ),
        (
            "neuromorphic_search",
            {"mutation": False, "cacheable": True, "admin_only": False, "migration": False},
        ),
        # bridges
        (
            "connect_bridge",
            {"mutation": True, "cacheable": False, "admin_only": False, "migration": False},
        ),
        (
            "list_bridges",
            {"mutation": False, "cacheable": False, "admin_only": False, "migration": False},
        ),
        # migration
        (
            "start_migration",
            {"mutation": True, "cacheable": False, "admin_only": False, "migration": True},
        ),
        (
            "migration_status",
            {"mutation": False, "cacheable": False, "admin_only": False, "migration": True},
        ),
        (
            "commit_migration",
            {"mutation": True, "cacheable": False, "admin_only": False, "migration": True},
        ),
        # replay
        (
            "replay_observe",
            {"mutation": False, "cacheable": False, "admin_only": True, "migration": False},
        ),
        (
            "replay_reconstruct",
            {"mutation": True, "cacheable": False, "admin_only": True, "migration": False},
        ),
        (
            "get_event_provenance",
            {"mutation": False, "cacheable": False, "admin_only": False, "migration": False},
        ),
        (
            "explain_memory",
            {"mutation": False, "cacheable": False, "admin_only": False, "migration": False},
        ),
        (
            "explain_past_decision",
            {"mutation": True, "cacheable": False, "admin_only": True, "migration": False},
        ),
        (
            "explain_config_change",
            {"mutation": False, "cacheable": False, "admin_only": True, "migration": False},
        ),
        # a2a
        (
            "a2a_create_grant",
            {"mutation": True, "cacheable": False, "admin_only": False, "migration": False},
        ),
        (
            "a2a_list_grants",
            {"mutation": False, "cacheable": False, "admin_only": False, "migration": False},
        ),
        # admin
        (
            "manage_namespace",
            {"mutation": True, "cacheable": False, "admin_only": False, "migration": False},
        ),
        (
            "get_health",
            {"mutation": False, "cacheable": False, "admin_only": False, "migration": False},
        ),
        # snapshots
        (
            "create_snapshot",
            {"mutation": True, "cacheable": False, "admin_only": False, "migration": False},
        ),
        (
            "compare_states",
            {"mutation": False, "cacheable": False, "admin_only": False, "migration": False},
        ),
        (
            "import_snapshot",
            {"mutation": True, "cacheable": False, "admin_only": False, "migration": False},
        ),
        # catalog
        (
            "suggest_queries",
            {"mutation": False, "cacheable": False, "admin_only": False, "migration": False},
        ),
        # d365
        (
            "d365_query_case",
            {"mutation": False, "cacheable": True, "admin_only": False, "migration": False},
        ),
        (
            "d365_sync_now",
            {"mutation": True, "cacheable": False, "admin_only": True, "migration": False},
        ),
        (
            "d365_case_stress_report",
            {"mutation": False, "cacheable": True, "admin_only": False, "migration": False},
        ),
        (
            "d365_list_sla_breaches",
            {"mutation": False, "cacheable": False, "admin_only": True, "migration": False},
        ),
        (
            "evaluate_circuit_impact",
            {"mutation": False, "cacheable": False, "admin_only": False, "migration": False},
        ),
    ],
)
def test_tool_flags(tool_name: str, expected_flags: dict):
    spec = TOOL_REGISTRY[tool_name]
    for flag, expected in expected_flags.items():
        actual = getattr(spec, flag)
        assert actual == expected, f"{tool_name}.{flag}: expected {expected!r}, got {actual!r}"


@pytest.mark.asyncio
async def test_handle_neuromorphic_search_success():
    import json
    from unittest.mock import AsyncMock, MagicMock

    from nce.graph_mcp_handlers import handle_neuromorphic_search
    from nce.graph_query import Subgraph

    # Mock engine and traverser
    mock_engine = MagicMock()
    mock_traverser = AsyncMock()
    mock_engine._graph_traverser = mock_traverser

    # Mock subgraph result
    dummy_subgraph = Subgraph(anchor="mock_anchor")
    mock_traverser.neuromorphic_search.return_value = dummy_subgraph

    # Valid arguments
    args = {
        "namespace_id": "00000000-0000-4000-8000-000000000001",
        "query": "test query",
        "telemetry_severity": 0.8,
        "theta": 0.6,
        "decay": 0.9,
        "alpha": 1.1,
        "ticks": 3,
        "max_depth": 3,
        "anchor_top_k": 2,
    }

    # Call handler
    resp = await handle_neuromorphic_search(mock_engine, args)
    resp_dict = json.loads(resp)

    assert resp_dict["anchor"] == "mock_anchor"
    mock_traverser.neuromorphic_search.assert_called_once_with(
        query="test query",
        namespace_id="00000000-0000-4000-8000-000000000001",
        max_depth=3,
        anchor_top_k=2,
        user_id=None,
        private=False,
        as_of=None,
        max_edges_per_node=512,
        edge_limit=None,
        edge_offset=0,
        telemetry_severity=0.8,
        theta=0.6,
        decay=0.9,
        alpha=1.1,
        ticks=3,
    )
