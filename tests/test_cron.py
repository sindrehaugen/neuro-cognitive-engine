"""Unit tests for scheduled cron jobs in nce/cron.py."""

from __future__ import annotations

import os

os.environ.setdefault("NCE_MASTER_KEY", "x" * 32)

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nce.cron import _reembedding_tick


@pytest.mark.asyncio
async def test_reembedding_tick_acquires_and_releases_lock():
    mock_pool = MagicMock()
    mock_mongo = MagicMock()

    mock_lock = MagicMock()

    with (
        patch(
            "nce.cron.acquire_cron_lock", new_callable=AsyncMock, return_value=mock_lock
        ) as mock_acquire,
        patch("nce.cron.release_cron_lock", new_callable=AsyncMock) as mock_release,
        patch("nce.cron.ReembeddingWorker") as mock_worker_cls,
    ):
        mock_worker = MagicMock()
        mock_worker.run_once = AsyncMock(return_value={"processed": 5})
        mock_worker_cls.return_value = mock_worker

        await _reembedding_tick(mock_pool, mock_mongo)

        # Verify acquire_cron_lock is called with the correct parameters
        from nce.cron import _REEMBED_INTERVAL

        expected_ttl = _REEMBED_INTERVAL * 60 + 60
        mock_acquire.assert_awaited_once_with("reembedding", expected_ttl)

        # Verify ReembeddingWorker is run
        mock_worker.run_once.assert_awaited_once_with(mock_pool, mock_mongo)

        # Verify lock is released
        mock_release.assert_awaited_once_with(mock_lock)


@pytest.mark.asyncio
async def test_reembedding_tick_bails_if_lock_held():
    mock_pool = MagicMock()
    mock_mongo = MagicMock()

    with (
        patch("nce.cron.acquire_cron_lock", new_callable=AsyncMock, return_value=None),
        patch("nce.cron.release_cron_lock", new_callable=AsyncMock) as mock_release,
        patch("nce.cron.ReembeddingWorker") as mock_worker_cls,
    ):
        await _reembedding_tick(mock_pool, mock_mongo)

        mock_worker_cls.assert_not_called()
        mock_release.assert_not_called()


@pytest.mark.asyncio
async def test_reembedding_tick_failure_alerts():
    mock_pool = MagicMock()
    mock_mongo = MagicMock()
    mock_lock = MagicMock()

    with (
        patch("nce.cron.acquire_cron_lock", new_callable=AsyncMock, return_value=mock_lock),
        patch("nce.cron.release_cron_lock", new_callable=AsyncMock),
        patch("nce.cron.ReembeddingWorker") as mock_worker_cls,
        patch(
            "nce.notifications.dispatcher.dispatch_alert", new_callable=AsyncMock
        ) as mock_dispatch,
    ):
        mock_worker = MagicMock()
        mock_worker.run_once = AsyncMock(side_effect=ValueError("Simulated worker error"))
        mock_worker_cls.return_value = mock_worker

        await _reembedding_tick(mock_pool, mock_mongo)

        mock_dispatch.assert_awaited_once()
        args, kwargs = mock_dispatch.call_args
        assert "Cron Job Failed: reembedding" in args[0]
        assert "Simulated worker error" in args[1]


@pytest.mark.asyncio
async def test_outbox_relay_tick_failure_alerts():
    import asyncpg

    from nce.cron import _outbox_relay_tick

    mock_pool = MagicMock()
    mock_lock = MagicMock()

    with (
        patch("nce.cron.acquire_cron_lock", new_callable=AsyncMock, return_value=mock_lock),
        patch("nce.cron.release_cron_lock", new_callable=AsyncMock),
        patch(
            "nce.outbox_relay.run_outbox_relay_once",
            new_callable=AsyncMock,
            side_effect=asyncpg.PostgresError("Simulated relay DB error"),
        ),
        patch(
            "nce.notifications.dispatcher.dispatch_alert", new_callable=AsyncMock
        ) as mock_dispatch,
    ):
        await _outbox_relay_tick(mock_pool)

        mock_dispatch.assert_awaited_once()
        args, kwargs = mock_dispatch.call_args
        assert "Cron Job Failed: outbox_relay" in args[0]
        assert "Simulated relay DB error" in args[1]
