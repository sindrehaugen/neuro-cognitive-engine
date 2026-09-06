"""
nce/vertical_modules/sales/__init__.py
======================================
Sales Engine vertical module (Module 5).

Exports public core functions for baseline freezing, signing, deal management,
lead scoring, read-model queries, and commission tracking.
"""

from __future__ import annotations

from nce.vertical_modules.sales.ai import (
    do_draft_quote,
    do_record_ai_decision,
    do_score_lead,
    do_win_loss_recall,
)
from nce.vertical_modules.sales.baseline import do_freeze_baseline
from nce.vertical_modules.sales.commission import (
    do_calculate_commission,
    do_initiate_quote_flow,
    do_record_deal_loss_feedback,
)
from nce.vertical_modules.sales.dealroom import do_open_dealroom
from nce.vertical_modules.sales.flip import (
    do_flip_function,
    do_morning_brief_slice,
    do_stalled_deal_watcher,
)
from nce.vertical_modules.sales.graph import do_create_deal, do_edit_deal
from nce.vertical_modules.sales.lines import do_add_quote_line, do_get_quote_lines
from nce.vertical_modules.sales.read_model import (
    do_agreement_detail,
    do_customer_profile,
    do_get_targets,
    do_list_agreements,
    do_list_customers,
    do_quote_detail,
    do_sales_dashboard,
    do_sales_manager,
    do_sales_overview,
    do_sales_stats,
    do_seller_detail,
    do_set_target,
)
from nce.vertical_modules.sales.signing import (
    do_on_declined_callback,
    do_on_signed_callback,
    do_request_signature,
)

__all__ = [
    "do_add_quote_line",
    "do_agreement_detail",
    "do_calculate_commission",
    "do_create_deal",
    "do_customer_profile",
    "do_draft_quote",
    "do_edit_deal",
    "do_flip_function",
    "do_freeze_baseline",
    "do_get_quote_lines",
    "do_get_targets",
    "do_initiate_quote_flow",
    "do_list_agreements",
    "do_list_customers",
    "do_morning_brief_slice",
    "do_on_declined_callback",
    "do_on_signed_callback",
    "do_open_dealroom",
    "do_quote_detail",
    "do_record_ai_decision",
    "do_record_deal_loss_feedback",
    "do_request_signature",
    "do_sales_dashboard",
    "do_sales_manager",
    "do_sales_overview",
    "do_sales_stats",
    "do_score_lead",
    "do_seller_detail",
    "do_set_target",
    "do_stalled_deal_watcher",
    "do_win_loss_recall",
]
