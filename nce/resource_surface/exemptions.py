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
    # Product Engine (Lane E Wave E-2 / Product resources)
    # ---------------------------------------------------------------------------
    "PRODUCT_SKU": ResourceExemption(
        owner_engine="product",
        reason=(
            "Real attribute table (product_catalog, 9 cols). Scheduled for Lane E "
            "Wave E-2 declaration once global scope contract is verified."
        ),
    ),
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
    "PO_LINE": ResourceExemption(
        owner_engine="procurement",
        reason=(
            "Real attribute table (procurement_po_lines, ~14 cols). Transition-split "
            "node across draft/ordered/received/cancelled. Scheduled for Wave C-8 / Lane E."
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
    # Sales Engine (Lane B Waves B-1, B-4 / Lane E)
    # ---------------------------------------------------------------------------
    "CUSTOMER": ResourceExemption(
        owner_engine="sales",
        reason=(
            "Real attribute table (sales_customers). Scheduled for Wave B-1 "
            "CUSTOMER resource declaration replacing duplicate hand-written routes."
        ),
    ),
    "LEAD": ResourceExemption(
        owner_engine="sales",
        reason=(
            "Real attribute table (sales_leads). Scheduled for Wave B-1 LEAD "
            "resource declaration replacing duplicate hand-written routes."
        ),
    ),
    "OPPORTUNITY": ResourceExemption(
        owner_engine="sales",
        reason=(
            "Sub-deal stage in the sales pipeline mapped onto sales_deals; scheduled "
            "for Wave B-1 / B-3 deal lifecycle restructuring."
        ),
    ),
    "DEAL": ResourceExemption(
        owner_engine="sales",
        reason=(
            "Real attribute table (sales_deals). Scheduled for Wave B-1 DEAL "
            "resource declaration replacing duplicate hand-written routes."
        ),
    ),
    "QUOTE": ResourceExemption(
        owner_engine="sales",
        reason=(
            "Real attribute table (sales_quotes). Scheduled for Wave B-1 QUOTE "
            "resource declaration replacing duplicate hand-written routes."
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
    "CONTRACTOR": ResourceExemption(
        owner_engine="vendors",
        reason=(
            "Real attribute table (contractor_profiles, 8 cols). Scheduled for Lane E "
            "/ Wave D-8 Vendors resource declaration."
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
            "Real attribute table (agreements). Scheduled for Wave B-9 AGREEMENT "
            "resource declaration replacing duplicate hand-written routes."
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
            "Real attribute table (economy_invoices). Scheduled for Wave B-13 "
            "CUSTOMER_INVOICE resource declaration."
        ),
    ),
    "POSTING": ResourceExemption(
        owner_engine="economy",
        reason=(
            "General ledger posting line. Scheduled for Wave B-14 ECONOMY_POSTING_LINE "
            "resource declaration."
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
    "TICKET": ResourceExemption(
        owner_engine="support",
        reason=(
            "Real attribute table (support_tickets). Scheduled for Wave D-5 TICKET_ACTION "
            "and ticket resource declaration."
        ),
    ),
    "SLA": ResourceExemption(
        owner_engine="support",
        reason=(
            "Operational SLA policy clock and breach tracker; scheduled for Support "
            "resource surface wave."
        ),
    ),
    "SUPPORT_HEALTH_SCORE": ResourceExemption(
        owner_engine="support",
        reason=(
            "Customer rolling health and sentiment score; scheduled for Support "
            "resource surface wave."
        ),
    ),
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
    "WORK_ORDER": ResourceExemption(
        owner_engine="field_tech",
        reason=(
            "Real attribute table (field_tech_work_orders). Scheduled for Wave C-9 "
            "WORK_ORDER resource declaration."
        ),
    ),
    "FIELD_TECH_CHECKLIST": ResourceExemption(
        owner_engine="field_tech",
        reason=(
            "ISO9001 compliance checklist entries; scheduled for Field Tech "
            "work order sub-resources."
        ),
    ),
    "FIELD_TECH_TIME_ENTRY": ResourceExemption(
        owner_engine="field_tech",
        reason=(
            "Real attribute table (field_tech_time_entries). Scheduled for Wave C-7 "
            "TIME_ENTRY spec and approval workflow."
        ),
    ),
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
    # Staff & Resources Engine (Lane E / Resources wave)
    # ---------------------------------------------------------------------------
    "RESOURCE": ResourceExemption(
        owner_engine="resources",
        reason=(
            "Schedulable personnel and vehicle master data; scheduled for Resources "
            "resource surface wave."
        ),
    ),
    "ALLOCATION": ResourceExemption(
        owner_engine="resources",
        reason=(
            "Time-window personnel and equipment booking allocation; scheduled for "
            "Resources resource surface wave."
        ),
    ),
    "TRAVEL_LEG": ResourceExemption(
        owner_engine="resources",
        reason=(
            "Technician travel route leg and Norwegian diet allowance segment; "
            "scheduled for Resources resource surface wave."
        ),
    ),
}
