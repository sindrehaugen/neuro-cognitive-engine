"""Delta pagination URL safety for document bridges."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from nce.bridges.sharepoint import GRAPH_DELTA_URL_PREFIXES, SharePointBridge
from nce.net_safety import BridgeURLValidationError, assert_url_allowed_prefix


@pytest.mark.asyncio
async def test_sharepoint_delta_prefix_allows_graph_https() -> None:
    await assert_url_allowed_prefix(
        "https://graph.microsoft.com/v1.0/sites/x/drives/y/root/delta",
        GRAPH_DELTA_URL_PREFIXES,
        what="test",
    )


@pytest.mark.asyncio
async def test_sharepoint_delta_prefix_rejects_internal_host() -> None:
    with pytest.raises(BridgeURLValidationError, match="non-public"):
        await assert_url_allowed_prefix(
            "https://169.254.169.254/latest/meta-data",
            GRAPH_DELTA_URL_PREFIXES,
            what="SharePoint Graph delta URL",
        )


@pytest.mark.asyncio
async def test_sharepoint_delta_pages_rejects_poisoned_next_link() -> None:
    import ipaddress

    bridge = SharePointBridge()
    bridge._oauth_token_override = "token"  # noqa: SLF001 — test hook
    poisoned = "https://evil.example.com/delta"
    mock_redis = MagicMock()
    mock_redis.get.return_value = poisoned.encode("utf-8")

    with patch("nce.bridges.sharepoint.redis_client", return_value=mock_redis):
        with patch(
            "nce.net_safety._resolve_ips",
            return_value=[ipaddress.ip_address("8.8.8.8")],
        ):
            with pytest.raises(BridgeURLValidationError, match="allowed prefixes"):
                async for _ in bridge._delta_pages("site", "drive"):  # noqa: SLF001
                    pass
