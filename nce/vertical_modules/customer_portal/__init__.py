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

from nce.vertical_modules.customer_portal.actions import (
    do_raise_service_request,
    do_register_expansion_interest,
)
from nce.vertical_modules.customer_portal.advisor import do_advisor_answer
from nce.vertical_modules.customer_portal.documents import (
    do_get_document,
    do_list_documents,
)
from nce.vertical_modules.customer_portal.invoices import do_list_invoices
from nce.vertical_modules.customer_portal.rooms import (
    do_asset_register,
    do_room_overview,
    do_room_tracker,
)
from nce.vertical_modules.customer_portal.sla import do_sla_status

__all__ = [
    "do_advisor_answer",
    "do_asset_register",
    "do_get_document",
    "do_list_documents",
    "do_list_invoices",
    "do_raise_service_request",
    "do_register_expansion_interest",
    "do_room_overview",
    "do_room_tracker",
    "do_sla_status",
]
