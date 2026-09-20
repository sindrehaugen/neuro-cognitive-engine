"""nce.vertical_modules.support.resources — Resource definitions for Support Engine.

Lane E Wave E-8 & Phase D Wave D-5:
Registers C12 ResourceSpecs for Support's owned node types:
  - TICKET (service_tickets table)
  - SLA (sla_clocks table; PK is ticket_id, no id column)
  - SUPPORT_HEALTH_SCORE (customer_health table; PK is (namespace_id, customer_id), no id column)
  - TICKET_ACTION (support_ticket_actions table, ADR 0008 append-only log)

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
    # archive wave (2026-09-20): soft_delete_field=None falls back to a
    # literal "is_archived" column that service_tickets does not have.
    # status's own CHECK enum (open/in_progress/waiting_customer/
    # waiting_parts/resolved/closed/cancelled) already covers ticket
    # lifecycle termination ("closed"/"cancelled") and is already writable
    # via generic PATCH -- not a hidden soft-delete gap, and no
    # hand-written archive path either. See
    # _internal/work-docs/mlv16-orchestration/ARCHIVE_COLUMN_SWEEP.md.
    excluded_verbs=frozenset({"archive"}),
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
    # archive wave (2026-09-20): soft_delete_field=None falls back to a
    # literal "is_archived" column that sla_clocks does not have; no
    # alternate soft-delete column or hand-written archive path either. See
    # _internal/work-docs/mlv16-orchestration/ARCHIVE_COLUMN_SWEEP.md.
    excluded_verbs=frozenset({"archive"}),
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
    # archive wave (2026-09-20): soft_delete_field=None falls back to a
    # literal "is_archived" column that customer_health does not have; no
    # alternate soft-delete column or hand-written archive path either. See
    # _internal/work-docs/mlv16-orchestration/ARCHIVE_COLUMN_SWEEP.md.
    excluded_verbs=frozenset({"archive"}),
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
    # writable_fields cleared to () because "upsert" is excluded: fail-safe,
    # not cosmetic -- see ResourceSpec.excluded_verbs's own docstring. A
    # populated list has no reader today, but if a future change re-enables
    # upsert, a stale populated list would make every field immediately
    # writable with no review; an empty one forces a deliberate re-listing.
    writable_fields=(),
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
    # archive wave (2026-09-20): soft_delete_field=None falls back to a
    # literal "is_archived" column that support_ticket_actions does not
    # have; no alternate soft-delete column either. See
    # _internal/work-docs/mlv16-orchestration/ARCHIVE_COLUMN_SWEEP.md.
    #
    # upsert excluded per ADR-0008 (docs/adr/0008-append-only-ticket-action-log.md):
    # excluded_verbs reason (2), storage itself permanently forbids the
    # verb -- migration 108_support_ticket_actions_append_only.sql revokes
    # UPDATE/DELETE on this table, matching event_log's own grant shape.
    excluded_verbs=frozenset({"archive", "upsert"}),
    description="Append-only ticket action log tracking interventions (tiltak) and outcomes (utfall) per ticket per ADR-0008.",
    enabled_guard=require_support_enabled,
)
register_resource(TICKET_ACTION_SPEC)

SUPPORT_SPECS = [TICKET_SPEC, SLA_SPEC, SUPPORT_HEALTH_SCORE_SPEC, TICKET_ACTION_SPEC]
