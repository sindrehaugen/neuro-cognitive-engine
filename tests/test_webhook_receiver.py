import hashlib
import hmac
import os
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("DROPBOX_APP_SECRET", "test_dropbox_secret")

import nce.webhook_receiver.main as wh
from nce.webhook_receiver.main import DROPBOX_APP_SECRET

# Per-bridge secrets (formerly the global GRAPH_CLIENT_STATE / DRIVE_CHANNEL_TOKEN
# env vars) now live in bridge_subscriptions.client_state and are resolved per
# request via wh._fetch_bridge_subscription. Tests stub that lookup.
GRAPH_CLIENT_STATE = "per-bridge-graph-state"
DRIVE_CHANNEL_TOKEN = "per-bridge-drive-token"


def _stub_bridge_lookup(monkeypatch, *, client_state, subscription_id):
    """Stub wh._fetch_bridge_subscription to return a row only for the given id."""

    async def _fake(provider, sub_id):
        if sub_id == subscription_id:
            return {"client_state": client_state, "subscription_id": sub_id}
        return None

    monkeypatch.setattr(wh, "_fetch_bridge_subscription", _fake)


@pytest.fixture
def client():
    """Fresh TestClient per test — avoids stale middleware state in long pytest runs."""
    with TestClient(wh.app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def _webhook_test_isolation(monkeypatch):
    """Isolate webhook tests from shared-process Redis state (dedup + rate keys)."""
    monkeypatch.setattr(wh, "_ip_windows", {}, raising=False)
    monkeypatch.setattr(wh, "_allow_webhook_request_redis", lambda *_a, **_k: None)
    wh._redis_client.cache_clear()


def _stub_enqueue(monkeypatch, job_id: str) -> MagicMock:
    mock = MagicMock(return_value=job_id)
    monkeypatch.setattr(wh, "enqueue_process_bridge_event", mock)
    return mock


def test_dropbox_challenge(client):
    response = client.get("/webhooks/dropbox?challenge=test_challenge_string")
    assert response.status_code == 200
    assert response.text == "test_challenge_string"


def test_dropbox_webhook_valid_signature(client, monkeypatch):
    mock_enqueue = _stub_enqueue(monkeypatch, "job-db-1")
    body = b'{"list_folder": {"accounts": ["dbid:123"]}}'
    signature = hmac.new(DROPBOX_APP_SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()

    response = client.post(
        "/webhooks/dropbox",
        content=body,
        headers={"X-Dropbox-Signature": signature},
    )
    assert response.status_code == 200
    assert response.json() == {"status": "queued", "job_id": "job-db-1"}
    mock_enqueue.assert_called_once()
    args, _kwargs = mock_enqueue.call_args
    assert args[0] == "dropbox"
    assert args[1]["list_folder"]["accounts"] == ["dbid:123"]


def test_dropbox_webhook_invalid_signature(client):
    body = b'{"list_folder": {"accounts": ["dbid:123"]}}'

    response = client.post(
        "/webhooks/dropbox",
        content=body,
        headers={"X-Dropbox-Signature": "invalid_signature"},
    )
    assert response.status_code == 403
    assert "Invalid signature" in response.json()["detail"]


def test_dropbox_webhook_missing_signature(client):
    body = b'{"list_folder": {"accounts": ["dbid:123"]}}'

    response = client.post(
        "/webhooks/dropbox",
        content=body,
    )
    assert response.status_code == 403
    assert "Missing X-Dropbox-Signature" in response.json()["detail"]


def test_graph_webhook_challenge(client):
    response = client.post("/webhooks/graph?validationToken=test_token")
    assert response.status_code == 200
    assert response.text == "test_token"


def test_graph_webhook_valid_client_state(client, monkeypatch):
    mock_enqueue = _stub_enqueue(monkeypatch, "job-sp-1")
    _stub_bridge_lookup(monkeypatch, client_state=GRAPH_CLIENT_STATE, subscription_id="sub-1")
    payload = {
        "value": [
            {
                "subscriptionId": "sub-1",
                "clientState": GRAPH_CLIENT_STATE,
                "resource": "/users/user/drive/root",
                "changeType": "updated",
            }
        ]
    }
    response = client.post("/webhooks/graph", json=payload)
    assert response.status_code == 200
    assert response.json() == {"status": "queued", "job_id": "job-sp-1"}
    mock_enqueue.assert_called_once()
    call_provider, call_payload = mock_enqueue.call_args[0]
    assert call_provider == "sharepoint"
    assert len(call_payload["notifications"]) == 1


def test_graph_webhook_invalid_client_state(client, monkeypatch):
    _stub_bridge_lookup(monkeypatch, client_state=GRAPH_CLIENT_STATE, subscription_id="sub-1")
    payload = {
        "value": [
            {
                "subscriptionId": "sub-1",
                "clientState": "wrong_state",
                "resource": "Users/user/drive/root",
                "changeType": "updated",
            }
        ]
    }
    response = client.post("/webhooks/graph", json=payload)
    assert response.status_code == 403
    assert "Invalid clientState" in response.json()["detail"]


def test_drive_webhook_valid(client, monkeypatch):
    mock_enqueue = _stub_enqueue(monkeypatch, "job-gd-1")
    _stub_bridge_lookup(monkeypatch, client_state=DRIVE_CHANNEL_TOKEN, subscription_id="chan-abc")
    response = client.post(
        "/webhooks/drive",
        headers={
            "X-Goog-Channel-Token": DRIVE_CHANNEL_TOKEN,
            "X-Goog-Resource-State": "update",
            "X-Goog-Channel-Id": "chan-abc",
            "X-Goog-Resource-Id": "res-xyz",
            "X-Goog-Message-Number": "1",
        },
    )
    assert response.status_code == 200
    assert response.json() == {"status": "queued", "job_id": "job-gd-1"}
    mock_enqueue.assert_called_once()
    prov, payload = mock_enqueue.call_args[0]
    assert prov == "gdrive"
    assert payload["channel_id"] == "chan-abc"
    assert payload["resource_state"] == "update"


def test_drive_webhook_sync_no_enqueue(client, monkeypatch):
    mock_enqueue = _stub_enqueue(monkeypatch, "unused")
    _stub_bridge_lookup(monkeypatch, client_state=DRIVE_CHANNEL_TOKEN, subscription_id="chan-sync")
    response = client.post(
        "/webhooks/drive",
        headers={
            "X-Goog-Channel-Token": DRIVE_CHANNEL_TOKEN,
            "X-Goog-Resource-State": "sync",
            "X-Goog-Channel-Id": "chan-sync",
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] == "acknowledged"
    mock_enqueue.assert_not_called()


def test_drive_webhook_invalid_token(client, monkeypatch):
    _stub_bridge_lookup(monkeypatch, client_state=DRIVE_CHANNEL_TOKEN, subscription_id="chan-abc")
    response = client.post(
        "/webhooks/drive",
        headers={
            "X-Goog-Channel-Token": "wrong_token",
            "X-Goog-Resource-State": "update",
            "X-Goog-Channel-Id": "chan-abc",
        },
    )
    assert response.status_code == 403


def test_drive_webhook_missing_state(client, monkeypatch):
    _stub_bridge_lookup(monkeypatch, client_state=DRIVE_CHANNEL_TOKEN, subscription_id="chan-abc")
    response = client.post(
        "/webhooks/drive",
        headers={
            "X-Goog-Channel-Token": DRIVE_CHANNEL_TOKEN,
            "X-Goog-Channel-Id": "chan-abc",
        },
    )
    assert response.status_code == 400


# --- SSRF guard: Graph webhook resource URL validation ---


def test_graph_webhook_valid_sites_resource(client, monkeypatch):
    """Valid Graph /sites/ resource path must be accepted."""
    _stub_enqueue(monkeypatch, "job-sp-2")
    _stub_bridge_lookup(monkeypatch, client_state=GRAPH_CLIENT_STATE, subscription_id="sub-1")
    payload = {
        "value": [
            {
                "subscriptionId": "sub-1",
                "clientState": GRAPH_CLIENT_STATE,
                "resource": "/sites/abc-123/drives/def-456/root",
                "changeType": "updated",
            }
        ]
    }
    response = client.post("/webhooks/graph", json=payload)
    assert response.status_code == 200
    assert response.json()["status"] == "queued"


def test_graph_webhook_valid_drives_resource(client, monkeypatch):
    """Valid Graph /drives/ resource path must be accepted."""
    _stub_enqueue(monkeypatch, "job-sp-3")
    _stub_bridge_lookup(monkeypatch, client_state=GRAPH_CLIENT_STATE, subscription_id="sub-1")
    payload = {
        "value": [
            {
                "subscriptionId": "sub-1",
                "clientState": GRAPH_CLIENT_STATE,
                "resource": "/drives/drive-id/root/delta",
                "changeType": "updated",
            }
        ]
    }
    response = client.post("/webhooks/graph", json=payload)
    assert response.status_code == 200


def test_graph_webhook_rejects_internal_resource(client, monkeypatch):
    """SSRF guard must reject a resource path pointing to internal services."""
    _stub_bridge_lookup(monkeypatch, client_state=GRAPH_CLIENT_STATE, subscription_id="sub-1")
    payload = {
        "value": [
            {
                "subscriptionId": "sub-1",
                "clientState": GRAPH_CLIENT_STATE,
                "resource": "/internal/admin/panel",
                "changeType": "updated",
            }
        ]
    }
    response = client.post("/webhooks/graph", json=payload)
    assert response.status_code == 400
    assert "Invalid resource URL" in response.json()["detail"]


def test_graph_webhook_rejects_path_traversal_resource(client, monkeypatch):
    """SSRF guard must reject path traversal in resource URLs."""
    _stub_bridge_lookup(monkeypatch, client_state=GRAPH_CLIENT_STATE, subscription_id="sub-1")
    payload = {
        "value": [
            {
                "subscriptionId": "sub-1",
                "clientState": GRAPH_CLIENT_STATE,
                "resource": "/../../internal/secret",
                "changeType": "updated",
            }
        ]
    }
    response = client.post("/webhooks/graph", json=payload)
    assert response.status_code == 400
    assert "Invalid resource URL" in response.json()["detail"]


def test_graph_webhook_rejects_http_resource(client, monkeypatch):
    """SSRF guard must reject non-HTTPS fully-qualified resource URLs."""
    _stub_bridge_lookup(monkeypatch, client_state=GRAPH_CLIENT_STATE, subscription_id="sub-1")
    payload = {
        "value": [
            {
                "subscriptionId": "sub-1",
                "clientState": GRAPH_CLIENT_STATE,
                "resource": "http://evil.internal/hack",
                "changeType": "updated",
            }
        ]
    }
    response = client.post("/webhooks/graph", json=payload)
    assert response.status_code == 400


def test_dropbox_webhook_rejects_oversize_body(client, monkeypatch):
    monkeypatch.setattr(wh, "_MAX_BODY_BYTES", 32, raising=False)
    body = b"x" * 64
    signature = hmac.new(DROPBOX_APP_SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()
    response = client.post(
        "/webhooks/dropbox",
        content=body,
        headers={
            "X-Dropbox-Signature": signature,
            "Content-Length": str(len(body)),
        },
    )
    assert response.status_code == 413


def test_dropbox_webhook_dedup_skips_duplicate(client, monkeypatch):
    """Second identical Dropbox payload must not enqueue twice."""
    mock_q = MagicMock()
    mock_q.enqueue.return_value = MagicMock(id="job-db-dedup")
    monkeypatch.setattr(wh, "get_priority_queue", lambda *_a, **_k: mock_q)
    monkeypatch.setattr(wh, "_claim_dedup", MagicMock(side_effect=[True, False]))

    body = b'{"list_folder": {"accounts": ["dbid:dedup"]}}'
    signature = hmac.new(DROPBOX_APP_SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()
    headers = {"X-Dropbox-Signature": signature}

    first = client.post("/webhooks/dropbox", content=body, headers=headers)
    assert first.status_code == 200
    assert first.json()["job_id"] == "job-db-dedup"

    second = client.post("/webhooks/dropbox", content=body, headers=headers)
    assert second.status_code == 200
    assert second.json()["job_id"] == "dedup-skipped"
    assert mock_q.enqueue.call_count == 1


def test_claim_dedup_fail_closed_when_redis_unavailable(monkeypatch):
    monkeypatch.setattr(wh, "_DEDUP_FAIL_OPEN", False, raising=False)
    wh._redis_client.cache_clear()
    mock_redis = MagicMock()
    mock_redis.set.side_effect = ConnectionError("redis down")
    monkeypatch.setattr(wh, "_redis_client", lambda: mock_redis)
    assert wh._claim_dedup("nce:webhook:dedup:test-key") is False


def test_claim_dedup_fail_open_when_configured(monkeypatch):
    monkeypatch.setattr(wh, "_DEDUP_FAIL_OPEN", True, raising=False)
    wh._redis_client.cache_clear()
    mock_redis = MagicMock()
    mock_redis.set.side_effect = ConnectionError("redis down")
    monkeypatch.setattr(wh, "_redis_client", lambda: mock_redis)
    assert wh._claim_dedup("nce:webhook:dedup:test-key") is True


def test_webhook_rate_limit_returns_429(client, monkeypatch):
    monkeypatch.setattr(wh, "_RATE_LIMIT", 1, raising=False)
    monkeypatch.setattr(wh, "_RATE_PERIOD_S", 60, raising=False)
    monkeypatch.setattr(wh, "_ip_windows", {}, raising=False)
    monkeypatch.setattr(
        wh,
        "_allow_webhook_request",
        lambda ip, path: wh._allow_webhook_request_memory(ip, path),
    )

    first = client.get("/webhooks/dropbox?challenge=one")
    assert first.status_code == 200
    second = client.get("/webhooks/dropbox?challenge=two")
    assert second.status_code == 429
