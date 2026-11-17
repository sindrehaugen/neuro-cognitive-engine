"""Unit tests for v3.5.2 post-burn-in LLM-loop optimizations & cross-layer remediation."""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_discover_tools_tokenized_scoring_and_compact_schema() -> None:
    from nce.mcp_http_gateway import (
        MCPGatewaySession,
        _all_gateway_tools,
        _compact_input_schema,
        discover_tools_handler,
    )

    tools_1 = _all_gateway_tools()
    tools_2 = _all_gateway_tools()
    assert tools_1 is tools_2, "_all_gateway_tools() should return cached list in O(1)"

    raw_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "mcp_api_key": {"type": "string"},
            "admin_api_key": {"type": "string"},
        },
        "required": ["query", "mcp_api_key"],
    }
    compacted = _compact_input_schema(raw_schema)
    assert "mcp_api_key" not in compacted["properties"]
    assert "admin_api_key" not in compacted["properties"]
    assert "query" in compacted["properties"]
    assert compacted["required"] == ["query"]

    session = MCPGatewaySession(
        session_id="sess-discover-test",
        namespace_id="00000000-0000-4000-8000-000000000001",
        principal_id="agent-test",
        scopes=["tools:read", "tools:call"],
        allowed_engines=["*"],
        tool_exposure_mode="compact",
        expanded_tools=set(),
        rate_limit_rpm=120,
        event_queue=asyncio.Queue(maxsize=32),
    )

    res = await discover_tools_handler(
        session,
        {"query": "semantic memory search", "limit": 5},
    )
    assert res["matched_count"] <= 5
    assert res["matched_count"] >= 1
    for tool_entry in res["tools"]:
        props = tool_entry.get("inputSchema", {}).get("properties", {})
        assert "mcp_api_key" not in props
        assert "admin_api_key" not in props


@pytest.mark.asyncio
async def test_verified_token_cache_and_invalidation() -> None:
    from nce.mcp_http_gateway import (
        _VERIFIED_TOKEN_CACHE,
        MCPGatewaySession,
        _is_session_revoked_or_expired,
        invalidate_verified_token_cache,
    )

    invalidate_verified_token_cache()
    assert len(_VERIFIED_TOKEN_CACHE) == 0

    tok_hash = "deadbeef" * 8
    session = MCPGatewaySession(
        session_id="sess-v352-test",
        namespace_id="00000000-0000-4000-8000-000000000001",
        principal_id="agent-1",
        scopes=["tools:read", "tools:call"],
        allowed_engines=["*"],
        tool_exposure_mode="compact",
        expanded_tools=set(),
        rate_limit_rpm=120,
        event_queue=asyncio.Queue(maxsize=32),
        token_sha256=tok_hash,
    )
    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(return_value={"revoked_at": None, "expires_at": None})
    mock_acquire = MagicMock()
    mock_acquire.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_acquire.__aexit__ = AsyncMock(return_value=None)
    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock(return_value=mock_acquire)

    revoked_1 = await _is_session_revoked_or_expired(session, mock_pool)
    assert revoked_1 is False
    assert mock_conn.fetchrow.await_count == 1
    assert tok_hash in _VERIFIED_TOKEN_CACHE

    # Second call within 30s TTL hits in-memory cache without DB query
    revoked_2 = await _is_session_revoked_or_expired(session, mock_pool)
    assert revoked_2 is False
    assert mock_conn.fetchrow.await_count == 1

    # Explicit invalidation clears entry
    invalidate_verified_token_cache(tok_hash)
    assert tok_hash not in _VERIFIED_TOKEN_CACHE


@pytest.mark.asyncio
async def test_coalesced_salience_ring_buffer_batching() -> None:
    from contextlib import asynccontextmanager

    from nce import semantic_search as ss

    ss._SALIENCE_RING_BUFFER.clear()
    mock_pool = MagicMock()
    mock_conn = AsyncMock()
    id1, id2, id3 = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    ns_id = "00000000-0000-4000-8000-000000000001"

    @asynccontextmanager
    async def fake_scoped_pg_session(pool, namespace_id):
        yield mock_conn

    with (
        patch("nce.semantic_search.scoped_pg_session", side_effect=fake_scoped_pg_session),
        patch("nce.salience.reinforce_batch", new_callable=AsyncMock) as mock_rb,
    ):
        await asyncio.gather(
            ss._fire_reinforcement(
                mock_pool,
                [{"memory_id": id1}, {"memory_id": id2}],
                "ag1",
                ns_id,
                0.05,
            ),
            ss._fire_reinforcement(
                mock_pool,
                [{"memory_id": id2}, {"memory_id": id3}],
                "ag1",
                ns_id,
                0.05,
            ),
        )
        assert mock_rb.await_count >= 1
        all_ids: set[str] = set()
        for call in mock_rb.await_args_list:
            all_ids.update(call.args[1])
        assert all_ids == {id1, id2, id3}


def test_plugins_descriptor_and_rls_caches() -> None:
    from nce.plugins import (
        get_all_plugin_global_tables,
        get_all_plugin_tenant_rls_tables,
        get_plugin_descriptors,
    )

    d1 = get_plugin_descriptors()
    d2 = get_plugin_descriptors()
    assert d1 == d2

    t1 = get_all_plugin_tenant_rls_tables()
    t2 = get_all_plugin_tenant_rls_tables()
    assert t1 == t2

    g1 = get_all_plugin_global_tables()
    g2 = get_all_plugin_global_tables()
    assert g1 == g2
