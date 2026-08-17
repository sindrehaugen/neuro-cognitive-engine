"""Focused bridge MCP handler tests (oauth resilience path)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from nce import bridge_mcp_handlers


@pytest.mark.asyncio
async def test_exchange_oauth_uses_resilient_token_post(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bridge_mcp_handlers.cfg, "AZURE_CLIENT_ID", "c1")
    monkeypatch.setattr(bridge_mcp_handlers.cfg, "AZURE_CLIENT_SECRET", "s1")
    monkeypatch.setattr(bridge_mcp_handlers.cfg, "BRIDGE_OAUTH_REDIRECT_URI", "http://127.0.0.1/r")
    mock_post = AsyncMock(
        return_value={
            "access_token": "at",
            "refresh_token": "rt",
            "expires_in": 3600,
        }
    )
    with patch("nce.bridge_mcp_handlers.oauth_token_post_form", mock_post) as patched:
        tok = await bridge_mcp_handlers._exchange_oauth_code("sharepoint", "code")
    assert tok["access_token"] == "at"
    patched.assert_awaited_once()
    assert patched.await_args.kwargs["operation"] == "oauth_exchange:sharepoint"


def test_token_payload_from_oauth_response_requires_access_token() -> None:
    with pytest.raises(ValueError, match="missing access_token"):
        bridge_mcp_handlers._token_payload_from_oauth_response({})
