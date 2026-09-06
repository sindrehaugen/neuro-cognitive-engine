"""
Vendors & Contractors Engine package initialization.
"""

from __future__ import annotations

from nce.vertical_modules.vendors.certs import do_check_cert_expiry, do_upsert_cert
from nce.vertical_modules.vendors.contractors import do_get_contractor, do_upsert_contractor
from nce.vertical_modules.vendors.feed import (
    do_check_tier_at_risk,
    do_detect_reliability_degradation,
)
from nce.vertical_modules.vendors.frontier import do_calibrate_weights, do_reliability_radar
from nce.vertical_modules.vendors.matching import do_match_contractor
from nce.vertical_modules.vendors.partner_view import do_partner_view
from nce.vertical_modules.vendors.performance import do_compute_performance, do_recall_similar_jobs
from nce.vertical_modules.vendors.registry import do_get_vendor, do_upsert_vendor
from nce.vertical_modules.vendors.scorecard import do_compute_scorecard
from nce.vertical_modules.vendors.tiers import do_get_tier_status, do_record_outcome

__all__ = [
    "do_upsert_vendor",
    "do_get_vendor",
    "do_compute_scorecard",
    "do_partner_view",
    "do_upsert_cert",
    "do_check_cert_expiry",
    "do_match_contractor",
    "do_compute_performance",
    "do_recall_similar_jobs",
    "do_reliability_radar",
    "do_calibrate_weights",
    "do_get_contractor",
    "do_upsert_contractor",
    "do_check_tier_at_risk",
    "do_detect_reliability_degradation",
    "do_get_tier_status",
    "do_record_outcome",
]
