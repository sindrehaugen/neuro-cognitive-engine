"""Tests for Admin UI Datastore & Connector Status REST Endpoints."""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.requests import Request

# Ensure master key is populated for imports
os.environ.setdefault("NCE_MASTER_KEY", "dev-test-key-32chars-long!!")


@pytest.fixture
def mock_admin_engine():
    """Fixture to provide a fully mocked NCEEngine for the Admin server."""
    engine = MagicMock()

    # Mock Postgres pool & connection
    pg_conn = AsyncMock()
    pg_conn.fetch = AsyncMock(
        return_value=[
            {
                "name": "memories",
                "row_count_estimate": 1000,
                "table_size_bytes": 1024,
                "relation_size_bytes": 512,
            }
        ]
    )
    pg_conn.fetchrow = AsyncMock(return_value={"cnt": 3})

    # Setup acquire async context manager
    acquire_cm = engine.pg_pool.acquire.return_value
    acquire_cm.__aenter__ = AsyncMock(return_value=pg_conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=False)

    # Mock MongoDB Client and stats
    db_mock = AsyncMock()
    db_mock.list_collection_names = AsyncMock(return_value=["memories_store"])
    db_mock.command = AsyncMock(
        return_value={"count": 500, "storageSize": 4096, "indexSizes": {"_id_": 1024}}
    )
    engine.mongo_client.get_database = MagicMock(return_value=db_mock)

    # Mock Redis client and info
    redis_mock = AsyncMock()
    redis_mock.info = AsyncMock(
        return_value={
            "used_memory_human": "5.2M",
            "connected_clients": 2,
            "instantaneous_ops_per_sec": 5,
            "db0": {"keys": 120},
        }
    )
    redis_mock.scan = AsyncMock(return_value=(0, [b"nce:cache:xyz", b"nce:lock:abc"]))
    engine.redis_client = redis_mock

    # Mock MinIO S3 client
    minio_mock = MagicMock()
    b1 = MagicMock()
    b1.name = "nce-audio"
    minio_mock.list_buckets = MagicMock(return_value=[b1])
    obj1 = MagicMock()
    obj1.size = 2048
    minio_mock.list_objects = MagicMock(return_value=[obj1])
    engine.minio_client = minio_mock

    return engine


@pytest.mark.asyncio
async def test_postgres_status_endpoint(mock_admin_engine):
    """Verify that the PostgreSQL status endpoint retrieves and formats estimates."""
    with patch("nce.admin_state.engine", mock_admin_engine):
        from admin_server import api_admin_db_postgres_status

        request = Request(
            {"type": "http", "method": "GET", "path": "/api/admin/db/postgres/status"}
        )
        response = await api_admin_db_postgres_status(request)

        assert response.status_code == 200
        data = json.loads(response.body.decode())
        assert "tables" in data
        assert len(data["tables"]) == 1
        assert data["tables"][0]["name"] == "memories"
        assert data["partition_status"]["runway_months"] == 3


@pytest.mark.asyncio
async def test_mongo_status_endpoint(mock_admin_engine):
    """Verify that the MongoDB status explorer lists collection sizes and indices."""
    with patch("nce.admin_state.engine", mock_admin_engine):
        from admin_server import api_admin_db_mongo_status

        request = Request({"type": "http", "method": "GET", "path": "/api/admin/db/mongo/status"})
        response = await api_admin_db_mongo_status(request)

        assert response.status_code == 200
        data = json.loads(response.body.decode())
        assert "collections" in data
        assert len(data["collections"]) == 1
        assert data["collections"][0]["name"] == "memories_store"
        assert data["collections"][0]["document_count"] == 500


@pytest.mark.asyncio
async def test_redis_status_endpoint(mock_admin_engine):
    """Verify that the Redis status explorer categorizes caches vs locks using SCAN."""
    with patch("nce.admin_state.engine", mock_admin_engine):
        from admin_server import api_admin_db_redis_status

        request = Request({"type": "http", "method": "GET", "path": "/api/admin/db/redis/status"})
        response = await api_admin_db_redis_status(request)

        assert response.status_code == 200
        data = json.loads(response.body.decode())
        assert "info" in data
        assert data["info"]["used_memory_human"] == "5.2M"
        assert len(data["keyspaces"]) == 3
        # Match pattern allocations
        assert data["keyspaces"][0]["count"] == 1  # nce:cache:xyz
        assert data["keyspaces"][1]["count"] == 1  # nce:lock:abc


@pytest.mark.asyncio
async def test_minio_status_endpoint(mock_admin_engine):
    """Verify that the MinIO status explorer parses S3 bucket footprints asynchronously."""
    with patch("nce.admin_state.engine", mock_admin_engine):
        from admin_server import api_admin_db_minio_status

        request = Request({"type": "http", "method": "GET", "path": "/api/admin/db/minio/status"})
        response = await api_admin_db_minio_status(request)

        assert response.status_code == 200
        data = json.loads(response.body.decode())
        assert "buckets" in data
        assert len(data["buckets"]) == 1
        assert data["buckets"][0]["name"] == "nce-audio"
        assert data["buckets"][0]["object_count"] == 1
        assert data["buckets"][0]["total_size_bytes"] == 2048


@pytest.mark.asyncio
async def test_connectors_status_endpoint():
    """Verify that Document Bridges configurations and active models are read from configuration."""
    with patch("nce.admin_handlers._shared.cfg") as mock_cfg:
        mock_cfg.GDRIVE_OAUTH_CLIENT_ID = "gdrive-id"
        mock_cfg.GDRIVE_BRIDGE_TOKEN = "gdrive-token"
        mock_cfg.DROPBOX_OAUTH_CLIENT_ID = ""
        mock_cfg.DROPBOX_BRIDGE_TOKEN = ""
        mock_cfg.AZURE_CLIENT_ID = "azure-id"
        mock_cfg.GRAPH_BRIDGE_TOKEN = ""
        mock_cfg.BRIDGE_CRON_INTERVAL_MINUTES = 45
        mock_cfg.NCE_COGNITIVE_BASE_URL = ""
        mock_cfg.NLI_MODEL_ID = "nli-deberta"

        from admin_server import api_admin_connectors_status

        request = Request({"type": "http", "method": "GET", "path": "/api/admin/connectors/status"})
        response = await api_admin_connectors_status(request)

        assert response.status_code == 200
        data = json.loads(response.body.decode())

        # Verify bridge states match env-var configs
        assert data["bridges"]["google_drive"]["enabled"] is True
        assert data["bridges"]["google_drive"]["token_status"] == "active"
        assert data["bridges"]["dropbox"]["enabled"] is False
        assert data["bridges"]["dropbox"]["token_status"] == "missing"
        assert data["bridges"]["sharepoint"]["enabled"] is True
        assert data["bridges"]["sharepoint"]["mcp_connect_provider"] == "sharepoint"
        assert data["bridges"]["sharepoint"]["token_status"] == "missing"
        assert data["bridges"]["onedrive"]["mcp_connect_provider"] == "sharepoint"
        assert data["bridges"]["onedrive"]["deprecated_ui_key"] is True


@pytest.mark.asyncio
async def test_endpoints_unconnected_fallbacks():
    """Verify that datastore endpoints fail-closed gracefully if database engines are disconnected."""
    with patch("nce.admin_state.engine", None):
        from admin_server import (
            api_admin_db_minio_status,
            api_admin_db_mongo_status,
            api_admin_db_postgres_status,
            api_admin_db_redis_status,
        )

        request = Request(
            {"type": "http", "method": "GET", "path": "/api/admin/db/postgres/status"}
        )

        for handler in (
            api_admin_db_postgres_status,
            api_admin_db_mongo_status,
            api_admin_db_redis_status,
            api_admin_db_minio_status,
        ):
            response = await handler(request)
            assert response.status_code == 503
            data = json.loads(response.body.decode())
            assert "error" in data
            assert "not connected" in data["error"].lower()
