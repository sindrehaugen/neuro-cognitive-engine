"""nce.vertical_modules.notifications.resources — Resource definitions for Notifications and Reminders.

Phase A Wave A-3:
Registers C12 ResourceSpecs for:
  - NOTIFICATION (notifications table)
  - REMINDER (reminders table)
"""

from __future__ import annotations

from nce.resource_surface import register_resource
from nce.resource_surface.spec import ResourceSpec

# 1. NOTIFICATION
NOTIFICATION_SPEC = ResourceSpec(
    engine="notifications",
    entity="notifications",
    node_type="NOTIFICATION",
    table_name="notifications",
    id_field="id",
    version_field="updated_at",
    soft_delete_field="is_archived",
    filterable_fields=("principal_id", "severity", "category", "source_selector", "source_id"),
    searchable_fields=("title", "body"),
    writable_fields=(
        "principal_id",
        "title",
        "body",
        "severity",
        "category",
        "source_selector",
        "source_id",
        "read_at",
        "seen_at",
        "is_archived",
    ),
    description="Persistent principal notifications and inbox alerts.",
)
register_resource(NOTIFICATION_SPEC)


# 2. REMINDER
REMINDER_SPEC = ResourceSpec(
    engine="notifications",
    entity="reminders",
    node_type="REMINDER",
    table_name="reminders",
    id_field="id",
    version_field="updated_at",
    soft_delete_field="is_archived",
    filterable_fields=("principal_id", "node_type", "node_id", "status"),
    searchable_fields=("title", "note"),
    writable_fields=(
        "principal_id",
        "node_type",
        "node_id",
        "title",
        "note",
        "remind_at",
        "status",
        "fired_at",
        "is_archived",
    ),
    description="User-set reminders attached to knowledge graph nodes.",
)
register_resource(REMINDER_SPEC)


NOTIFICATIONS_SPECS = [
    NOTIFICATION_SPEC,
    REMINDER_SPEC,
]
