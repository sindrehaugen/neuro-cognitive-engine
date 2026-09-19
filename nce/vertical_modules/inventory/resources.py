"""nce.vertical_modules.inventory.resources — Resource definitions for Inventory.

Phase A Wave A-1:
Registers C12 ResourceSpecs for Inventory's owned node types in node-ownership.json:
  - STOCK_LOCATION (stock_locations table)
  - INVENTORY_ITEM (inventory_items table)
  - GOODS_RECEIPT (goods_receipts table)
  - INVENTORY_RMA (inventory_rma table)
"""

from __future__ import annotations

from nce.resource_surface import register_resource
from nce.resource_surface.spec import ResourceSpec
from nce.vertical_modules.inventory._guard import require_inventory_enabled

# 1. STOCK_LOCATION
STOCK_LOCATION_SPEC = ResourceSpec(
    engine="inventory",
    entity="stock-locations",
    node_type="STOCK_LOCATION",
    table_name="stock_locations",
    id_field="id",
    version_field="updated_at",
    soft_delete_field=None,
    filterable_fields=("kind", "parent_id", "level"),
    searchable_fields=("name", "vehicle_ref"),
    writable_fields=("kind", "name", "parent_id", "level", "vehicle_ref", "raw"),
    description="Stock locations hierarchy (warehouses, vans, zones, bins).",
    enabled_guard=require_inventory_enabled,
)
register_resource(STOCK_LOCATION_SPEC)


# 2. INVENTORY_ITEM
INVENTORY_ITEM_SPEC = ResourceSpec(
    engine="inventory",
    entity="inventory-items",
    node_type="INVENTORY_ITEM",
    table_name="inventory_items",
    id_field="id",
    version_field="updated_at",
    soft_delete_field=None,
    filterable_fields=("sku", "location_id"),
    searchable_fields=("sku",),
    writable_fields=(
        "sku",
        "location_id",
        "qty_on_hand",
        "qty_reserved",
        "qty_blocked",
        "reorder_point",
    ),
    tier_allowlists={
        "external-customer": (
            "id",
            "sku",
            "location_id",
            "qty_on_hand",
            "created_at",
            "updated_at",
        ),
        "contractor": (
            "id",
            "sku",
            "location_id",
            "qty_on_hand",
            "qty_reserved",
            "created_at",
            "updated_at",
        ),
    },
    description="Per-SKU inventory on hand, reserved, and reorder thresholds.",
    enabled_guard=require_inventory_enabled,
)
register_resource(INVENTORY_ITEM_SPEC)


# 3. GOODS_RECEIPT
GOODS_RECEIPT_SPEC = ResourceSpec(
    engine="inventory",
    entity="goods-receipts",
    node_type="GOODS_RECEIPT",
    table_name="goods_receipts",
    id_field="id",
    version_field="received_at",
    soft_delete_field=None,
    filterable_fields=("po_ref", "delivery_note_ref", "location_id"),
    searchable_fields=("po_ref", "delivery_note_ref"),
    writable_fields=(
        "po_ref",
        "delivery_note_ref",
        "location_id",
        "lines",
        "scans",
        "match_result",
        "receipt_hash",
    ),
    description="Physical inbound goods receipts with line matching and package verification.",
    enabled_guard=require_inventory_enabled,
)
register_resource(GOODS_RECEIPT_SPEC)


# 4. INVENTORY_RMA
INVENTORY_RMA_SPEC = ResourceSpec(
    engine="inventory",
    entity="inventory-rma",
    node_type="INVENTORY_RMA",
    table_name="inventory_rma",
    id_field="id",
    version_field=None,
    soft_delete_field=None,
    filterable_fields=("rma_ref", "sku", "location_id", "reason"),
    searchable_fields=("rma_ref", "sku", "serial", "reason"),
    writable_fields=("rma_ref", "sku", "serial", "location_id", "qty", "reason"),
    description="Return Merchandise Authorizations for defective or quarantined stock.",
    enabled_guard=require_inventory_enabled,
)
register_resource(INVENTORY_RMA_SPEC)


INVENTORY_SPECS = [
    STOCK_LOCATION_SPEC,
    INVENTORY_ITEM_SPEC,
    GOODS_RECEIPT_SPEC,
    INVENTORY_RMA_SPEC,
]
