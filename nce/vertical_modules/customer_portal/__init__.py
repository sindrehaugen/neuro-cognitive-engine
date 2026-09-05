"""
nce/vertical_modules/customer_portal
====================================
Module 17: Customer Portal Engine (The External-Facing Surface).
Read-projection engine over the cognitive graph (FUNCTIONAL_LOCATION, BOM_LINE.status, ASSET,
SLA, INVOICE) and thin inbound customer actions (SERVICE_REQUEST -> TICKET hand-off).

Security Architecture (Charter §2):
  L1: Customer-scope RLS (nce.external_scope_id GUC, deny-when-unset sentinel).
  L2: Explicit field allow-list projection (customer-redaction.json).
  L3: Dedicated rate-limited customer app shell, no internal tool surface.
  L4: Sandboxed prompt-injection-resistant customer Advisor.
"""

__version__ = "0.1.0"

from nce.vertical_modules.customer_portal.rooms import (
    do_asset_register,
    do_room_overview,
    do_room_tracker,
)

__all__ = [
    "do_asset_register",
    "do_room_overview",
    "do_room_tracker",
]
