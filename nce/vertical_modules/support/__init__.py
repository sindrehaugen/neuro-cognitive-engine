"""
nce/vertical_modules/support/__init__.py
=======================================
Module 10: Support Engine (Vertical Module).

Provides native ServiceTicket lifecycle, SLA clocks, customer health scoring,
and cognitive AI troubleshooting unblocking Copper waves B191–B193.
"""

from nce.vertical_modules.support._guard import (
    SupportDisabledError,
    require_support_enabled,
)
from nce.vertical_modules.support.health import (
    compute_health_score,
    do_health_score,
    do_record_touchpoint,
    load_health_weights,
)
from nce.vertical_modules.support.mcp_handlers import (
    handle_support_health_score,
    handle_support_open_ticket,
    handle_support_query_ticket,
    handle_support_resolve_ticket,
    handle_support_sla_clock,
    handle_support_troubleshoot,
)
from nce.vertical_modules.support.sla import (
    calculate_sla_targets,
    do_sla_clock,
    evaluate_sla_status,
    load_sla_profiles,
)
from nce.vertical_modules.support.tickets import (
    InvalidTicketStatusError,
    TicketAlreadyResolvedError,
    TicketNotFoundError,
    do_open_ticket,
    do_query_ticket,
    do_resolve_ticket,
)
from nce.vertical_modules.support.triage import do_triage_ticket
from nce.vertical_modules.support.troubleshoot import do_troubleshoot

__all__ = [
    "InvalidTicketStatusError",
    "SupportDisabledError",
    "TicketAlreadyResolvedError",
    "TicketNotFoundError",
    "calculate_sla_targets",
    "compute_health_score",
    "do_health_score",
    "do_open_ticket",
    "do_query_ticket",
    "do_record_touchpoint",
    "do_resolve_ticket",
    "do_sla_clock",
    "do_triage_ticket",
    "do_troubleshoot",
    "evaluate_sla_status",
    "handle_support_health_score",
    "handle_support_open_ticket",
    "handle_support_query_ticket",
    "handle_support_resolve_ticket",
    "handle_support_sla_clock",
    "handle_support_troubleshoot",
    "load_health_weights",
    "load_sla_profiles",
    "require_support_enabled",
]
