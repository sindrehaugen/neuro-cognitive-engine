"""nce.vertical_modules.support.resources — Resource definitions for Support.

Lane E Wave E-8:
Registers C12 ResourceSpecs for Support's owned node types in
node-ownership.json that have a real, tenant-scoped, single-table DDL:
  - TICKET (service_tickets table -- NOT "support_tickets"; the prior
    exemption text named the wrong table, corrected here)
  - SLA (sla_clocks table; PK is ticket_id, no id column)
  - SUPPORT_HEALTH_SCORE (customer_health table; PK is
    (namespace_id, customer_id), no id column)

SUPPORT_DIAGNOSIS stays exempted: it is not a separate table at all -- the
AI diagnosis payload lives in service_tickets.ai_diagnosis (JSONB), so there
is no independent row to declare a spec against.
"""

from __future__ import annotations

from nce.resource_surface import register_resource
from nce.resource_surface.spec import ResourceSpec

# 1. TICKET
TICKET_SPEC = ResourceSpec(
    engine="support",
    entity="tickets",
    node_type="TICKET",
    table_name="service_tickets",
    id_field="id",
    version_field="updated_at",
    soft_delete_field=None,
    filterable_fields=("status", "priority", "customer_id", "room_id", "asset_id", "source"),
    searchable_fields=("summary", "description", "customer_id"),
    writable_fields=(
        "source",
        "source_id",
        "asset_id",
        "room_id",
        "customer_id",
        "status",
        "priority",
        "summary",
        "description",
        "sla_profile",
    ),
    description="Service tickets: status/priority lifecycle, SLA profile, and AI diagnosis payload.",
)
register_resource(TICKET_SPEC)


# 2. SLA
SLA_SPEC = ResourceSpec(
    engine="support",
    entity="sla-clocks",
    node_type="SLA",
    table_name="sla_clocks",
    id_field="ticket_id",
    version_field="updated_at",
    soft_delete_field=None,
    filterable_fields=("breached", "breach_type"),
    searchable_fields=(),
    writable_fields=(
        "sla_profile",
        "first_response_due",
        "resolution_due",
        "breached",
        "breach_type",
        "paused_intervals",
    ),
    description="Per-ticket SLA countdown and breach state, keyed 1:1 on ticket_id.",
)
register_resource(SLA_SPEC)


# 3. SUPPORT_HEALTH_SCORE
SUPPORT_HEALTH_SCORE_SPEC = ResourceSpec(
    engine="support",
    entity="customer-health",
    node_type="SUPPORT_HEALTH_SCORE",
    table_name="customer_health",
    id_field="customer_id",
    version_field="computed_at",
    soft_delete_field=None,
    filterable_fields=("churn_risk",),
    searchable_fields=("customer_id",),
    writable_fields=("score", "trend", "churn_risk", "drivers", "last_touchpoint_at"),
    description="Rolling per-customer health score, churn risk, and contributing drivers.",
)
register_resource(SUPPORT_HEALTH_SCORE_SPEC)
