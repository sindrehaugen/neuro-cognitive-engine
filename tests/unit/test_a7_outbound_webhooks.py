"""
tests/unit/test_a7_outbound_webhooks.py

Wave A-7: Outbound Webhooks on C4
Unit and integration tests for outbound webhooks:
- Migration 094 schema, table creation, and RLS policy registration.
- Selector matching logic (exact, wildcard *, prefix .*).
- Three-header HMAC-SHA256 signature scheme verification matching nce/auth.py.
- Domain service operations (create, list, get, delete, matching).
- Admin HTTP handlers (/api/admin/webhooks CRUD).
- Outbox relay integration:
  - Produced event delivery to test receiver via PostCommitAction.
  - Delivery gate: Delivered exactly once across simulated relay restart.
  - Multi-tenant boundary isolation.
"""

from __future__ import annotations

import json
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import httpx
import pytest
from starlette.requests import Request

from nce import admin_state, outbox_relay
from nce.auth import verify_hmac
from nce.event_log import EXPECTED_TENANT_RLS_TABLES
from nce.outbound_webhooks import (
    create_webhook,
    delete_webhook,
    get_matching_webhooks,
    get_webhook,
    list_webhooks,
    matches_selector,
    sign_outbound_webhook,
)

# ---------------------------------------------------------------------------
# Test 1: Migration 094 & RLS Table Registration
# ---------------------------------------------------------------------------


def test_predicate_migration_094_and_rls_registration():
    """Verify migration 094 exists, defines outbound_webhooks with RLS, and is in EXPECTED_TENANT_RLS_TABLES."""
    assert "outbound_webhooks" in EXPECTED_TENANT_RLS_TABLES
    assert EXPECTED_TENANT_RLS_TABLES["outbound_webhooks"] == "namespace_id"

    root = Path(__file__).resolve().parents[2]
    mig = root / "nce" / "migrations" / "094_outbound_webhooks.sql"
    assert mig.exists(), "094_outbound_webhooks.sql migration must exist"

    content = mig.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS outbound_webhooks" in content
    assert "ALTER TABLE outbound_webhooks ENABLE ROW LEVEL SECURITY;" in content
    assert "ALTER TABLE outbound_webhooks FORCE ROW LEVEL SECURITY;" in content
    assert "CREATE POLICY tenant_isolation_policy ON outbound_webhooks" in content
    assert "WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())" in content


# ---------------------------------------------------------------------------
# Test 2: Selector Pattern Matching
# ---------------------------------------------------------------------------


def test_selector_matching_exact():
    """Exact selector matches only the specified event type."""
    selectors = ["memory.stored", "order.created"]
    assert matches_selector(selectors, "memory.stored") is True
    assert matches_selector(selectors, "order.created") is True
    assert matches_selector(selectors, "memory.deleted") is False
    assert matches_selector(selectors, "user.login") is False


def test_selector_matching_wildcard():
    """Wildcard selector '*' matches every event type."""
    selectors = ["*"]
    assert matches_selector(selectors, "memory.stored") is True
    assert matches_selector(selectors, "arbitrary.deep.domain.event") is True
    assert matches_selector(selectors, "system_design.node.created") is True


def test_selector_matching_prefix():
    """Prefix wildcard 'prefix.*' matches events starting with the prefix."""
    selectors = ["memory.*", "system_design.*"]
    assert matches_selector(selectors, "memory.stored") is True
    assert matches_selector(selectors, "memory.aspect.updated") is True
    assert matches_selector(selectors, "system_design.geometry.saved") is True
    assert matches_selector(selectors, "order.created") is False
    assert matches_selector(selectors, "memorial.event") is False  # not "memory."


def test_selector_matching_empty():
    """Empty or whitespace-only selector list matches nothing."""
    assert matches_selector([], "memory.stored") is False
    assert matches_selector(["", "  "], "memory.stored") is False


# ---------------------------------------------------------------------------
# Test 3: Three-Header HMAC-SHA256 Signing Contract
# ---------------------------------------------------------------------------


def test_three_header_hmac_signing_contract():
    """Verify outbound webhook signing produces valid headers matching verify_hmac."""
    secret = "webhook-test-secret-key-32-chars-long"
    url = "https://receiver.example.com/api/v1/webhook/callback"
    body_payload = {"event_id": str(uuid4()), "event_type": "memory.stored", "text": "test"}
    body_bytes = json.dumps(body_payload, sort_keys=True).encode("utf-8")

    fixed_ts = 1774000000
    fixed_nonce = "fixed-nonce-value-1234"

    headers = sign_outbound_webhook(
        secret=secret,
        method="POST",
        url=url,
        body_bytes=body_bytes,
        timestamp=fixed_ts,
        nonce=fixed_nonce,
    )

    # 1. Inspect headers
    assert headers["Content-Type"] == "application/json"
    assert headers["X-NCE-Timestamp"] == str(fixed_ts)
    assert headers["X-NCE-Nonce"] == fixed_nonce
    assert headers["Authorization"].startswith("HMAC-SHA256 ")

    sig = headers["Authorization"].partition(" ")[2]
    assert len(sig) == 64  # SHA256 hex digest length

    # 2. Verify with nce.auth.verify_hmac
    parsed_path = urllib.parse.urlparse(url).path
    assert (
        verify_hmac(
            api_key=secret,
            method="POST",
            path=parsed_path,
            timestamp=fixed_ts,
            body_bytes=body_bytes,
            provided_sig=sig,
        )
        is True
    )


def test_hmac_tamper_rejection():
    """Tampering with body, timestamp, path, or secret invalidates signature."""
    secret = "webhook-test-secret"
    url = "https://receiver.example.com/webhook"
    body = b'{"msg":"hello"}'
    ts = 1774000000

    headers = sign_outbound_webhook(secret, "POST", url, body, timestamp=ts)
    sig = headers["Authorization"].partition(" ")[2]
    path = "/webhook"

    # Valid
    assert verify_hmac(secret, "POST", path, ts, body, sig) is True

    # Tampered body
    assert verify_hmac(secret, "POST", path, ts, b'{"msg":"tampered"}', sig) is False

    # Tampered timestamp
    assert verify_hmac(secret, "POST", path, ts + 10, body, sig) is False

    # Tampered path
    assert verify_hmac(secret, "POST", "/other-path", ts, body, sig) is False

    # Wrong secret
    assert verify_hmac("wrong-secret", "POST", path, ts, body, sig) is False


# ---------------------------------------------------------------------------
# Test 4: Domain Service CRUD Operations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_domain_crud_service():
    """Test create, list, get, and delete webhook functions with mock connection."""
    mock_conn = AsyncMock()
    ns_id = uuid4()
    wh_id = uuid4()
    now = datetime.now(timezone.utc)

    mock_row = {
        "id": wh_id,
        "namespace_id": ns_id,
        "url": "https://receiver.example.com/hook",
        "secret": "s3cr3t",
        "selectors": ["memory.*"],
        "is_active": True,
        "description": "Test hook",
        "created_at": now,
        "updated_at": now,
    }

    # create_webhook
    mock_conn.fetchrow.return_value = mock_row
    created = await create_webhook(
        mock_conn,
        namespace_id=ns_id,
        url="https://receiver.example.com/hook",
        secret="s3cr3t",
        selectors=["memory.*"],
        description="Test hook",
    )
    assert created.id == wh_id
    assert created.selectors == ("memory.*",)
    assert created.to_dict()["url"] == "https://receiver.example.com/hook"
    assert "secret" not in created.to_dict()
    assert created.to_dict(include_secret=True)["secret"] == "s3cr3t"

    # list_webhooks
    mock_conn.fetch.return_value = [mock_row]
    listed = await list_webhooks(mock_conn, namespace_id=ns_id)
    assert len(listed) == 1
    assert listed[0].id == wh_id

    # get_webhook
    mock_conn.fetchrow.return_value = mock_row
    fetched = await get_webhook(mock_conn, namespace_id=ns_id, webhook_id=wh_id)
    assert fetched is not None
    assert fetched.id == wh_id

    # delete_webhook
    mock_conn.fetchval.return_value = wh_id
    deleted = await delete_webhook(mock_conn, namespace_id=ns_id, webhook_id=wh_id)
    assert deleted is True

    # delete_webhook not found
    mock_conn.fetchval.return_value = None
    deleted_missing = await delete_webhook(mock_conn, namespace_id=ns_id, webhook_id=uuid4())
    assert deleted_missing is False


@pytest.mark.asyncio
async def test_get_matching_webhooks():
    """Verify get_matching_webhooks filters active webhooks by selector."""
    mock_conn = AsyncMock()
    ns_id = uuid4()

    rows = [
        {
            "id": uuid4(),
            "namespace_id": ns_id,
            "url": "https://receiver.example.com/all",
            "secret": "sec1",
            "selectors": ["*"],
            "is_active": True,
            "description": "All events",
            "created_at": None,
            "updated_at": None,
        },
        {
            "id": uuid4(),
            "namespace_id": ns_id,
            "url": "https://receiver.example.com/memory",
            "secret": "sec2",
            "selectors": ["memory.*"],
            "is_active": True,
            "description": "Memory events only",
            "created_at": None,
            "updated_at": None,
        },
        {
            "id": uuid4(),
            "namespace_id": ns_id,
            "url": "https://receiver.example.com/design",
            "secret": "sec3",
            "selectors": ["system_design.*"],
            "is_active": True,
            "description": "Design events only",
            "created_at": None,
            "updated_at": None,
        },
    ]
    mock_conn.fetch.return_value = rows

    # Query for memory.stored -> should match hook 1 (*) and hook 2 (memory.*), not hook 3
    matches = await get_matching_webhooks(mock_conn, ns_id, "memory.stored")
    assert len(matches) == 2
    matched_urls = {m.url for m in matches}
    assert "https://receiver.example.com/all" in matched_urls
    assert "https://receiver.example.com/memory" in matched_urls
    assert "https://receiver.example.com/design" not in matched_urls


# ---------------------------------------------------------------------------
# Test 5: Admin HTTP Handlers
# ---------------------------------------------------------------------------


def _build_request(
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    query_params: dict[str, str] | None = None,
    path_params: dict[str, str] | None = None,
) -> Request:
    """Build a Starlette Request for testing admin handlers directly."""
    qp = urllib.parse.urlencode(query_params or {}).encode("ascii")
    raw_body = json.dumps(body).encode("utf-8") if body is not None else b""

    headers = [
        (b"content-type", b"application/json"),
    ]

    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode("ascii"),
        "headers": headers,
        "query_string": qp,
        "path_params": path_params or {},
    }

    async def receive():
        return {"type": "http.request", "body": raw_body, "more_body": False}

    return Request(scope, receive=receive)


@pytest.mark.asyncio
async def test_admin_handlers_post_and_get():
    """Test api_admin_webhooks_post and api_admin_webhooks_get."""
    from nce.admin_handlers.webhooks import (
        api_admin_webhooks_get,
        api_admin_webhooks_post,
    )

    mock_pool = MagicMock()
    mock_conn = AsyncMock()
    mock_pool.acquire.return_value.__aenter__.return_value = mock_conn

    mock_engine = MagicMock()
    mock_engine.pg_pool = mock_pool
    admin_state.engine = mock_engine

    ns_id = uuid4()
    wh_id = uuid4()

    mock_conn.fetchrow.return_value = {
        "id": wh_id,
        "namespace_id": ns_id,
        "url": "https://receiver.example.com/webhook",
        "secret": "s3cr3t",
        "selectors": ["*"],
        "is_active": True,
        "description": "Test subscription",
        "created_at": None,
        "updated_at": None,
    }

    # POST success
    req_post = _build_request(
        "POST",
        "/api/admin/webhooks",
        body={
            "namespace_id": str(ns_id),
            "url": "https://receiver.example.com/webhook",
            "secret": "s3cr3t",
            "selectors": ["*"],
            "description": "Test subscription",
        },
    )
    resp_post = await api_admin_webhooks_post(req_post)
    assert resp_post.status_code == 201
    post_data = json.loads(resp_post.body.decode("utf-8"))
    assert post_data["id"] == str(wh_id)
    assert post_data["url"] == "https://receiver.example.com/webhook"
    assert "secret" not in post_data  # Secret masked/omitted

    # GET success
    mock_conn.fetch.return_value = [mock_conn.fetchrow.return_value]
    req_get = _build_request(
        "GET",
        "/api/admin/webhooks",
        query_params={"namespace_id": str(ns_id)},
    )
    resp_get = await api_admin_webhooks_get(req_get)
    assert resp_get.status_code == 200
    get_data = json.loads(resp_get.body.decode("utf-8"))
    assert get_data["total"] == 1
    assert get_data["webhooks"][0]["id"] == str(wh_id)


@pytest.mark.asyncio
async def test_admin_handlers_delete():
    """Test api_admin_webhooks_delete."""
    from nce.admin_handlers.webhooks import api_admin_webhooks_delete

    mock_pool = MagicMock()
    mock_conn = AsyncMock()
    mock_pool.acquire.return_value.__aenter__.return_value = mock_conn

    mock_engine = MagicMock()
    mock_engine.pg_pool = mock_pool
    admin_state.engine = mock_engine

    ns_id = uuid4()
    wh_id = uuid4()

    # DELETE success
    mock_conn.fetchval.return_value = wh_id
    req_del = _build_request(
        "DELETE",
        f"/api/admin/webhooks/{wh_id}",
        query_params={"namespace_id": str(ns_id)},
        path_params={"id": str(wh_id)},
    )
    resp_del = await api_admin_webhooks_delete(req_del)
    assert resp_del.status_code == 200
    del_data = json.loads(resp_del.body.decode("utf-8"))
    assert del_data["deleted"] is True

    # DELETE not found
    mock_conn.fetchval.return_value = None
    resp_del_404 = await api_admin_webhooks_delete(req_del)
    assert resp_del_404.status_code == 404


@pytest.mark.asyncio
async def test_admin_handlers_validation_errors():
    """Missing or invalid fields return 400 or 422."""
    from nce.admin_handlers.webhooks import api_admin_webhooks_post

    mock_pool = MagicMock()
    mock_conn = AsyncMock()
    mock_pool.acquire.return_value.__aenter__.return_value = mock_conn

    mock_engine = MagicMock()
    mock_engine.pg_pool = mock_pool
    admin_state.engine = mock_engine

    # Missing namespace_id
    req = _build_request(
        "POST", "/api/admin/webhooks", body={"url": "https://receiver.example.com", "secret": "s"}
    )
    resp = await api_admin_webhooks_post(req)
    assert resp.status_code == 422

    # Missing url
    req = _build_request(
        "POST", "/api/admin/webhooks", body={"namespace_id": str(uuid4()), "secret": "s"}
    )
    resp = await api_admin_webhooks_post(req)
    assert resp.status_code == 422

    # Missing secret
    req = _build_request(
        "POST",
        "/api/admin/webhooks",
        body={"namespace_id": str(uuid4()), "url": "https://receiver.example.com"},
    )
    resp = await api_admin_webhooks_post(req)
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Test 6: Relay Delivery Gate (Produced event reaches test receiver exactly once across restart)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_outbox_relay_webhook_delivery_gate(monkeypatch):
    """GATE: A produced outbox event reaches a test receiver exactly once across relay restart.

    Proves:
    1. Outbox relay delivers event to matching outbound webhook with valid HMAC signature.
    2. Event is deduped in processed_outbox_events.
    3. Simulated relay restart / redelivery skips handler and does NOT call test receiver twice.
    """
    ns_id = uuid4()
    event_id = uuid4()
    webhook_id = uuid4()
    secret = "relay-gate-test-secret"
    target_url = "https://receiver.example.com/api/events"
    test_event_type = f"webhook.gate.test.{uuid4().hex}"

    # Track requests received by the test receiver
    received_requests: list[httpx.Request] = []

    def mock_transport_handler(request: httpx.Request) -> httpx.Response:
        received_requests.append(request)
        return httpx.Response(200, json={"status": "ok"})

    mock_client = httpx.Client(transport=httpx.MockTransport(mock_transport_handler))
    monkeypatch.setattr("httpx.Client", lambda *args, **kwargs: mock_client)

    # Simulated DB state
    processed_events: set[UUID] = set()
    webhook_row = {
        "id": webhook_id,
        "namespace_id": ns_id,
        "url": target_url,
        "secret": secret,
        "selectors": ["webhook.gate.*"],
        "is_active": True,
        "description": "Relay gate receiver",
        "created_at": None,
        "updated_at": None,
    }

    class MockTransaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            return None

    class MockConnection:
        def transaction(self):
            return MockTransaction()

        async def fetch(self, query, *args):
            if "FROM outbound_webhooks" in query:
                # Return webhook if namespace matches
                if args[0] == ns_id:
                    return [webhook_row]
                return []
            return []

        async def fetchval(self, query, *args):
            if "INSERT INTO processed_outbox_events" in query:
                ev_id = args[0]
                if ev_id in processed_events:
                    return None  # conflict, already processed
                processed_events.add(ev_id)
                return ev_id
            return None

    conn = MockConnection()

    event = {
        "id": event_id,
        "namespace_id": ns_id,
        "aggregate_type": "webhook_test",
        "aggregate_id": uuid4(),
        "event_type": test_event_type,
        "payload": {"text": "Event payload content", "salience": 0.85},
        "headers": {"trace_id": "test-trace-123"},
        "attempt_count": 0,
        "created_at": datetime.now(timezone.utc),
    }

    # Pass 1: Relay delivers event
    actions = await outbox_relay.deliver_one(conn, event)
    assert isinstance(actions, list)
    assert len(actions) == 1, "Must have exactly 1 post-commit action for matching webhook"

    # Fire post-commit actions (simulating relay loop after commit)
    for action in actions:
        action()

    # Verify receiver received exactly 1 request with correct headers
    assert len(received_requests) == 1
    req1 = received_requests[0]
    assert req1.url == target_url
    assert req1.headers.get("x-nce-timestamp") is not None
    assert req1.headers.get("x-nce-nonce") is not None

    auth_hdr = req1.headers.get("authorization", "")
    assert auth_hdr.startswith("HMAC-SHA256 ")
    sig = auth_hdr.partition(" ")[2]
    ts = int(req1.headers["x-nce-timestamp"])

    # Verify HMAC signature on received request
    path = urllib.parse.urlparse(target_url).path
    assert verify_hmac(secret, "POST", path, ts, req1.content, sig) is True

    # Pass 2: Relay restart simulation (redelivery of same event_id)
    # The event was already inserted into processed_outbox_events in Pass 1.
    result_pass2 = await outbox_relay.deliver_one(conn, event)
    assert isinstance(result_pass2, outbox_relay._AlreadyProcessed)

    # Receiver received NO additional requests across the restart!
    assert len(received_requests) == 1, (
        f"Receiver was called {len(received_requests)} times; expected exactly 1 across relay restart"
    )


# ---------------------------------------------------------------------------
# Test 7: Multi-Tenant Isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_outbox_relay_multi_tenant_isolation(monkeypatch):
    """Tenant A's webhook must NEVER receive events produced in Tenant B's namespace."""
    ns_tenant_a = uuid4()
    ns_tenant_b = uuid4()
    test_event_type = f"unhandled.event.{uuid4().hex}"

    received_requests: list[httpx.Request] = []

    def mock_transport_handler(request: httpx.Request) -> httpx.Response:
        received_requests.append(request)
        return httpx.Response(200, json={"status": "ok"})

    mock_client = httpx.Client(transport=httpx.MockTransport(mock_transport_handler))
    monkeypatch.setattr("httpx.Client", lambda *args, **kwargs: mock_client)

    tenant_a_webhook = {
        "id": uuid4(),
        "namespace_id": ns_tenant_a,
        "url": "https://tenant-a.example.com/webhook",
        "secret": "secret-a",
        "selectors": ["unhandled.event.*"],
        "is_active": True,
        "description": "Tenant A only",
        "created_at": None,
        "updated_at": None,
    }

    class MockTransaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            return None

    class MockConnection:
        def transaction(self):
            return MockTransaction()

        async def fetch(self, query, *args):
            if "FROM outbound_webhooks" in query:
                ns_queried = args[0]
                if ns_queried == ns_tenant_a:
                    return [tenant_a_webhook]
                return []
            return []

        async def fetchval(self, query, *args):
            return args[0]

    conn = MockConnection()

    # Event produced in Tenant B
    event_b = {
        "id": uuid4(),
        "namespace_id": ns_tenant_b,
        "event_type": test_event_type,
        "attempt_count": 0,
    }

    # Because Tenant B has no webhooks and no in-memory handlers, deliver_one raises OutboxDeliveryError
    # (or drains if uncatalogued), but definitely does NOT fire Tenant A's webhook.
    with pytest.raises(outbox_relay.OutboxDeliveryError):
        await outbox_relay.deliver_one(conn, event_b)

    assert len(received_requests) == 0, (
        "Tenant A webhook received Tenant B event — isolation breach!"
    )
