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
