"""Tests for Admin Server Namespace REST Endpoints."""

from __future__ import annotations

import json
import os
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.requests import Request

os.environ.setdefault("NCE_MASTER_KEY", "dev-test-key-32chars-long!!")


@pytest.mark.asyncio
async def test_api_admin_namespaces_list():
    """Verify listing namespaces retrieves from DB pool and structures correctly."""
    mock_engine = MagicMock()
    mock_conn = AsyncMock()
    mock_engine.pg_pool.acquire.return_value.__aenter__.return_value = mock_conn

    # Mock DB rows
    ns_id_1 = uuid.uuid4()
    ns_id_2 = uuid.uuid4()
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)

    mock_conn.fetchrow.return_value = {"total": 2}
    mock_conn.fetch.return_value = [
        {
            "id": ns_id_1,
            "slug": "namespace-1",
            "parent_id": None,
            "created_at": now,
            "metadata": '{"temporal_retention_days": 10}',
        },
        {
            "id": ns_id_2,
            "slug": "namespace-2",
            "parent_id": ns_id_1,
            "created_at": now,
            "metadata": '{"consolidation": {"enabled": true}}',
        },
    ]

    with patch("nce.admin_state.engine", mock_engine):
        from admin_server import api_admin_namespaces_list

        request = Request(
            {"type": "http", "method": "GET", "path": "/api/admin/namespaces", "query_string": b""}
        )
        response = await api_admin_namespaces_list(request)

        assert response.status_code == 200
        data = json.loads(response.body.decode())
        assert len(data["namespaces"]) == 2
        assert data["total"] == 2
        assert data["items"] == data["namespaces"]
        assert data["page"] == 1
        assert len(data["namespaces"]) > 0
        assert data["namespaces"][0]["slug"] == "namespace-1"
        assert data["namespaces"][0]["id"] == str(ns_id_1)
        assert data["namespaces"][0]["metadata"]["temporal_retention_days"] == 10
        assert data["namespaces"][1]["slug"] == "namespace-2"
        assert data["namespaces"][1]["parent_id"] == str(ns_id_1)
        assert data["namespaces"][1]["metadata"]["consolidation"]["enabled"] is True


@pytest.mark.asyncio
async def test_api_admin_namespaces_get():
    """Verify fetching a specific namespace."""
    mock_engine = MagicMock()
    mock_conn = AsyncMock()
    mock_engine.pg_pool.acquire.return_value.__aenter__.return_value = mock_conn

    ns_id = uuid.uuid4()
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)

    mock_conn.fetchrow.return_value = {
        "id": ns_id,
        "slug": "test-ns",
        "parent_id": None,
        "created_at": now,
        "metadata": '{"consolidation": {"enabled": false}}',
    }

    with patch("nce.admin_state.engine", mock_engine):
        from admin_server import api_admin_namespaces_get

        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": f"/api/admin/namespaces/{ns_id}",
                "path_params": {"namespace_id": str(ns_id)},
            }
        )
        response = await api_admin_namespaces_get(request)

        assert response.status_code == 200
        data = json.loads(response.body.decode())
        assert data["slug"] == "test-ns"
        assert data["id"] == str(ns_id)
        assert data["metadata"]["consolidation"]["enabled"] is False


@pytest.mark.asyncio
async def test_api_admin_namespaces_get_not_found():
    """Verify proper 404 response for non-existent namespace UUID."""
    mock_engine = MagicMock()
    mock_conn = AsyncMock()
    mock_engine.pg_pool.acquire.return_value.__aenter__.return_value = mock_conn
    mock_conn.fetchrow.return_value = None

    ns_id = uuid.uuid4()

    with patch("nce.admin_state.engine", mock_engine):
        from admin_server import api_admin_namespaces_get

        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": f"/api/admin/namespaces/{ns_id}",
                "path_params": {"namespace_id": str(ns_id)},
            }
        )
        response = await api_admin_namespaces_get(request)
        assert response.status_code == 404


@pytest.mark.asyncio
async def test_api_admin_namespaces_update_metadata():
    """Verify validation and invoking engine.manage_namespace works correctly."""
    mock_engine = MagicMock()
    mock_engine.manage_namespace = AsyncMock()
    mock_engine.manage_namespace.return_value = {"status": "success", "message": "Updated!"}

    ns_id = uuid.uuid4()
    payload = {
        "temporal_retention_days": 30,
        "consolidation": {
            "enabled": True,
            "llm_provider": "openai",
            "llm_model": "gpt-4o",
            "llm_temperature": 0.5,
            "decay_sources": True,
        },
    }

    async def receive():
        return {"type": "http.request", "body": json.dumps(payload).encode()}

    with patch("nce.admin_state.engine", mock_engine):
        from admin_server import api_admin_namespaces_update_metadata

        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": f"/api/admin/namespaces/{ns_id}/metadata",
                "path_params": {"namespace_id": str(ns_id)},
            },
            receive=receive,
        )

        response = await api_admin_namespaces_update_metadata(request)

        assert response.status_code == 200
        data = json.loads(response.body.decode())
        assert data["status"] == "success"

        # Verify engine.manage_namespace was called with correct structure
        mock_engine.manage_namespace.assert_called_once()
        call_arg = mock_engine.manage_namespace.call_args[0][0]
        assert call_arg.command == "update_metadata"
        assert str(call_arg.namespace_id) == str(ns_id)
        assert call_arg.metadata_patch.temporal_retention_days == 30
        assert call_arg.metadata_patch.consolidation.enabled is True
        assert call_arg.metadata_patch.consolidation.llm_provider == "openai"
        assert call_arg.metadata_patch.consolidation.llm_model == "gpt-4o"
        assert call_arg.metadata_patch.consolidation.llm_temperature == 0.5
        assert call_arg.metadata_patch.consolidation.decay_sources is True


@pytest.mark.asyncio
async def test_api_admin_namespaces_update_metadata_invalid_pydantic():
    """Verify invalid pydantic inputs are safely rejected at validation layer."""
    mock_engine = MagicMock()
    ns_id = uuid.uuid4()

    # Extra key forbidden by Pydantic model
    payload = {
        "invalid_extra_field_key": 45,
    }

    async def receive():
        return {"type": "http.request", "body": json.dumps(payload).encode()}

    with patch("nce.admin_state.engine", mock_engine):
        from admin_server import api_admin_namespaces_update_metadata

        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": f"/api/admin/namespaces/{ns_id}/metadata",
                "path_params": {"namespace_id": str(ns_id)},
            },
            receive=receive,
        )

        response = await api_admin_namespaces_update_metadata(request)
        assert response.status_code == 422
        data = json.loads(response.body.decode())
        assert "Validation failed" in data["error"]
