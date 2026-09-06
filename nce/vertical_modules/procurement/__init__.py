"""
nce/vertical_modules/procurement/__init__.py
============================================
Procurement Engine vertical module (Module 1).

Exports public core functions for purchase orders, supplier ranking,
bids, rebate frontiers, TCO calculation, and 3-way matching.
"""

from __future__ import annotations

from nce.vertical_modules.procurement.bids import do_resolve_bids
from nce.vertical_modules.procurement.frontier import (
    do_forecast_rebate,
    do_recommend_move_spend,
    do_whatif_spend,
)
from nce.vertical_modules.procurement.po import do_generate_po, do_submit_po
from nce.vertical_modules.procurement.po_line import (
    ALLOWED_TRANSITIONS,
    NODE_TYPE_PO_LINE,
    POLineStatus,
    po_line_label,
    update_po_line_status,
    upsert_po_line_node,
    validate_status_transition,
)
from nce.vertical_modules.procurement.ranking import do_rank_suppliers
from nce.vertical_modules.procurement.recalibration import (
    do_recalibrate_supplier,
    do_record_match_decision,
)
from nce.vertical_modules.procurement.savings import do_aggregate_savings
from nce.vertical_modules.procurement.tco import do_calculate_tco
from nce.vertical_modules.procurement.three_way_match import (
    do_evaluate_three_way_match,
)
from nce.vertical_modules.procurement.transports import (
    ManualPoTransport,
    NetsetPoTransport,
    PoTransport,
)

__all__ = [
    "ALLOWED_TRANSITIONS",
    "ManualPoTransport",
    "NODE_TYPE_PO_LINE",
    "NetsetPoTransport",
    "POLineStatus",
    "PoTransport",
    "do_aggregate_savings",
    "do_calculate_tco",
    "do_evaluate_three_way_match",
    "do_forecast_rebate",
    "do_generate_po",
    "do_rank_suppliers",
    "do_recalibrate_supplier",
    "do_recommend_move_spend",
    "do_record_match_decision",
    "do_resolve_bids",
    "do_submit_po",
    "do_whatif_spend",
    "po_line_label",
    "update_po_line_status",
    "upsert_po_line_node",
    "validate_status_transition",
]
