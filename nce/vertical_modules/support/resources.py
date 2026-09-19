"""nce.vertical_modules.support.resources — Resource definitions for Support Engine.

Lane E Wave E-8 & Phase D Wave D-5:
Registers C12 ResourceSpecs for Support's owned node types:
  - TICKET (service_tickets table)
  - SLA (sla_clocks table; PK is ticket_id, no id column)
  - SUPPORT_HEALTH_SCORE (customer_health table; PK is (namespace_id, customer_id), no id column)
  - TICKET_ACTION (support_ticket_actions table, ADR 0042 append-only log)

SUPPORT_DIAGNOSIS stays exempted: it is not a separate table at all -- the
AI diagnosis payload lives in service_tickets.ai_diagnosis (JSONB), so there
is no independent row to declare a spec against.
"""

from __future__ import annotations

from nce.resource_surface import register_resource
from nce.resource_surface.spec import ResourceSpec
from nce.vertical_modules.support._guard import require_support_enabled

# 1. TICKET
TICKET_SPEC = ResourceSpec(
    engine="support",
    entity="tickets",
    node_type="TICKET",
    table_name="service_tickets",
    id_field="id",
    version_field="updated_at",
    soft_delete_field=None,
    filterable_fields=(
        "source",
        "status",
        "priority",
        "asset_id",
        "room_id",
        "customer_id",
        "sla_profile",
    ),
    searchable_fields=("summary", "description", "customer_id", "room_id", "source_id"),
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
        "change_origin",
    ),
    tier_allowlists={
        "external-customer": (
            "id",
            "source",
            "status",
            "priority",
            "summary",
            "description",
            "asset_id",
            "room_id",
            "created_at",
            "updated_at",
            "first_response_at",
            "resolved_at",
        ),
        "contractor": (
            "id",
            "source",
            "status",
            "priority",
            "summary",
            "description",
            "asset_id",
            "room_id",
            "customer_id",
            "sla_profile",
            "created_at",
            "updated_at",
            "first_response_at",
            "resolved_at",
        ),
    },
    description="Service tickets: status/priority lifecycle, SLA profile, and AI diagnosis payload.",
    enabled_guard=require_support_enabled,
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
    enabled_guard=require_support_enabled,
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
    enabled_guard=require_support_enabled,
)
register_resource(SUPPORT_HEALTH_SCORE_SPEC)


# 4. TICKET_ACTION
TICKET_ACTION_SPEC = ResourceSpec(
    engine="support",
    entity="ticket-actions",
    node_type="TICKET_ACTION",
    table_name="support_ticket_actions",
    id_field="id",
    version_field="updated_at",
    soft_delete_field=None,
    filterable_fields=("ticket_id", "action_type", "outcome", "performed_by"),
    searchable_fields=("action_summary", "outcome_notes", "action_details"),
    writable_fields=(
        "ticket_id",
        "action_type",
        "action_summary",
        "action_details",
        "outcome",
        "outcome_notes",
        "performed_by",
        "performed_at",
        "change_origin",
    ),
    tier_allowlists={
        "external-customer": (
            "id",
            "ticket_id",
            "action_type",
            "action_summary",
            "outcome",
            "outcome_notes",
            "performed_at",
        ),
        "contractor": (
            "id",
            "ticket_id",
            "action_type",
            "action_summary",
            "action_details",
            "outcome",
            "outcome_notes",
            "performed_by",
            "performed_at",
        ),
    },
    description="Append-only ticket action log tracking interventions (tiltak) and outcomes (utfall) per ticket per ADR 0042.",
    enabled_guard=require_support_enabled,
)
register_resource(TICKET_ACTION_SPEC)

SUPPORT_SPECS = [TICKET_SPEC, SLA_SPEC, SUPPORT_HEALTH_SCORE_SPEC, TICKET_ACTION_SPEC]
