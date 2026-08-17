"""Integration and unit tests for scheduled Merkle chain verification in nce/cron.py."""

from __future__ import annotations

import os
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nce.config import cfg
from nce.cron import _chain_verification_tick, async_main
from nce.db_utils import scoped_pg_session
from nce.event_log import append_event
from nce.observability import MERKLE_CHAIN_VALID

# Ensure NCE_MASTER_KEY is populated for the config loader
os.environ.setdefault("NCE_MASTER_KEY", "x" * 32)


class StopMain(Exception):
    pass


@pytest.mark.asyncio
async def test_cron_boot_registers_chain_verification():
    """Verify that during cron server initialization (async_main),
    the chain verification job is registered with the correct ID.
    """
    mock_pool = MagicMock()
    mock_pool.close = AsyncMock()

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
        patch("nce.cron._chain_verification_tick", new_callable=AsyncMock),
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

            assert "chain_verification" in added_jobs


@pytest.mark.integration
@pytest.mark.asyncio
async def test_chain_verification_integration(pg_pool, make_namespace, monkeypatch) -> None:
    """Integration test for continuous Merkle chain verification tick.
    Tampers a row via a dev NCE_BYPASS_WORM conn, runs the tick,
    asserts gauge=0 + a chain_verification_failed event exists;
    clean run leaves gauge=1.
    """
    # 1. Create a namespace
    ns_id = await make_namespace()
    agent_id = "test-chain-verify-agent"

    # Intercept namespace scan to only return this namespace, isolating our test
    # from other leftover namespaces in the shared test database.
    from contextlib import asynccontextmanager

    import nce.cron as cron_mod

    original_unmanaged = cron_mod.unmanaged_pg_connection

    class ConnectionProxy:
        def __init__(self, conn):
            self._conn = conn

        def __getattr__(self, name):
            return getattr(self._conn, name)

        async def fetch(self, query, *args, **kwargs):
            if "SELECT id FROM namespaces" in query:
                return [{"id": ns_id}]
            return await self._conn.fetch(query, *args, **kwargs)

    @asynccontextmanager
    async def mock_unmanaged(pool, *, site):
        if site == "cron.chain_verify.namespace_scan":
            async with original_unmanaged(pool, site=site) as conn:
                yield ConnectionProxy(conn)
        else:
            async with original_unmanaged(pool, site=site) as conn:
                yield conn

    monkeypatch.setattr(cron_mod, "unmanaged_pg_connection", mock_unmanaged)

    # 2. Append 3 pristine events to namespace
    async with scoped_pg_session(pg_pool, ns_id) as conn:
        for i in range(3):
            await append_event(
                conn=conn,
                namespace_id=ns_id,
                agent_id=agent_id,
                event_type="store_memory",
                params={
                    "saga_id": str(uuid.uuid4()),
                    "memory_id": str(uuid.uuid4()),
                    "payload_ref": f"00000000000000000000000{i}",
                    "assertion_type": "fact",
                    "entities": [],
                    "triplets": [],
                },
            )

    # 3. Perform a clean verification run
    MERKLE_CHAIN_VALID.set(-1)
    await _chain_verification_tick(pg_pool)

    # Check gauge is set to 1 if it has _value
    if hasattr(MERKLE_CHAIN_VALID, "_value"):
        assert MERKLE_CHAIN_VALID._value.get() == 1

    # 4. Tamper a row inside event_log under NCE_BYPASS_WORM by disabling trigger first
    monkeypatch.setenv("NCE_BYPASS_WORM", "true")
    monkeypatch.setattr(cfg, "NCE_BYPASS_WORM", True)

    async with scoped_pg_session(pg_pool, ns_id) as conn:
        await conn.execute("ALTER TABLE event_log DISABLE TRIGGER trg_event_log_worm")
        try:
            # Update the params of event_seq = 2 to corrupt the Merkle chain hash
            await conn.execute(
                """
                UPDATE event_log
                SET params = '{"tampered": true, "saga_id": "00000000-0000-0000-0000-000000000000", "memory_id": "00000000-0000-0000-0000-000000000000", "payload_ref": "000000000000000000000000", "assertion_type": "fact", "entities": [], "triplets": []}'::jsonb
                WHERE namespace_id = $1 AND event_seq = 2
                """,
                ns_id,
            )
        finally:
            await conn.execute("ALTER TABLE event_log ENABLE TRIGGER trg_event_log_worm")

    # 5. Run verification tick again
    await _chain_verification_tick(pg_pool)

    # Check gauge is set to 0 if it has _value
    if hasattr(MERKLE_CHAIN_VALID, "_value"):
        assert MERKLE_CHAIN_VALID._value.get() == 0

    # 6. Assert that a 'chain_verification_failed' event exists for this namespace
    async with scoped_pg_session(pg_pool, ns_id) as conn:
        rows = await conn.fetch(
            """
            SELECT event_type, params
            FROM event_log
            WHERE namespace_id = $1 AND event_type = 'chain_verification_failed'
            ORDER BY event_seq DESC
            """,
            ns_id,
        )
        assert len(rows) >= 1
        failed_event = rows[0]
        params = failed_event["params"]
        if isinstance(params, str):
            import json

            params = json.loads(params)
        assert params.get("first_break") == 2
        assert (
            "mismatch" in params.get("reason", "").lower()
            or "broken" in params.get("reason", "").lower()
        )
