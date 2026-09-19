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
            "kg_nodes-only spine node with no separate attribute table today. "
            "Scheduled for Wave C-8 PURCHASE_ORDER resource declaration."
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
    "FUNCTIONAL_LOCATION": ResourceExemption(
        owner_engine="system_design",
        reason=(
            "kg_nodes stub today; kind/address/coordinates await C17 SITE master data "
            "(A-9). Tree operations (children, ancestors, path) scheduled for Wave C-1."
        ),
    ),
    "DESIGN": ResourceExemption(
        owner_engine="system_design",
        reason=(
            "Solution design versions per functional location are managed via "
            "Wave C-3 design_versions domain service backed by kg_nodes, kg_edges, "
            "and system_design_geometry; standalone ResourceSpec deferred to Lane E."
        ),
    ),
    "DESIGN_LINE": ResourceExemption(
        owner_engine="system_design",
        reason=(
            "kg_nodes stub representing individual line items within a solution design. "
            "Scheduled for Wave C-3 along with design versioning."
        ),
    ),
    "DEVICE": ResourceExemption(
        owner_engine="system_design",
        reason=(
            "Multi-table spread across device_capabilities, node_state, and geometry. "
            "Spec is not 1:1 with a table; scheduled for System Design resource wave."
        ),
    ),
    "PORT": ResourceExemption(
        owner_engine="system_design",
        reason=(
            "Multi-table spread sharing device capabilities with DEVICE and barred by "
            "CHECK constraint from standalone 1:1 table; pending System Design wave."
        ),
    ),
    "SIGNAL_CHAIN": ResourceExemption(
        owner_engine="system_design",
        reason=(
            "Retired in Batch 067i as a virtual connected_to walk over PORT; retained "
            "in node-ownership as an inert reservation with no table anywhere."
        ),
    ),
    "RACK": ResourceExemption(
        owner_engine="system_design",
        reason=(
            "Multi-table spread covering physical cabinet placement, rack units, and "
            "geometry; scheduled for System Design physical layout resource wave."
        ),
    ),
    "CABLE": ResourceExemption(
        owner_engine="system_design",
        reason=(
            "Multi-table spread covering physical cable runs, wire gauge, and topology "
            "endpoints; scheduled for System Design infrastructure resource wave."
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
    "PROJECT_PROJECT": ResourceExemption(
        owner_engine="project",
        reason=(
            "kg_nodes-only spine node with phase gates and capacity metadata; scheduled "
            "for Wave C-6 PROJECT resource declaration."
        ),
    ),
    "PROJECT_GATE": ResourceExemption(
        owner_engine="project",
        reason=(
            "kg_nodes-only stub representing project phase gates and approval criteria; "
            "scheduled for Wave C-6 project sub-resources."
        ),
    ),
    "PROJECT_TASK": ResourceExemption(
        owner_engine="project",
        reason=(
            "kg_nodes-only stub representing work breakdown tasks and timeline items; "
            "scheduled for Wave C-6 project sub-resources."
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
    "RESOURCE": ResourceExemption(
        owner_engine="resources",
        reason=(
            "Real attribute table (resources, tenant-scoped) but node_type name "
            "equals the engine name: entity='resources' generates MCP tool "
            "resources_list_resources, which collides with and silently overwrites "
            "(via TOOL_REGISTRY.update()) the existing hand-written tool of that "
            "exact name (handle_resources_list_resources). get/upsert/archive do "
            "not collide (hand-written tools use singular resources_get_resource "
            "etc.), only list does. Needs a naming decision (rename the entity, "
            "which also changes the REST path, or fix collision detection in "
            "resource_surface/__init__.py) before declaring; Lane E Wave E-6 found "
            "this by diffing the exact TOOL_REGISTRY key set before/after."
        ),
    ),
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
            "Sub-resource for contract terms, index series, and SLA parameters; "
            "scheduled for Wave B-10 agreement price rules and terms surfaces."
        ),
    ),
    # ---------------------------------------------------------------------------
    # Economy Engine (Lane B Waves B-13, B-14 / Lane E)
    # ---------------------------------------------------------------------------
    "INVOICE": ResourceExemption(
        owner_engine="economy",
        reason=(
            "kg_nodes-only stub -- no economy_invoices table exists in nce/schema.sql "
            "(grep -n \"CREATE TABLE IF NOT EXISTS.*invoice\" nce/schema.sql returns "
            "nothing). The prior exemption text claiming a real table was wrong; "
            "corrected by Lane E Wave E-7. Scheduled for Wave B-13 CUSTOMER_INVOICE "
            "once that table is built."
        ),
    ),
    "PERIOD": ResourceExemption(
        owner_engine="economy",
        reason=(
            "Financial period balance model. Scheduled for Wave B-14 ECONOMY_PERIOD_BALANCE "
            "resource declaration."
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
}
