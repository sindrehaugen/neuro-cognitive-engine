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

from nce.resource_surface import build_all_resource_tool_specs
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
#
# Since Wave A-1b, every C12 ResourceSpec registration mounts 4 tools (list,
# get, upsert, archive) into TOOL_REGISTRY with no edit to any tool file --
# TOOL_REGISTRY.update(build_all_resource_tool_specs()) in
# nce/tool_registry.py. A raw pin on len(TOOL_REGISTRY) (or on MUTATION_TOOLS
# / CACHEABLE_TOOLS, which each gain 2 of those 4 per spec) therefore breaks
# on every future Lane A/E resource-surface registration -- it moved
# 135->137->139->141 in one day before this fix (janitor pass 7, K-H4).
#
# Fix: split each pin into a hand-written baseline (changes only when a lane
# adds/removes a hand-written tool -- same maintenance as before) plus the
# live C12 contribution (derived from the real registry, via the same
# production function TOOL_REGISTRY itself is built from, not a
# reimplementation of its naming rules).
#
# What this fix does NOT catch (K-H5, filed after ML-orch asked whether it
# needed extending): every assertion below, and the three engine-scoped
# siblings in test_{agreements,economy,product}_hardening.py, checks set
# MEMBERSHIP of tool NAMES -- it has no way to notice that
# TOOL_REGISTRY.update(build_all_resource_tool_specs()) silently replaced
# the VALUE (ToolSpec/handler) at an EXISTING key with a same-named C12 one.
# A collision doesn't change the key set at all, so a name-set-equality
# check is structurally blind to it -- this was already true of the raw
# pins these assertions replaced, not a regression introduced here.
# tests/test_generated_tool_collision_ratchet.py is the check for that
# failure mode, and it has to work differently: it reads hand-written names
# from nce/tool_registry.py's literal source via AST rather than from the
# live (already-mutated) TOOL_REGISTRY object, because by the time any test
# here runs, a real collision would already have silently happened and the
# losing entry would simply be gone from the object these tests inspect.

_C12_TOOL_SPECS = build_all_resource_tool_specs()
_C12_TOOL_NAMES = frozenset(_C12_TOOL_SPECS)
_C12_MUTATION_TOOLS = frozenset(n for n, s in _C12_TOOL_SPECS.items() if s.mutation)
_C12_CACHEABLE_TOOLS = frozenset(n for n, s in _C12_TOOL_SPECS.items() if s.cacheable)
_EXPECTED_STATIC_TOTAL = 334  # 292 hand-written tools + 8 FUNCTIONAL_LOCATION tree tools (Lane C Wave C-1) + 1 C17 site master data address-registry feed (Lane F Wave F-9) + 2 support action/timeline tools (Lane D Wave D-5) + 1 support on-call rota (Lane D Wave D-7) + 1 assets service history (Lane D Wave D-3) + 8 room cat & FL metadata tools (Lane C Wave C-2) + 6 assets person/subcomponent tools (Lane D Wave D-2) + 8 design versions & room spec tools (Lane C Wave C-3) + 4 agreements price rules & index series tools (Lane B Wave B-10) + 3 support summary/links tools (Lane D Wave D-6); see _C12_TOOL_NAMES above

# Re-exported for tests/unit/test_{assets,economy,inventory}_surface.py and
# test_sales_skeleton.py, which each do `from tests.test_tool_registry import
# _EXPECTED_TOTAL` as their own repo-wide cross-check against len(TOOL_REGISTRY).
# Kept as the FULL live total (static + C12), not the static baseline alone,
# so those four imports keep meaning exactly what their own assertions say
# ("Expected N tools (repo-wide ratchet)") without needing four more edits
# every time a C12 spec is registered -- the real regression protection is
# _EXPECTED_STATIC_TOTAL above; this name exists only for backward compatibility.
_EXPECTED_TOTAL = _EXPECTED_STATIC_TOTAL + len(_C12_TOOL_NAMES)


def test_registry_has_expected_entries():
    hand_written = TOOL_REGISTRY.keys() - _C12_TOOL_NAMES
    assert len(TOOL_REGISTRY) >= _EXPECTED_STATIC_TOTAL, (
        f"Sanity floor: expected at least {_EXPECTED_STATIC_TOTAL} tools, got {len(TOOL_REGISTRY)}."
    )
    assert len(hand_written) == _EXPECTED_STATIC_TOTAL, (
        f"Hand-written (non-C12) tool count changed: expected "
        f"{_EXPECTED_STATIC_TOTAL}, got {len(hand_written)}. If you "
        f"added/removed a hand-written tool, update this pin by import. If "
        f"you only registered a new C12 ResourceSpec, this number should "
        f"not move -- investigate. Tools: {sorted(hand_written)}"
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
        # ML9b-P1/P2: Assets tools
        "assets_seed_from_bom",
        "assets_pull_telemetry",
        "assets_attach_sla",
        "assets_compute_health",
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
        # Wave C-5 (System Design capability sync from product ETIM specs)
        "system_design_sync_device_capabilities",
        # Wave C-1 (System Design FUNCTIONAL_LOCATION tree mutations)
        "system_design_move_functional_location",
        "system_design_merge_functional_locations",
        "system_design_promote_functional_location",
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
        # ML10b-P1 -- Support Engine touchpoint record mutation
        "support_record_touchpoint",
        # ML10b-P2/P3 -- Support Engine dispatch and sync mutations
        "support_dispatch_work_order",
        "support_sync_now",
        # Wave SU-3 -- Support ecosystem mutations (2 tools)
        "support_failure_pattern",
        "support_upsell_signal",
        # ML15-B7 (M15.W7) -- Resources Engine mutations (4 tools)
        "resources_reserve",
        "resources_release",
        "resources_plan_material_flow",
        "resources_plan_travel",
        # ML17-B5 (M17.W5) -- Customer Portal Engine mutations (2 tools)
        "customer_portal_raise_service_request",
        "customer_portal_register_expansion_interest",
        # MLV15B-S2a -- Sales quote signing request Actor tool
        "sales_request_signature",
        # Wave PR-1 -- Procurement PO lifecycle Actor tools (2 tools)
        "procurement_generate_po",
        "procurement_submit_po",
        # MLV15D-AG2 -- Agreements surface completion Actor mutations (6 tools)
        "agreements_extract",
        "agreements_create",
        "agreements_suggest_revision",
        "agreements_request_signature",
        "agreements_record_signature",
        "agreements_review_extraction",
        # MLV15D-V1 -- Vendors master-data mutations (3 tools)
        "vendors_upsert_vendor",
        "vendors_upsert_contractor",
        "vendors_upsert_cert",
        # MLV15D-HR2 -- HR surface completion Actor mutations (2 tools)
        "hr_record_skill",
        "hr_update_absence_compliance",
        # Wave C10 -- Decision Feedback service mutation
        "decision_feedback_record",
        # Wave P-2 -- Product Spec Ingestion mutation
        "product_ingest_spec",
        # Wave PJ-1 -- Project record outcome mutation
        "project_record_outcome",
        # Wave C-PJ2 -- Project generate case study edge mutation
        "project_generate_case_study_edge",
        # Wave RS-3 -- Resources record allocation outcome mutation
        "resources_record_allocation_outcome",
        # Wave T-1 -- Trust dial set tier mutation
        "trust_dial_set_tier",
        # MLV15D-RS1 -- Resources master-data mutations (2 tools)
        "resources_create",
        "resources_update",
        # MLV15B-E3 -- Economy invoice approval cascade (Wave E-3)
        "economy_approve_invoice",
        # MLV15D-MK2 -- Marketing retract testimonial (Wave MK-2)
        "marketing_retract_testimonial",
        # MLV15D-S1 -- Sales native write path mutations (4 tools)
        "sales_create_customer",
        "sales_create_lead",
        "sales_create_deal",
        "sales_edit_deal",
        # Wave A-3 -- Assets NetBox sync bridge mutation
        "assets_sync_netbox",
        # Wave A-5 -- Assets failure pattern recorder (Actor mutation)
        "assets_record_failure_pattern",
        # Wave D-2 -- Assets person assignment & sub-components (Actor mutations)
        "assets_assign_person",
        "assets_unassign_person",
        "assets_link_subcomponent",
        "assets_unlink_subcomponent",
        # Wave IN-2 -- Inventory kitting & package reservation (Actor mutations)
        "inventory_reserve_kit",
        "inventory_release_kit",
        # Wave IN-3 -- Inventory restock PO creation (Actor mutation)
        "inventory_create_restock_po",
        # Wave A-1 -- C12 Inventory resource surface mutations (upsert + archive)
        "inventory_upsert_stock_locations",
        "inventory_archive_stock_locations",
        "inventory_upsert_inventory_items",
        "inventory_archive_inventory_items",
        "inventory_upsert_goods_receipts",
        "inventory_archive_goods_receipts",
        "inventory_upsert_inventory_rma",
        "inventory_archive_inventory_rma",
        # Wave A-3 -- C12 Notifications resource surface mutations (upsert + archive)
        "notifications_upsert_notifications",
        "notifications_archive_notifications",
        "notifications_upsert_reminders",
        "notifications_archive_reminders",
        # Lane E Wave E-3 -- C12 Procurement PO_LINE resource surface mutations (upsert + archive)
        "procurement_upsert_po_lines",
        "procurement_archive_po_lines",
        # Lane A Wave A-4 -- C14 Document Register resource surface mutations (upsert + archive)
        "documents_upsert_documents",
        "documents_archive_documents",
        # Lane E Wave E-6 -- C12 Resources ALLOCATION/TRAVEL_LEG resource surface mutations
        "resources_upsert_allocations",
        "resources_archive_allocations",
        "resources_upsert_travel_legs",
        "resources_archive_travel_legs",
        # Lane E Wave E-2 -- C12 Product resource surface mutations (upsert + archive)
        "product_upsert_product_skus",
        "product_archive_product_skus",
        # Lane A Wave A-5 -- C15 Legal-Entity Register resource surface mutations (upsert + archive)
        "legal_entities_upsert_legal_entities",
        "legal_entities_archive_legal_entities",
        # Lane F Wave F-8 -- C15 Legal-Entity Register BRREG registry-feed enrichment
        "legal_entities_enrich_from_registry",
        # Lane F Wave F-11 -- geodata OSM local-store import (global table, no namespace_id)
        "geodata_import_osm_elements",
        # Lane F Wave F-12 -- geodata N50 land-cover import (global table, no namespace_id)
        "geodata_import_n50_land_cover",
        # Lane F Wave F-13 -- geodata place-name import (global table, no namespace_id)
        "geodata_import_place_names",
        # Lane F Wave F-9 -- C17 Site Master Data address-registry enrichment
        "sites_enrich_address_from_registry",
        # Lane E Wave E-8 -- C12 Support TICKET/SLA/SUPPORT_HEALTH_SCORE resource surface mutations
        "support_upsert_tickets",
        "support_archive_tickets",
        "support_upsert_sla_clocks",
        "support_archive_sla_clocks",
        "support_upsert_customer_health",
        "support_archive_customer_health",
        # Lane E Wave E-7 -- C12 Economy POSTING resource surface mutations (upsert + archive)
        "economy_upsert_postings",
        "economy_archive_postings",
        # Lane E Wave E-10 -- C12 Field Tech WORK_ORDER/TIME_ENTRY/CHECKLIST resource surface mutations
        "field_tech_upsert_work_orders",
        "field_tech_archive_work_orders",
        "field_tech_upsert_time_entries",
        "field_tech_archive_time_entries",
        "field_tech_upsert_checklists",
        "field_tech_archive_checklists",
        # Wave C-1 -- FUNCTIONAL_LOCATION tree mutations (3 tools)
        "system_design_move_functional_location",
        "system_design_merge_functional_locations",
        "system_design_promote_functional_location",
        # Wave C-2 -- Room Categories & FL Metadata mutations (3 tools)
        "system_design_set_fl_room_category",
        "system_design_assign_fl_responsible",
        "system_design_unassign_fl_responsible",
        # Wave C-3 -- DESIGN versions & Room Specifications mutations (4 tools)
        "system_design_create_design",
        "system_design_update_design",
        "system_design_set_active_design",
        "system_design_set_room_spec",
        # Lane D Wave D-5 -- support_log_ticket_action
        "support_log_ticket_action",
        # Lane D Wave D-6 -- support_link_ticket
        "support_link_ticket",
        # Lane B Wave B-1 -- C12 Sales resource surface mutations (upsert + archive)
        "sales_upsert_customers",
        "sales_archive_customers",
        "sales_upsert_leads",
        "sales_archive_leads",
        "sales_upsert_deals",
        "sales_archive_deals",
        "sales_upsert_quotes",
        "sales_archive_quotes",
    }
)


def test_mutation_tools_exact_match():
    """_EXPECTED_MUTATION_TOOLS is the hand-written frozenset (its own history
    of C12 additions predates this fix and is harmless to leave in -- union
    is idempotent). The live C12 contribution is unioned in fresh each run,
    so a newly registered ResourceSpec's tools appear on both sides
    automatically and never require editing this frozenset again."""
    expected = _EXPECTED_MUTATION_TOOLS | _C12_MUTATION_TOOLS
    assert MUTATION_TOOLS == expected, (
        f"Extra: {MUTATION_TOOLS - expected}  Missing: {expected - MUTATION_TOOLS}"
    )


def test_mutation_tools_count():
    """Converted to a derived assertion (janitor pass 7, K-H4) -- see the
    module-level comment above _EXPECTED_STATIC_TOTAL. Original docstring
    history of every hand-written addition preserved below; it explains the
    123 hand-written baseline and stops at Wave A-3, the last hand-written
    addition before C12 registrations took over all further growth:

    131 baseline (pre-C12) - 8 C12 tools already folded into that baseline by
    earlier hand-edits (Wave A-3 Notifications & Reminders, +4) = 123 net
    hand-written mutation tools; see inline history below for every wave
    that landed a hand-written mutation tool.
    ... Batch 067c system_design_author_topology/system_design_author_functional_location, M6.W13b
    ... Batch 067h system_design_delete_planned, M6.W17
    ... Batch 138a inventory Actor tools, M11.W10a (7 of 11 registered tools mutate)
    ... ML12-B5 field_tech, ML13-B3 HR, ML14-B3 marketing, and every wave through
    Wave A-4 documents (now derived, not counted here).
    +1 Lane F Wave F-8 legal_entities_enrich_from_registry -> 124.
    +1 Lane F Wave F-11 geodata_import_osm_elements -> 125.
    +1 Lane F Wave F-12 geodata_import_n50_land_cover -> 126.
    +1 Lane F Wave F-13 geodata_import_place_names -> 127.
    +3 Wave C-1 FL tree mutations -> 130.
    +1 Lane F Wave F-9 sites_enrich_address_from_registry -> 131.
    +1 Lane D Wave D-5 support_log_ticket_action -> 132.
    +3 Wave C-2 Room Categories & FL Metadata mutations -> 135.
    +4 Wave C-3 DESIGN versions & Room Specifications mutations -> 143.
    +1 Lane D Wave D-6 support_link_ticket -> 144."""
    c12_mutation_tools = frozenset(
        n for n, s in build_all_resource_tool_specs().items() if s.mutation
    )
    hand_written_mutation_tools = MUTATION_TOOLS - c12_mutation_tools

    assert len(MUTATION_TOOLS) >= 144, (
        f"Sanity floor: expected at least 144 mutation tools, got {len(MUTATION_TOOLS)}."
    )
    assert len(hand_written_mutation_tools) == 144, (
        "Hand-written (non-C12) mutation tool count changed: expected 144, "
        f"got {len(hand_written_mutation_tools)}. If you added/removed a "
        "hand-written mutation tool, update this pin by import. If you only "
        "registered a new C12 ResourceSpec, this number should not move -- "
        f"investigate. Tools: {sorted(hand_written_mutation_tools)}"
    )


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
        # Procurement vertical module (M1.W4 / PR-3) — advisor reads, cacheable
        "procurement_aggregate_savings",
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
        # Wave C-5 (System Design standards & signals)
        "system_design_get_standards",
        "system_design_get_signal_rules",
        # Wave C-1 (System Design FUNCTIONAL_LOCATION tree reads)
        "system_design_list_functional_locations",
        "system_design_get_functional_location",
        "system_design_get_fl_children",
        "system_design_get_fl_ancestors",
        "system_design_get_fl_path",
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
        # Agreements vertical module (Batch 109 & Wave B-10) — term-lookup & price-rules/index-series advisor reads, cacheable
        "agreements_lookup_terms",
        "agreements_get_index_series",
        "agreements_calculate_index_adjustment",
        "agreements_get_price_rules",
        "agreements_evaluate_price_rule",
        # Economy vertical module (Batch 119, M8.W4) — Advisor reads, cacheable
        "economy_match_invoice",
        "economy_compute_periodisering",
        "economy_emit_event",
        # Economy vertical module (MLV15D Wave E-1) — surface completion reads, cacheable
        "economy_forecast_cashflow",
        "economy_snapshot_mrr_arr_churn",
        "economy_compute_dunning",
        "economy_compute_recognition_schedule",
        "economy_gl_sync_status",
        "economy_generate_close_narrative",
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
        # ML10b-P1 -- Support Engine triage advisor (cacheable)
        "support_triage_ticket",
        # Wave SU-3 -- Support ecosystem read-only aggregate (1 tool)
        "support_at_risk_aggregate",
        # Wave D-7 -- Support on-call rota and active responder routing (1 tool)
        "support_get_on_call",
        # ML15-B7 (M15.W7) -- Resources Engine cacheable reads (4 tools)
        "resources_resolve_capacity",
        "resources_detect_conflicts",
        "resources_forecast_demand",
        "resources_field_schedule",
        # ML17-B5 (M17.W5) -- Customer Portal Engine cacheable reads (6 tools)
        "customer_portal_room_tracker",
        "customer_portal_room_overview",
        "customer_portal_asset_register",
        "customer_portal_list_documents",
        "customer_portal_sla_status",
        "customer_portal_list_invoices",
        # ML16 (Business Insights Engine) -- executive watcher reads (3 cacheable tools)
        "business_insights_morning_brief",
        "business_insights_risk_radar",
        "business_insights_kpi_dashboard",
        # MLV15D-AG2 -- Agreements surface completion cacheable reads (3 tools)
        "agreements_coverage_matrix",
        "agreements_reconcile_kickback",
        "agreements_run_compliance_audit",
        # MLV15D-V1 -- Vendors cacheable read (1 tool)
        "vendors_get_contractor",
        # MLV15D-HR2 -- HR surface completion cacheable reads (3 tools)
        "hr_query_absences",
        "hr_compliance_deadlines",
        "hr_get_onboarding_progress",
        # Wave P-3 -- Product golden record read tool (cacheable)
        "product_golden_record",
        # Wave T-1 -- Trust dial status read tool (cacheable)
        "trust_dial_get_status",
        # MLV15D-RS1 -- Resources master-data cacheable reads (2 tools)
        "resources_get_resource",
        "resources_list_resources",
        # Wave S-6 -- Sales commission calculation Advisor tool (cacheable)
        "sales_calculate_commission",
        # MLV15D-E2 -- Economy PEPPOL and validation cacheable reads (3 tools)
        "economy_generate_kid",
        "economy_validate_kid",
        "economy_validate_contract",
        # Wave B-AG1 / B-E2 -- Economy GL records retrieval (cacheable read)
        "economy_get_gl_records",
        # Wave S-7 -- Sales divergence log parity window reader (cacheable read)
        "sales_divergence_log",
        # Wave S-4 -- Sales morning brief slice reader (cacheable read)
        "sales_morning_brief_slice",
        # Wave S-5 -- Sales lead score & quote draft advisors (cacheable read)
        "sales_score_lead",
        "sales_draft_quote",
        # Wave PR-4 -- Procurement resolve bids (cacheable read)
        "procurement_resolve_bids",
        # Wave PJ-3 -- Project recall similar (cacheable read)
        "project_recall_similar",
        "project_my_day",
        "project_capacity",
        "project_detect_scope_creep",
        "project_status_report",
        # Wave SD-6 -- System Design procurement view (cacheable read)
        "system_design_procurement_view",
        # Wave A-3 -- Assets warranty/EOL watcher (cacheable read)
        "assets_check_warranty_eol",
        # Wave A-4 -- Assets QR generator (cacheable read)
        "assets_generate_qr",
        # Wave D-3 -- Assets service history reader (cacheable read)
        "assets_service_history",
        # Wave D-2 -- Assets person assignment & sub-components (cacheable reads)
        "assets_list_person_assets",
        "assets_list_subcomponents",
        # Wave A-1 -- C12 Inventory resource surface cacheable reads (list + get)
        "inventory_list_stock_locations",
        "inventory_get_stock_locations",
        "inventory_list_inventory_items",
        "inventory_get_inventory_items",
        "inventory_list_goods_receipts",
        "inventory_get_goods_receipts",
        "inventory_list_inventory_rma",
        "inventory_get_inventory_rma",
        # Wave A-3 -- C12 Notifications resource surface cacheable reads (list + get)
        "notifications_list_notifications",
        "notifications_get_notifications",
        "notifications_list_reminders",
        "notifications_get_reminders",
        # Lane E Wave E-3 -- C12 Procurement PO_LINE resource surface cacheable reads (list + get)
        "procurement_list_po_lines",
        "procurement_get_po_lines",
        # Lane A Wave A-4 -- C14 Document Register resource surface cacheable reads (list + get)
        "documents_list_documents",
        "documents_get_documents",
        # Lane E Wave E-6 -- C12 Resources ALLOCATION/TRAVEL_LEG resource surface cacheable reads
        "resources_list_allocations",
        "resources_get_allocations",
        "resources_list_travel_legs",
        "resources_get_travel_legs",
        # Lane E Wave E-2 -- C12 Product resource surface cacheable reads (list + get)
        "product_list_product_skus",
        "product_get_product_skus",
        # Lane A Wave A-5 -- C15 Legal-Entity Register resource surface cacheable reads (list + get)
        "legal_entities_list_legal_entities",
        "legal_entities_get_legal_entities",
        # Lane F Wave F-11 -- geodata OSM bbox query (global table, no namespace_id)
        "geodata_query_osm_elements",
        # Lane F Wave F-12 -- geodata N50 land-cover query (global table, no namespace_id)
        "geodata_query_n50_land_cover",
        # Lane F Wave F-13 -- geodata nearest place-name query (global table, no namespace_id)
        "geodata_query_nearest_place_name",
        # Lane E Wave E-8 -- C12 Support TICKET/SLA/SUPPORT_HEALTH_SCORE resource surface cacheable reads
        "support_list_tickets",
        "support_get_tickets",
        "support_list_sla_clocks",
        "support_get_sla_clocks",
        "support_list_customer_health",
        "support_get_customer_health",
        # Lane E Wave E-7 -- C12 Economy POSTING resource surface cacheable reads (list + get)
        "economy_list_postings",
        "economy_get_postings",
        # Lane E Wave E-10 -- C12 Field Tech WORK_ORDER/TIME_ENTRY/CHECKLIST resource surface cacheable reads
        "field_tech_list_work_orders",
        "field_tech_get_work_orders",
        "field_tech_list_time_entries",
        "field_tech_get_time_entries",
        "field_tech_list_checklists",
        "field_tech_get_checklists",
        # Lane F Wave F-15 -- FX rate feed + weather live-read (global, no namespace_id)
        "pricing_get_fx_rates",
        "geodata_get_weather",
        # Wave C-1 -- FUNCTIONAL_LOCATION tree cacheable reads (5 tools)
        "system_design_list_functional_locations",
        "system_design_get_functional_location",
        "system_design_get_fl_children",
        "system_design_get_fl_ancestors",
        "system_design_get_fl_path",
        # Wave C-2 -- Room Categories & FL Metadata cacheable reads (5 tools)
        "system_design_list_room_categories",
        "system_design_get_room_category",
        "system_design_get_fl_room_category",
        "system_design_list_fl_responsible",
        "system_design_list_my_responsible_fls",
        # Wave C-3 -- DESIGN Versions & Room Specifications cacheable reads (4 tools)
        "system_design_list_designs",
        "system_design_get_design",
        "system_design_get_active_design",
        "system_design_get_room_spec",
        # Lane D Wave D-5 -- Support ticket timeline reader (cacheable read)
        "support_ticket_timeline",
        # Lane D Wave D-7 -- Support on-call rota reader (cacheable read)
        "support_get_on_call",
        # Lane D Wave D-6 -- Support ticket summary & links readers (2 cacheable reads)
        "support_summarise_ticket",
        "support_get_ticket_links",
        # Lane B Wave B-1 -- C12 Sales resource surface cacheable reads (list + get)
        "sales_list_customers",
        "sales_get_customers",
        "sales_list_leads",
        "sales_get_leads",
        "sales_list_deals",
        "sales_get_deals",
        "sales_list_quotes",
        "sales_get_quotes",
    }
)


def test_cacheable_tools_exact_match():
    """Same treatment as test_mutation_tools_exact_match above: the live C12
    contribution is unioned in fresh each run, so a newly registered
    ResourceSpec's list/get tools never require editing this frozenset."""
    expected = _EXPECTED_CACHEABLE | _C12_CACHEABLE_TOOLS
    assert CACHEABLE_TOOLS == expected, (
        f"Extra: {CACHEABLE_TOOLS - expected}  Missing: {expected - CACHEABLE_TOOLS}"
    )


def test_cacheable_tools_count():
    """Converted to a derived assertion (janitor pass 7, K-H4) -- same
    treatment as test_mutation_tools_count above. 115 is the hand-written
    baseline + 5 Lane F Wave F-11..F-15 tools + 5 Wave C-1 FL tree reads + 1 Wave D-5 support timeline + 1 Wave D-7 on-call + 1 Wave D-3 service history + 5 Wave C-2 room cat/FL metadata reads + 2 Wave D-2 assets person & subcomponents + 4 Wave C-3 design versions/room spec reads + 4 Wave B-10 agreements price rules/index series reads + 2 Wave D-6 support summary & links = 145."""
    c12_cacheable_tools = frozenset(
        n for n, s in build_all_resource_tool_specs().items() if s.cacheable
    )
    hand_written_cacheable_tools = CACHEABLE_TOOLS - c12_cacheable_tools

    assert len(CACHEABLE_TOOLS) >= 145, (
        f"Sanity floor: expected at least 145 cacheable tools, got {len(CACHEABLE_TOOLS)}."
    )
    assert len(hand_written_cacheable_tools) == 145, (
        "Hand-written (non-C12) cacheable tool count changed: expected 145, "
        f"got {len(hand_written_cacheable_tools)}. If you added/removed a "
        "hand-written cacheable tool, update this pin by import. If you "
        "only registered a new C12 ResourceSpec, this number should not "
        f"move -- investigate. Tools: {sorted(hand_written_cacheable_tools)}"
    )


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
        # ML10b-P2/P3 -- Support Engine dispatch and sync mutations
        "support_dispatch_work_order",
        "support_sync_now",
        # Wave SU-3 -- Support ecosystem admin-only tools (2 tools)
        "support_failure_pattern",
        "support_upsell_signal",
        # ML15-B7 (M15.W7) -- Resources Engine admin_only tools (1 tool)
        "resources_plan_material_flow",
        # ML16 (Business Insights Engine) -- executive/board admin-only tools (6 tools)
        "business_insights_morning_brief",
        "business_insights_risk_radar",
        "business_insights_run_scenario",
        "business_insights_generate_board_pack",
        "business_insights_kpi_dashboard",
        "business_insights_ask_business",
        # ML9b-P1 -- Assets Engine admin_only tools (operator/cron telemetry pull)
        "assets_pull_telemetry",
        # MLV15B-S2a -- Sales quote signing request Actor tool
        "sales_request_signature",
        # Wave PR-1 -- Procurement PO lifecycle Actor tools (2 tools)
        "procurement_generate_po",
        "procurement_submit_po",
        # MLV15D-AG2 -- Agreements surface completion admin tools (6 tools)
        "agreements_extract",
        "agreements_create",
        "agreements_suggest_revision",
        "agreements_request_signature",
        "agreements_record_signature",
        "agreements_review_extraction",
        # MLV15D-V1 -- Vendors master-data admin tools (3 tools)
        "vendors_upsert_vendor",
        "vendors_upsert_contractor",
        "vendors_upsert_cert",
        # MLV15D-HR2 -- HR surface completion admin tools (2 tools)
        "hr_record_skill",
        "hr_update_absence_compliance",
        # Wave C10 -- Decision Feedback service admin_only tool
        "decision_feedback_record",
        # Wave PJ-1 -- Project record outcome admin_only tool
        "project_record_outcome",
        # Wave C-PJ2 -- Project generate case study edge admin_only tool
        "project_generate_case_study_edge",
        # Wave RS-3 -- Resources record allocation outcome admin_only tool
        "resources_record_allocation_outcome",
        # Wave T-1 -- Trust dial set tier admin_only tool
        "trust_dial_set_tier",
        # MLV15D-RS1 -- Resources master-data admin tools (2 tools)
        "resources_create",
        "resources_update",
        # MLV15B-E3 -- Economy invoice approval cascade (Wave E-3)
        "economy_approve_invoice",
        # MLV15D-MK2 -- Marketing retract testimonial (Wave MK-2)
        "marketing_retract_testimonial",
        # MLV15D-S1 -- Sales native write path admin tools (4 tools)
        "sales_create_customer",
        "sales_create_lead",
        "sales_create_deal",
        "sales_edit_deal",
        # MLV15D-E2 -- Economy outbound EHF generation ([ADMIN])
        "economy_generate_ehf",
        # Wave T-6 / Estate Review -- Customer Portal Engine internal MCP admin tools (9 tools)
        "customer_portal_room_tracker",
        "customer_portal_room_overview",
        "customer_portal_asset_register",
        "customer_portal_list_documents",
        "customer_portal_sla_status",
        "customer_portal_list_invoices",
        "customer_portal_advisor_answer",
        "customer_portal_raise_service_request",
        "customer_portal_register_expansion_interest",
        # Wave A-3 -- Assets NetBox sync bridge (admin_only mutation)
        "assets_sync_netbox",
        # Wave A-5 -- Assets failure pattern recorder (admin_only mutation)
        "assets_record_failure_pattern",
        # Wave D-2 -- Assets person assignment & sub-components (admin_only mutations)
        "assets_assign_person",
        "assets_unassign_person",
        "assets_link_subcomponent",
        "assets_unlink_subcomponent",
        # Wave IN-2 -- Inventory kitting & package reservation (admin_only mutations)
        "inventory_reserve_kit",
        "inventory_release_kit",
        # Wave IN-3 -- Inventory restock PO creation (admin_only mutation)
        "inventory_create_restock_po",
        # Lane F Wave F-8 -- C15 Legal-Entity Register BRREG registry-feed enrichment
        # (operator/cron pull against an external registry, admin_only mutation)
        "legal_entities_enrich_from_registry",
        # Lane F Wave F-11 -- geodata OSM import (admin/batch job, admin_only mutation)
        "geodata_import_osm_elements",
        # Lane F Wave F-12 -- geodata N50 land-cover import (admin/batch job, admin_only mutation)
        "geodata_import_n50_land_cover",
        # Lane F Wave F-13 -- geodata place-name import (admin/batch job, admin_only mutation)
        "geodata_import_place_names",
        # Lane F Wave F-9 -- C17 Site Master Data address-registry enrichment
        # (operator/cron pull against an external registry, admin_only mutation)
        "sites_enrich_address_from_registry",
        # Lane D Wave D-5 -- support_log_ticket_action (admin_only mutation)
        "support_log_ticket_action",
        # Lane D Wave D-6 -- support_link_ticket (admin_only mutation)
        "support_link_ticket",
    }
)


def test_admin_only_tools_exact_match():
    assert ADMIN_ONLY_TOOLS == _EXPECTED_ADMIN_ONLY, (
        f"Extra: {ADMIN_ONLY_TOOLS - _EXPECTED_ADMIN_ONLY}  "
        f"Missing: {_EXPECTED_ADMIN_ONLY - ADMIN_ONLY_TOOLS}"
    )


def test_admin_only_tools_count():
    assert (
        len(ADMIN_ONLY_TOOLS) == 104
    )  # 90 baseline + 2 Inventory kitting (Wave IN-2) + 1 Inventory restock PO (Wave IN-3) + 1 BRREG registry-feed enrichment (Lane F Wave F-8) + 1 geodata OSM import (Lane F Wave F-11) + 1 geodata N50 land-cover import (Lane F Wave F-12) + 1 geodata place-name import (Lane F Wave F-13) + 1 sites address-registry enrichment (Lane F Wave F-9) + 1 support_log_ticket_action (Lane D Wave D-5) + 4 assets person & sub-components (Lane D Wave D-2) + 1 support_link_ticket (Lane D Wave D-6)


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
