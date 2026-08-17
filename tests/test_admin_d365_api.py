"""Unit tests for Dynamics 365 Admin REST API endpoints."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.requests import Request

from nce.admin_handlers.d365 import (
    api_admin_d365_config,
    api_admin_d365_integrations,
    api_admin_d365_namespace_update,
    api_admin_d365_sync_now,
)


@pytest.mark.asyncio
async def test_api_admin_d365_config() -> None:
    """Verify D365 configuration endpoint returns the expected fields."""
    request = Request({"type": "http", "method": "GET", "path": "/api/admin/d365/config"})
    resp = await api_admin_d365_config(request)
    assert resp.status_code == 200
    data = json.loads(resp.body.decode())
    assert "enabled" in data
    assert "org_url" in data
    assert "high_priority_salience_boost" in data


@pytest.mark.asyncio
async def test_api_admin_d365_integrations() -> None:
    """Verify listing integrations queries the database with paging."""
    mock_engine = MagicMock()
    mock_conn = AsyncMock()
    mock_engine.pg_pool.acquire.return_value.__aenter__.return_value = mock_conn

    now = datetime.now(timezone.utc)
    mock_conn.fetchrow.return_value = {"total": 1}
    mock_conn.fetch.return_value = [
        {
            "id": uuid.uuid4(),
            "namespace_id": uuid.uuid4(),
            "namespace_slug": "test-ns",
            "org_url": "https://test.crm.dynamics.com",
            "status": "ACTIVE",
            "last_sync_at": None,
            "last_sync_stats": {},
            "created_at": now,
            "updated_at": now,
            "d365_enabled": True,
        }
    ]

    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/admin/d365/integrations",
            "query_string": b"page=1&limit=10",
        }
    )

    with patch("nce.admin_state.engine", mock_engine):
        resp = await api_admin_d365_integrations(request)
        assert resp.status_code == 200
        data = json.loads(resp.body.decode())
        assert data["total"] == 1
        assert len(data["items"]) == 1
        assert data["items"][0]["status"] == "ACTIVE"


@pytest.mark.asyncio
async def test_api_admin_d365_sync_now_success() -> None:
    """Verify triggering manual sync invokes client and engine correctly."""
    ns_id = uuid.uuid4()
    body = {"namespace_id": str(ns_id)}

    async def receive():
        return {"type": "http.request", "body": json.dumps(body).encode()}

    request = Request(
        {"type": "http", "method": "POST", "path": "/api/admin/d365/sync"},
        receive=receive,
    )

    mock_engine = MagicMock()
    mock_engine.redis_client = AsyncMock()
    mock_conn = AsyncMock()
    mock_engine.pg_pool.acquire.return_value.__aenter__.return_value = mock_conn
    mock_txn = MagicMock()
    mock_txn.__aenter__ = AsyncMock(return_value=mock_txn)
    mock_txn.__aexit__ = AsyncMock(return_value=False)
    mock_conn.transaction = MagicMock(return_value=mock_txn)

    token_mgr_mock = AsyncMock()
    token_mgr_mock.get_token.return_value = "token123"
    token_mgr_mock.get_access_token.return_value = "token123"

    client_mock = MagicMock()
    sync_engine_mock = AsyncMock()
    sync_engine_mock.run_full_sync.return_value = {"upserted": 5}

    with (
        patch("nce.admin_state.engine", mock_engine),
        patch("nce.config.cfg.NCE_D365_ENABLED", True),
        patch(
            "nce.vertical_modules.dynamics365.auth.DataverseTokenManager",
            return_value=token_mgr_mock,
        ),
        patch("nce.vertical_modules.dynamics365.client.DataverseClient", return_value=client_mock),
        patch(
            "nce.vertical_modules.dynamics365.sync.DataverseSyncEngine",
            return_value=sync_engine_mock,
        ),
    ):
        resp = await api_admin_d365_sync_now(request)
        assert resp.status_code == 200
        data = json.loads(resp.body.decode())
        assert data["status"] == "ok"
        assert data["stats"]["upserted"] == 5
        assert mock_conn.execute.await_count >= 1


@pytest.mark.asyncio
async def test_api_admin_d365_namespace_update() -> None:
    """Verify namespace configuration update merges the metadata JSON correctly."""
    ns_id = uuid.uuid4()
    body = {"enabled": True}

    async def receive():
        return {"type": "http.request", "body": json.dumps(body).encode()}

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": f"/api/admin/d365/namespace/{ns_id}/d365-enabled",
            "path_params": {"ns_id": str(ns_id)},
        },
        receive=receive,
    )

    mock_engine = MagicMock()
    mock_conn = AsyncMock()
    mock_engine.pg_pool.acquire.return_value.__aenter__.return_value = mock_conn

    mock_conn.fetchrow.return_value = {"id": ns_id, "metadata": {"existing_key": 42}}

    with patch("nce.admin_state.engine", mock_engine):
        resp = await api_admin_d365_namespace_update(request)
        assert resp.status_code == 200
        data = json.loads(resp.body.decode())
        assert data["d365_enabled"] is True
        assert data["namespace_id"] == str(ns_id)

        # Check metadata update includes the D365 block merged
        args = mock_conn.execute.call_args[0]
        meta_dict = json.loads(args[1])
        assert meta_dict["existing_key"] == 42
        assert meta_dict["d365"]["enabled"] is True
