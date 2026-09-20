"""nce.resource_surface.exemptions — Shrink-only exemptions for un-surfaced node types.

Phase A Wave A-2:
Every distinct node_type in nce/config_data/node-ownership.json must have either:
  1. A registered ResourceSpec in nce/resource_surface/, OR
  2. A reasoned exemption entry here specifying owner engine and substantive rationale.

This registry is SHRINK-ONLY. When a vertical engine wave (e.g. Lane E, B, C, D)
registers a ResourceSpec for an exempt node type, the corresponding exemption entry
MUST be deleted. Stale exemptions will fail test_c12_resource_surface_ratchet.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ResourceExemption:
    """Exemption metadata for an un-surfaced node type.

    Attributes:
        owner_engine: Engine responsible for the node type.
        reason: Substantive technical explanation of why no ResourceSpec is declared yet.
    """

    owner_engine: str
    reason: str


RESOURCE_SURFACE_EXEMPTIONS: dict[str, ResourceExemption] = {
    # ---------------------------------------------------------------------------
    # Procurement Engine (Lane C Wave C-8 / Lane E)
    # ---------------------------------------------------------------------------
    "PO": ResourceExemption(
        owner_engine="procurement",
        reason=(
            "kg_nodes-only spine node (label PO:<PO_NUMBER>, procurement/graph.py). "
            "No purchase-order table exists anywhere in schema.sql or migration "
            "history (grep -icE \"CREATE TABLE IF NOT EXISTS (purchase_orders|"
            "procurement_purchase_orders)\\b\" nce/schema.sql -> 0). PO_LINE, the "
            "priced line-item half, already has its own registered ResourceSpec. "
            "Wave C-8 is declined as host-only: no host purchase-order concept "
            "exists to adapt, so no wave is scheduled to add an attribute table "
            "here (CHARTER_WAVE_AUDIT.md, 2026-09-20)."
        ),
    ),
    "PROCUREMENT_MATCH": ResourceExemption(
        owner_engine="procurement",
        reason=(
            "kg_nodes-only stub representing automated invoice/PO matching pairs; "
            "pending Procurement resource surface and match queue backfill."
        ),
    ),
    # ---------------------------------------------------------------------------
    # System Design Engine (Lane C Waves C-1, C-3 / Lane E)
    # ---------------------------------------------------------------------------
    # FUNCTIONAL_LOCATION: registered (Wave E-19, 2026-09-20) --
    # nce/vertical_modules/system_design/resources.py. Thin, identity-only
    # (kind/as_built are derived at read time by fl_tree.py, not stored --
    # exposing them would give the same fact a second home). excluded_verbs=
    # {"list"} closes the system_design_list_functional_locations collision
    # the same way RESOURCE_SPEC does. See FUNCTIONAL_LOCATION_SPEC's own
    # comment for the full reasoning.
    # DESIGN: registered (Wave E-20, 2026-09-20) --
    # nce/vertical_modules/system_design/resources.py. Identity-only --
    # is_active is real, stored data but lives on system_design_geometry.meta,
    # the same validation-choke-point table DEVICE/RACK/CABLE's geometry
    # fields already couldn't route through. excluded_verbs={"list"} closes
    # the system_design_list_designs collision. See DESIGN_SPEC's own comment
    # for the full reasoning.
    "DESIGN_LINE": ResourceExemption(
        owner_engine="system_design",
        reason=(
            "kg_nodes-only identity/edge anchor (label DESIGN_LINE:<DESIGN_ID>:"
            "<LINE_REF>, system_design/graph.py). kg_nodes has no qty/price "
            "column for it -- a declared omission (D48, to_quote.py) -- so those "
            "values live on the priced sibling, bom_line_content, via BOM_LINE "
            "(its own exemption above), not on DESIGN_LINE itself. Wave C-3 "
            "(#329) registered DESIGN identity-only and never touched "
            "DESIGN_LINE; no wave is scheduled to add an attribute table here, "
            "since there is no further data to expose."
        ),
    ),
    "SIGNAL_CHAIN": ResourceExemption(
        owner_engine="system_design",
        reason=(
            "Retired in Batch 067i as a virtual connected_to walk over PORT; retained "
            "in node-ownership as an inert reservation with no table anywhere."
        ),
    ),
    "BOM_LINE": ResourceExemption(
        owner_engine="system_design",
        reason=(
            "Cross-engine transition-split node across five engines (system_design, "
            "sales, procurement, inventory, field_tech); scheduled for multi-engine wave."
        ),
    ),
    # ---------------------------------------------------------------------------
    # Project Engine (Lane C Wave C-6 / Lane E)
    # ---------------------------------------------------------------------------
    # PROJECT_PROJECT: registered (Wave C-6, 2026-09-20) --
    # nce/vertical_modules/project/resources.py. Thin kg_nodes-primary
    # identity only (label/entity_type/change_origin/timestamps) -- kg_nodes
    # itself has no attribute storage (confirmed empty for every PROJECT_*
    # node type, Q-47, still open). Phase gates/capacity/my-day/reports stay
    # on their own hand-written routes; this registration does not expose
    # them and was never meant to -- the old exemption reason here
    # ("phase gates and capacity metadata") described data that was never
    # actually queryable via kg_nodes, the same shape as the E-unblock
    # miscalculation from earlier tonight.
    "PROJECT_GATE": ResourceExemption(
        owner_engine="project",
        reason=(
            "kg_nodes-only identity/edge anchor. The current gate lives entirely "
            "as an in_phase kg_edge from the project node to a gate node, whose "
            "own created_at supplies dwell time (insights.py::do_status_report); "
            "read live today by the existing hand-written status-report route, "
            "not a stub. Measured and confirmed correct (Lane E, "
            "PROJECT_ENGINE_DESIGN.md, Q-47, 2026-09-20). Wave C-6 (#313) "
            "registered PROJECT_PROJECT identity-only and never touched "
            "sub-resources; no further wave is scheduled."
        ),
    ),
    "PROJECT_TASK": ResourceExemption(
        owner_engine="project",
        reason=(
            "kg_nodes-only identity/edge anchor. Task status (open/closed) is "
            "edge-presence, not a stored column -- a generates edge from the "
            "originating BOM_LINE marks it open, and closure is edge deletion "
            "(tasks.py::_close_superseded_tasks); task properties "
            "(gate_blocking/deadline/value) are themselves kg_edges predicates, "
            "string-encoded and parsed on read (pl.py::do_my_day/do_capacity). "
            "All of it is read live in the existing hand-written routes today, "
            "not a stub. Measured and confirmed correct (Lane E, "
            "PROJECT_ENGINE_DESIGN.md, Q-47, 2026-09-20). Wave C-6 (#313) "
            "registered PROJECT_PROJECT identity-only and never touched "
            "sub-resources; no further wave is scheduled."
        ),
    ),
    "PROJECT_CASE_STUDY": ResourceExemption(
        owner_engine="project",
        reason=(
            "kg_nodes-only stub representing customer case studies and historical "
            "delivery baselines; pending Project marketing integration wave."
        ),
    ),
    # ---------------------------------------------------------------------------
    # Staff & Resources Engine (Lane E / Resources wave)
    # ---------------------------------------------------------------------------
    # RESOURCE: registered (Wave E-19, 2026-09-20) --
    # nce/vertical_modules/resources/resources.py. excluded_verbs={"list"}
    # closes the resources_list_resources collision without renaming the
    # entity or touching collision detection -- see RESOURCE_SPEC's own
    # comment for the full reasoning.
    # ---------------------------------------------------------------------------
    # Sales Engine (Lane B Waves B-1, B-4 / Lane E)
    # ---------------------------------------------------------------------------
    "OPPORTUNITY": ResourceExemption(
        owner_engine="sales",
        reason=(
            "No sales_deals table exists (grep -c \"CREATE TABLE IF NOT EXISTS "
            "sales_deals\" nce/schema.sql -> 0); the prior reason's own claim that "
            "OPPORTUNITY maps onto it does not hold either. Same polymorphic "
            "sales_read_model shape as CUSTOMER. Corrected by Lane E's exemptions "
            "sweep. Scheduled for Wave B-1 / B-3 deal lifecycle restructuring."
        ),
    ),
    # ---------------------------------------------------------------------------
    # Vendors Engine (Lane D Wave D-8 / Lane E)
    # ---------------------------------------------------------------------------
    "VENDOR": ResourceExemption(
        owner_engine="vendors",
        reason=(
            "Not Postgres at all: attributes reside in Mongo episodes addressed by "
            "payload_ref; scheduled for storage_kind='mongo' Vendors resource wave."
        ),
    ),
    "CERT": ResourceExemption(
        owner_engine="vendors",
        reason=(
            "Not Postgres at all: certification payloads reside in Mongo episodes; "
            "scheduled for storage_kind='mongo' Vendors resource wave."
        ),
    ),
    "VENDORS_CERT": ResourceExemption(
        owner_engine="vendors",
        reason=(
            "Dead registry row with zero references across nce/ or tests/ (documented "
            "in Charter §13 Update 12); retained as an inert reservation."
        ),
    ),
    "CONTRACTOR": ResourceExemption(
        owner_engine="vendors",
        reason=(
            "Regression: Lane E's own Wave E-5 (PR #236) declared this against a real "
            "table (contractor_profiles) without checking its RLS policy first, and "
            "the spec is unreachable in production. contractor_profiles' ONLY policy "
            "is external_isolation_policy, requiring partner_scope_id = "
            "get_nce_external_scope() (grep -n \"POLICY.*contractor_profiles\" "
            "nce/schema.sql -> one policy, no separate tenant policy to OR against "
            "it; confirmed live via pg_policies too). admin_app.py:59 documents that "
            "the nce.external_scope_id GUC is NEVER set on admin_app sessions, and "
            "every C12 route/tool runs through admin_app's connection pool (the "
            "nce_app role this table's FORCE ROW LEVEL SECURITY applies to). So the "
            "CONTRACTOR resource surface returns zero rows on every read and fails "
            "every write, served from admin_app -- the same unreachable-by-RLS class "
            "found on customer_portal's three tables. The only legitimate path is "
            "hand-written: vendors/contractors.py and partner_view.py both call "
            "set_external_scope(conn, partner_scope_uuid) explicitly per request "
            "before querying, something resource_surface has no generic hook for. "
            "Do not re-declare without either a resource_surface capability for "
            "dual-key RLS tables or routing partner-facing reads through a separate "
            "app that establishes the external scope, the way customer_portal does."
        ),
    ),
    # ---------------------------------------------------------------------------
    # Agreements Engine (Lane B Wave B-10 / Lane E)
    # ---------------------------------------------------------------------------
    "AGREEMENT_TERM": ResourceExemption(
        owner_engine="agreements",
        reason=(
            "kg_nodes has no attribute column, so AGREEMENT_TERM nodes are "
            "identity/edge anchors only (agreements/authoring.py); the "
            "structured term data lives in agreement_review_queue.extracted, "
            "which lookup (B109), coverage (B108), and kickback (B110) already "
            "read. Wave B-10 (#298) shipped price_rules.py/index_series.py, "
            "neither of which touches AGREEMENT_TERM. A ResourceSpec here would "
            "need to expose agreement_review_queue, not AGREEMENT_TERM itself; "
            "no such surface is scheduled."
        ),
    ),
    # ---------------------------------------------------------------------------
    # Economy Engine (Lane B Wave B-14 / Lane E)
    # ---------------------------------------------------------------------------
    "INVOICE": ResourceExemption(
        owner_engine="economy",
        reason=(
            "kg_nodes-only stub -- no economy_invoices table exists in nce/schema.sql "
            "(grep -n \"CREATE TABLE IF NOT EXISTS.*invoice\" nce/schema.sql returns "
            "nothing). The prior exemption text claiming a real table was wrong; "
            "corrected by Lane E Wave E-7. This is the supplier-side node "
            "(economy/graph.py's upsert_invoice_from_procurement, the "
            "PO -[posted_to]-> INVOICE boundary edge) -- distinct from CUSTOMER_INVOICE "
            "(migration 105, Wave B-13), which answers the customer-facing half of "
            "this exemption's original forward reference. Still unbuilt on its own "
            "terms; no wave currently scheduled."
        ),
    ),
    "PERIOD": ResourceExemption(
        owner_engine="economy",
        reason=(
            "kg_nodes-only identity/edge anchor for accounting period/close "
            "events (graph.py::upsert_period_node, close_narrative.py); no "
            "balance-bearing table exists (grep -icE \"CREATE TABLE IF NOT "
            "EXISTS.*period\" nce/schema.sql -> 0). The financial period "
            "balance model a ResourceSpec would need is B-14's target, and "
            "B-14 was struck by Sindre, 2026-09-20 ('ok strike it') -- the "
            "remainder is the host's own local-mirror warehouse architecture, "
            "not an NCE-adapter job, and out of scope for v1.6. No wave is "
            "currently scheduled."
        ),
    ),
    "MARGIN": ResourceExemption(
        owner_engine="economy",
        reason=(
            "Per-dimension node where Economy owns only the 'actual' dimension cascade. "
            "Scheduled for Economy margin reconciliation resource wave."
        ),
    ),
    # ---------------------------------------------------------------------------
    # Support Engine (Lane D Waves D-5, D-6 / Lane E)
    # ---------------------------------------------------------------------------
    # TICKET, SLA, and SUPPORT_HEALTH_SCORE registered via C12 support resources.
    "SUPPORT_DIAGNOSIS": ResourceExemption(
        owner_engine="support",
        reason=(
            "AI Troubleshooter diagnostic analysis and proposed corrective action logs; "
            "scheduled for Support resource wave."
        ),
    ),
    # ---------------------------------------------------------------------------
    # Field Tech Engine (Lane C Waves C-7, C-9 / Lane E)
    # ---------------------------------------------------------------------------
    "FIELD_TECH_SCAN": ResourceExemption(
        owner_engine="field_tech",
        reason=(
            "Hardware barcode/QR serial scan events seeding BOM_LINE to ASSET links; "
            "scheduled for Field Tech resource wave."
        ),
    ),
    "FIELD_TECH_PHOTO": ResourceExemption(
        owner_engine="field_tech",
        reason=(
            "Photographic installation proof attachments linked to work orders; "
            "scheduled for C14 document integration."
        ),
    ),
    # ---------------------------------------------------------------------------
    # Customer Portal Engine (Module 17 / Lane E Wave E-17)
    # ---------------------------------------------------------------------------
    "PORTAL_USER": ResourceExemption(
        owner_engine="customer_portal",
        reason=(
            "Real, tenant-scoped table (portal_users) but its RLS policy "
            "(external_isolation_policy) requires customer_scope_id = "
            "get_nce_external_scope(), and admin_app.py:59 documents the contract "
            "explicitly: \"the nce.external_scope_id GUC is NEVER set on admin_app "
            "sessions.\" Every C12 route/tool runs through admin_app's connection "
            "pool (the same nce_app role the table's FORCE ROW LEVEL SECURITY "
            "applies to), so get_nce_external_scope() always returns NULL there and "
            "customer_scope_id = NULL is never true in SQL: a C12 spec on this "
            "table would silently return zero rows on every read and raise a raw "
            "Postgres RLS-violation error on every write, not a translated NCE "
            "error. This is a structural mismatch between C12's tenant_scope model "
            "(namespace_id only) and this table's dual-key RLS (namespace_id AND "
            "customer_scope_id), not a missing field list -- needs either a "
            "resource_surface capability for dual-key RLS tables or a decision "
            "that this table is only ever queried from the separate customer_portal "
            "app (nce/vertical_modules/customer_portal/app.py), which does "
            "establish the external scope. Confirmed the table itself is fine: "
            "grep -n \"CREATE TABLE IF NOT EXISTS portal_users\" nce/schema.sql."
        ),
    ),
    "PORTAL_DOCUMENT_SHARE": ResourceExemption(
        owner_engine="customer_portal",
        reason=(
            "Same RLS gap as PORTAL_USER: portal_document_shares carries the "
            "identical external_isolation_policy keyed on customer_scope_id, which "
            "admin_app's connection pool never sets."
        ),
    ),
    "PORTAL_SERVICE_REQUEST": ResourceExemption(
        owner_engine="customer_portal",
        reason=(
            "Same RLS gap as PORTAL_USER: portal_service_requests carries the "
            "identical external_isolation_policy keyed on customer_scope_id, which "
            "admin_app's connection pool never sets."
        ),
    ),
    # Business Insights Engine (Module 16 / Lane E Wave E-16)
    # ---------------------------------------------------------------------------
    "BUSINESS_INSIGHTS_BRIEFING": ResourceExemption(
        owner_engine="business_insights",
        reason=(
            "provenance.py documents this as a graph node type, but grep -rn "
            "\"INSERT INTO kg_nodes\" nce/vertical_modules/business_insights/ has zero "
            "matches. brief.py builds the briefing dict in memory and returns it "
            "directly in the HTTP response payload; it is never persisted anywhere, "
            "not even as a kg_nodes row -- worse than a kg_nodes-only stub. No "
            "ResourceSpec is possible until a future wave adds real persistence."
        ),
    ),
    "BUSINESS_INSIGHTS_FINDING": ResourceExemption(
        owner_engine="business_insights",
        reason=(
            "Same gap as BUSINESS_INSIGHTS_BRIEFING: documented in provenance.py's "
            "graph contract, never persisted (radar.py builds the finding dict in "
            "memory only, zero kg_nodes writes anywhere in the module)."
        ),
    ),
    "BUSINESS_INSIGHTS_SCENARIO": ResourceExemption(
        owner_engine="business_insights",
        reason=(
            "Same gap as BUSINESS_INSIGHTS_BRIEFING: documented in provenance.py's "
            "graph contract, never persisted (scenario.py builds the scenario dict "
            "in memory only, zero kg_nodes writes anywhere in the module)."
        ),
    ),
    # BILLING_RUN, BILLING_CANDIDATE, CUSTOMER_INVOICE (Waves B-12/B-13) were
    # exempted here pending "Lane E's mechanical follow-on" -- registered for
    # real as read-only ResourceSpecs in economy/resources.py by the economy
    # resource-surface registration row (2026-09-20, unrelated to the struck
    # B-14 warehouse-mirror wave; see
    # _internal/work-docs/mlv16-orchestration/ECONOMY_SURFACE_DECISION.md),
    # after confirming each has a real governed sole writer that must stay the
    # only write path. No longer exempted.
}
