"""
nce/vertical_modules/inventory/__init__.py
==========================================
Inventory Engine vertical module (Module 11).

Exports public core functions for stock levels, transfers, reservations,
goods receipt, replenishment, valuation, and RMA handling.
"""

from __future__ import annotations

from nce.vertical_modules.inventory._guard import (
    InventoryDisabledError,
    require_inventory_enabled,
)
from nce.vertical_modules.inventory.forecast import do_forecast_demand
from nce.vertical_modules.inventory.goods_receipt import do_record_goods_receipt
from nce.vertical_modules.inventory.reconcile import do_reconcile_dead_stock
from nce.vertical_modules.inventory.replenishment import do_recommend_restock
from nce.vertical_modules.inventory.reservation import (
    do_release_stock,
    do_reserve_stock,
)
from nce.vertical_modules.inventory.restock_po import do_create_restock_po
from nce.vertical_modules.inventory.rma import (
    do_dispose_rma_weee,
    do_record_rma,
    do_restock_from_rma,
)
from nce.vertical_modules.inventory.stock import (
    do_record_consumption,
    do_stock_levels,
    do_transfer_stock,
)
from nce.vertical_modules.inventory.transactions import do_valuation
from nce.vertical_modules.inventory.triggers import (
    do_advance_bom_line_to_delivered,
    do_record_goods_receipt_and_evaluate_match,
)
from nce.vertical_modules.inventory.watchers import do_flag_stock_alerts

__all__ = [
    "InventoryDisabledError",
    "do_advance_bom_line_to_delivered",
    "do_create_restock_po",
    "do_dispose_rma_weee",
    "do_flag_stock_alerts",
    "do_forecast_demand",
    "do_record_consumption",
    "do_record_goods_receipt",
    "do_record_goods_receipt_and_evaluate_match",
    "do_record_rma",
    "do_reconcile_dead_stock",
    "do_recommend_restock",
    "do_release_stock",
    "do_reserve_stock",
    "do_restock_from_rma",
    "do_stock_levels",
    "do_transfer_stock",
    "do_valuation",
    "require_inventory_enabled",
]
