"""Unit and boot tests for scheduled temporal decay jobs in nce/cron.py."""

from __future__ import annotations

import os

os.environ.setdefault("NCE_MASTER_KEY", "x" * 32)

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nce.cron import async_main


class StopMain(Exception):
    pass


@pytest.mark.asyncio
async def test_cron_boot_registers_decay_prune():
    """Verify that during cron server initialization (async_main),

    the temporal decay prune job is registered with the correct ID.
    """
    mock_pool = MagicMock()
    mock_pool.close = AsyncMock()

    # We patch the database/event loops and all tick functions to avoid executing
    # actual background queries or connecting to live services during boot.
    with (
        patch("asyncpg.create_pool", new_callable=AsyncMock, return_value=mock_pool),
        patch("asyncio.Event.wait", side_effect=StopMain),
        patch("nce.cron._renewal_tick", new_callable=AsyncMock),
        patch("nce.cron._reembedding_tick", new_callable=AsyncMock),
        patch("nce.cron._consolidation_tick", new_callable=AsyncMock),
        patch("nce.cron._partition_maintenance_tick", new_callable=AsyncMock),
        patch("nce.cron._saga_recovery_tick", new_callable=AsyncMock),
        patch("nce.cron._outbox_relay_tick", new_callable=AsyncMock),
        patch("nce.cron._decay_prune_tick", new_callable=AsyncMock),
        patch("nce.cron._d365_sync_tick", new_callable=AsyncMock),
        patch("nce.cron._d365_netbox_bridge_tick", new_callable=AsyncMock),
    ):
        added_jobs = []

        def mock_add_job(func, trigger, *args, **kwargs):
            added_jobs.append(kwargs.get("id"))

        with patch("nce.cron.AsyncIOScheduler") as mock_scheduler_cls:
            mock_scheduler = MagicMock()
            mock_scheduler.add_job = mock_add_job
            mock_scheduler_cls.return_value = mock_scheduler

            try:
                await async_main()
            except StopMain:
                pass

            assert "phase_2_2_decay_prune" in added_jobs
