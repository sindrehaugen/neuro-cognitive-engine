"""Tests for Datastore Connection Parameters Config REST Endpoints."""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import pytest
from starlette.requests import Request

os.environ.setdefault("NCE_MASTER_KEY", "dev-test-key-32chars-long!!")


@pytest.mark.asyncio
async def test_datastores_status_endpoint():
    """Verify datastore status fetches correctly and masks secrets."""
    with patch("nce.admin_handlers._shared.cfg") as mock_cfg:
        mock_cfg.PG_DSN = "postgresql://user:password@localhost/nce"
        mock_cfg.DB_READ_URL = "postgresql://user:password@localhost/nce_replica"
        mock_cfg.DB_WRITE_URL = "postgresql://user:password@localhost/nce"
        mock_cfg.PG_MIN_POOL = 3
        mock_cfg.PG_MAX_POOL = 15
        mock_cfg.MONGO_URI = "mongodb://user:pwd@host:27017/nce"
        mock_cfg.REDIS_URL = "redis://:foobar@host:6379/0"
        mock_cfg.REDIS_TTL = 3600
        mock_cfg.REDIS_MAX_CONNECTIONS = 50
        mock_cfg.MINIO_ENDPOINT = "localhost:9000"
        mock_cfg.MINIO_ACCESS_KEY = "minio_user"
        mock_cfg.MINIO_SECRET_KEY = "minio_secret_password"
        mock_cfg.MINIO_SECURE = False

        from admin_server import api_admin_datastores_status

        request = Request({"type": "http", "method": "GET", "path": "/api/admin/datastores/status"})
        response = await api_admin_datastores_status(request)

        assert response.status_code == 200
        data = json.loads(response.body.decode())

        assert "postgres" in data
        assert "mongodb" in data
        assert "redis" in data
        assert "minio" in data

        # Check postgres fields are populated
        assert data["postgres"]["pg_dsn"] == "postgresql://user:••••••••@localhost/nce"
        assert data["postgres"]["pg_min_pool"] == 3

        # Check secrets are masked and reported properly (has_secret_key is True, actual secret is not leaked)
        assert data["minio"]["has_secret_key"] is True


@pytest.mark.asyncio
async def test_datastores_save_endpoint_with_secret_masked():
    """Verify saving connection parameters respects masked values and writes env file."""
    mock_engine = MagicMock()
    mock_update_dotenv = MagicMock()
    with (
        patch("nce.admin_state.engine", mock_engine),
        patch("nce.admin_handlers._shared.cfg") as mock_cfg,
        patch("nce.admin_handlers._shared.update_dotenv", mock_update_dotenv),
        patch("nce.admin_handlers.fleet.update_dotenv", mock_update_dotenv),
    ):
        mock_cfg.PG_DSN = "postgresql://user:password@localhost/nce"
        mock_cfg.DB_READ_URL = "postgresql://user:password@localhost/nce_replica"
        mock_cfg.DB_WRITE_URL = "postgresql://user:password@localhost/nce"
        mock_cfg.PG_MIN_POOL = 3
        mock_cfg.PG_MAX_POOL = 15
        mock_cfg.MONGO_URI = "mongodb://user:pwd@host:27017/nce"
        mock_cfg.REDIS_URL = "redis://:foobar@host:6379/0"
        mock_cfg.REDIS_TTL = 3600
        mock_cfg.REDIS_MAX_CONNECTIONS = 50
        mock_cfg.MINIO_ENDPOINT = "localhost:9000"
        mock_cfg.MINIO_ACCESS_KEY = "minio_user"
        mock_cfg.MINIO_SECRET_KEY = "real_password"
        mock_cfg.MINIO_SECURE = False
        mock_cfg.NCE_ALLOW_ADMIN_DOTENV_PERSIST = True

        from admin_server import api_admin_datastores_save

        payload = {
            "minio": {
                "minio_endpoint": "s3.amazonaws.com",
                "minio_access_key": "aws_key",
                "minio_secret_key": "••••••••",  # Masked! Should not update the actual password.
                "minio_secure": True,
            }
        }

        # Setup standard Starlette request mock
        async def receive():
            return {"type": "http.request", "body": json.dumps(payload).encode()}

        request = Request(
            {"type": "http", "method": "POST", "path": "/api/admin/datastores/save"},
            receive=receive,
        )
        response = await api_admin_datastores_save(request)

        assert response.status_code == 200
        data = json.loads(response.body.decode())
        assert data["status"] == "success"

        mock_update_dotenv.assert_called_once()
        written = mock_update_dotenv.call_args[0][0]
        assert written["MINIO_ENDPOINT"] == "s3.amazonaws.com"
        assert written["MINIO_ACCESS_KEY"] == "aws_key"
        assert written["MINIO_SECURE"] == "true"
        assert "MINIO_SECRET_KEY" not in written
