"""Unit regression tests for v3.4.4 core optimizations."""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient

from nce.auth import NonceStore
from nce.config import cfg
from nce.mcp_http_gateway import (
    _TOOLS_LIST_CACHE,
    create_mcp_gateway_app,
    register_test_mcp_client_token,
)


def test_redis_fail_closed_timeouts_default_under_2s() -> None:
    """Governance and NonceStore Redis timeouts must default to 1.5s (< 2000ms)."""
    assert cfg.REDIS_SOCKET_TIMEOUT_S == 1.5
    assert cfg.REDIS_CONNECT_TIMEOUT_S == 1.5

    store = NonceStore("redis://localhost:6379/0", max_connections=5)
    with patch("redis.asyncio.ConnectionPool.from_url") as mock_from_url:
        mock_from_url.return_value = MagicMock()
        import asyncio

        asyncio.run(store._get_redis())
        _, kwargs = mock_from_url.call_args
        assert kwargs["socket_connect_timeout"] == 1.5
        assert kwargs["socket_timeout"] == 1.5


def test_compact_mcp_router_exposes_core_working_memory_and_defers_admin_a2a_replay() -> None:
    """Compact mode exposes <=16 core working-memory tools; full mode exposes all 71."""
    _TOOLS_LIST_CACHE.clear()
    ns = str(uuid.uuid4())
    compact_tok = f"nce_mcp_compact_{uuid.uuid4().hex[:8]}"
    full_tok = f"nce_mcp_full_{uuid.uuid4().hex[:8]}"

    register_test_mcp_client_token(
        compact_tok,
        namespace_id=ns,
        principal_id="compact-agent",
        allowed_engines=["*"],
        tool_exposure_mode="compact",
    )
    register_test_mcp_client_token(
        full_tok,
        namespace_id=ns,
        principal_id="full-agent",
        allowed_engines=["*"],
        tool_exposure_mode="full",
    )

    mock_engine = MagicMock()
    mock_engine.redis_client = None
    client = TestClient(create_mcp_gateway_app(engine=mock_engine))

    # 1. Compact initial tools/list <= 16 core working-memory tools
    r_compact = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {compact_tok}"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert r_compact.status_code == 200
    session_id = r_compact.headers["Mcp-Session-Id"]
    compact_names = {t["name"] for t in r_compact.json()["result"]["tools"]}
    assert len(compact_names) <= 16
    assert {
        "discover_tools",
        "store_memory",
        "store_artifact",
        "semantic_search",
        "compile_working_memory",
        "graph_search",
        "describe_schema",
        "boost_memory",
        "forget_memory",
        "list_contradictions",
        "resolve_contradiction",
        "verify_memory",
    }.issubset(compact_names)
    for deferred in ("replay_fork", "a2a_query_shared", "list_bridges", "rotate_signing_key"):
        assert deferred not in compact_names

    # 2. discover_tools(query="replay") dynamically unlocks replay schemas
    r_disc = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {compact_tok}", "Mcp-Session-Id": session_id},
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "discover_tools", "arguments": {"query": "replay"}},
        },
    )
    assert r_disc.status_code == 200
    r_compact_after = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {compact_tok}", "Mcp-Session-Id": session_id},
        json={"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
    )
    compact_after_names = {t["name"] for t in r_compact_after.json()["result"]["tools"]}
    assert "replay_fork" in compact_after_names

    # 3. Full mode exposes all 71 core tools
    r_full = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {full_tok}"},
        json={"jsonrpc": "2.0", "id": 4, "method": "tools/list"},
    )
    assert r_full.status_code == 200
    full_names = {t["name"] for t in r_full.json()["result"]["tools"]}
    assert len(full_names) >= 71
    assert "replay_fork" in full_names
    assert "rotate_signing_key" in full_names


def test_dep_report_send_email_skips_empty_smtp_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """send_email skips gracefully without ValueError when SMTP secrets are empty strings."""
    from scripts.dep_report import send_email

    monkeypatch.setenv("NCE_SMTP_PORT", "")
    monkeypatch.setenv("NCE_SMTP_HOST", "")
    monkeypatch.setenv("NCE_SMTP_USER", "")
    monkeypatch.setenv("NCE_SMTP_PASS", "")
    monkeypatch.setenv("NCE_ADMIN_EMAIL", "")

    assert asyncio.run(send_email("subj", "body")) is None
