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

    # Force a small startup depth, and seed a neighbour namespace with a higher
    # event_seq counter than the test namespace. CI runs against a fresh,
    # empty event_log per run, so without a neighbour the global max(event_seq)
    # equals this namespace's own max and the unpredicated query is
    # indistinguishable from the namespace-scoped one -- the test would pass
    # even if the WHERE namespace_id predicate were missing.
    monkeypatch.setenv("NCE_CHAIN_VERIFY_STARTUP_DEPTH", "2")
    monkeypatch.setattr(cfg, "NCE_CHAIN_VERIFY_STARTUP_DEPTH", 2)

    neighbour_ns_id = await make_namespace()
    async with scoped_pg_session(pg_pool, neighbour_ns_id) as conn:
        for i in range(10):
            await append_event(
                conn=conn,
                namespace_id=neighbour_ns_id,
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


@pytest.mark.asyncio
async def test_chain_verification_scopes_max_event_seq_to_namespace(monkeypatch):
    """Unit guard (no DB): the max(event_seq) lookup inside _chain_verification_tick
    must carry a WHERE namespace_id predicate, with the *matching* namespace id bound
    as a query argument for each namespace scanned -- two distinct namespaces are used
    so a hardcoded or swapped id would be caught, not just "some id was passed".
    """
    from contextlib import asynccontextmanager

    import nce.cron as cron_mod

    ns_a = uuid.uuid4()
    ns_b = uuid.uuid4()
    calls: list[tuple[str, tuple]] = []
    opened: list = []

    class FakeConn:
        async def fetchval(self, query, *args):
            calls.append((query, args))
            return 0

        async def fetch(self, query, *args):
            calls.append((query, args))
            if "SELECT id FROM namespaces" in query:
                return [{"id": ns_a}, {"id": ns_b}]
            return []

        async def execute(self, query, *args):
            calls.append((query, args))
            return None

    fake_conn = FakeConn()

    @asynccontextmanager
    async def fake_scoped_pg_session(pool, namespace_id):
        # Record which namespace this session was opened for, so the assertion
        # below can pair each max(event_seq) call with its own namespace rather
        # than checking that both ids merely appear somewhere.
        opened.append(namespace_id)
        yield fake_conn

    @asynccontextmanager
    async def fake_unmanaged_pg_connection(pool, *, site):
        yield fake_conn

    async def fake_verify_merkle_chain(conn, *, namespace_id, start_seq):
        return {"valid": True}

    monkeypatch.setattr(cron_mod, "scoped_pg_session", fake_scoped_pg_session)
    monkeypatch.setattr(cron_mod, "unmanaged_pg_connection", fake_unmanaged_pg_connection)
    monkeypatch.setattr("nce.event_log.verify_merkle_chain", fake_verify_merkle_chain)
    monkeypatch.setattr(cron_mod, "acquire_cron_lock", AsyncMock(return_value=object()))
    monkeypatch.setattr(cron_mod, "release_cron_lock", AsyncMock())

    await _chain_verification_tick(MagicMock())

    max_seq_calls = [(q, a) for q, a in calls if "max(event_seq)" in q]
    assert len(max_seq_calls) == 2
    for query, args in max_seq_calls:
        assert "WHERE namespace_id" in query
        assert len(args) == 1

    # Pair each call with the namespace whose scoped session produced it. A set
    # comparison would pass on a symmetric swap -- ns_a's iteration binding
    # ns_b's id and vice versa -- which is precisely the cross-namespace
    # confusion this batch exists to prevent, so assert the pairing in order.
    assert opened == [ns_a, ns_b]
    assert [args[0] for _, args in max_seq_calls] == [ns_a, ns_b]
