"""Production-safe JSON-RPC error payloads from server.call_tool (P0-B)."""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nce.config import cfg
from nce.quotas import null_reservation


def _parse_error_payload(result: list) -> dict:
    assert len(result) == 1
    body = json.loads(result[0].text)
    assert "error" in body
    return body["error"]


@pytest.fixture
def mock_engine():
    engine = MagicMock()
    engine.redis_client = AsyncMock()
    engine.redis_client.get.return_value = None
    # Ensure the tool-disabled interceptor always passes (tools are enabled by default)
    engine.redis_client.hexists = AsyncMock(return_value=False)
    engine.pg_pool = MagicMock()
    return engine


@pytest.fixture(autouse=True)
def _server_engine(mock_engine):
    import server

    original = server.engine
    server.engine = mock_engine
    yield
    server.engine = original


@pytest.fixture(autouse=True)
def _disable_quotas(monkeypatch):
    monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)


@pytest.fixture(autouse=True)
def _prod_safe_errors(monkeypatch):
    """Keep error sanitization active even if another test reloaded nce.config."""

    import nce.mcp_errors as mcp_errors_mod

    monkeypatch.setattr(cfg, "IS_DEV", False)
    monkeypatch.setattr(mcp_errors_mod.cfg, "IS_DEV", False)


@pytest.mark.asyncio
async def test_call_tool_internal_error_hides_detail_in_prod(monkeypatch, mock_engine):
    monkeypatch.setattr(cfg, "IS_DEV", False)

    import server as srv

    async def _boom(*_a, **_k):
        raise RuntimeError("postgresql://user:secret@db:5432/memory_meta")

    import nce.mcp_stdio_dispatch as dispatch

    with patch.object(dispatch.memory_mcp_handlers, "handle_store_memory", _boom):
        err = _parse_error_payload(
            await srv.call_tool(
                "store_memory",
                {
                    "namespace_id": str(uuid.uuid4()),
                    "agent_id": "agent",
                    "content": "hello",
                },
            )
        )
    data = err.get("data") or {}
    assert err["code"] == -32603
    assert "detail" not in data
    assert "postgresql://" not in json.dumps(data)
    assert data.get("request_id")
    assert data.get("type") == "RuntimeError"


@pytest.mark.asyncio
async def test_call_tool_scope_error_hides_detail_in_prod(monkeypatch):
    monkeypatch.setattr(cfg, "IS_DEV", False)

    import server as srv

    from nce.auth import ScopeError

    async def _scoped(*_a, **_k):
        raise ScopeError("admin", "invalid admin_api_key")

    import nce.mcp_stdio_dispatch as dispatch

    with patch.object(dispatch.admin_mcp_handlers, "handle_manage_namespace", _scoped):
        with patch(
            "nce.mcp_stdio_dispatch._consume_quota_for_mcp_tool",
            AsyncMock(return_value=null_reservation()),
        ):
            err = _parse_error_payload(
                await srv.call_tool(
                    "manage_namespace",
                    {"namespace_id": str(uuid.uuid4()), "command": "list"},
                )
            )
    data = err.get("data") or {}
    assert err["code"] == -32005
    assert "detail" not in data
    assert "invalid admin_api_key" not in json.dumps(data)


def test_check_admin_delegates_to_validate_scope(monkeypatch):
    monkeypatch.delenv("NCE_ADMIN_OVERRIDE", raising=False)
    monkeypatch.setenv("NCE_ADMIN_API_KEY", "server-secret-key")

    import server as srv

    from nce.mcp_errors import McpError

    with pytest.raises(McpError) as ei:
        srv._check_admin({"admin_api_key": "wrong"})
    assert ei.value.code == -32001

    srv._check_admin({"admin_api_key": "server-secret-key"})


@pytest.mark.asyncio
async def test_call_tool_database_exception_masking_in_prod(monkeypatch, mock_engine):
    monkeypatch.setattr(cfg, "IS_DEV", False)

    # Simulate a third party exception, e.g. asyncpg QueryCanceledError
    import asyncpg
    import server as srv

    class DummyQueryCanceledError(asyncpg.exceptions.QueryCanceledError):
        __module__ = "asyncpg.exceptions"

    async def _boom(*_a, **_k):
        raise DummyQueryCanceledError("database query was cancelled")

    import nce.mcp_stdio_dispatch as dispatch

    with patch.object(dispatch.memory_mcp_handlers, "handle_store_memory", _boom):
        err = _parse_error_payload(
            await srv.call_tool(
                "store_memory",
                {
                    "namespace_id": str(uuid.uuid4()),
                    "agent_id": "agent",
                    "content": "hello",
                },
            )
        )
    data = err.get("data") or {}
    assert err["code"] == -32603
    assert "detail" not in data
    assert data.get("type") == "DatabaseError"  # Masked!


@pytest.mark.asyncio
async def test_dispatch_concurrency_limit(monkeypatch, mock_engine):
    import asyncio

    import nce.mcp_stdio_dispatch as dispatch
    from nce.mcp_stdio_dispatch import get_concurrency_semaphore

    # Set max concurrent tools to 2
    monkeypatch.setattr(cfg, "NCE_MAX_CONCURRENT_TOOLS", 2)

    # Reset the global semaphore so it is recreated with our new limit
    monkeypatch.setattr(dispatch, "_concurrency_semaphore", None)

    sem = get_concurrency_semaphore()
    assert sem._value == 2

    active_count = 0
    max_observed_active = 0

    async def _slow_tool(*_a, **_k):
        nonlocal active_count, max_observed_active
        active_count += 1
        max_observed_active = max(max_observed_active, active_count)
        await asyncio.sleep(0.05)
        active_count -= 1
        return "ok"

    import server as srv

    with patch.object(dispatch.memory_mcp_handlers, "handle_store_memory", _slow_tool):
        # Call the tool 4 times concurrently
        tasks = [
            srv.call_tool(
                "store_memory",
                {
                    "namespace_id": str(uuid.uuid4()),
                    "agent_id": "agent",
                    "content": "hello",
                },
            )
            for _ in range(4)
        ]
        await asyncio.gather(*tasks)

    # Max observed active should be capped at 2
    assert max_observed_active <= 2
