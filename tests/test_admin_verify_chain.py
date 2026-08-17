"""Tests for /api/admin/verify-chain/{namespace_id} endpoint (B2)."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

os.environ.setdefault("NCE_MASTER_KEY", "dev-test-key-32chars-long!!")

import pytest
from starlette.requests import Request


@pytest.fixture
def mock_engine():
    """Patch nce.admin_state.engine with a mock that has pg_pool.acquire."""
    engine = MagicMock()
    conn = AsyncMock()
    engine.pg_pool.acquire = MagicMock(return_value=AsyncMock())
    # Async context manager for acquire()
    acquire_cm = engine.pg_pool.acquire.return_value
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=False)
    return engine, conn


@pytest.mark.asyncio
async def test_verify_chain_valid(mock_engine):
    engine, conn = mock_engine
    ns = uuid4()

    with patch("nce.admin_state.engine", engine):
        with patch(
            "nce.admin_handlers.fleet.verify_merkle_chain",
            new=AsyncMock(
                return_value={
                    "valid": True,
                    "checked": 5,
                    "first_break": None,
                    "last_verified_seq": 5,
                }
            ),
        ):
            from admin_server import api_admin_verify_chain

            request = Request(
                {
                    "type": "http",
                    "method": "GET",
                    "path": f"/api/admin/verify-chain/{ns}",
                    "path_params": {"namespace_id": str(ns)},
                    "query_string": b"",
                    "headers": [],
                }
            )
            response = await api_admin_verify_chain(request)

    assert response.status_code == 200
    body = response.body.decode()
    assert '"valid":true' in body or '"valid": true' in body
    assert '"checked":5' in body or '"checked": 5' in body


@pytest.mark.asyncio
async def test_verify_chain_corrupted(mock_engine):
    engine, conn = mock_engine
    ns = uuid4()

    with patch("nce.admin_state.engine", engine):
        with patch(
            "nce.admin_handlers.fleet.verify_merkle_chain",
            new=AsyncMock(
                return_value={
                    "valid": False,
                    "checked": 10,
                    "first_break": 3,
                    "last_verified_seq": 10,
                }
            ),
        ):
            from admin_server import api_admin_verify_chain

            request = Request(
                {
                    "type": "http",
                    "method": "GET",
                    "path": f"/api/admin/verify-chain/{ns}",
                    "path_params": {"namespace_id": str(ns)},
                    "query_string": b"",
                    "headers": [],
                }
            )
            response = await api_admin_verify_chain(request)

    assert response.status_code == 200
    body = response.body.decode()
    assert '"valid":false' in body or '"valid": false' in body
    assert '"first_break":3' in body or '"first_break": 3' in body


@pytest.mark.asyncio
async def test_verify_chain_invalid_namespace(mock_engine):
    engine, _ = mock_engine

    with patch("nce.admin_state.engine", engine):
        from admin_server import api_admin_verify_chain

        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/admin/verify-chain/not-a-uuid",
                "path_params": {"namespace_id": "not-a-uuid"},
                "query_string": b"",
                "headers": [],
            }
        )
        response = await api_admin_verify_chain(request)

    assert response.status_code == 422
    body = response.body.decode()
    assert "Invalid namespace_id" in body


@pytest.mark.asyncio
async def test_verify_chain_missing_namespace():
    with patch("nce.admin_state.engine", MagicMock()):
        from admin_server import api_admin_verify_chain

        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/admin/verify-chain/",
                "path_params": {"namespace_id": ""},
                "query_string": b"",
                "headers": [],
            }
        )
        response = await api_admin_verify_chain(request)

    assert response.status_code == 422
    body = response.body.decode()
    assert "namespace_id required" in body


@pytest.mark.asyncio
async def test_verify_chain_engine_not_connected():
    with patch("nce.admin_state.engine", None):
        from admin_server import api_admin_verify_chain

        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": f"/api/admin/verify-chain/{uuid4()}",
                "path_params": {"namespace_id": str(uuid4())},
                "query_string": b"",
                "headers": [],
            }
        )
        response = await api_admin_verify_chain(request)

    assert response.status_code == 503
    body = response.body.decode()
    assert "Engine not connected" in body
