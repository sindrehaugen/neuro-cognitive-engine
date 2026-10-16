"""Unit and boot tests for scheduled temporal decay jobs in nce/cron.py."""

from __future__ import annotations

import os

os.environ.setdefault("NCE_MASTER_KEY", "x" * 32)
os.environ.setdefault("MINIO_ACCESS_KEY", "test-minio-key")
os.environ.setdefault("MINIO_SECRET_KEY", "test-minio-secret")

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nce.config import cfg
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

    # We patch the database/event loops, sleep jitter, and all tick functions to avoid executing
    # actual background queries or connecting to live services during boot.
    with (
        patch("asyncpg.create_pool", new_callable=AsyncMock, return_value=mock_pool),
        patch("asyncio.Event.wait", side_effect=StopMain),
        patch("asyncio.sleep", new_callable=AsyncMock),
        patch.object(cfg, "CRON_STARTUP_JITTER_MAX_SECONDS", 0.0),
        patch.object(cfg, "validate"),
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


@pytest.mark.asyncio
async def test_decay_prune_tick_scopes_queries_per_namespace() -> None:
    """Verify _decay_prune_tick iterates namespaces, sets RLS context, and filters by namespace_id."""
    import uuid
    from contextlib import asynccontextmanager

    from nce.cron_lock import CronLock
    from nce.temporal_decay import MemoryClass, _decay_prune_tick

    ns_a = uuid.uuid4()
    ns_b = uuid.uuid4()
    active_ns: list[str] = []
    prune_calls: list[tuple[str, str, object]] = []

    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[{"id": ns_a}, {"id": ns_b}])

    async def _execute(sql: str, *args):
        if "set_config('nce.namespace_id'" in sql:
            active_ns.append(str(args[0]))
            return "SELECT 1"
        if "WITH candidates AS" in sql:
            assert "namespace_id = $4::uuid" in sql
            assert "m.namespace_id = $4::uuid" in sql
            prune_calls.append((active_ns[-1] if active_ns else "", str(args[0]), args[3]))
            return "UPDATE 1"
        return "OK"

    conn.execute = AsyncMock(side_effect=_execute)

    @asynccontextmanager
    async def _unmanaged(_pool, *, site: str):
        yield conn

    fake_lock = CronLock(job_id="decay_prune", key="k", token="t", ttl_seconds=3660)
    with (
        patch("nce.cron_lock.acquire_cron_lock", new_callable=AsyncMock, return_value=fake_lock),
        patch("nce.cron_lock.release_cron_lock", new_callable=AsyncMock),
        patch("nce.db_utils.unmanaged_pg_connection", _unmanaged),
    ):
        await _decay_prune_tick(MagicMock())

    assert active_ns == [str(ns_a), str(ns_b)]
    assert len(prune_calls) == 2 * len(MemoryClass)
    for bound_ns, _mc, arg_ns in prune_calls[: len(MemoryClass)]:
        assert bound_ns == str(ns_a)
        assert arg_ns == ns_a
    for bound_ns, _mc, arg_ns in prune_calls[len(MemoryClass) :]:
        assert bound_ns == str(ns_b)
        assert arg_ns == ns_b


@pytest.mark.asyncio
async def test_decay_prune_tick_uses_salience_updated_at_in_ebbinghaus_clock() -> None:
    """Verify _decay_prune_tick uses GREATEST(m.valid_from, COALESCE((SELECT MAX(ms2.updated_at)...))) for Ebbinghaus clock."""
    import uuid
    from contextlib import asynccontextmanager

    from nce.cron_lock import CronLock
    from nce.temporal_decay import _decay_prune_tick

    ns = uuid.uuid4()
    executed_queries: list[str] = []

    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[{"id": ns}])

    async def _execute(sql: str, *args):
        if "WITH candidates AS" in sql:
            executed_queries.append(sql)
            return "UPDATE 1"
        return "OK"

    conn.execute = AsyncMock(side_effect=_execute)

    @asynccontextmanager
    async def _unmanaged(_pool, *, site: str):
        yield conn

    fake_lock = CronLock(job_id="decay_prune", key="k", token="t", ttl_seconds=3660)
    with (
        patch("nce.cron_lock.acquire_cron_lock", new_callable=AsyncMock, return_value=fake_lock),
        patch("nce.cron_lock.release_cron_lock", new_callable=AsyncMock),
        patch("nce.db_utils.unmanaged_pg_connection", _unmanaged),
    ):
        await _decay_prune_tick(MagicMock())

    assert len(executed_queries) > 0
    candidate_sql = executed_queries[0]
    assert (
        "GREATEST(m.valid_from, COALESCE((SELECT MAX(ms2.updated_at) FROM memory_salience ms2 WHERE ms2.memory_id = m.id), m.valid_from))"
        in candidate_sql
    )
