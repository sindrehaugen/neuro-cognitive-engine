"""Bridge provider allowlist must stay consistent across runtime, MCP, and repo."""

from __future__ import annotations

import pytest

from nce import bridge_mcp_handlers, bridge_repo, bridge_runtime
from nce.bridge_providers import BRIDGE_PROVIDERS


def test_bridge_provider_allowlists_match() -> None:
    assert bridge_runtime.PROVIDERS == BRIDGE_PROVIDERS
    assert bridge_mcp_handlers.PROVIDERS == BRIDGE_PROVIDERS
    assert BRIDGE_PROVIDERS == frozenset({"sharepoint", "gdrive", "dropbox"})


@pytest.mark.asyncio
async def test_bridge_repo_rejects_unknown_provider() -> None:
    from unittest.mock import AsyncMock

    conn = AsyncMock()
    assert await bridge_repo.fetch_oauth_token_enc(conn, "onedrive", client_state="x") is None
    assert await bridge_repo.fetch_active_subscription(conn, "box", client_state="x") is None
    conn.fetchrow.assert_not_called()


def test_bridge_base_redis_client_is_lru_cached() -> None:
    """redis_client() in nce.bridges.base caches the Redis client instance via lru_cache."""
    from unittest.mock import MagicMock, patch

    from nce.bridges import base as bridge_base

    assert hasattr(bridge_base.redis_client, "cache_clear")
    bridge_base.redis_client.cache_clear()
    try:
        fake_client = MagicMock()
        with patch("redis.Redis.from_url", return_value=fake_client) as mock_from_url:
            c1 = bridge_base.redis_client()
            c2 = bridge_base.redis_client()
            assert c1 is fake_client
            assert c2 is fake_client
            mock_from_url.assert_called_once()
    finally:
        bridge_base.redis_client.cache_clear()


@pytest.mark.asyncio
async def test_sharepoint_delta_pages_offloads_redis_get_and_set_to_thread() -> None:
    """SharePointBridge._delta_pages runs synchronous r.get and r.set via asyncio.to_thread."""
    import asyncio
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock, MagicMock, patch

    from nce.bridges.sharepoint import SharePointBridge

    fake_redis = MagicMock()
    fake_redis.get.return_value = b"https://graph.microsoft.com/v1.0/saved-delta"

    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.raise_for_status = MagicMock()
    fake_resp.json.return_value = {
        "value": [{"id": "item-1", "name": "doc.txt"}],
        "@odata.deltaLink": "https://graph.microsoft.com/v1.0/new-delta",
    }

    fake_http_client = MagicMock()
    fake_http_client.get = AsyncMock(return_value=fake_resp)

    @asynccontextmanager
    async def _fake_async_client(*args, **kwargs):
        yield fake_http_client

    to_thread_calls: list[tuple[object, ...]] = []
    real_to_thread = asyncio.to_thread

    async def _spy_to_thread(func, *args, **kwargs):
        to_thread_calls.append((func, *args))
        return await real_to_thread(func, *args, **kwargs)

    bridge = SharePointBridge()
    bridge._oauth_token_override = "tok-1"
    with (
        patch("nce.bridges.sharepoint.redis_client", return_value=fake_redis),
        patch("nce.bridges.sharepoint.asyncio.to_thread", side_effect=_spy_to_thread),
        patch("nce.bridges.sharepoint.assert_url_allowed_prefix", new_callable=AsyncMock),
        patch("nce.bridges.sharepoint.httpx.AsyncClient", side_effect=_fake_async_client),
    ):
        items = [item async for item in bridge._delta_pages("site-1", "drive-1")]

    assert items == [{"id": "item-1", "name": "doc.txt"}]
    ck = "bridge:cursor:sharepoint:site-1:drive-1"
    assert (fake_redis.get, ck) in to_thread_calls
    assert (fake_redis.set, ck, "https://graph.microsoft.com/v1.0/new-delta") in to_thread_calls
