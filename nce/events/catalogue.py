"""nce.events.catalogue — Central event-contract catalogue and schema definitions.

Phase 0 Wave I-2:
Defines every event selector ("{node_type}.{op}") across the estate, recording
its declared producers, declared consumers, lifecycle status, and architectural reasons.
Eliminates dead seams where consumers subscribe to unproduced events, and enforces
that any newly introduced emitter must register its event contract here.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class EventContract:
    """Declared contract for an event selector emitted or subscribed via the C4 outbox bus."""

    selector: str
    node_type: str
    op: str
    declared_producers: tuple[str, ...]
    declared_consumers: tuple[str, ...]
    status: str  # "ACTIVE" | "UNPRODUCED" | "PARKED" | "UNCONSUMED"
    reason: str | None = None
    description: str = ""

    @property
    def producers(self) -> tuple[str, ...]:
        """Alias for declared_producers."""
        return self.declared_producers

    @property
    def consumers(self) -> tuple[str, ...]:
        """Alias for declared_consumers."""
        return self.declared_consumers


# Canonical catalogue of all event selectors across the Neuro-Cognitive Engine estate.
EVENT_CATALOGUE: Mapping[str, EventContract] = {
    # ---------------------------------------------------------------------------
    # Live Seams: Subscribed selectors currently lacking in-code producers
    # (Wave I-2 starting RED baseline — shrink-only allowlist)
    # ---------------------------------------------------------------------------
    "PO_LINE.status_changed": EventContract(
        selector="PO_LINE.status_changed",
        node_type="PO_LINE",
        op="status_changed",
        declared_producers=("nce/vertical_modules/procurement/po_line.py",),
        declared_consumers=("nce/vertical_modules/project/automation.py",),
        status="ACTIVE",
        description="Emitted when a purchase order line status advances (e.g. ORDERED).",
    ),
    "GOODS_RECEIPT.created": EventContract(
        selector="GOODS_RECEIPT.created",
        node_type="GOODS_RECEIPT",
        op="created",
        declared_producers=("nce/vertical_modules/inventory/goods_receipt.py",),
        declared_consumers=("nce/vertical_modules/project/automation.py",),
        status="ACTIVE",
        description="Emitted when incoming goods receipt is posted in the warehouse.",
    ),
    "TICKET.dispatched": EventContract(
        selector="TICKET.dispatched",
        node_type="TICKET",
        op="dispatched",
        declared_producers=("nce/vertical_modules/support/dispatch.py",),
        declared_consumers=("nce/vertical_modules/field_tech/work_orders.py",),
        status="ACTIVE",
        description="Emitted when a support ticket is dispatched to Field Tech as a work order.",
    ),
    "BOM_LINE.status_changed": EventContract(
        selector="BOM_LINE.status_changed",
        node_type="BOM_LINE",
        op="status_changed",
        declared_producers=(),
        declared_consumers=("nce/vertical_modules/project/tasks.py",),
        status="UNPRODUCED",
        reason=(
            "Subscribed in project/tasks.py; scheduled to be emitted by "
            "update_bom_line_status in v1.5 Phase 1 Wave FT-1 and PR-1."
        ),
        description="Emitted when a BOM line status transitions along the delivery ladder.",
    ),
    "CERTIFICATION.CREATED": EventContract(
        selector="CERTIFICATION.CREATED",
        node_type="CERTIFICATION",
        op="CREATED",
        declared_producers=(),
        declared_consumers=("nce/vertical_modules/resources/watcher.py",),
        status="UNPRODUCED",
        reason=(
            "Subscribed in resources/watcher.py for HR compliance; scheduled to be "
            "produced by HR engine in v1.5 Phase 1 Wave HR-1."
        ),
        description="Emitted when an employee or contractor certification is registered.",
    ),
    "CERTIFICATION.UPDATED": EventContract(
        selector="CERTIFICATION.UPDATED",
        node_type="CERTIFICATION",
        op="UPDATED",
        declared_producers=(),
        declared_consumers=("nce/vertical_modules/resources/watcher.py",),
        status="UNPRODUCED",
        reason=(
            "Subscribed in resources/watcher.py for HR compliance; scheduled to be "
            "produced by HR engine in v1.5 Phase 1 Wave HR-1."
        ),
        description="Emitted when a certification record is renewed or updated.",
    ),
    "CERTIFICATION.EXPIRED": EventContract(
        selector="CERTIFICATION.EXPIRED",
        node_type="CERTIFICATION",
        op="EXPIRED",
        declared_producers=(
            "nce/vertical_modules/hr/certs.py",
            "nce/vertical_modules/vendors/certs.py",
        ),
        declared_consumers=("nce/vertical_modules/resources/watcher.py",),
        status="ACTIVE",
        description="Emitted when a certification lapses, triggering allocation invalidation.",
    ),
    # ---------------------------------------------------------------------------
    # Legacy emitter with mismatched spelling (aligned with CERTIFICATION.EXPIRED in Wave V-2)
    # ---------------------------------------------------------------------------
    "cert.expiry": EventContract(
        selector="cert.expiry",
        node_type="cert",
        op="expiry",
        declared_producers=(),
        declared_consumers=(),
        status="DEPRECATED",
        reason=(
            "Legacy lowercase event aligned with CERTIFICATION.EXPIRED in "
            "v1.5 Phase 1 Wave HR-1 / V-2."
        ),
        description="Deprecated notification for vendor contractor certification expiry.",
    ),
    # ---------------------------------------------------------------------------
    # Active reactive event contracts (both produced and consumed in production)
    # ---------------------------------------------------------------------------
    "DESIGN.upserted": EventContract(
        selector="DESIGN.upserted",
        node_type="DESIGN",
        op="upserted",
        declared_producers=(
            "nce/vertical_modules/system_design/graph.py",
            "nce/vertical_modules/system_design/from_quote.py",
        ),
        declared_consumers=("nce/vertical_modules/system_design/subscribers.py",),
        status="ACTIVE",
        description="Emitted when an audiovisual system design graph root is upserted.",
    ),
    "DESIGN_LINE.upserted": EventContract(
        selector="DESIGN_LINE.upserted",
        node_type="DESIGN_LINE",
        op="upserted",
        declared_producers=("nce/vertical_modules/system_design/graph.py",),
        declared_consumers=("nce/vertical_modules/system_design/subscribers.py",),
        status="ACTIVE",
        description="Emitted when a bill-of-materials line within a system design is upserted.",
    ),
    "FUNCTIONAL_LOCATION.upserted": EventContract(
        selector="FUNCTIONAL_LOCATION.upserted",
        node_type="FUNCTIONAL_LOCATION",
        op="upserted",
        declared_producers=("nce/vertical_modules/system_design/graph.py",),
        declared_consumers=("nce/vertical_modules/system_design/subscribers.py",),
        status="ACTIVE",
        description="Emitted when an architectural or topological room location is upserted.",
    ),
    "DEVICE.upserted": EventContract(
        selector="DEVICE.upserted",
        node_type="DEVICE",
        op="upserted",
        declared_producers=("nce/vertical_modules/system_design/devices.py",),
        declared_consumers=("nce/vertical_modules/system_design/subscribers.py",),
        status="ACTIVE",
        description="Emitted when a hardware device instance is positioned in a design.",
    ),
    "PORT.upserted": EventContract(
        selector="PORT.upserted",
        node_type="PORT",
        op="upserted",
        declared_producers=("nce/vertical_modules/system_design/devices.py",),
        declared_consumers=("nce/vertical_modules/system_design/subscribers.py",),
        status="ACTIVE",
        description="Emitted when a signal or network port on a device is upserted.",
    ),
    "RACK.upserted": EventContract(
        selector="RACK.upserted",
        node_type="RACK",
        op="upserted",
        declared_producers=("nce/vertical_modules/system_design/devices.py",),
        declared_consumers=("nce/vertical_modules/system_design/subscribers.py",),
        status="ACTIVE",
        description="Emitted when an equipment rack enclosure is upserted in a room design.",
    ),
    "CABLE.upserted": EventContract(
        selector="CABLE.upserted",
        node_type="CABLE",
        op="upserted",
        declared_producers=("nce/vertical_modules/system_design/devices.py",),
        declared_consumers=("nce/vertical_modules/system_design/subscribers.py",),
        status="ACTIVE",
        description="Emitted when a cable interconnect between device ports is upserted.",
    ),
    # ---------------------------------------------------------------------------
    # Active graph writes (produced via emit_graph_write; observation & audit)
    # ---------------------------------------------------------------------------
    "BOM_LINE.upserted": EventContract(
        selector="BOM_LINE.upserted",
        node_type="BOM_LINE",
        op="upserted",
        declared_producers=("nce/bom_lines.py",),
        declared_consumers=(),
        status="UNCONSUMED",
        description="Emitted when a unified bill-of-materials line row is persisted.",
    ),
    "AGREEMENT.upserted": EventContract(
        selector="AGREEMENT.upserted",
        node_type="AGREEMENT",
        op="upserted",
        declared_producers=("nce/vertical_modules/agreements/graph.py",),
        declared_consumers=(),
        status="UNCONSUMED",
        description="Emitted when a master customer or service agreement is written.",
    ),
    "AGREEMENT_TERM.upserted": EventContract(
        selector="AGREEMENT_TERM.upserted",
        node_type="AGREEMENT_TERM",
        op="upserted",
        declared_producers=("nce/vertical_modules/agreements/graph.py",),
        declared_consumers=(),
        status="UNCONSUMED",
        description="Emitted when terms or conditions on an agreement are updated.",
    ),
    "AGREEMENT_SIGNATURE.upserted": EventContract(
        selector="AGREEMENT_SIGNATURE.upserted",
        node_type="AGREEMENT_SIGNATURE",
        op="upserted",
        declared_producers=("nce/vertical_modules/agreements/signing.py",),
        declared_consumers=(),
        status="UNCONSUMED",
        description="Emitted when a legal signature is recorded on an agreement.",
    ),
    "ASSET.upserted": EventContract(
        selector="ASSET.upserted",
        node_type="ASSET",
        op="upserted",
        declared_producers=("nce/vertical_modules/assets/graph.py",),
        declared_consumers=(),
        status="UNCONSUMED",
        description="Emitted when a physical customer asset is registered or updated.",
    ),
    "DYNAMICS_ENTITY.upserted": EventContract(
        selector="DYNAMICS_ENTITY.upserted",
        node_type="DYNAMICS_ENTITY",
        op="upserted",
        declared_producers=("nce/vertical_modules/dynamics365/sync.py",),
        declared_consumers=(),
        status="UNCONSUMED",
        description="Emitted when a Dynamics 365 entity synchronization completes.",
    ),
    "ACCOUNT.upserted": EventContract(
        selector="ACCOUNT.upserted",
        node_type="ACCOUNT",
        op="upserted",
        declared_producers=("nce/vertical_modules/economy/graph.py",),
        declared_consumers=(),
        status="UNCONSUMED",
        description="Emitted when a chart-of-accounts general ledger node is upserted.",
    ),
    "GOODS_RECEIPT.upserted": EventContract(
        selector="GOODS_RECEIPT.upserted",
        node_type="GOODS_RECEIPT",
        op="upserted",
        declared_producers=("nce/vertical_modules/inventory/goods_receipt.py",),
        declared_consumers=(),
        status="UNCONSUMED",
        description="Emitted when a goods receipt record is written into kg_nodes.",
    ),
    "INVENTORY_RMA.upserted": EventContract(
        selector="INVENTORY_RMA.upserted",
        node_type="INVENTORY_RMA",
        op="upserted",
        declared_producers=("nce/vertical_modules/inventory/rma.py",),
        declared_consumers=(),
        status="UNCONSUMED",
        description="Emitted when a return merchandise authorization (RMA) is registered.",
    ),
    "INVENTORY_ITEM.upserted": EventContract(
        selector="INVENTORY_ITEM.upserted",
        node_type="INVENTORY_ITEM",
        op="upserted",
        declared_producers=("nce/vertical_modules/inventory/stock.py",),
        declared_consumers=(),
        status="UNCONSUMED",
        description="Emitted when a warehouse inventory stock item balance changes.",
    ),
    "PO.upserted": EventContract(
        selector="PO.upserted",
        node_type="PO",
        op="upserted",
        declared_producers=("nce/vertical_modules/procurement/graph.py",),
        declared_consumers=(),
        status="UNCONSUMED",
        description="Emitted when a vendor purchase order header node is persisted.",
    ),
    "PROCUREMENT_MATCH.upserted": EventContract(
        selector="PROCUREMENT_MATCH.upserted",
        node_type="PROCUREMENT_MATCH",
        op="upserted",
        declared_producers=("nce/vertical_modules/procurement/graph.py",),
        declared_consumers=(),
        status="UNCONSUMED",
        description="Emitted when a purchase order matching node is written in the graph.",
    ),
    "PURCHASE_ORDER.upserted": EventContract(
        selector="PURCHASE_ORDER.upserted",
        node_type="PURCHASE_ORDER",
        op="upserted",
        declared_producers=("nce/vertical_modules/procurement/graph.py",),
        declared_consumers=(),
        status="UNCONSUMED",
        description="Emitted when a vendor purchase order header is persisted in the graph.",
    ),
    "PO_LINE.upserted": EventContract(
        selector="PO_LINE.upserted",
        node_type="PO_LINE",
        op="upserted",
        declared_producers=("nce/vertical_modules/procurement/graph.py",),
        declared_consumers=(),
        status="UNCONSUMED",
        description="Emitted when a purchase order line item is created or updated.",
    ),
    "PRODUCT_SKU.upserted": EventContract(
        selector="PRODUCT_SKU.upserted",
        node_type="PRODUCT_SKU",
        op="upserted",
        declared_producers=("nce/vertical_modules/product/graph.py",),
        declared_consumers=(),
        status="UNCONSUMED",
        description="Emitted when a canonical product SKU node is written.",
    ),
    "PRODUCT.upserted": EventContract(
        selector="PRODUCT.upserted",
        node_type="PRODUCT",
        op="upserted",
        declared_producers=("nce/vertical_modules/product/graph.py",),
        declared_consumers=(),
        status="UNCONSUMED",
        description="Emitted when a product catalog entry or equipment spec is ingested.",
    ),
    "PROJECT_PROJECT.upserted": EventContract(
        selector="PROJECT_PROJECT.upserted",
        node_type="PROJECT_PROJECT",
        op="upserted",
        declared_producers=("nce/vertical_modules/project/convert.py",),
        declared_consumers=(),
        status="UNCONSUMED",
        description="Emitted when a project node is created from a signed quote.",
    ),
    "PROJECT_GATE.upserted": EventContract(
        selector="PROJECT_GATE.upserted",
        node_type="PROJECT_GATE",
        op="upserted",
        declared_producers=(
            "nce/vertical_modules/project/advance.py",
            "nce/vertical_modules/project/convert.py",
        ),
        declared_consumers=(),
        status="UNCONSUMED",
        description="Emitted when a project stage gate is written or advanced.",
    ),
    "PROJECT_TASK.upserted": EventContract(
        selector="PROJECT_TASK.upserted",
        node_type="PROJECT_TASK",
        op="upserted",
        declared_producers=("nce/vertical_modules/project/convert.py",),
        declared_consumers=(),
        status="UNCONSUMED",
        description="Emitted when a project delivery or installation task is written.",
    ),
    "PROJECT_CASE_STUDY.upserted": EventContract(
        selector="PROJECT_CASE_STUDY.upserted",
        node_type="PROJECT_CASE_STUDY",
        op="upserted",
        declared_producers=("nce/vertical_modules/project/case_study.py",),
        declared_consumers=(),
        status="UNCONSUMED",
        description="Emitted when a completed project case study node is derived.",
    ),
    "PROJECT.upserted": EventContract(
        selector="PROJECT.upserted",
        node_type="PROJECT",
        op="upserted",
        declared_producers=("nce/vertical_modules/project/convert.py",),
        declared_consumers=(),
        status="UNCONSUMED",
        description="Emitted when a project is converted from a signed quote.",
    ),
    "GATE.upserted": EventContract(
        selector="GATE.upserted",
        node_type="GATE",
        op="upserted",
        declared_producers=(
            "nce/vertical_modules/project/advance.py",
            "nce/vertical_modules/project/convert.py",
        ),
        declared_consumers=(),
        status="UNCONSUMED",
        description="Emitted when a stage gate milestone is initialized or approved.",
    ),
    "TASK.upserted": EventContract(
        selector="TASK.upserted",
        node_type="TASK",
        op="upserted",
        declared_producers=("nce/vertical_modules/project/convert.py",),
        declared_consumers=(),
        status="UNCONSUMED",
        description="Emitted when a delivery or installation project task is created.",
    ),
    "CASE_STUDY.upserted": EventContract(
        selector="CASE_STUDY.upserted",
        node_type="CASE_STUDY",
        op="upserted",
        declared_producers=("nce/vertical_modules/project/case_study.py",),
        declared_consumers=(),
        status="UNCONSUMED",
        description="Emitted when a project case study node is derived at completion.",
    ),
    "QUOTE.upserted": EventContract(
        selector="QUOTE.upserted",
        node_type="QUOTE",
        op="upserted",
        declared_producers=("nce/vertical_modules/sales/graph.py",),
        declared_consumers=(),
        status="UNCONSUMED",
        description="Emitted when a sales proposal quote is written or updated.",
    ),
    "QUOTE.edge_realized_as": EventContract(
        selector="QUOTE.edge_realized_as",
        node_type="QUOTE",
        op="edge_realized_as",
        declared_producers=("nce/vertical_modules/system_design/from_quote.py",),
        declared_consumers=(),
        status="UNCONSUMED",
        description="Emitted when a quote establishes an edge to its realized engineering design.",
    ),
    "CERT.upserted": EventContract(
        selector="CERT.upserted",
        node_type="CERT",
        op="upserted",
        declared_producers=("nce/vertical_modules/vendors/certs.py",),
        declared_consumers=(),
        status="UNCONSUMED",
        description="Emitted when a vendor certification node is upserted in the graph.",
    ),
    "CONTRACTOR.upserted": EventContract(
        selector="CONTRACTOR.upserted",
        node_type="CONTRACTOR",
        op="upserted",
        declared_producers=("nce/vertical_modules/vendors/contractors.py",),
        declared_consumers=(),
        status="UNCONSUMED",
        description="Emitted when an external vendor contractor record is registered.",
    ),
    "VENDOR.upserted": EventContract(
        selector="VENDOR.upserted",
        node_type="VENDOR",
        op="upserted",
        declared_producers=("nce/vertical_modules/vendors/registry.py",),
        declared_consumers=(),
        status="UNCONSUMED",
        description="Emitted when a vendor supplier profile is created or updated.",
    ),
    "TICKET.sla_breached": EventContract(
        selector="TICKET.sla_breached",
        node_type="TICKET",
        op="sla_breached",
        declared_producers=("nce/vertical_modules/support/sla.py",),
        declared_consumers=(),
        status="UNCONSUMED",
        description="Emitted when a support service ticket breaches its first-response or resolution SLA deadline.",
    ),
    "ABSENCE.compliance_alert": EventContract(
        selector="ABSENCE.compliance_alert",
        node_type="ABSENCE",
        op="compliance_alert",
        declared_producers=("nce/vertical_modules/hr/compliance.py",),
        declared_consumers=("nce/vertical_modules/hr/compliance.py",),
        status="ACTIVE",
        description="Emitted when Norwegian statutory sick-leave compliance milestones are approaching or overdue.",
    ),
    # ---------------------------------------------------------------------------
    # System design component retirement and deletion lifecycles
    # ---------------------------------------------------------------------------
    "DEVICE.retired": EventContract(
        selector="DEVICE.retired",
        node_type="DEVICE",
        op="retired",
        declared_producers=("nce/vertical_modules/system_design/retire.py",),
        declared_consumers=(),
        status="UNCONSUMED",
        description="Emitted when a hardware device is decommissioned or retired.",
    ),
    "PORT.retired": EventContract(
        selector="PORT.retired",
        node_type="PORT",
        op="retired",
        declared_producers=("nce/vertical_modules/system_design/retire.py",),
        declared_consumers=(),
        status="UNCONSUMED",
        description="Emitted when a device port is retired.",
    ),
    "CABLE.retired": EventContract(
        selector="CABLE.retired",
        node_type="CABLE",
        op="retired",
        declared_producers=("nce/vertical_modules/system_design/retire.py",),
        declared_consumers=(),
        status="UNCONSUMED",
        description="Emitted when an interconnect cable is retired.",
    ),
    "RACK.retired": EventContract(
        selector="RACK.retired",
        node_type="RACK",
        op="retired",
        declared_producers=("nce/vertical_modules/system_design/retire.py",),
        declared_consumers=(),
        status="UNCONSUMED",
        description="Emitted when an equipment rack is retired from service.",
    ),
    "FUNCTIONAL_LOCATION.retired": EventContract(
        selector="FUNCTIONAL_LOCATION.retired",
        node_type="FUNCTIONAL_LOCATION",
        op="retired",
        declared_producers=("nce/vertical_modules/system_design/retire.py",),
        declared_consumers=(),
        status="UNCONSUMED",
        description="Emitted when a functional location / room is decommissioned.",
    ),
    "DESIGN.retired": EventContract(
        selector="DESIGN.retired",
        node_type="DESIGN",
        op="retired",
        declared_producers=("nce/vertical_modules/system_design/retire.py",),
        declared_consumers=(),
        status="UNCONSUMED",
        description="Emitted when a complete system design revision is superseded or retired.",
    ),
    "DESIGN_LINE.retired": EventContract(
        selector="DESIGN_LINE.retired",
        node_type="DESIGN_LINE",
        op="retired",
        declared_producers=("nce/vertical_modules/system_design/retire.py",),
        declared_consumers=(),
        status="UNCONSUMED",
        description="Emitted when a design BOM line is removed from a design.",
    ),
    "PORT.deleted": EventContract(
        selector="PORT.deleted",
        node_type="PORT",
        op="deleted",
        declared_producers=("nce/vertical_modules/system_design/retire.py",),
        declared_consumers=(),
        status="UNCONSUMED",
        description="Emitted when a physical port is permanently deleted.",
    ),
    "CABLE.deleted": EventContract(
        selector="CABLE.deleted",
        node_type="CABLE",
        op="deleted",
        declared_producers=("nce/vertical_modules/system_design/retire.py",),
        declared_consumers=(),
        status="UNCONSUMED",
        description="Emitted when a cable connection is disconnected and deleted.",
    ),
    "D365_Account.upserted": EventContract(
        selector="D365_Account.upserted",
        node_type="D365_Account",
        op="upserted",
        declared_producers=("nce/vertical_modules/dynamics365/sync.py",),
        declared_consumers=(),
        status="UNCONSUMED",
        description="Emitted when a Dynamics 365 customer account entity is synchronized.",
    ),
    "D365_KnowledgeArticle.upserted": EventContract(
        selector="D365_KnowledgeArticle.upserted",
        node_type="D365_KnowledgeArticle",
        op="upserted",
        declared_producers=("nce/vertical_modules/dynamics365/sync.py",),
        declared_consumers=(),
        status="UNCONSUMED",
        description="Emitted when a Dynamics 365 knowledge base article is synchronized.",
    ),
    "INVOICE.upserted": EventContract(
        selector="INVOICE.upserted",
        node_type="INVOICE",
        op="upserted",
        declared_producers=("nce/vertical_modules/economy/graph.py",),
        declared_consumers=(),
        status="UNCONSUMED",
        description="Emitted when a sales or vendor invoice record is persisted in the graph.",
    ),
    "POSTING.upserted": EventContract(
        selector="POSTING.upserted",
        node_type="POSTING",
        op="upserted",
        declared_producers=("nce/vertical_modules/economy/graph.py",),
        declared_consumers=(),
        status="UNCONSUMED",
        description="Emitted when a general ledger financial journal posting is written.",
    ),
    "PERIOD.upserted": EventContract(
        selector="PERIOD.upserted",
        node_type="PERIOD",
        op="upserted",
        declared_producers=("nce/vertical_modules/economy/graph.py",),
        declared_consumers=(),
        status="UNCONSUMED",
        description="Emitted when an accounting fiscal period boundary node is upserted.",
    ),
    "MARGIN.upserted": EventContract(
        selector="MARGIN.upserted",
        node_type="MARGIN",
        op="upserted",
        declared_producers=("nce/vertical_modules/economy/graph.py",),
        declared_consumers=(),
        status="UNCONSUMED",
        description="Emitted when project or deal gross margin economics are calculated.",
    ),
    "STOCK_LOCATION.upserted": EventContract(
        selector="STOCK_LOCATION.upserted",
        node_type="STOCK_LOCATION",
        op="upserted",
        declared_producers=("nce/vertical_modules/inventory/stock.py",),
        declared_consumers=(),
        status="UNCONSUMED",
        description="Emitted when a warehouse stock storage location node is upserted.",
    ),
    "CUSTOMER.upserted": EventContract(
        selector="CUSTOMER.upserted",
        node_type="CUSTOMER",
        op="upserted",
        declared_producers=("nce/vertical_modules/sales/graph.py",),
        declared_consumers=(),
        status="UNCONSUMED",
        description="Emitted when a customer client account node is created or updated.",
    ),
    "DEAL.upserted": EventContract(
        selector="DEAL.upserted",
        node_type="DEAL",
        op="upserted",
        declared_producers=("nce/vertical_modules/sales/graph.py",),
        declared_consumers=(),
        status="UNCONSUMED",
        description="Emitted when a commercial sales opportunity deal pipeline node is written.",
    ),
    "LEAD.upserted": EventContract(
        selector="LEAD.upserted",
        node_type="LEAD",
        op="upserted",
        declared_producers=("nce/vertical_modules/sales/graph.py",),
        declared_consumers=(),
        status="UNCONSUMED",
        description="Emitted when an inbound sales prospect lead node is upserted in the graph.",
    ),
    "OPPORTUNITY.upserted": EventContract(
        selector="OPPORTUNITY.upserted",
        node_type="OPPORTUNITY",
        op="upserted",
        declared_producers=("nce/vertical_modules/sales/graph.py",),
        declared_consumers=(),
        status="UNCONSUMED",
        description="Emitted when a qualified commercial sales opportunity is registered.",
    ),
    "DEVICE.deleted": EventContract(
        selector="DEVICE.deleted",
        node_type="DEVICE",
        op="deleted",
        declared_producers=("nce/vertical_modules/system_design/retire.py",),
        declared_consumers=(),
        status="UNCONSUMED",
        description="Emitted when a planned device hardware node is permanently deleted.",
    ),
    "RACK.deleted": EventContract(
        selector="RACK.deleted",
        node_type="RACK",
        op="deleted",
        declared_producers=("nce/vertical_modules/system_design/retire.py",),
        declared_consumers=(),
        status="UNCONSUMED",
        description="Emitted when an equipment rack enclosure is permanently deleted.",
    ),
}


def get_contract(selector: str) -> EventContract | None:
    """Retrieve an EventContract for a given selector, or None if uncatalogued."""
    return EVENT_CATALOGUE.get(selector)


def get_unproduced_selectors() -> dict[str, EventContract]:
    """Return all catalogued selectors currently in UNPRODUCED or PARKED state."""
    return {s: c for s, c in EVENT_CATALOGUE.items() if c.status in ("UNPRODUCED", "PARKED")}


def get_active_selectors() -> dict[str, EventContract]:
    """Return all catalogued selectors currently in ACTIVE state."""
    return {s: c for s, c in EVENT_CATALOGUE.items() if c.status == "ACTIVE"}
