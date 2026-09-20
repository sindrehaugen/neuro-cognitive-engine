"""tests/tool_pins.py — single source of truth for hand-written per-engine tool pins.

**Adding or removing an MCP tool for one of the engines below? Edit HERE, not in the
test file for that engine.** Before this module existed, the same invariant (exact
tool names + their ``cacheable``/``admin_only``/``mutation`` flags, or just the exact
name set) was hand-typed independently in twelve places. A lane adding a tool had no
way to discover which of those twelve files it also needed to touch, so the failure
stayed silent until CI caught the drift in whichever file happened to still test it.

This module does not weaken any check: every test file below still imports its own
entry from here and still asserts it against the live ``TOOL_REGISTRY`` exactly as
before. Forgetting to update this file for a new tool fails exactly as loudly as
forgetting to update the old per-file literal did — the mechanical enforcement is
unchanged, only the number of places a lane has to know about is (twelve -> one).

Two shapes, because the underlying tests check two different things:

``FLAG_PINS[engine]``
    ``{tool_name: {"cacheable": bool, "admin_only": bool, "mutation": bool, ...}}``
    Pins both the exact name set AND each tool's registry flags.

``NAME_PINS[engine]``
    ``frozenset[str]`` of exact tool names only, no flags. These three engines
    (agreements, economy, product) already union this against the live C12
    resource-surface contribution (``build_all_resource_tool_specs()``) at the
    call site, per janitor pass 7 / K-H4 — that union stays in the test file,
    not here, since it is test *logic*, not a pin.

Not in scope here: ``tests/test_tool_registry.py``'s own
``_EXPECTED_MUTATION_TOOLS`` / ``_EXPECTED_CACHEABLE`` / ``_EXPECTED_ADMIN_ONLY`` /
``_EXPECTED_MIGRATION``. Those pin category membership across the ENTIRE registry
(~500+ tools, all engines at once) and already use their own hand-baseline +
C12-derived-union pattern (see that file's module docstring). This module pins
per-ENGINE detail instead. The two are a real tangle -- an engine tool's flags are
technically asserted twice, once here and once via category membership there -- but
collapsing that is a separate, estate-wide change and out of scope for this pass.
"""

from __future__ import annotations

FLAG_PINS: dict[str, dict[str, dict[str, bool]]] = {
    # -----------------------------------------------------------------------
    # system_design -- Module 6 (tests/unit/test_system_design_toolcount.py)
    # -----------------------------------------------------------------------
    "system_design": {
        "system_design_ping": {
            "cacheable": True,
            "admin_only": False,
            "mutation": False,
        },
        "system_design_publish_design_docs": {
            "cacheable": False,
            "admin_only": False,
            "mutation": True,
        },
        # M6.W13a — read-only topology surface; the first row of Copper's contract
        # table.  These three flags ARE the contract: never adjust them to make a
        # failing test pass.
        "system_design_get_topology": {
            "cacheable": True,
            "admin_only": False,
            "mutation": False,
        },
        # Wave SD-5 / Copper Contract-I — thin signal-flow inspection.
        "system_design_inspect_signal_flow": {
            "cacheable": False,
            "admin_only": False,
            "mutation": False,
        },
        # Wave SD-6 — frozen design grouped by ranked supplier for PR-1.
        "system_design_procurement_view": {
            "cacheable": True,
            "admin_only": False,
            "mutation": False,
        },
        # M6.W13b — the authoring surface, rows two and three of Copper's contract
        # table.  ``mutation=True`` is not decoration: it is what makes the dispatch
        # loop bump the MCP cache generation, and that bump is the only thing that
        # keeps the cacheable ``system_design_get_topology`` entry above from
        # serving pre-write data for the full MCP_CACHE_TTL_S.  Flipping it to False
        # to quiet something is a silent stale-read bug.
        "system_design_author_topology": {
            "cacheable": False,
            "admin_only": False,
            "mutation": True,
        },
        "system_design_author_functional_location": {
            "cacheable": False,
            "admin_only": False,
            "mutation": True,
        },
        # M6.W13c — the design-graph validator, row four of Copper's contract table.
        # ``cacheable=False`` next to ``mutation=False`` is neither a contradiction
        # nor a copy-paste slip: it IS a read, but a design under active canvas
        # editing must never be served a stale verdict, and no write sits on this
        # path whose cache-generation bump would refresh one.  Flipping it to True
        # to win a cache hit is a silent stale-verdict bug.
        "system_design_validate_design_graph": {
            "cacheable": False,
            "admin_only": False,
            "mutation": False,
        },
        # M6.W17 — retire planned nodes, and the codebase's FIRST delete path.
        # ``admin_only=True`` is the flag that separates this row from the two
        # authoring rows above, and it is the contract rather than a preference:
        # those add and update, this is the only tool in the module that can take
        # something away. Flipping it to False to let a tenant key call it hands
        # every Copper caller a delete.
        #
        # ``mutation=True`` for the same cache reason as the authoring tools, and
        # more sharply: without the generation bump the cacheable
        # ``system_design_get_topology`` entry above keeps serving a device the
        # caller just removed for the full MCP_CACHE_TTL_S.
        #
        # The NAME is a deliberate mismatch with the behaviour — the default is a
        # soft retire and nothing is removed without ``permanent=true`` — and the
        # name is pinned by Copper's contract, so this row must never be "fixed"
        # by renaming the tool to match what it does.
        "system_design_delete_planned": {
            "cacheable": False,
            "admin_only": True,
            "mutation": True,
        },
        # M6.W26 (Batch 230a) -- the COMMERCIAL half of the design loop. Four cores
        # that had no route and no tool. Flags come from each core's call graph:
        #   from_quote           _upsert_edge + do_author_functional_location +
        #                        emit_graph_write            -> mutation=True
        #   to_quote             _upsert_edge                -> mutation=True
        #   enrich_design_lines  _fire_product_enrichment -> enqueue_product_enrichment
        #                        writes no graph row but QUEUES work -> mutation=True
        #   generate_sow         only _read_* helpers         -> mutation=False
        # cacheable=False on all four for validate_design_graph's stated reason: a
        # design under active canvas editing must not be served a stale answer.
        "system_design_from_quote": {
            "cacheable": False,
            "admin_only": False,
            "mutation": True,
        },
        "system_design_to_quote": {
            "cacheable": False,
            "admin_only": False,
            "mutation": True,
        },
        "system_design_generate_sow": {
            "cacheable": False,
            "admin_only": False,
            "mutation": False,
        },
        "system_design_enrich_design_lines": {
            "cacheable": False,
            "admin_only": False,
            "mutation": True,
        },
        # M6.W27 (Batch 230a2) -- propose-only, so mutation=False. Exposed separately
        "system_design_propose_design": {
            "cacheable": False,
            "admin_only": False,
            "mutation": False,
        },
        # Wave C-5 — standards, signal-distribution rules, and capability sync
        "system_design_get_standards": {
            "cacheable": True,
            "admin_only": False,
            "mutation": False,
        },
        "system_design_get_signal_rules": {
            "cacheable": True,
            "admin_only": False,
            "mutation": False,
        },
        "system_design_sync_device_capabilities": {
            "cacheable": False,
            "admin_only": False,
            "mutation": True,
        },
        # Wave C-1 — FUNCTIONAL_LOCATION tree operations
        "system_design_list_functional_locations": {
            "cacheable": True,
            "admin_only": False,
            "mutation": False,
        },
        "system_design_get_functional_location": {
            "cacheable": True,
            "admin_only": False,
            "mutation": False,
        },
        "system_design_get_fl_children": {
            "cacheable": True,
            "admin_only": False,
            "mutation": False,
        },
        "system_design_get_fl_ancestors": {
            "cacheable": True,
            "admin_only": False,
            "mutation": False,
        },
        "system_design_get_fl_path": {
            "cacheable": True,
            "admin_only": False,
            "mutation": False,
        },
        "system_design_move_functional_location": {
            "cacheable": False,
            "admin_only": False,
            "mutation": True,
        },
        "system_design_merge_functional_locations": {
            "cacheable": False,
            "admin_only": False,
            "mutation": True,
        },
        "system_design_promote_functional_location": {
            "cacheable": False,
            "admin_only": False,
            "mutation": True,
        },
        # Wave C-2 — Room Categories & FL Metadata
        "system_design_list_room_categories": {
            "cacheable": True,
            "admin_only": False,
            "mutation": False,
        },
        "system_design_get_room_category": {
            "cacheable": True,
            "admin_only": False,
            "mutation": False,
        },
        "system_design_set_fl_room_category": {
            "cacheable": False,
            "admin_only": False,
            "mutation": True,
        },
        "system_design_get_fl_room_category": {
            "cacheable": True,
            "admin_only": False,
            "mutation": False,
        },
        "system_design_assign_fl_responsible": {
            "cacheable": False,
            "admin_only": False,
            "mutation": True,
        },
        "system_design_unassign_fl_responsible": {
            "cacheable": False,
            "admin_only": False,
            "mutation": True,
        },
        "system_design_list_fl_responsible": {
            "cacheable": True,
            "admin_only": False,
            "mutation": False,
        },
        "system_design_list_my_responsible_fls": {
            "cacheable": True,
            "admin_only": False,
            "mutation": False,
        },
        # Wave C-3 — DESIGN Versions & Room Specifications
        "system_design_list_designs": {
            "cacheable": True,
            "admin_only": False,
            "mutation": False,
        },
        "system_design_get_design": {
            "cacheable": True,
            "admin_only": False,
            "mutation": False,
        },
        "system_design_create_design": {
            "cacheable": False,
            "admin_only": False,
            "mutation": True,
        },
        "system_design_update_design": {
            "cacheable": False,
            "admin_only": False,
            "mutation": True,
        },
        "system_design_set_active_design": {
            "cacheable": False,
            "admin_only": False,
            "mutation": True,
        },
        "system_design_get_active_design": {
            "cacheable": True,
            "admin_only": False,
            "mutation": False,
        },
        "system_design_get_room_spec": {
            "cacheable": True,
            "admin_only": False,
            "mutation": False,
        },
        "system_design_set_room_spec": {
            "cacheable": False,
            "admin_only": False,
            "mutation": True,
        },
        # Wave C-4 — Solution Design Intake Queue (losningsdesign-ko / DESIGN_REQUEST)
        "system_design_create_design_request": {
            "cacheable": False,
            "admin_only": False,
            "mutation": True,
        },
        "system_design_get_design_request": {
            "cacheable": True,
            "admin_only": False,
            "mutation": False,
        },
        "system_design_list_design_requests": {
            "cacheable": True,
            "admin_only": False,
            "mutation": False,
        },
        "system_design_update_design_request": {
            "cacheable": False,
            "admin_only": False,
            "mutation": True,
        },
        "system_design_assign_design_request": {
            "cacheable": False,
            "admin_only": False,
            "mutation": True,
        },
        "system_design_complete_design_request": {
            "cacheable": False,
            "admin_only": False,
            "mutation": True,
        },
        "system_design_fulfill_request_from_quote": {
            "cacheable": False,
            "admin_only": False,
            "mutation": True,
        },
        # Lane E Wave E-11 (PR #311) -- C12 kg_nodes-primary resource surface for
        # DEVICE/PORT/RACK/CABLE (nce/vertical_modules/system_design/resources.py).
        # list/get are cacheable reads; upsert/archive are mutations. Flags read
        # directly off the live TOOL_REGISTRY entries before pinning them here,
        # not assumed from the list/get/upsert/archive naming convention.
        "system_design_list_devices": {"cacheable": True, "admin_only": False, "mutation": False},
        "system_design_get_devices": {"cacheable": True, "admin_only": False, "mutation": False},
        "system_design_upsert_devices": {"cacheable": False, "admin_only": False, "mutation": True},
        "system_design_archive_devices": {
            "cacheable": False,
            "admin_only": False,
            "mutation": True,
        },
        "system_design_list_ports": {"cacheable": True, "admin_only": False, "mutation": False},
        "system_design_get_ports": {"cacheable": True, "admin_only": False, "mutation": False},
        "system_design_upsert_ports": {"cacheable": False, "admin_only": False, "mutation": True},
        "system_design_archive_ports": {"cacheable": False, "admin_only": False, "mutation": True},
        "system_design_list_racks": {"cacheable": True, "admin_only": False, "mutation": False},
        "system_design_get_racks": {"cacheable": True, "admin_only": False, "mutation": False},
        "system_design_upsert_racks": {"cacheable": False, "admin_only": False, "mutation": True},
        "system_design_archive_racks": {"cacheable": False, "admin_only": False, "mutation": True},
        "system_design_list_cables": {"cacheable": True, "admin_only": False, "mutation": False},
        "system_design_get_cables": {"cacheable": True, "admin_only": False, "mutation": False},
        "system_design_upsert_cables": {"cacheable": False, "admin_only": False, "mutation": True},
        "system_design_archive_cables": {"cacheable": False, "admin_only": False, "mutation": True},
        # Wave E-19 (charter's ResourceSpec.excluded_verbs) -- FUNCTIONAL_LOCATION.
        # list is excluded (see FUNCTIONAL_LOCATION_SPEC's own comment); the
        # hand-written system_design_list_functional_locations stays authoritative.
        "system_design_get_functional_locations": {
            "cacheable": True,
            "admin_only": False,
            "mutation": False,
        },
        "system_design_upsert_functional_locations": {
            "cacheable": False,
            "admin_only": False,
            "mutation": True,
        },
        "system_design_archive_functional_locations": {
            "cacheable": False,
            "admin_only": False,
            "mutation": True,
        },
        # Wave E-20 (charter's C-3) -- DESIGN. list is excluded (see
        # DESIGN_SPEC's own comment); the hand-written
        # system_design_list_designs stays authoritative.
        "system_design_get_designs": {
            "cacheable": True,
            "admin_only": False,
            "mutation": False,
        },
        "system_design_upsert_designs": {
            "cacheable": False,
            "admin_only": False,
            "mutation": True,
        },
        "system_design_archive_designs": {
            "cacheable": False,
            "admin_only": False,
            "mutation": True,
        },
    },
    # -----------------------------------------------------------------------
    # vendors -- Wave V-1 (tests/unit/test_vendors_surface.py)
    # -----------------------------------------------------------------------
    "vendors": {
        "vendors_get_vendor": {"cacheable": True, "admin_only": False, "mutation": False},
        "vendors_compute_scorecard": {"cacheable": True, "admin_only": False, "mutation": False},
        "vendors_get_tier_status": {"cacheable": True, "admin_only": False, "mutation": False},
        "vendors_detect_reliability_degradation": {
            "cacheable": True,
            "admin_only": False,
            "mutation": False,
        },
        "vendors_check_tier_at_risk": {"cacheable": True, "admin_only": False, "mutation": False},
        "vendors_match_contractor": {"cacheable": True, "admin_only": False, "mutation": False},
        "vendors_compute_performance": {"cacheable": True, "admin_only": False, "mutation": False},
        "vendors_recall_similar_jobs": {"cacheable": True, "admin_only": False, "mutation": False},
        "vendors_reliability_radar": {"cacheable": True, "admin_only": False, "mutation": False},
        "vendors_calibrate_weights": {"cacheable": True, "admin_only": False, "mutation": False},
        "vendors_upsert_vendor": {"cacheable": False, "admin_only": True, "mutation": True},
        "vendors_upsert_contractor": {"cacheable": False, "admin_only": True, "mutation": True},
        "vendors_get_contractor": {"cacheable": True, "admin_only": False, "mutation": False},
        "vendors_upsert_cert": {"cacheable": False, "admin_only": True, "mutation": True},
    },
    # -----------------------------------------------------------------------
    # support -- Module D (tests/unit/test_support_surface.py)
    # -----------------------------------------------------------------------
    "support": {
        "support_query_ticket": {
            "cacheable": True,
            "admin_only": False,
            "mutation": False,
        },
        "support_open_ticket": {
            "cacheable": False,
            "admin_only": True,
            "mutation": True,
        },
        "support_sla_clock": {
            "cacheable": True,
            "admin_only": False,
            "mutation": False,
        },
        "support_health_score": {
            "cacheable": True,
            "admin_only": False,
            "mutation": False,
        },
        "support_troubleshoot": {
            "cacheable": True,
            "admin_only": False,
            "mutation": False,
        },
        "support_resolve_ticket": {
            "cacheable": False,
            "admin_only": True,
            "mutation": True,
        },
        "support_triage_ticket": {
            "cacheable": True,
            "admin_only": False,
            "mutation": False,
        },
        "support_record_touchpoint": {
            "cacheable": False,
            "admin_only": False,
            "mutation": True,
        },
        "support_dispatch_work_order": {
            "cacheable": False,
            "admin_only": True,
            "mutation": True,
        },
        "support_sync_now": {
            "cacheable": False,
            "admin_only": True,
            "mutation": True,
        },
    },
    # -----------------------------------------------------------------------
    # field_tech -- Module 12 (tests/unit/test_field_tech_surface.py)
    # -----------------------------------------------------------------------
    "field_tech": {
        "field_tech_dispatch": {"cacheable": True, "admin_only": False, "mutation": False},
        "field_tech_partner_view": {"cacheable": True, "admin_only": False, "mutation": False},
        "field_tech_create_work_order": {"cacheable": False, "admin_only": True, "mutation": True},
        "field_tech_assign": {"cacheable": False, "admin_only": True, "mutation": True},
        "field_tech_complete_checklist": {
            "cacheable": False,
            "admin_only": False,
            "mutation": True,
        },
        "field_tech_scan_serial": {"cacheable": False, "admin_only": False, "mutation": True},
        "field_tech_log_time": {"cacheable": False, "admin_only": False, "mutation": True},
        "field_tech_attach_photo": {"cacheable": False, "admin_only": False, "mutation": True},
        "field_tech_sync": {"cacheable": False, "admin_only": False, "mutation": True},
        "field_tech_record_outcome": {"cacheable": False, "admin_only": True, "mutation": True},
    },
    # -----------------------------------------------------------------------
    # marketing -- Module 14 (tests/unit/test_marketing_surface.py)
    # -----------------------------------------------------------------------
    "marketing": {
        "marketing_find_case_study_candidates": {
            "cacheable": True,
            "admin_only": False,
            "mutation": False,
        },
        "marketing_draft_case_study": {"cacheable": False, "admin_only": True, "mutation": True},
        "marketing_request_testimonial": {
            "cacheable": False,
            "admin_only": True,
            "mutation": True,
        },
        "marketing_capture_testimonial": {
            "cacheable": False,
            "admin_only": True,
            "mutation": True,
        },
        "marketing_suggest_content": {"cacheable": True, "admin_only": False, "mutation": False},
        "marketing_audit_seo": {"cacheable": True, "admin_only": False, "mutation": False},
        "marketing_approve_content": {"cacheable": False, "admin_only": True, "mutation": True},
        "marketing_publish_content": {"cacheable": False, "admin_only": True, "mutation": True},
        "marketing_retract_testimonial": {
            "cacheable": False,
            "admin_only": True,
            "mutation": True,
        },
    },
    # -----------------------------------------------------------------------
    # assets -- (tests/unit/test_assets_surface.py)
    # -----------------------------------------------------------------------
    "assets": {
        "assets_get": {
            "cacheable": True,
            "admin_only": False,
            "mutation": False,
            "migration": False,
        },
        "assets_list": {
            "cacheable": True,
            "admin_only": False,
            "mutation": False,
            "migration": False,
        },
        "assets_advance_lifecycle": {
            "cacheable": False,
            "admin_only": False,
            "mutation": True,
            "migration": False,
        },
        "assets_seed_from_bom": {
            "cacheable": False,
            "admin_only": False,
            "mutation": True,
            "migration": False,
        },
        "assets_pull_telemetry": {
            "cacheable": False,
            "admin_only": True,
            "mutation": True,
            "migration": False,
        },
        "assets_attach_sla": {
            "cacheable": False,
            "admin_only": False,
            "mutation": True,
            "migration": False,
        },
        "assets_generate_qr": {
            "cacheable": True,
            "admin_only": False,
            "mutation": False,
            "migration": False,
        },
    },
    # -----------------------------------------------------------------------
    # business_insights (tests/test_business_insights_hardening.py)
    # -----------------------------------------------------------------------
    "business_insights": {
        "business_insights_morning_brief": {
            "cacheable": True,
            "admin_only": True,
            "mutation": False,
        },
        "business_insights_risk_radar": {"cacheable": True, "admin_only": True, "mutation": False},
        "business_insights_run_scenario": {
            "cacheable": False,
            "admin_only": True,
            "mutation": False,
        },
        "business_insights_generate_board_pack": {
            "cacheable": False,
            "admin_only": True,
            "mutation": False,
        },
        "business_insights_kpi_dashboard": {
            "cacheable": True,
            "admin_only": True,
            "mutation": False,
        },
        "business_insights_ask_business": {
            "cacheable": False,
            "admin_only": True,
            "mutation": False,
        },
    },
    # -----------------------------------------------------------------------
    # economy_peppol -- Wave E-2 (tests/unit/test_economy_peppol_surface.py)
    #
    # A strict subset of the "economy_" prefix namespace also covered (as
    # names only, no flags) by NAME_PINS["economy"] below -- this dict is the
    # only place the flags for these four are pinned. Not merged into one
    # structure: that would change what each test asserts, not just where the
    # data lives, so it is filed as an observation rather than done here.
    # -----------------------------------------------------------------------
    "economy_peppol": {
        "economy_generate_kid": {"cacheable": True, "admin_only": False, "mutation": False},
        "economy_validate_kid": {"cacheable": True, "admin_only": False, "mutation": False},
        "economy_generate_ehf": {"cacheable": False, "admin_only": True, "mutation": False},
        "economy_validate_contract": {"cacheable": True, "admin_only": False, "mutation": False},
    },
    # -----------------------------------------------------------------------
    # inventory -- (tests/unit/test_inventory_surface.py)
    # -----------------------------------------------------------------------
    "inventory": {
        "inventory_stock_levels": {
            "cacheable": True,
            "admin_only": False,
            "mutation": False,
            "migration": False,
        },
        "inventory_transfer_stock": {
            "cacheable": False,
            "admin_only": True,
            "mutation": True,
            "migration": False,
        },
        "inventory_record_consumption": {
            "cacheable": False,
            "admin_only": True,
            "mutation": True,
            "migration": False,
        },
    },
}

NAME_PINS: dict[str, frozenset[str]] = {
    # -----------------------------------------------------------------------
    # agreements (tests/unit/test_agreements_hardening.py)
    # -----------------------------------------------------------------------
    "agreements": frozenset(
        {
            "agreements_lookup_terms",
            "agreements_coverage_matrix",
            "agreements_reconcile_kickback",
            "agreements_run_compliance_audit",
            "agreements_extract",
            "agreements_create",
            "agreements_suggest_revision",
            "agreements_request_signature",
            "agreements_record_signature",
            "agreements_review_extraction",
            # Wave B-10: Price Rules & Index Series (Config-as-IP)
            "agreements_get_index_series",
            "agreements_calculate_index_adjustment",
            "agreements_get_price_rules",
            "agreements_evaluate_price_rule",
        }
    ),
    # -----------------------------------------------------------------------
    # economy (tests/unit/test_economy_hardening.py)
    #
    # Includes the four tools whose flags are ALSO pinned, separately, in
    # FLAG_PINS["economy_peppol"] above -- see the note there.
    # -----------------------------------------------------------------------
    "economy": frozenset(
        {
            "economy_approve_invoice",
            "economy_match_invoice",
            "economy_compute_periodisering",
            "economy_emit_event",
            "economy_forecast_cashflow",
            "economy_snapshot_mrr_arr_churn",
            "economy_compute_dunning",
            "economy_compute_recognition_schedule",
            "economy_gl_sync_status",
            "economy_generate_close_narrative",
            "economy_generate_kid",
            "economy_validate_kid",
            "economy_generate_ehf",
            "economy_validate_contract",
            "economy_get_gl_records",
            # Lane E Wave E-7 -- C12 POSTING resource surface (list/get/upsert/archive)
            "economy_list_postings",
            "economy_get_postings",
            "economy_upsert_postings",
            "economy_archive_postings",
        }
    ),
    # -----------------------------------------------------------------------
    # product -- Waves 3-7 + P-2/P-3 (tests/unit/test_product_hardening.py)
    # -----------------------------------------------------------------------
    "product": frozenset(
        {
            "product_search",
            "product_get",
            "product_price",
            "product_related",
            "product_match_bom_line",
            "product_enrich",
            "product_ingest_spec",
            "product_golden_record",
            # Lane E Wave E-2 -- C12 PRODUCT_SKU resource surface (list/get/upsert/archive)
            "product_list_product_skus",
            "product_get_product_skus",
            "product_upsert_product_skus",
            "product_archive_product_skus",
        }
    ),
}
