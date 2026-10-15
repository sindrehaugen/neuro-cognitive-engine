"""Bridge dispatch and RQ worker entrypoints."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nce.bridge_runtime import resolve_stored_oauth_access_token_async
from nce.bridges import dispatch_bridge_event
from nce.bridges.base import BridgeAuthError
from nce.bridges.sharepoint import SharePointBridge
from nce.config import cfg
from nce.tasks import process_bridge_event


@pytest.mark.asyncio
async def test_dispatch_bridge_event_unknown_provider() -> None:
    with pytest.raises(ValueError, match="Unknown bridge provider"):
        await dispatch_bridge_event("onedrive", {})


@patch("nce.bridges.dispatch_bridge_event", return_value={"status": "ok"})
@patch("nce.tasks._clear_attempt")
@patch("nce.tasks._get_redis")
@patch("nce.tasks._get_job_id", return_value="job-1")
def test_process_bridge_event_success(
    _job: object,
    _redis: object,
    _clear: object,
    mock_dispatch: object,
) -> None:
    out = process_bridge_event("sharepoint", {"notifications": []})
    assert out == {"status": "ok"}
    mock_dispatch.assert_called_once_with("sharepoint", {"notifications": []})


@patch(
    "nce.bridges.dispatch_bridge_event",
    side_effect=ValueError("bad payload"),
)
@patch("nce.tasks._get_redis")
@patch("nce.tasks._get_job_id", return_value="job-2")
def test_process_bridge_event_value_error_returns_error_dict(
    _job: object,
    _redis: object,
    _dispatch: object,
) -> None:
    out = process_bridge_event("gdrive", {})
    assert out["status"] == "error"
    assert "bad payload" in out["error"]


@patch("nce.bridges.dispatch_bridge_event", side_effect=BridgeAuthError("Token expired"))
@patch("nce.tasks._check_poison_pill", return_value=(True, 3, "BridgeAuthError: Token expired"))
@patch("nce.tasks._store_dlq_async")
@patch("nce.tasks._get_redis")
@patch("nce.tasks._get_job_id", return_value="job-3")
def test_process_bridge_event_auth_error_routes_to_dlq(
    _job: object,
    _redis: object,
    mock_store_dlq: object,
    mock_check_poison: object,
    _dispatch: object,
) -> None:
    out = process_bridge_event("sharepoint", {"notifications": []})
    assert out == {"status": "dead_lettered", "job_id": "job-3"}
    mock_check_poison.assert_called_once()
    mock_store_dlq.assert_called_once()


@pytest.mark.asyncio
async def test_resolve_stored_oauth_access_token_async_reuses_provided_pg_pool() -> None:
    mock_conn = object()
    mock_cm = AsyncMock()
    mock_cm.__aenter__.return_value = mock_conn
    mock_cm.__aexit__.return_value = False
    mock_pool = MagicMock()
    mock_pool.acquire.return_value = mock_cm
    sub_row = {"id": "sub-123", "provider": "sharepoint"}

    with (
        patch("nce.bridge_runtime.NCEEngine") as mock_engine_cls,
        patch(
            "nce.bridge_runtime.bridge_repo.fetch_active_subscription",
            new=AsyncMock(return_value=sub_row),
        ) as mock_fetch,
        patch(
            "nce.bridge_runtime.ensure_fresh_oauth_token",
            new=AsyncMock(return_value="pooled-access-token"),
        ) as mock_ensure,
    ):
        token = await resolve_stored_oauth_access_token_async(
            "sharepoint",
            client_state="cs-pool-1",
            pg_pool=mock_pool,
        )

    assert token == "pooled-access-token"
    mock_engine_cls.assert_not_called()
    mock_pool.acquire.assert_called_once_with(timeout=10.0)
    mock_fetch.assert_awaited_once_with(
        mock_conn,
        "sharepoint",
        client_state="cs-pool-1",
        subscription_id=None,
        resource_id=None,
    )
    mock_ensure.assert_awaited_once_with(mock_pool, sub_row, "")


@pytest.mark.asyncio
async def test_sharepoint_walk_delta_awaits_async_token_resolver_in_running_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cfg, "GRAPH_BRIDGE_TOKEN", "")
    bridge = SharePointBridge()
    observed_tokens: list[str] = []

    async def _fake_delta_pages(site_id: str, drive_id: str) -> AsyncIterator[dict[str, Any]]:
        assert (site_id, drive_id) == ("site-1", "drive-1")
        observed_tokens.append(bridge.bearer_token())
        yield {"id": "item-1", "file": {}, "name": "spec.pdf"}

    monkeypatch.setattr(bridge, "_delta_pages", _fake_delta_pages)

    mock_conn = object()
    mock_cm = AsyncMock()
    mock_cm.__aenter__.return_value = mock_conn
    mock_cm.__aexit__.return_value = False
    mock_pool = MagicMock()
    mock_pool.acquire.return_value = mock_cm
    sub_row = {"id": "sub-sp-1", "provider": "sharepoint"}

    with (
        patch("nce.bridge_runtime.NCEEngine") as mock_engine_cls,
        patch(
            "nce.bridge_runtime.bridge_repo.fetch_active_subscription",
            new=AsyncMock(return_value=sub_row),
        ) as mock_fetch,
        patch(
            "nce.bridge_runtime.ensure_fresh_oauth_token",
            new=AsyncMock(return_value="async-loop-token"),
        ) as mock_ensure,
    ):
        items = [
            item
            async for item in bridge.walk_delta(
                {
                    "pg_pool": mock_pool,
                    "notifications": [
                        {
                            "clientState": "cs-live",
                            "resource": "sites/site-1/drives/drive-1/root",
                        }
                    ],
                }
            )
        ]

    assert len(items) == 1
    assert items[0]["id"] == "item-1"
    assert observed_tokens == ["async-loop-token"]
    mock_engine_cls.assert_not_called()
    mock_fetch.assert_awaited_once_with(
        mock_conn,
        "sharepoint",
        client_state="cs-live",
        subscription_id=None,
        resource_id=None,
    )
    mock_ensure.assert_awaited_once_with(mock_pool, sub_row, "")
