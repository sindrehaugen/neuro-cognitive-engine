"""
tests/test_a2a_hardening.py

Unit and integration tests for Batch 42 A2A security hardening controls:
1. Sliding-window rate-limiting on /tasks/send.
2. Optional one-time grants mode.
3. Production boot audience verification.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nce.a2a import (
    A2AAuthorizationError,
    verify_token,
)
from nce.a2a_server import tasks_send
from nce.auth import NamespaceContext
from nce.config import cfg

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _prod_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "NCE_ENV": "prod",
            "NCE_LOAD_DOTENV": "false",
            "NCE_MASTER_KEY": "prod-master-key-32-characters-min!!",
            "NCE_API_KEY": "prod-api-key-for-ci-tests-only",
            "NCE_MCP_API_KEY": "prod-mcp-key-for-config-tests-only!!",
            "NCE_MCP_NAMESPACE_ID": "00000000-0000-4000-8000-000000000001",
            "NCE_ADMIN_API_KEY": "prod-admin-key-for-config-tests",
            "NCE_ADMIN_USERNAME": "admin",
            "NCE_ADMIN_PASSWORD": ("$pbkdf2$sha256$600000$testsalt$notarealhashbutformatok"),
            "PG_DSN": "postgresql://mcp_user:secret@db.internal.example:5432/memory_meta",
            "MONGO_URI": "mongodb://mongo.internal.example:27017",
            "REDIS_URL": "redis://redis.internal.example:6379/0",
            "MINIO_ACCESS_KEY": "minio-access-key",
            "MINIO_SECRET_KEY": "minio-secret-key-value",
            "NCE_JWT_SECRET": "jwt-secret-for-prod-config-tests!!",
        }
    )
    return env


@pytest.mark.asyncio
async def test_tasks_send_rate_limiting(monkeypatch) -> None:
    """Verify that requests to tasks/send trip the rate limit when limit is exceeded."""
    monkeypatch.setattr(cfg, "NCE_A2A_HTTP_RATE_LIMIT", 2)
    monkeypatch.setattr(cfg, "NCE_A2A_HTTP_RATE_PERIOD", 60)

    ns_id = uuid.uuid4()
    body = {
        "id": str(uuid.uuid4()),
        "skill": "recall_relevant_context",
        "params": {"query": "hello", "namespace_id": str(ns_id)},
    }

    async def receive():
        return {"type": "http.request", "body": json.dumps(body).encode()}

    # Call tasks_send multiple times with same IP
    request1 = MagicMock()
    request1.client = MagicMock()
    request1.client.host = "192.168.1.100"
    request1.json = AsyncMock(return_value=body)
    request1.state.namespace_ctx = NamespaceContext(namespace_id=ns_id, agent_id="default")

    request2 = MagicMock()
    request2.client = MagicMock()
    request2.client.host = "192.168.1.100"
    request2.json = AsyncMock(return_value=body)
    request2.state.namespace_ctx = NamespaceContext(namespace_id=ns_id, agent_id="default")

    request3 = MagicMock()
    request3.client = MagicMock()
    request3.client.host = "192.168.1.100"
    request3.json = AsyncMock(return_value=body)
    request3.state.namespace_ctx = NamespaceContext(namespace_id=ns_id, agent_id="default")

    mock_engine = MagicMock()
    mock_engine.redis_client = None  # Force fallback to RAM limits

    with (
        patch("nce.a2a_server._engine", mock_engine),
        patch("nce.a2a_server._dispatch_skill", new_callable=AsyncMock) as mock_dispatch,
        patch("nce.a2a_server._store_task", new_callable=AsyncMock),
    ):
        mock_dispatch.return_value = {"status": "ok"}

        # Request 1: Allowed
        response1 = await tasks_send(request1)
        assert response1.status_code == 200

        # Request 2: Allowed
        response2 = await tasks_send(request2)
        assert response2.status_code == 200

        # Request 3: Exceeded rate limit -> 429
        response3 = await tasks_send(request3)
        assert response3.status_code == 429
        data = json.loads(bytes(response3.body).decode())
        assert data["error"]["code"] == -32013
        assert data["error"]["message"] == "Rate limit exceeded"
        assert data["error"]["data"]["reason"] == "too_many_requests"


@pytest.mark.asyncio
async def test_verify_token_one_time_grant_rules() -> None:
    """Verify that one_time grants succeed on first use and get rejected on subsequent use."""
    owner_ns = uuid.uuid4()
    consumer_ns = uuid.uuid4()

    # First verification passes and executes update statement
    conn = AsyncMock()
    tx = AsyncMock()
    tx.__aenter__ = AsyncMock()
    tx.__aexit__ = AsyncMock()
    conn.transaction = MagicMock(return_value=tx)

    row_data = {
        "id": uuid.uuid4(),
        "owner_namespace_id": owner_ns,
        "owner_agent_id": "agent-a",
        "target_namespace_id": consumer_ns,
        "target_agent_id": None,
        "scopes": json.dumps(
            [{"resource_type": "namespace", "resource_id": str(owner_ns), "permissions": ["read"]}]
        ),
        "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
        "status": "active",
        "can_delegate": False,
        "one_time": True,
        "usage_count": 0,
    }
    conn.fetchrow = AsyncMock(return_value=row_data)

    consumer = NamespaceContext(namespace_id=consumer_ns, agent_id="agent-b")
    verified = await verify_token(conn, "test_token", consumer)
    assert verified.one_time is True
    conn.execute.assert_awaited()

    # Subsequent verification is rejected if token is inactive/revoked (fetchrow returns None)
    conn2 = AsyncMock()
    conn2.fetchrow = AsyncMock(return_value=None)
    with pytest.raises(A2AAuthorizationError, match="Invalid or revoked"):
        await verify_token(conn2, "test_token", consumer)

    # Subsequent verification is also explicitly rejected if token remains active but usage_count >= 1
    conn3 = AsyncMock()
    used_row = dict(row_data)
    used_row["usage_count"] = 1
    conn3.fetchrow = AsyncMock(return_value=used_row)
    with pytest.raises(A2AAuthorizationError, match="already been used"):
        await verify_token(conn3, "test_token", consumer)


def test_prod_boot_fails_without_audience() -> None:
    """Verify that configuration validation in production fails if NCE_A2A_JWT_AUDIENCE is not set."""
    env = _prod_env()
    env.pop("NCE_A2A_JWT_AUDIENCE", None)  # Ensure it is unset

    code = "from nce.config import _Config; _Config.validate()"
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "NCE_A2A_JWT_AUDIENCE" in (result.stderr + result.stdout)


def test_prod_boot_succeeds_with_audience() -> None:
    """Verify that configuration validation in production succeeds if NCE_A2A_JWT_AUDIENCE is set."""
    env = _prod_env()
    env["NCE_A2A_JWT_AUDIENCE"] = "nce_a2a_production_custom"

    code = "from nce.config import _Config; _Config.validate()"
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"Boot failed with: {result.stderr}"
