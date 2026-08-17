import hashlib
import hmac as _hmac
import time
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient

from nce.admin_app import app
from nce.config import cfg


@pytest.fixture(autouse=True)
def bypass_lifespan():
    """Bypass Starlette app lifespan to avoid real DB connections at startup."""

    @asynccontextmanager
    async def dummy_lifespan(app):
        yield

    original_lifespan = app.router.lifespan_context
    app.router.lifespan_context = dummy_lifespan
    yield
    app.router.lifespan_context = original_lifespan


def _make_signature(key: str, method: str, path: str, timestamp: int, body: bytes = b"") -> str:
    parts = [method.upper(), path, str(timestamp)]
    if body:
        parts.append(hashlib.sha256(body).hexdigest())
    canonical = "\n".join(parts)
    return _hmac.new(key.encode(), canonical.encode(), hashlib.sha256).hexdigest()


def _valid_headers(key: str, method: str, path: str, body: bytes = b"") -> dict[str, str]:
    ts = int(time.time())
    sig = _make_signature(key, method, path, ts, body)
    return {
        "X-NCE-Timestamp": str(ts),
        "Authorization": f"HMAC-SHA256 {sig}",
    }


@pytest.mark.asyncio
async def test_actor_trust_endpoint():
    mock_engine = MagicMock()
    mock_conn = AsyncMock()
    mock_engine.pg_pool.acquire.return_value.__aenter__.return_value = mock_conn

    # Empty list returned from DB
    mock_conn.fetch.return_value = []

    key = cfg.NCE_API_KEY or "test-key"
    with (
        patch("nce.admin_state.engine", mock_engine),
        patch("nce.config.cfg.NCE_ADMIN_MTLS_ENABLED", False),
    ):
        with TestClient(app, raise_server_exceptions=False) as client:
            # 1. Unauthenticated request -> should return 401
            r = client.get("/api/admin/actor-trust")
            assert r.status_code == 401

            # 2. Authenticated request -> should return 200 and empty list
            headers = _valid_headers(key, "GET", "/api/admin/actor-trust")
            r = client.get("/api/admin/actor-trust", headers=headers)
            assert r.status_code == 200
            assert r.json() == []


@pytest.mark.asyncio
async def test_approval_queue_list_endpoint():
    mock_engine = MagicMock()
    mock_conn = AsyncMock()
    mock_engine.pg_pool.acquire.return_value.__aenter__.return_value = mock_conn

    # Empty list returned from DB
    mock_conn.fetch.return_value = []

    key = cfg.NCE_API_KEY or "test-key"
    with (
        patch("nce.admin_state.engine", mock_engine),
        patch("nce.config.cfg.NCE_ADMIN_MTLS_ENABLED", False),
    ):
        with TestClient(app, raise_server_exceptions=False) as client:
            # 1. Unauthenticated request -> should return 401
            r = client.get("/api/admin/approval-queue")
            assert r.status_code == 401

            # 2. Authenticated request -> should return 200 and empty list
            headers = _valid_headers(key, "GET", "/api/admin/approval-queue")
            r = client.get("/api/admin/approval-queue", headers=headers)
            assert r.status_code == 200
            assert r.json() == []


@pytest.mark.asyncio
async def test_approval_queue_get_endpoint():
    mock_engine = MagicMock()
    mock_conn = AsyncMock()
    mock_engine.pg_pool.acquire.return_value.__aenter__.return_value = mock_conn

    # None returned (not found)
    mock_conn.fetchrow.return_value = None

    item_id = uuid.uuid4()
    path = f"/api/admin/approval-queue/{item_id}"
    key = cfg.NCE_API_KEY or "test-key"
    with (
        patch("nce.admin_state.engine", mock_engine),
        patch("nce.config.cfg.NCE_ADMIN_MTLS_ENABLED", False),
    ):
        with TestClient(app, raise_server_exceptions=False) as client:
            # 1. Unauthenticated request -> should return 401
            r = client.get(path)
            assert r.status_code == 401

            # 2. Authenticated request -> should return 404
            headers = _valid_headers(key, "GET", path)
            r = client.get(path, headers=headers)
            assert r.status_code == 404
            assert "not found" in r.json()["error"].lower()
