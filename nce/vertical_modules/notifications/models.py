"""nce.vertical_modules.notifications.models — Domain models for C13 notifications and reminders.

Phase A Wave A-3:
Defines dataclasses for notification records, reminders, and subscriptions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True)
class NotificationRecord:
    """A persistent notification in a principal's inbox."""

    id: UUID
    namespace_id: UUID
    principal_id: str
    title: str
    body: str
    severity: str
    category: str
    source_selector: str | None
    source_id: str | None
    idempotency_key: str | None
    read_at: datetime | None
    seen_at: datetime | None
    is_archived: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class ReminderRecord:
    """A user-set reminder deadline on any node in the knowledge graph."""

    id: UUID
    namespace_id: UUID
    principal_id: str
    node_type: str
    node_id: str
    title: str
    note: str
    remind_at: datetime
    status: str
    fired_at: datetime | None
    is_archived: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class NotificationSubscription:
    """A subscription mapping a principal to an event selector."""

    id: UUID
    namespace_id: UUID
    principal_id: str
    selector: str
    created_at: datetime
