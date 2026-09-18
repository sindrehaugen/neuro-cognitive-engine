"""tests.unit.test_c13_notifications — Acceptance tests for C13 Notifications and Reminders.

Phase A Wave A-3:
Covers:
  1. Service layer: idempotent notification creation, principal subscription, and lookup.
  2. Reminders engine: reminder creation, deadline-based evaluation, and status transition.
  3. C4 outbox event subscribers: all 7 candidate event handlers translate events into inbox alerts.
  4. Idempotency on replay: duplicate outbox events do not create duplicate notifications.
  5. Negative RLS & multi-tenant isolation: strict namespace scoping on C12 routes and database DDL.
  6. C12 resource surfaces: 13 uniform REST endpoints and 4 MCP tools for NOTIFICATION and REMINDER.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from nce import admin_state
from nce.event_log import EXPECTED_TENANT_RLS_TABLES
from nce.resource_surface import (
    ResourceSpec,
    get_resource_spec,
    load_all_engine_resources,
)
from nce.resource_surface.rest import _clear_mem_store, make_resource_routes
from nce.tool_registry import TOOL_REGISTRY
from nce.vertical_modules.notifications import (
    NOTIFICATION_SPEC,
    REMINDER_SPEC,
    create_notification,
    create_reminder,
    fire_pending_reminders,
    get_subscribed_principals,
    register_notifications_subscribers,
    subscribe_principal,
)
from nce.vertical_modules.notifications.subscribers import (
    handle_agreement_renewal_due,
    handle_asset_health_changed,
    handle_certification_expired,
    handle_deal_stalled,
    handle_po_line_status_changed,
    handle_reminder_fired,
    handle_ticket_sla_breached,
)

_NS_A = UUID("11111111-1111-4111-8111-111111111111")
_NS_B = UUID("22222222-2222-4222-8222-222222222222")


@pytest.fixture(autouse=True)
def _reset_env():
    _clear_mem_store()
    admin_state.engine = None
    yield
    _clear_mem_store()


def _client_for_spec(spec: ResourceSpec) -> TestClient:
    app = Starlette(routes=make_resource_routes(spec))
    return TestClient(app, raise_server_exceptions=False)


# ===========================================================================
# 1. Service Layer & Idempotency Tests
# ===========================================================================


@pytest.mark.asyncio
async def test_create_notification_success():
    """Verify clean insertion of a notification record."""
    conn = AsyncMock()
    fake_row = {
        "id": uuid4(),
        "namespace_id": _NS_A,
        "principal_id": "usr_123",
        "title": "System Alert",
        "body": "Disk usage is normal.",
        "severity": "info",
        "category": "system",
        "source_selector": "test.alert",
        "source_id": "node_1",
        "idempotency_key": "test.alert:node_1:usr_123",
        "read_at": None,
        "seen_at": None,
        "is_archived": False,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    conn.fetchrow.return_value = fake_row

    result = await create_notification(
        conn,
        namespace_id=_NS_A,
        principal_id="usr_123",
        title="System Alert",
        body="Disk usage is normal.",
        severity="info",
        category="system",
        source_selector="test.alert",
        source_id="node_1",
        idempotency_key="test.alert:node_1:usr_123",
    )

    assert result["id"] == fake_row["id"]
    assert result["principal_id"] == "usr_123"
    assert result["severity"] == "info"
    conn.fetchrow.assert_called_once()


@pytest.mark.asyncio
async def test_create_notification_replay_idempotency():
    """Verify that a second insert with the same idempotency key returns the existing row."""
    conn = AsyncMock()
    existing_row = {
        "id": uuid4(),
        "namespace_id": _NS_A,
        "principal_id": "usr_123",
        "title": "System Alert",
        "body": "Disk usage is normal.",
        "severity": "info",
        "category": "system",
        "idempotency_key": "test.alert:node_1:usr_123",
    }
    # First fetchrow returns None (conflict triggered ON CONFLICT DO NOTHING), second returns existing row
    conn.fetchrow.side_effect = [None, existing_row]

    result = await create_notification(
        conn,
        namespace_id=_NS_A,
        principal_id="usr_123",
        title="System Alert",
        idempotency_key="test.alert:node_1:usr_123",
    )

    assert result["id"] == existing_row["id"]
    assert result.get("idempotent_skip") is True
    assert conn.fetchrow.call_count == 2


@pytest.mark.asyncio
async def test_subscribe_principal_and_lookup():
    """Verify principal event selector subscription and retrieval."""
    conn = AsyncMock()
    fake_sub = {
        "id": uuid4(),
        "namespace_id": _NS_A,
        "principal_id": "usr_456",
        "selector": "TICKET.sla_breached",
        "created_at": datetime.now(timezone.utc),
    }
    conn.fetchrow.return_value = fake_sub

    sub_res = await subscribe_principal(
        conn,
        namespace_id=_NS_A,
        principal_id="usr_456",
        selector="TICKET.sla_breached",
    )
    assert sub_res["principal_id"] == "usr_456"

    # Query subscribed principals
    conn.fetch.return_value = [{"principal_id": "usr_456"}, {"principal_id": "usr_789"}]
    principals = await get_subscribed_principals(
        conn,
        namespace_id=_NS_A,
        selector="TICKET.sla_breached",
    )
    assert principals == ["usr_456", "usr_789"]


# ===========================================================================
# 2. Reminders Engine Tests
@pytest.mark.asyncio
async def test_create_reminder_helper():
    """Verify create_reminder helper inserts a pending reminder."""
    conn = AsyncMock()
    now = datetime.now(timezone.utc)
    fake_row = {
        "id": uuid4(),
        "namespace_id": _NS_A,
        "principal_id": "usr_test",
        "node_type": "DEVICE",
        "node_id": "DEV-1",
        "title": "Test Reminder",
        "note": "Note",
        "remind_at": now,
        "status": "pending",
    }
    conn.fetchrow.return_value = fake_row
    res = await create_reminder(
        conn,
        namespace_id=_NS_A,
        principal_id="usr_test",
        node_type="DEVICE",
        node_id="DEV-1",
        title="Test Reminder",
        remind_at=now,
    )
    assert res["id"] == fake_row["id"]
    assert res["status"] == "pending"


@pytest.mark.asyncio
async def test_create_and_fire_pending_reminders():
    """Verify that elapsed reminders transition to 'fired' and publish REMINDER.fired."""
    conn = AsyncMock()
    conn.transaction = MagicMock()
    now = datetime.now(timezone.utc)
    past_due = now - timedelta(minutes=10)
    rem_id = uuid4()

    fake_reminder = {
        "id": rem_id,
        "namespace_id": _NS_A,
        "principal_id": "usr_admin",
        "node_type": "DEVICE",
        "node_id": "DEV-001",
        "title": "Check firmware update",
        "note": "Verify switch port 4",
        "remind_at": past_due,
        "status": "pending",
        "fired_at": None,
        "is_archived": False,
        "created_at": past_due,
        "updated_at": past_due,
    }

    # fire_pending_reminders queries pending reminders
    conn.fetch.return_value = [fake_reminder]
    conn.execute.return_value = "UPDATE 1"

    with patch(
        "nce.vertical_modules.notifications.reminders.publish", new_callable=AsyncMock
    ) as mock_pub:
        fired = await fire_pending_reminders(conn, namespace_id=_NS_A, now=now)

        assert len(fired) == 1
        assert fired[0]["id"] == rem_id
        assert fired[0]["status"] == "fired"

        # Verify REMINDER.fired was published to C4 transactional outbox
        mock_pub.assert_called_once()
        kws = mock_pub.call_args.kwargs
        assert kws["namespace_id"] == _NS_A
        assert kws["node_type"] == "REMINDER"
        assert kws["op"] == "fired"
        assert kws["aggregate_id"] == f"REMINDER:{rem_id}"
        assert kws["payload"]["reminder_id"] == str(rem_id)
        assert kws["payload"]["principal_id"] == "usr_admin"


# ===========================================================================
# 3. Outbox Subscriber Handlers Tests
# ===========================================================================


@pytest.mark.asyncio
async def test_handle_ticket_sla_breached():
    """Test TICKET.sla_breached event handling."""
    conn = AsyncMock()
    event = {
        "namespace_id": str(_NS_A),
        "aggregate_type": "TICKET",
        "aggregate_id": "TICKET:tick-100",
        "event_type": "TICKET.sla_breached",
        "payload": {
            "ticket_id": "tick-100",
            "namespace_id": str(_NS_A),
            "breach_type": "first_response",
            "priority": "critical",
            "status": "open",
            "assigned_to": "agent_smith",
        },
    }

    with patch(
        "nce.vertical_modules.notifications.subscribers.create_notification", new_callable=AsyncMock
    ) as mock_create:
        mock_create.return_value = {"id": uuid4()}
        with patch(
            "nce.vertical_modules.notifications.subscribers.get_subscribed_principals",
            new_callable=AsyncMock,
        ) as mock_subs:
            mock_subs.return_value = ["manager_bob"]

            await handle_ticket_sla_breached(conn, event)

            # Both assigned_to and subscribed manager_bob should be notified
            assert mock_create.call_count == 2
            principals_notified = {
                call.kwargs["principal_id"] for call in mock_create.call_args_list
            }
            assert principals_notified == {"agent_smith", "manager_bob"}

            sample_call = mock_create.call_args_list[0].kwargs
            assert sample_call["severity"] == "critical"
            assert sample_call["category"] == "support"
            assert sample_call["source_selector"] == "TICKET.sla_breached"
            assert sample_call["source_id"] == "tick-100"


@pytest.mark.asyncio
async def test_handle_certification_expired():
    """Test CERTIFICATION.EXPIRED event handling."""
    conn = AsyncMock()
    event = {
        "namespace_id": str(_NS_A),
        "aggregate_type": "CERTIFICATION",
        "aggregate_id": "CERTIFICATION:cert-99",
        "event_type": "CERTIFICATION.EXPIRED",
        "payload": {
            "cert_id": "cert-99",
            "cert_name": "Dante Level 3",
            "employee_id": "tech_john",
        },
    }

    with patch(
        "nce.vertical_modules.notifications.subscribers.create_notification", new_callable=AsyncMock
    ) as mock_create:
        with patch(
            "nce.vertical_modules.notifications.subscribers.get_subscribed_principals",
            new_callable=AsyncMock,
        ) as mock_subs:
            mock_subs.return_value = []
            await handle_certification_expired(conn, event)

            assert mock_create.call_count == 1
            call_kw = mock_create.call_args.kwargs
            assert call_kw["principal_id"] == "tech_john"
            assert call_kw["severity"] == "warning"
            assert call_kw["category"] == "compliance"
            assert "Dante Level 3" in call_kw["title"]


@pytest.mark.asyncio
async def test_handle_po_line_status_changed():
    """Test PO_LINE.status_changed event handling."""
    conn = AsyncMock()
    event = {
        "namespace_id": str(_NS_A),
        "aggregate_type": "PO_LINE",
        "aggregate_id": "PO_LINE:line-55",
        "event_type": "PO_LINE.status_changed",
        "payload": {
            "po_line_id": "line-55",
            "status": "ORDERED",
            "sku": "CISCO-CAT9K",
        },
    }

    with patch(
        "nce.vertical_modules.notifications.subscribers.create_notification", new_callable=AsyncMock
    ) as mock_create:
        with patch(
            "nce.vertical_modules.notifications.subscribers.get_subscribed_principals",
            new_callable=AsyncMock,
        ) as mock_subs:
            mock_subs.return_value = ["buyer_alice"]
            await handle_po_line_status_changed(conn, event)

            assert mock_create.call_count == 1
            call_kw = mock_create.call_args.kwargs
            assert call_kw["principal_id"] == "buyer_alice"
            assert call_kw["category"] == "procurement"
            assert "ORDERED" in call_kw["title"]


@pytest.mark.asyncio
async def test_handle_reminder_fired():
    """Test REMINDER.fired event handling."""
    conn = AsyncMock()
    event = {
        "namespace_id": str(_NS_A),
        "aggregate_type": "REMINDER",
        "aggregate_id": "REMINDER:rem-1",
        "event_type": "REMINDER.fired",
        "payload": {
            "reminder_id": "rem-1",
            "principal_id": "user_charlie",
            "title": "Call Client",
            "note": "Discuss contract renewal",
            "node_type": "AGREEMENT",
            "node_id": "AGR-101",
        },
    }

    with patch(
        "nce.vertical_modules.notifications.subscribers.create_notification", new_callable=AsyncMock
    ) as mock_create:
        with patch(
            "nce.vertical_modules.notifications.subscribers.get_subscribed_principals",
            new_callable=AsyncMock,
        ) as mock_subs:
            mock_subs.return_value = []
            await handle_reminder_fired(conn, event)

            assert mock_create.call_count == 1
            call_kw = mock_create.call_args.kwargs
            assert call_kw["principal_id"] == "user_charlie"
            assert call_kw["category"] == "reminder"
            assert call_kw["severity"] == "info"
            assert "Reminder: Call Client" in call_kw["title"]


@pytest.mark.asyncio
async def test_handle_agreement_renewal_due():
    """Test AGREEMENT.renewal_due event handling."""
    conn = AsyncMock()
    event = {
        "namespace_id": str(_NS_A),
        "aggregate_type": "AGREEMENT",
        "aggregate_id": "AGREEMENT:agr-1",
        "event_type": "AGREEMENT.renewal_due",
        "payload": {"agreement_id": "agr-1", "customer_id": "cust-1", "renewal_date": "2026-10-01"},
    }
    with patch(
        "nce.vertical_modules.notifications.subscribers.create_notification", new_callable=AsyncMock
    ) as mock_create:
        with patch(
            "nce.vertical_modules.notifications.subscribers.get_subscribed_principals",
            new_callable=AsyncMock,
        ) as mock_subs:
            mock_subs.return_value = ["sales_rep"]
            await handle_agreement_renewal_due(conn, event)
            assert mock_create.call_count == 1
            assert mock_create.call_args.kwargs["category"] == "sales"
            assert mock_create.call_args.kwargs["principal_id"] == "sales_rep"


@pytest.mark.asyncio
async def test_handle_deal_stalled():
    """Test DEAL.stalled event handling."""
    conn = AsyncMock()
    event = {
        "namespace_id": str(_NS_A),
        "aggregate_type": "DEAL",
        "aggregate_id": "DEAL:deal-2",
        "event_type": "DEAL.stalled",
        "payload": {
            "deal_id": "deal-2",
            "deal_title": "Enterprise Rollout",
            "owner_id": "sales_lead",
        },
    }
    with patch(
        "nce.vertical_modules.notifications.subscribers.create_notification", new_callable=AsyncMock
    ) as mock_create:
        with patch(
            "nce.vertical_modules.notifications.subscribers.get_subscribed_principals",
            new_callable=AsyncMock,
        ) as mock_subs:
            mock_subs.return_value = []
            await handle_deal_stalled(conn, event)
            assert mock_create.call_count == 1
            assert mock_create.call_args.kwargs["principal_id"] == "sales_lead"
            assert mock_create.call_args.kwargs["category"] == "sales"


@pytest.mark.asyncio
async def test_handle_asset_health_changed():
    """Test ASSET.health_changed event handling."""
    conn = AsyncMock()
    event = {
        "namespace_id": str(_NS_A),
        "aggregate_type": "ASSET",
        "aggregate_id": "ASSET:ast-3",
        "event_type": "ASSET.health_changed",
        "payload": {"asset_id": "ast-3", "old_health": "good", "new_health": "faulty"},
    }
    with patch(
        "nce.vertical_modules.notifications.subscribers.create_notification", new_callable=AsyncMock
    ) as mock_create:
        with patch(
            "nce.vertical_modules.notifications.subscribers.get_subscribed_principals",
            new_callable=AsyncMock,
        ) as mock_subs:
            mock_subs.return_value = []
            await handle_asset_health_changed(conn, event)
            assert mock_create.call_count == 1
            assert mock_create.call_args.kwargs["category"] == "assets"


def test_register_notifications_subscribers_idempotency():
    """Verify that calling register_notifications_subscribers multiple times is safe."""
    register_notifications_subscribers()
    register_notifications_subscribers()


# ===========================================================================
# 4. Multi-Tenant Negative RLS & Schema Pins
# ===========================================================================


def test_schema_rls_pins():
    """Verify that notifications, reminders, and notification_subscriptions have RLS enforced."""
    assert "notifications" in EXPECTED_TENANT_RLS_TABLES
    assert "reminders" in EXPECTED_TENANT_RLS_TABLES
    assert "notification_subscriptions" in EXPECTED_TENANT_RLS_TABLES


def test_c12_resource_specs_registered():
    """Verify C12 resource specifications for NOTIFICATION and REMINDER."""
    load_all_engine_resources()
    notif_spec = get_resource_spec("notifications", "notifications")
    rem_spec = get_resource_spec("notifications", "reminders")

    assert notif_spec is not None
    assert notif_spec.node_type == "NOTIFICATION"
    assert notif_spec.table_name == "notifications"
    assert notif_spec.tenant_scope == "tenant"

    assert rem_spec is not None
    assert rem_spec.node_type == "REMINDER"
    assert rem_spec.table_name == "reminders"
    assert rem_spec.tenant_scope == "tenant"


def test_negative_rls_isolation_between_tenants():
    """Tenant B cannot read or access Tenant A's notifications."""
    client = _client_for_spec(NOTIFICATION_SPEC)

    # 1. Tenant A creates a notification
    res_a = client.post(
        "/api/notifications/notifications",
        json={
            "namespace_id": str(_NS_A),
            "principal_id": "agent_a",
            "title": "Confidential A",
            "body": "Secret info",
            "severity": "high",
        },
    )
    assert res_a.status_code == 201
    item_id = res_a.json()["id"]

    # 2. Tenant A can read it
    read_a = client.get(f"/api/notifications/notifications/{item_id}?namespace_id={_NS_A}")
    assert read_a.status_code == 200
    assert read_a.json()["title"] == "Confidential A"

    # 3. Tenant B querying the same ID receives 404 (strictly isolated)
    read_b = client.get(f"/api/notifications/notifications/{item_id}?namespace_id={_NS_B}")
    assert read_b.status_code == 404

    # 4. Tenant B list does not leak Tenant A's notification
    list_b = client.get(f"/api/notifications/notifications?namespace_id={_NS_B}")
    assert list_b.status_code == 200
    items_b = list_b.json()["items"]
    assert not any(item["id"] == item_id for item in items_b)


# ===========================================================================
# 5. C12 REST Routes and MCP Tools Execution
# ===========================================================================


def test_notifications_rest_crud_lifecycle():
    """Verify standard REST collection, get, update, and soft-archive lifecycle."""
    client = _client_for_spec(NOTIFICATION_SPEC)

    # 1. Create
    resp = client.post(
        "/api/notifications/notifications",
        json={
            "namespace_id": str(_NS_A),
            "principal_id": "usr_test",
            "title": "Test Title",
            "body": "Test Body",
            "severity": "warning",
            "category": "system",
        },
    )
    assert resp.status_code == 201
    notif_id = resp.json()["id"]

    # 2. Patch / Mark seen
    seen_time = datetime.now(timezone.utc).isoformat()
    patch_resp = client.patch(
        f"/api/notifications/notifications/{notif_id}",
        json={
            "namespace_id": str(_NS_A),
            "seen_at": seen_time,
        },
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["seen_at"] == seen_time

    # 3. Archive
    arch_resp = client.post(
        f"/api/notifications/notifications/{notif_id}/archive",
        json={
            "namespace_id": str(_NS_A),
            "reason": "Dismissed by user",
        },
    )
    assert arch_resp.status_code == 200
    assert arch_resp.json()["archived"] is True


def test_reminders_rest_crud_lifecycle():
    """Verify standard REST lifecycle for reminders."""
    client = _client_for_spec(REMINDER_SPEC)

    remind_time = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    create_resp = client.post(
        "/api/notifications/reminders",
        json={
            "namespace_id": str(_NS_A),
            "principal_id": "usr_rem",
            "node_type": "DEVICE",
            "node_id": "DEV-42",
            "title": "Calibrate Sensor",
            "note": "Annual calibration check",
            "remind_at": remind_time,
            "status": "pending",
        },
    )
    assert create_resp.status_code == 201
    rem_id = create_resp.json()["id"]

    # Get
    get_resp = client.get(f"/api/notifications/reminders/{rem_id}?namespace_id={_NS_A}")
    assert get_resp.status_code == 200
    assert get_resp.json()["title"] == "Calibrate Sensor"
    assert get_resp.json()["status"] == "pending"


@pytest.mark.asyncio
async def test_mcp_tool_execution():
    """Verify that generated MCP tools are callable via TOOL_REGISTRY."""
    assert "notifications_list_notifications" in TOOL_REGISTRY
    assert "notifications_get_notifications" in TOOL_REGISTRY
    assert "notifications_upsert_notifications" in TOOL_REGISTRY
    assert "notifications_archive_notifications" in TOOL_REGISTRY

    assert "notifications_list_reminders" in TOOL_REGISTRY
    assert "notifications_get_reminders" in TOOL_REGISTRY
    assert "notifications_upsert_reminders" in TOOL_REGISTRY
    assert "notifications_archive_reminders" in TOOL_REGISTRY

    # Test invoking list tool handler against in-memory state
    list_tool = TOOL_REGISTRY["notifications_list_notifications"]
    raw_res = await list_tool.handler(None, {"namespace_id": str(_NS_A)})
    res = json.loads(raw_res) if isinstance(raw_res, str) else raw_res
    assert "items" in res
    assert "total" in res
