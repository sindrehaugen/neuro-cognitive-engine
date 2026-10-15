"""Unit tests for A2A Admin REST API authorization boundaries and task status cancellation."""

from __future__ import annotations

import asyncio
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.requests import Request

from nce.a2a import A2AScopeViolationError
from nce.a2a_server import _dispatch_skill, _get_task, tasks_send
from nce.admin_handlers.a2a import (
    api_a2a_create_grant,
    api_a2a_list_grants,
    api_a2a_revoke_grant,
)
from nce.auth import NamespaceContext


@pytest.mark.asyncio
async def test_api_a2a_create_grant_authorized():
    """Verify grant creation succeeds when namespace_id matches context."""
    ns_id = uuid.uuid4()
    body = {
        "namespace_id": str(ns_id),
        "scopes": [{"resource_type": "namespace", "resource_id": str(ns_id)}],
    }

    async def receive():
        return {"type": "http.request", "body": json.dumps(body).encode()}

    mock_engine = MagicMock()
    mock_engine.pg_pool.acquire.return_value.__aenter__.return_value = AsyncMock()

    request = Request(
        {"type": "http", "method": "POST", "path": "/api/a2a/grants/create"},
        receive=receive,
    )
    request.state.namespace_ctx = NamespaceContext(namespace_id=ns_id, agent_id="default")

    mock_resp = MagicMock()
    mock_resp.grant_id = uuid.uuid4()
    mock_resp.sharing_token = "nce_a2a_test_token"
    mock_resp.expires_at = MagicMock()
    mock_resp.expires_at.isoformat.return_value = "2026-06-08T00:00:00Z"

    with (
        patch("nce.admin_state.engine", mock_engine),
        patch("nce.a2a.create_grant", new_callable=AsyncMock) as mock_create,
    ):
        mock_create.return_value = mock_resp
        response = await api_a2a_create_grant(request)
        assert response.status_code == 201
        data = json.loads(response.body.decode())
        assert data["sharing_token"] == "nce_a2a_test_token"


@pytest.mark.asyncio
async def test_api_a2a_create_grant_unauthorized_mismatch():
    """Verify grant creation fails (403) when namespace_id mismatches context."""
    ns_id_request = uuid.uuid4()
    ns_id_authenticated = uuid.uuid4()
    body = {
        "namespace_id": str(ns_id_request),
        "scopes": [{"resource_type": "namespace", "resource_id": str(ns_id_request)}],
    }

    async def receive():
        return {"type": "http.request", "body": json.dumps(body).encode()}

    request = Request(
        {"type": "http", "method": "POST", "path": "/api/a2a/grants/create"},
        receive=receive,
    )
    request.state.namespace_ctx = NamespaceContext(
        namespace_id=ns_id_authenticated, agent_id="default"
    )

    mock_engine = MagicMock()
    with patch("nce.admin_state.engine", mock_engine):
        response = await api_a2a_create_grant(request)
        assert response.status_code == 403
        data = json.loads(response.body.decode())
        assert "Forbidden" in data["error"]


@pytest.mark.asyncio
async def test_api_a2a_revoke_grant_authorized():
    """Verify grant revocation succeeds when namespace_id matches context."""
    ns_id = uuid.uuid4()
    grant_id = uuid.uuid4()
    body = {"namespace_id": str(ns_id)}

    async def receive():
        return {"type": "http.request", "body": json.dumps(body).encode()}

    mock_engine = MagicMock()
    mock_engine.pg_pool.acquire.return_value.__aenter__.return_value = AsyncMock()

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": f"/api/a2a/grants/{grant_id}/revoke",
            "path_params": {"grant_id": str(grant_id)},
        },
        receive=receive,
    )
    request.state.namespace_ctx = NamespaceContext(namespace_id=ns_id, agent_id="default")

    with (
        patch("nce.admin_state.engine", mock_engine),
        patch("nce.a2a.revoke_grant", new_callable=AsyncMock) as mock_revoke,
    ):
        mock_revoke.return_value = True
        response = await api_a2a_revoke_grant(request)
        assert response.status_code == 200
        data = json.loads(response.body.decode())
        assert data["revoked"] is True


@pytest.mark.asyncio
async def test_api_a2a_revoke_grant_unauthorized_mismatch():
    """Verify grant revocation fails (403) when namespace_id mismatches context."""
    ns_id_request = uuid.uuid4()
    ns_id_auth = uuid.uuid4()
    grant_id = uuid.uuid4()
    body = {"namespace_id": str(ns_id_request)}

    async def receive():
        return {"type": "http.request", "body": json.dumps(body).encode()}

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": f"/api/a2a/grants/{grant_id}/revoke",
            "path_params": {"grant_id": str(grant_id)},
        },
        receive=receive,
    )
    request.state.namespace_ctx = NamespaceContext(namespace_id=ns_id_auth, agent_id="default")

    mock_engine = MagicMock()
    with patch("nce.admin_state.engine", mock_engine):
        response = await api_a2a_revoke_grant(request)
        assert response.status_code == 403
        data = json.loads(response.body.decode())
        assert "Forbidden" in data["error"]


@pytest.mark.asyncio
async def test_api_a2a_list_grants_authorized():
    """Verify listing grants succeeds when namespace_id matches context."""
    ns_id = uuid.uuid4()
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/a2a/grants",
            "query_string": f"namespace_id={ns_id}".encode(),
        }
    )
    request.state.namespace_ctx = NamespaceContext(namespace_id=ns_id, agent_id="default")

    mock_engine = MagicMock()
    mock_engine.pg_pool.acquire.return_value.__aenter__.return_value = AsyncMock()

    with (
        patch("nce.admin_state.engine", mock_engine),
        patch("nce.a2a.list_grants", new_callable=AsyncMock) as mock_list,
    ):
        mock_list.return_value = [{"grant_id": "test"}]
        response = await api_a2a_list_grants(request)
        assert response.status_code == 200
        data = json.loads(response.body.decode())
        assert data["total"] == 1


@pytest.mark.asyncio
async def test_api_a2a_list_grants_unauthorized_mismatch():
    """Verify listing grants fails (403) when namespace_id mismatches context."""
    ns_id_query = uuid.uuid4()
    ns_id_auth = uuid.uuid4()
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/a2a/grants",
            "query_string": f"namespace_id={ns_id_query}".encode(),
        }
    )
    request.state.namespace_ctx = NamespaceContext(namespace_id=ns_id_auth, agent_id="default")

    mock_engine = MagicMock()
    with patch("nce.admin_state.engine", mock_engine):
        response = await api_a2a_list_grants(request)
        assert response.status_code == 403
        data = json.loads(response.body.decode())
        assert "Forbidden" in data["error"]


@pytest.mark.asyncio
async def test_tasks_send_cancelled_error():
    """Verify CancelledError is caught to mark task status as canceled and then re-raised."""
    task_id = str(uuid.uuid4())
    ns_id = uuid.uuid4()
    body = {
        "id": task_id,
        "skill": "recall_relevant_context",
        "params": {
            "query": "hello",
            "namespace_id": str(ns_id),
        },
    }

    async def receive():
        return {"type": "http.request", "body": json.dumps(body).encode()}

    request = Request(
        {"type": "http", "method": "POST", "path": "/tasks/send"},
        receive=receive,
    )
    request.state.namespace_ctx = NamespaceContext(namespace_id=ns_id, agent_id="default")

    mock_engine = MagicMock()
    mock_engine.redis_client = None

    with (
        patch("nce.a2a_server._engine", mock_engine),
        patch(
            "nce.a2a_server._dispatch_skill", side_effect=asyncio.CancelledError
        ) as mock_dispatch,
    ):
        with pytest.raises(asyncio.CancelledError):
            await tasks_send(request)

        # Retrieve status from registry to check it was canceled correctly
        task = await _get_task(task_id)
        assert task is not None
        assert task["status"]["state"] == "canceled"
        assert "cancelled" in task["status"]["message"]["parts"][0]["text"].lower()


@pytest.mark.asyncio
async def test_tasks_send_other_base_exception():
    """Verify KeyboardInterrupt (BaseException) is caught to mark status and re-raised."""
    task_id = str(uuid.uuid4())
    ns_id = uuid.uuid4()
    body = {
        "id": task_id,
        "skill": "recall_relevant_context",
        "params": {
            "query": "hello",
            "namespace_id": str(ns_id),
        },
    }

    async def receive():
        return {"type": "http.request", "body": json.dumps(body).encode()}

    request = Request(
        {"type": "http", "method": "POST", "path": "/tasks/send"},
        receive=receive,
    )
    request.state.namespace_ctx = NamespaceContext(namespace_id=ns_id, agent_id="default")

    mock_engine = MagicMock()
    mock_engine.redis_client = None

    with (
        patch("nce.a2a_server._engine", mock_engine),
        patch("nce.a2a_server._dispatch_skill", side_effect=KeyboardInterrupt) as mock_dispatch,
    ):
        with pytest.raises(KeyboardInterrupt):
            await tasks_send(request)

        task = await _get_task(task_id)
        assert task is not None
        assert task["status"]["state"] == "failed"
        assert "KeyboardInterrupt" in task["status"]["message"]["parts"][0]["text"]


@pytest.mark.asyncio
async def test_dispatch_skill_matching_caller_namespace():
    """Enforce caller namespace context on direct skill dispatch when namespace_id matches."""
    ns_id = uuid.uuid4()
    caller_ctx = NamespaceContext(namespace_id=ns_id, agent_id="agent-a")

    mock_engine = MagicMock()
    mock_engine.redis_client = None
    mock_engine.semantic_search = AsyncMock(return_value=[{"id": "m1"}])
    mock_engine.graph_search = AsyncMock(return_value={"nodes": []})

    with patch("nce.a2a_server._engine", mock_engine):
        res = await _dispatch_skill(
            "recall_relevant_context",
            {"query": "hello", "namespace_id": str(ns_id)},
            caller_ctx,
        )
        assert res["semantic"] == [{"id": "m1"}]
        mock_engine.semantic_search.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispatch_skill_mismatched_caller_namespace():
    """Enforce caller namespace context on direct skill dispatch when namespace_id mismatches."""
    caller_ns = uuid.uuid4()
    requested_ns = uuid.uuid4()
    caller_ctx = NamespaceContext(namespace_id=caller_ns, agent_id="agent-a")

    mock_engine = MagicMock()
    mock_engine.redis_client = None
    mock_engine.semantic_search = AsyncMock(return_value=[])

    with patch("nce.a2a_server._engine", mock_engine):
        with pytest.raises(A2AScopeViolationError, match="Requested namespace_id does not match"):
            await _dispatch_skill(
                "recall_relevant_context",
                {"query": "hello", "namespace_id": str(requested_ns)},
                caller_ctx,
            )
        mock_engine.semantic_search.assert_not_awaited()


@pytest.mark.asyncio
async def test_tasks_send_direct_mismatched_namespace_returns_403():
    """Verify tasks_send returns 403 scope violation when direct request namespace mismatches caller context."""
    caller_ns = uuid.uuid4()
    requested_ns = uuid.uuid4()
    body = {
        "id": str(uuid.uuid4()),
        "skill": "recall_relevant_context",
        "params": {
            "query": "hello",
            "namespace_id": str(requested_ns),
            "_owner_agent_id": "untrusted-marker",
        },
    }

    async def receive():
        return {"type": "http.request", "body": json.dumps(body).encode()}

    request = Request(
        {"type": "http", "method": "POST", "path": "/tasks/send"},
        receive=receive,
    )
    request.state.namespace_ctx = NamespaceContext(namespace_id=caller_ns, agent_id="default")

    mock_engine = MagicMock()
    mock_engine.redis_client = None
    mock_engine.semantic_search = AsyncMock(return_value=[])

    with patch("nce.a2a_server._engine", mock_engine):
        response = await tasks_send(request)
        assert response.status_code == 403
        data = json.loads(response.body.decode())
        assert data["error"]["code"] == -32011
        assert "Requested namespace_id does not match" in data["error"]["data"]["reason"]
        mock_engine.semantic_search.assert_not_awaited()


@pytest.mark.asyncio
async def test_tasks_send_with_verified_sharing_token_overrides_namespace():
    """Verify cross-namespace tasks_send with verified sharing_token succeeds in owner namespace."""
    consumer_ns = uuid.uuid4()
    owner_ns = uuid.uuid4()
    body = {
        "id": str(uuid.uuid4()),
        "skill": "recall_relevant_context",
        "sharing_token": "nce_a2a_valid_token",
        "params": {
            "query": "shared context",
            "namespace_id": str(owner_ns),
        },
    }

    async def receive():
        return {"type": "http.request", "body": json.dumps(body).encode()}

    request = Request(
        {"type": "http", "method": "POST", "path": "/tasks/send"},
        receive=receive,
    )
    request.state.namespace_ctx = NamespaceContext(namespace_id=consumer_ns, agent_id="consumer")

    mock_conn = AsyncMock()
    mock_tx = AsyncMock()
    mock_tx.__aenter__ = AsyncMock()
    mock_tx.__aexit__ = AsyncMock()
    mock_conn.transaction = MagicMock(return_value=mock_tx)

    mock_engine = MagicMock()
    mock_engine.redis_client = None
    mock_engine.pg_pool.acquire.return_value.__aenter__.return_value = mock_conn
    mock_engine.semantic_search = AsyncMock(return_value=[{"id": "shared-1"}])
    mock_engine.graph_search = AsyncMock(return_value={"nodes": []})

    scope_item = MagicMock()
    scope_item.resource_type = "namespace"
    scope_item.resource_id = str(owner_ns)
    scope_item.permissions = ["read"]
    verified_grant = MagicMock()
    verified_grant.grant_id = uuid.uuid4()
    verified_grant.owner_namespace_id = owner_ns
    verified_grant.owner_agent_id = "owner-agent"
    verified_grant.scopes = [scope_item]

    with (
        patch("nce.a2a_server._engine", mock_engine),
        patch("nce.a2a_server.verify_token", AsyncMock(return_value=verified_grant)),
    ):
        response = await tasks_send(request)
        assert response.status_code == 200
        data = json.loads(response.body.decode())
        assert data["status"]["state"] == "completed"
        mock_engine.semantic_search.assert_awaited_once()
        call_kwargs = mock_engine.semantic_search.await_args.kwargs
        assert call_kwargs["namespace_id"] == str(owner_ns)
