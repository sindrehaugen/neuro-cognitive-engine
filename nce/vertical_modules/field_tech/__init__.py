"""
nce/vertical_modules/field_tech
================================
Module 12: Field Tech Engine.

Provides the mobile, offline-sync, and dispatch surface for physical field work:
  - work orders (install & service)
  - ISO9001 quality checklists
  - serial-number scans (seed edge for Assets register)
  - GPS & manual labor time logging
  - photo documentation
  - offline reconciliation with server-sequence ordering and conflict surfacing
  - partner-scoped and field-redacted views for external contractors
  - outcome recording to v3_cognitive_ledger
  - AI dispatch ranking
"""

from __future__ import annotations

from nce.vertical_modules.field_tech.checklist import do_complete_checklist
from nce.vertical_modules.field_tech.dispatch import do_dispatch
from nce.vertical_modules.field_tech.outcome import do_record_outcome
from nce.vertical_modules.field_tech.partner_view import do_partner_view
from nce.vertical_modules.field_tech.photo import do_attach_photo
from nce.vertical_modules.field_tech.scan import do_scan_serial
from nce.vertical_modules.field_tech.sync import do_sync
from nce.vertical_modules.field_tech.time_entry import do_log_time
from nce.vertical_modules.field_tech.work_orders import (
    do_assign,
    do_create_work_order,
    do_get_work_order,
    do_query_work_order,
)

__all__ = [
    "do_create_work_order",
    "do_get_work_order",
    "do_query_work_order",
    "do_assign",
    "do_complete_checklist",
    "do_scan_serial",
    "do_log_time",
    "do_attach_photo",
    "do_sync",
    "do_partner_view",
    "do_record_outcome",
    "do_dispatch",
]
