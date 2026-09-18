"""nce.vertical_modules.notifications.subscribers — C4 Outbox Event Subscribers for C13.

Phase A Wave A-3:
Listens to candidate transactional outbox events across the estate:
  - TICKET.sla_breached
  - CERTIFICATION.EXPIRED
  - PO_LINE.status_changed
  - AGREEMENT.renewal_due
  - DEAL.stalled
  - ASSET.health_changed
  - REMINDER.fired

Translates events into persistent, multi-tenant notifications in target principals' inboxes.
Guarantees replay idempotency via deterministic idempotency_key generation.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]

from nce.events.bus import subscribe
from nce.vertical_modules.notifications.service import (
    create_notification,
    get_subscribed_principals,
)

log = logging.getLogger("nce.notifications.subscribers")


def _get_payload(event: dict[str, Any]) -> dict[str, Any]:
    """Extract dict payload from outbox event."""
    raw = event.get("payload")
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return {}
    if isinstance(raw, dict):
        return raw
    return {}


def _get_namespace_id(event: dict[str, Any], payload: dict[str, Any]) -> UUID | None:
    """Extract namespace_id UUID safely."""
    raw_ns = event.get("namespace_id") or payload.get("namespace_id")
    if not raw_ns:
        return None
    try:
        return UUID(str(raw_ns))
    except (ValueError, TypeError):
        return None


async def _deliver_to_principals(
    conn: asyncpg.Connection,
    *,
    namespace_id: UUID,
    selector: str,
    source_id: str,
    title: str,
    body: str,
    severity: str,
    category: str,
    explicit_principals: list[str] | None = None,
) -> None:
    """Deliver notification to subscribed principals and explicit targets with dedup."""
    subscribed = await get_subscribed_principals(conn, namespace_id=namespace_id, selector=selector)
    targets = set(subscribed)
    if explicit_principals:
        for p in explicit_principals:
            if p:
                targets.add(p)

    if not targets:
        # Fallback default principal so alerts are never silently dropped in unconfigured tenants
        targets.add("admin")

    for principal_id in sorted(targets):
        idempotency_key = f"{selector}:{source_id}:{principal_id}"
        await create_notification(
            conn,
            namespace_id=namespace_id,
            principal_id=principal_id,
            title=title,
            body=body,
            severity=severity,
            category=category,
            source_selector=selector,
            source_id=source_id,
            idempotency_key=idempotency_key,
        )


async def handle_ticket_sla_breached(
    conn: asyncpg.Connection,
    event: dict[str, Any],
) -> None:
    """Outbox subscriber for TICKET.sla_breached."""
    payload = _get_payload(event)
    ns_id = _get_namespace_id(event, payload)
    if not ns_id:
        return

    ticket_id = str(payload.get("ticket_id") or event.get("aggregate_id") or "unknown")
    breach_type = payload.get("breach_type") or "response"
    priority = payload.get("priority") or "medium"
    title = f"SLA Breached: Ticket {ticket_id} ({breach_type})"
    body = (
        f"Support ticket {ticket_id} has breached its {breach_type} deadline. "
        f"Priority: {priority}. Status: {payload.get('status', 'open')}."
    )
    explicit = []
    if "assigned_to" in payload and payload["assigned_to"]:
        explicit.append(str(payload["assigned_to"]))

    await _deliver_to_principals(
        conn,
        namespace_id=ns_id,
        selector="TICKET.sla_breached",
        source_id=ticket_id,
        title=title,
        body=body,
        severity="critical",
        category="support",
        explicit_principals=explicit,
    )


async def handle_certification_expired(
    conn: asyncpg.Connection,
    event: dict[str, Any],
) -> None:
    """Outbox subscriber for CERTIFICATION.EXPIRED."""
    payload = _get_payload(event)
    ns_id = _get_namespace_id(event, payload)
    if not ns_id:
        return

    cert_id = str(
        payload.get("cert_id")
        or payload.get("certification_id")
        or event.get("aggregate_id")
        or "unknown"
    )
    cert_name = payload.get("cert_name") or payload.get("name") or cert_id
    employee_id = payload.get("employee_id") or payload.get("contractor_id")
    title = f"Certification Expired: {cert_name}"
    body = (
        f"Certification '{cert_name}' (ID: {cert_id}) has expired. "
        f"Associated personnel ID: {employee_id or 'unassigned'}."
    )
    explicit = [str(employee_id)] if employee_id else []

    await _deliver_to_principals(
        conn,
        namespace_id=ns_id,
        selector="CERTIFICATION.EXPIRED",
        source_id=cert_id,
        title=title,
        body=body,
        severity="warning",
        category="compliance",
        explicit_principals=explicit,
    )


async def handle_po_line_status_changed(
    conn: asyncpg.Connection,
    event: dict[str, Any],
) -> None:
    """Outbox subscriber for PO_LINE.status_changed."""
    payload = _get_payload(event)
    ns_id = _get_namespace_id(event, payload)
    if not ns_id:
        return

    po_line_id = str(payload.get("po_line_id") or event.get("aggregate_id") or "unknown")
    new_status = str(payload.get("status") or payload.get("new_status") or "updated")
    sku = payload.get("sku") or "unknown SKU"
    title = f"PO Line Status Changed: {po_line_id} -> {new_status}"
    body = f"Purchase order line {po_line_id} (SKU: {sku}) changed status to {new_status}."

    source_id = f"{po_line_id}:{new_status}"
    await _deliver_to_principals(
        conn,
        namespace_id=ns_id,
        selector="PO_LINE.status_changed",
        source_id=source_id,
        title=title,
        body=body,
        severity="info",
        category="procurement",
    )


async def handle_agreement_renewal_due(
    conn: asyncpg.Connection,
    event: dict[str, Any],
) -> None:
    """Outbox subscriber for AGREEMENT.renewal_due."""
    payload = _get_payload(event)
    ns_id = _get_namespace_id(event, payload)
    if not ns_id:
        return

    agreement_id = str(payload.get("agreement_id") or event.get("aggregate_id") or "unknown")
    customer_id = payload.get("customer_id") or "unknown"
    renewal_date = payload.get("renewal_date") or "upcoming"
    title = f"Agreement Renewal Due: {agreement_id}"
    body = f"Customer agreement {agreement_id} (Customer: {customer_id}) is due for renewal on {renewal_date}."

    await _deliver_to_principals(
        conn,
        namespace_id=ns_id,
        selector="AGREEMENT.renewal_due",
        source_id=agreement_id,
        title=title,
        body=body,
        severity="warning",
        category="sales",
    )


async def handle_deal_stalled(
    conn: asyncpg.Connection,
    event: dict[str, Any],
) -> None:
    """Outbox subscriber for DEAL.stalled."""
    payload = _get_payload(event)
    ns_id = _get_namespace_id(event, payload)
    if not ns_id:
        return

    deal_id = str(payload.get("deal_id") or event.get("aggregate_id") or "unknown")
    deal_title = payload.get("deal_title") or deal_id
    owner_id = payload.get("owner_id")
    title = f"Deal Stalled: {deal_title}"
    body = (
        f"Opportunity deal {deal_title} (ID: {deal_id}) has had no activity beyond the threshold."
    )
    explicit = [str(owner_id)] if owner_id else []

    await _deliver_to_principals(
        conn,
        namespace_id=ns_id,
        selector="DEAL.stalled",
        source_id=deal_id,
        title=title,
        body=body,
        severity="warning",
        category="sales",
        explicit_principals=explicit,
    )


async def handle_asset_health_changed(
    conn: asyncpg.Connection,
    event: dict[str, Any],
) -> None:
    """Outbox subscriber for ASSET.health_changed."""
    payload = _get_payload(event)
    ns_id = _get_namespace_id(event, payload)
    if not ns_id:
        return

    asset_id = str(payload.get("asset_id") or event.get("aggregate_id") or "unknown")
    old_state = payload.get("old_health") or "healthy"
    new_state = payload.get("new_health") or payload.get("health") or "degraded"
    title = f"Asset Health Changed: {asset_id} -> {new_state}"
    body = f"Asset {asset_id} health transitioned from {old_state} to {new_state}."

    await _deliver_to_principals(
        conn,
        namespace_id=ns_id,
        selector="ASSET.health_changed",
        source_id=f"{asset_id}:{new_state}",
        title=title,
        body=body,
        severity="warning",
        category="assets",
    )


async def handle_reminder_fired(
    conn: asyncpg.Connection,
    event: dict[str, Any],
) -> None:
    """Outbox subscriber for REMINDER.fired."""
    payload = _get_payload(event)
    ns_id = _get_namespace_id(event, payload)
    if not ns_id:
        return

    rem_id = str(payload.get("reminder_id") or event.get("aggregate_id") or "unknown")
    principal_id = payload.get("principal_id")
    title = payload.get("title") or "Scheduled Reminder"
    note = payload.get("note") or ""
    node_type = payload.get("node_type") or "NODE"
    node_id = payload.get("node_id") or ""
    body = f"{note}\n\nAttached to {node_type} ({node_id})" if node_id else note

    explicit = [str(principal_id)] if principal_id else []
    await _deliver_to_principals(
        conn,
        namespace_id=ns_id,
        selector="REMINDER.fired",
        source_id=rem_id,
        title=f"Reminder: {title}",
        body=body.strip(),
        severity="info",
        category="reminder",
        explicit_principals=explicit,
    )


def register_notifications_subscribers() -> None:
    """Register all 7 C13 outbox event subscribers with the C4 outbox bus.

    Idempotent across multiple registrations in the same process.
    Must be called before the relay poll loop starts in mcp_stdio_main and cron.
    """
    subscribe({"node_type": "TICKET", "op": "sla_breached"}, handle_ticket_sla_breached)
    subscribe({"node_type": "CERTIFICATION", "op": "EXPIRED"}, handle_certification_expired)
    subscribe({"node_type": "PO_LINE", "op": "status_changed"}, handle_po_line_status_changed)
    subscribe({"node_type": "AGREEMENT", "op": "renewal_due"}, handle_agreement_renewal_due)
    subscribe({"node_type": "DEAL", "op": "stalled"}, handle_deal_stalled)
    subscribe({"node_type": "ASSET", "op": "health_changed"}, handle_asset_health_changed)
    subscribe({"node_type": "REMINDER", "op": "fired"}, handle_reminder_fired)
    log.info("Registered C13 notifications outbox subscribers for 7 selectors.")
