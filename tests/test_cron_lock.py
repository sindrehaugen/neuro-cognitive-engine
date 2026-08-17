"""Unit tests for the distributed cron lock helper in nce/cron_lock.py."""

from __future__ import annotations

import os

os.environ.setdefault("NCE_MASTER_KEY", "x" * 32)

from unittest.mock import AsyncMock, patch

import pytest

from nce.cron_lock import CronLock
from nce.cron_lock import acquire_cron_lock as _acquire_cron_lock


class TestAcquireCronLockNoRedis:
    @pytest.mark.asyncio
    async def test_returns_local_disabled_lock_when_redis_url_empty(self):
        with patch("nce.cron_lock.cfg") as mock_cfg:
            mock_cfg.REDIS_URL = ""
            mock_cfg.IS_PROD = False
            result = await _acquire_cron_lock("test_job", 300)
        assert isinstance(result, CronLock)
        assert result.job_id == "test_job"
        assert result.token == "local-disabled"

    @pytest.mark.asyncio
    async def test_returns_none_on_redis_exception(self):
        with patch("nce.cron_lock.cfg") as mock_cfg:
            mock_cfg.REDIS_URL = "redis://localhost:6379"
            mock_cfg.IS_PROD = False
            with patch("redis.asyncio.Redis.from_url", side_effect=ConnectionError("down")):
                result = await _acquire_cron_lock("test_job", 300)
        assert result is None


class TestAcquireCronLockAcquired:
    @pytest.mark.asyncio
    async def test_returns_lock_when_set_nx_succeeds(self):
        mock_client = AsyncMock()
        mock_client.set = AsyncMock(return_value=True)
        mock_client.aclose = AsyncMock()

        with patch("nce.cron_lock.cfg") as mock_cfg:
            mock_cfg.REDIS_URL = "redis://localhost:6379"
            mock_cfg.IS_PROD = False
            with patch("redis.asyncio.Redis.from_url", return_value=mock_client):
                result = await _acquire_cron_lock("bridge_subscription_renewal", 2760)

        assert isinstance(result, CronLock)
        assert result.job_id == "bridge_subscription_renewal"
        mock_client.set.assert_awaited_once_with(
            "nce:cron:lock:bridge_subscription_renewal", result.token, nx=True, ex=2760
        )
        mock_client.aclose.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_returns_none_when_set_nx_returns_none(self):
        mock_client = AsyncMock()
        mock_client.set = AsyncMock(return_value=None)
        mock_client.aclose = AsyncMock()

        with patch("nce.cron_lock.cfg") as mock_cfg:
            mock_cfg.REDIS_URL = "redis://localhost:6379"
            mock_cfg.IS_PROD = False
            with patch("redis.asyncio.Redis.from_url", return_value=mock_client):
                result = await _acquire_cron_lock("sleep_consolidation", 7260)

        assert result is None

    @pytest.mark.asyncio
    async def test_lock_key_includes_job_id(self):
        mock_client = AsyncMock()
        mock_client.set = AsyncMock(return_value=True)
        mock_client.aclose = AsyncMock()

        with patch("nce.cron_lock.cfg") as mock_cfg:
            mock_cfg.REDIS_URL = "redis://localhost:6379"
            mock_cfg.IS_PROD = False
            with patch("redis.asyncio.Redis.from_url", return_value=mock_client):
                await _acquire_cron_lock("event_log_partition_maintenance", 3600)

        key_used = mock_client.set.call_args.args[0]
        assert key_used == "nce:cron:lock:event_log_partition_maintenance"
