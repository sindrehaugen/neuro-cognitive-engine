"""Regression tests for per-bridge webhook clientState / channel-token validation.

Bug fix/webhook-clientstate-per-bridge: the Graph (SharePoint) and Drive
webhook handlers used to validate inbound notifications against the *global*
``GRAPH_CLIENT_STATE`` / ``DRIVE_CHANNEL_TOKEN`` env vars, but subscriptions are
registered with a *per-bridge* random secret stored in
``bridge_subscriptions.client_state``. The receiver must now resolve the secret
from the matching ACTIVE subscription row (looked up by the provider's external
subscription / channel id) and compare with ``hmac.compare_digest``, failing
closed on any missing/unknown subscription or DB error.

Two layers of coverage:

* ``Test*Mocked`` — always run; stub ``wh._fetch_bridge_subscription`` so the
  assertion is purely "handler compares against the resolved row.client_state,
  not the env var", including fail-closed paths. (Acceptable fallback per spec.)
* ``Test*Integration`` — uses the real ``pg_pool`` fixture; inserts actual
  ``bridge_subscriptions`` rows and points the receiver's pool accessor at the
  integration pool. Skips automatically when Postgres is unavailable.
"""

from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

os.environ.setdefault("DROPBOX_APP_SECRET", "test_dropbox_secret")

import nce.webhook_receiver.main as wh
from nce import bridge_repo

KNOWN_SHAREPOINT_STATE = "sharepoint-per-bridge-secret-abc123"
KNOWN_DRIVE_TOKEN = "gdrive-per-bridge-token-xyz789"
SHAREPOINT_SUB_ID = "graph-subscription-id-1"
DRIVE_CHANNEL_ID = "drive-watch-channel-id-1"


@pytest.fixture
def client():
    with TestClient(wh.app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def _webhook_test_isolation(monkeypatch):
    """Isolate from shared-process Redis state (dedup + rate keys)."""
    monkeypatch.setattr(wh, "_ip_windows", {}, raising=False)
    monkeypatch.setattr(wh, "_allow_webhook_request_redis", lambda *_a, **_k: None)
    wh._redis_client.cache_clear()
    # Never actually enqueue to Redis/RQ in these tests.
    monkeypatch.setattr(wh, "enqueue_process_bridge_event", lambda *_a, **_k: "job-test")


# ---------------------------------------------------------------------------
# Mock-based: assert comparison is against the resolved row, not the env var.
# ---------------------------------------------------------------------------


def _stub_lookup(monkeypatch, *, rows: dict[tuple[str, str], dict | None]):
    """Stub wh._fetch_bridge_subscription with an explicit (provider, id) -> row map."""

    async def _fake(provider, subscription_id):
        return rows.get((provider, subscription_id))

    monkeypatch.setattr(wh, "_fetch_bridge_subscription", _fake)


class TestGraphMocked:
    def test_correct_per_bridge_client_state_is_queued(self, client, monkeypatch):
        _stub_lookup(
            monkeypatch,
            rows={
                ("sharepoint", SHAREPOINT_SUB_ID): {
                    "client_state": KNOWN_SHAREPOINT_STATE,
                    "subscription_id": SHAREPOINT_SUB_ID,
                }
            },
        )
        payload = {
            "value": [
                {
                    "subscriptionId": SHAREPOINT_SUB_ID,
                    "clientState": KNOWN_SHAREPOINT_STATE,
                    "resource": "/sites/site-1/drives/drive-1/root",
                    "changeType": "updated",
                }
            ]
        }
        resp = client.post("/webhooks/graph", json=payload)
        assert resp.status_code == 200
        assert resp.json()["status"] == "queued"

    def test_wrong_secret_is_rejected_403(self, client, monkeypatch):
        _stub_lookup(
            monkeypatch,
            rows={
                ("sharepoint", SHAREPOINT_SUB_ID): {
                    "client_state": KNOWN_SHAREPOINT_STATE,
                    "subscription_id": SHAREPOINT_SUB_ID,
                }
            },
        )
        payload = {
            "value": [
                {
                    "subscriptionId": SHAREPOINT_SUB_ID,
                    "clientState": "attacker-guessed-state",
                    "resource": "/sites/site-1/drives/drive-1/root",
                    "changeType": "updated",
                }
            ]
        }
        resp = client.post("/webhooks/graph", json=payload)
        assert resp.status_code == 403

    def test_unknown_subscription_is_rejected_403(self, client, monkeypatch):
        # Lookup returns None for any id -> fail closed.
        _stub_lookup(monkeypatch, rows={})
        payload = {
            "value": [
                {
                    "subscriptionId": "no-such-subscription",
                    "clientState": KNOWN_SHAREPOINT_STATE,
                    "resource": "/sites/site-1/drives/drive-1/root",
                    "changeType": "updated",
                }
            ]
        }
        resp = client.post("/webhooks/graph", json=payload)
        assert resp.status_code == 403

    def test_global_env_state_no_longer_accepted(self, client, monkeypatch):
        """Even if the old global secret is set, it must NOT authorize a bridge."""
        monkeypatch.setenv("GRAPH_CLIENT_STATE", "old-global-secret")
        _stub_lookup(
            monkeypatch,
            rows={
                ("sharepoint", SHAREPOINT_SUB_ID): {
                    "client_state": KNOWN_SHAREPOINT_STATE,
                    "subscription_id": SHAREPOINT_SUB_ID,
                }
            },
        )
        payload = {
            "value": [
                {
                    "subscriptionId": SHAREPOINT_SUB_ID,
                    "clientState": "old-global-secret",
                    "resource": "/sites/site-1/drives/drive-1/root",
                    "changeType": "updated",
                }
            ]
        }
        resp = client.post("/webhooks/graph", json=payload)
        assert resp.status_code == 403

    def test_db_error_fails_closed_403(self, client, monkeypatch):
        async def _boom(_provider, _sub):
            raise RuntimeError("pg pool exhausted")

        monkeypatch.setattr(wh, "_fetch_bridge_subscription", _boom)
        payload = {
            "value": [
                {
                    "subscriptionId": SHAREPOINT_SUB_ID,
                    "clientState": KNOWN_SHAREPOINT_STATE,
                    "resource": "/sites/site-1/drives/drive-1/root",
                    "changeType": "updated",
                }
            ]
        }
        resp = client.post("/webhooks/graph", json=payload)
        assert resp.status_code == 403


class TestDriveMocked:
    def test_correct_per_bridge_token_is_queued(self, client, monkeypatch):
        _stub_lookup(
            monkeypatch,
            rows={
                ("gdrive", DRIVE_CHANNEL_ID): {
                    "client_state": KNOWN_DRIVE_TOKEN,
                    "subscription_id": DRIVE_CHANNEL_ID,
                }
            },
        )
        resp = client.post(
            "/webhooks/drive",
            headers={
                "X-Goog-Channel-Token": KNOWN_DRIVE_TOKEN,
                "X-Goog-Resource-State": "update",
                "X-Goog-Channel-Id": DRIVE_CHANNEL_ID,
                "X-Goog-Resource-Id": "res-1",
                "X-Goog-Message-Number": "2",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "queued"

    def test_wrong_token_is_rejected_403(self, client, monkeypatch):
        _stub_lookup(
            monkeypatch,
            rows={
                ("gdrive", DRIVE_CHANNEL_ID): {
                    "client_state": KNOWN_DRIVE_TOKEN,
                    "subscription_id": DRIVE_CHANNEL_ID,
                }
            },
        )
        resp = client.post(
            "/webhooks/drive",
            headers={
                "X-Goog-Channel-Token": "attacker-token",
                "X-Goog-Resource-State": "update",
                "X-Goog-Channel-Id": DRIVE_CHANNEL_ID,
            },
        )
        assert resp.status_code == 403

    def test_unknown_channel_is_rejected_403(self, client, monkeypatch):
        _stub_lookup(monkeypatch, rows={})
        resp = client.post(
            "/webhooks/drive",
            headers={
                "X-Goog-Channel-Token": KNOWN_DRIVE_TOKEN,
                "X-Goog-Resource-State": "update",
                "X-Goog-Channel-Id": "no-such-channel",
            },
        )
        assert resp.status_code == 403

    def test_sync_handshake_validates_token_first(self, client, monkeypatch):
        """sync early-return still applies, but only after token validation."""
        _stub_lookup(
            monkeypatch,
            rows={
                ("gdrive", DRIVE_CHANNEL_ID): {
                    "client_state": KNOWN_DRIVE_TOKEN,
                    "subscription_id": DRIVE_CHANNEL_ID,
                }
            },
        )
        # Valid token + sync => acknowledged.
        ok = client.post(
            "/webhooks/drive",
            headers={
                "X-Goog-Channel-Token": KNOWN_DRIVE_TOKEN,
                "X-Goog-Resource-State": "sync",
                "X-Goog-Channel-Id": DRIVE_CHANNEL_ID,
            },
        )
        assert ok.status_code == 200
        assert ok.json()["status"] == "acknowledged"

        # Wrong token + sync => 403 (token checked before the sync early-return).
        bad = client.post(
            "/webhooks/drive",
            headers={
                "X-Goog-Channel-Token": "attacker-token",
                "X-Goog-Resource-State": "sync",
                "X-Goog-Channel-Id": DRIVE_CHANNEL_ID,
            },
        )
        assert bad.status_code == 403

    def test_db_error_fails_closed_403(self, client, monkeypatch):
        async def _boom(_provider, _channel):
            raise RuntimeError("pg pool exhausted")

        monkeypatch.setattr(wh, "_fetch_bridge_subscription", _boom)
        resp = client.post(
            "/webhooks/drive",
            headers={
                "X-Goog-Channel-Token": KNOWN_DRIVE_TOKEN,
                "X-Goog-Resource-State": "update",
                "X-Goog-Channel-Id": DRIVE_CHANNEL_ID,
            },
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Integration: real bridge_subscriptions rows + real receiver pool accessor.
# Skips when Postgres is unavailable (pg_pool fixture handles the skip).
# ---------------------------------------------------------------------------


@pytest.fixture
def _patch_receiver_pool(pg_pool, monkeypatch):
    """Point the receiver's lazy pool accessor at the integration pool."""

    async def _get_pool():
        return pg_pool

    monkeypatch.setattr(wh, "_get_pg_pool", _get_pool)
    return pg_pool


@pytest_asyncio.fixture
async def _seed_bridges(_patch_receiver_pool, make_namespace):
    """Insert one ACTIVE sharepoint + one ACTIVE gdrive bridge with known secrets."""
    pool = _patch_receiver_pool
    ns = await make_namespace()
    user_id = f"webhook-test-user-{uuid.uuid4().hex}"
    async with pool.acquire() as conn:
        sp_id = await bridge_repo.insert_subscription(
            conn,
            user_id=user_id,
            namespace_id=ns,
            provider="sharepoint",
            resource_id="site-1|drive-1",
            status="ACTIVE",
            subscription_id=SHAREPOINT_SUB_ID,
            client_state=KNOWN_SHAREPOINT_STATE,
        )
        gd_id = await bridge_repo.insert_subscription(
            conn,
            user_id=user_id,
            namespace_id=ns,
            provider="gdrive",
            resource_id="res-1",
            status="ACTIVE",
            subscription_id=DRIVE_CHANNEL_ID,
            client_state=KNOWN_DRIVE_TOKEN,
        )
    yield
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM bridge_subscriptions WHERE id = ANY($1::uuid[])",
            [sp_id, gd_id],
        )


@pytest.mark.integration
@pytest.mark.usefixtures("_seed_bridges")
def test_graph_real_db_per_bridge(client):
    good = client.post(
        "/webhooks/graph",
        json={
            "value": [
                {
                    "subscriptionId": SHAREPOINT_SUB_ID,
                    "clientState": KNOWN_SHAREPOINT_STATE,
                    "resource": "/sites/site-1/drives/drive-1/root",
                    "changeType": "updated",
                }
            ]
        },
    )
    assert good.status_code == 200
    assert good.json()["status"] == "queued"

    bad = client.post(
        "/webhooks/graph",
        json={
            "value": [
                {
                    "subscriptionId": SHAREPOINT_SUB_ID,
                    "clientState": "wrong",
                    "resource": "/sites/site-1/drives/drive-1/root",
                    "changeType": "updated",
                }
            ]
        },
    )
    assert bad.status_code == 403

    unknown = client.post(
        "/webhooks/graph",
        json={
            "value": [
                {
                    "subscriptionId": "missing-sub",
                    "clientState": KNOWN_SHAREPOINT_STATE,
                    "resource": "/sites/site-1/drives/drive-1/root",
                    "changeType": "updated",
                }
            ]
        },
    )
    assert unknown.status_code == 403


@pytest.mark.integration
@pytest.mark.usefixtures("_seed_bridges")
def test_drive_real_db_per_bridge(client):
    good = client.post(
        "/webhooks/drive",
        headers={
            "X-Goog-Channel-Token": KNOWN_DRIVE_TOKEN,
            "X-Goog-Resource-State": "update",
            "X-Goog-Channel-Id": DRIVE_CHANNEL_ID,
            "X-Goog-Resource-Id": "res-1",
        },
    )
    assert good.status_code == 200
    assert good.json()["status"] == "queued"

    bad = client.post(
        "/webhooks/drive",
        headers={
            "X-Goog-Channel-Token": "wrong",
            "X-Goog-Resource-State": "update",
            "X-Goog-Channel-Id": DRIVE_CHANNEL_ID,
        },
    )
    assert bad.status_code == 403

    unknown = client.post(
        "/webhooks/drive",
        headers={
            "X-Goog-Channel-Token": KNOWN_DRIVE_TOKEN,
            "X-Goog-Resource-State": "update",
            "X-Goog-Channel-Id": "missing-channel",
        },
    )
    assert unknown.status_code == 403
