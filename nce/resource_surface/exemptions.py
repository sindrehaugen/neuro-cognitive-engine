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
            "kg_nodes-only spine node referencing topology and geometry snapshots. "
            "Scheduled for Wave C-3 DESIGN versions per functional location."
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
    "CUSTOMER": ResourceExemption(
        owner_engine="sales",
        reason=(
            "No sales_customers table exists (grep -c \"CREATE TABLE IF NOT EXISTS "
            "sales_customers\" nce/schema.sql -> 0, and the string appears nowhere "
            "else in the file). CUSTOMER rows are multiplexed into the polymorphic "
            "sales_read_model table (entity discriminator column, natural key "
            "(namespace_id, entity, source_id), line 1644) alongside LEAD/DEAL/"
            "QUOTE/OPPORTUNITY -- a single ResourceSpec.table_name cannot represent "
            "one node type out of a shared multi-entity table without an "
            "entity-filter capability the spec shape does not have today. Corrected "
            "by Lane E's exemptions sweep (matches Lane B's independent K-B1 "
            "finding). Scheduled for Wave B-1 CUSTOMER resource declaration."
        ),
    ),
    "LEAD": ResourceExemption(
        owner_engine="sales",
        reason=(
            "No sales_leads table exists (grep -c \"CREATE TABLE IF NOT EXISTS "
            "sales_leads\" nce/schema.sql -> 0). Same polymorphic sales_read_model "
            "shape as CUSTOMER (see that entry). Corrected by Lane E's exemptions "
            "sweep. Scheduled for Wave B-1 LEAD resource declaration."
        ),
    ),
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
    "DEAL": ResourceExemption(
        owner_engine="sales",
        reason=(
            "No sales_deals table exists (grep -c \"CREATE TABLE IF NOT EXISTS "
            "sales_deals\" nce/schema.sql -> 0). Same polymorphic sales_read_model "
            "shape as CUSTOMER. Corrected by Lane E's exemptions sweep. Scheduled "
            "for Wave B-1 DEAL resource declaration."
        ),
    ),
    "QUOTE": ResourceExemption(
        owner_engine="sales",
        reason=(
            "No sales_quotes table exists (grep -c \"CREATE TABLE IF NOT EXISTS "
            "sales_quotes\" nce/schema.sql -> 0). Same polymorphic sales_read_model "
            "shape as CUSTOMER. Corrected by Lane E's exemptions sweep. Scheduled "
            "for Wave B-1 QUOTE resource declaration."
        ),
    ),
    "SIGNED_BASELINE": ResourceExemption(
        owner_engine="sales",
        reason=(
            "Legally signed immutable contract baseline written to WORM storage; "
            "pending Wave B-4 / 132i-b freeze baseline wiring."
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
    # ---------------------------------------------------------------------------
    # Agreements Engine (Lane B Wave B-9 / Lane E)
    # ---------------------------------------------------------------------------
    "AGREEMENT": ResourceExemption(
        owner_engine="agreements",
        reason=(
            "kg_nodes-only stub, not a real table -- no agreements table exists "
            "(grep -c \"CREATE TABLE IF NOT EXISTS agreements\" nce/schema.sql -> 0; "
            "the string 'agreements' appears only as the agreements_source_id column "
            "and in comments). nce/vertical_modules/agreements/graph.py inserts only "
            "into kg_nodes (label, entity_type, namespace_id, agreements_source_id, "
            "change_origin) -- no dedicated attribute row. Corrected by Lane E's "
            "exemptions sweep. Scheduled for Wave B-9 AGREEMENT resource declaration "
            "once a backing table exists."
        ),
    ),
    "AGREEMENT_TERM": ResourceExemption(
        owner_engine="agreements",
        reason=(
            "Sub-resource for contract terms, index series, and SLA parameters; "
            "scheduled for Wave B-9 / B-10 agreement term surfaces."
        ),
    ),
    "AGREEMENT_SIGNATURE": ResourceExemption(
        owner_engine="agreements",
        reason=(
            "Signature audit and Oneflow integration mirror state; scheduled for "
            "Wave B-9 agreement party and signature surfaces."
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
    # Assets Engine (Lane D Wave D-1 / Lane E)
    # ---------------------------------------------------------------------------
    "ASSET": ResourceExemption(
        owner_engine="assets",
        reason=(
            "Real attribute table (assets). Scheduled for Wave D-1 ASSET resource "
            "declaration including move, merge, and product linking."
        ),
    ),
    # ---------------------------------------------------------------------------
    # Support Engine (Lane D Waves D-5, D-6 / Lane E)
    # ---------------------------------------------------------------------------
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
}
