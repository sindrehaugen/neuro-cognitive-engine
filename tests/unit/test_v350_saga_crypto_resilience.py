"""Contract tests for v3.5.0 hardening: client factory, saga/memory, crypto, cron, GC, lifecycle."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from bson import ObjectId

# ---------------------------------------------------------------------------
# 1. R2 Client Factory
# ---------------------------------------------------------------------------


def test_client_factory_redis_motor_minio_defaults() -> None:
    from nce._client_factory import (
        make_async_redis,
        make_minio,
        make_sync_redis,
        motor_timeout_kwargs,
    )
    from nce.config import cfg

    with patch("nce._client_factory.aioredis.from_url", return_value=MagicMock()) as m_async:
        make_async_redis("redis://localhost:6379/0", max_connections=12)
        m_async.assert_called_once_with(
            "redis://localhost:6379/0",
            socket_connect_timeout=cfg.REDIS_CONNECT_TIMEOUT_S,
            socket_timeout=cfg.REDIS_SOCKET_TIMEOUT_S,
            health_check_interval=30,
            max_connections=12,
        )

    with patch("nce._client_factory.redis.from_url", return_value=MagicMock()) as m_sync:
        make_sync_redis("redis://localhost:6379/0", socket_timeout=9.0)
        m_sync.assert_called_once_with(
            "redis://localhost:6379/0",
            socket_connect_timeout=cfg.REDIS_CONNECT_TIMEOUT_S,
            socket_timeout=9.0,
            health_check_interval=30,
        )

    kw = motor_timeout_kwargs(serverSelectionTimeoutMS=2000)
    assert kw == {
        "serverSelectionTimeoutMS": 2000,
        "connectTimeoutMS": 5000,
        "socketTimeoutMS": 5000,
    }

    fake_minio_cls = MagicMock()
    make_minio("localhost:9000", "ak", "sk", secure=True, maxsize=8, minio_cls=fake_minio_cls)
    fake_minio_cls.assert_called_once()
    call_kw = fake_minio_cls.call_args.kwargs
    assert call_kw["secure"] is True
    http_client = call_kw["http_client"]
    assert http_client.connection_pool_kw["maxsize"] == 8
    assert http_client.connection_pool_kw["timeout"].connect_timeout == 2.0
    assert http_client.connection_pool_kw["timeout"].read_timeout == 5.0
    retries = http_client.connection_pool_kw["retries"]
    assert retries.total == 2
    assert retries.backoff_factor == 0.2
    assert set(retries.status_forcelist) == {502, 503, 504}


def test_consolidation_caches_minio_client() -> None:
    from nce import consolidation

    consolidation._get_cached_minio_client.cache_clear()
    try:
        sentinel_client = MagicMock()
        with patch("nce._client_factory.make_minio", return_value=sentinel_client) as m_make:
            c1 = consolidation._get_cached_minio_client("ep:9000", "a", "b", False)
            c2 = consolidation._get_cached_minio_client("ep:9000", "a", "b", False)
            assert c1 is sentinel_client
            assert c2 is sentinel_client
            m_make.assert_called_once()
    finally:
        consolidation._get_cached_minio_client.cache_clear()


@pytest.mark.asyncio
async def test_health_probe_closes_motor_client_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import health_probe

    monkeypatch.setattr(health_probe.cfg, "NCE_MASTER_KEY", "a" * 40)
    monkeypatch.setattr(health_probe.cfg, "validate", lambda: None)

    fake_redis = AsyncMock()
    fake_pg = AsyncMock()
    fake_motor = MagicMock()
    fake_motor.admin.command = AsyncMock(side_effect=RuntimeError("mongo down"))
    fake_motor.close = MagicMock()

    with (
        patch("nce._client_factory.make_async_redis", return_value=fake_redis),
        patch("health_probe.asyncpg.connect", AsyncMock(return_value=fake_pg)),
        patch("health_probe.AsyncIOMotorClient", return_value=fake_motor),
    ):
        ok = await health_probe.probe()

    assert ok is False
    fake_motor.close.assert_called_once()


# ---------------------------------------------------------------------------
# 2. S1, R1, P2, P3, P4 in MemoryOrchestrator
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_s1_store_artifact_fails_closed_without_staging_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from nce.models import ArtifactPayload
    from nce.orchestrators.memory import MemoryOrchestrator

    orch = MemoryOrchestrator(MagicMock(), MagicMock(), MagicMock(), minio_client=MagicMock())
    outside_file = tmp_path / "secret.txt"
    outside_file.write_text("secret")

    payload = ArtifactPayload(
        namespace_id=uuid4(),
        artifact_type="pdf",
        file_path_on_disk=str(outside_file),
        summary="test",
    )

    monkeypatch.setattr("nce.orchestrators.memory.cfg.NCE_ARTIFACT_STAGING_DIR", "")
    with pytest.raises(ValueError, match="NCE_ARTIFACT_STAGING_DIR must be configured"):
        await orch.store_artifact(payload)

    staging = tmp_path / "staging"
    staging.mkdir()
    monkeypatch.setattr("nce.orchestrators.memory.cfg.NCE_ARTIFACT_STAGING_DIR", str(staging))
    with pytest.raises(FileNotFoundError) as exc_info:
        await orch.store_artifact(payload)
    assert str(outside_file) not in str(exc_info.value)


@pytest.mark.asyncio
async def test_r1_rollback_outbox_savepoint_and_deferred_exhaustion() -> None:
    from nce.orchestrators.memory import MemoryOrchestrator

    conn = MagicMock()
    tx_count = 0

    @asynccontextmanager
    async def _tx():
        nonlocal tx_count
        tx_count += 1
        yield

    conn.transaction = _tx

    async def _exec(sql: str, *args: Any) -> str:
        if "DELETE FROM outbox_events" in sql:
            raise RuntimeError("outbox_events missing")
        return "DELETE 1"

    conn.execute = AsyncMock(side_effect=_exec)

    @asynccontextmanager
    async def _acquire(*args: Any, **kwargs: Any):
        yield conn

    pg_pool = MagicMock()
    pg_pool.acquire = _acquire

    orch = MemoryOrchestrator(pg_pool, MagicMock(), MagicMock())
    payload = MagicMock(namespace_id=uuid4(), agent_id="agent-1")

    with patch("nce.event_log.append_event", AsyncMock()):
        await orch._apply_rollback_on_failure(
            e=RuntimeError("boom"),
            payload=payload,
            collection=None,
            inserted_mongo_id=str(ObjectId()),
            inserted_result=None,
            memory_id=uuid4(),
            pg_committed=True,
        )

    assert tx_count == 2  # outer tx + nested savepoint for outbox_events

    bad_col = MagicMock()
    bad_col.delete_one = AsyncMock(side_effect=TimeoutError("still down"))
    with (
        patch("nce.orchestrators.memory.asyncio.sleep", AsyncMock()),
        patch("nce.orchestrators.memory.SagaMetrics.record_failure") as m_fail,
    ):
        await orch._deferred_mongo_cleanup(
            collection=bad_col,
            target_oid=ObjectId(),
            namespace_id=uuid4(),
            saga_id=str(uuid4()),
        )
    m_fail.assert_called_once_with("mongo_deferred_exhausted")


@pytest.mark.asyncio
async def test_p2_p3_shred_memory_namespace_filters_and_no_n1_loop() -> None:
    from nce.orchestrators.memory import MemoryOrchestrator

    orch = MemoryOrchestrator(MagicMock(), MagicMock(), MagicMock())
    ns_id = uuid4()
    mem_id = uuid4()
    executed_calls: list[tuple[str, tuple[Any, ...]]] = []

    conn = MagicMock()

    @asynccontextmanager
    async def _tx():
        yield

    conn.transaction = _tx
    conn.fetchrow = AsyncMock(
        return_value={
            "payload_ref": "507f1f77bcf86cd799439011",
            "dek_key_id": "k1",
            "was_encrypted": True,
            "user_id": "u1",
            "session_id": "s1",
            "agent_id": "a1",
            "metadata": {},
        }
    )

    async def _exec(sql: str, *args: Any) -> str:
        executed_calls.append((sql, args))
        return "DELETE 1"

    conn.execute = AsyncMock(side_effect=_exec)
    conn.fetch = AsyncMock(return_value=[])

    @asynccontextmanager
    async def _scoped(_pool: Any, _ns: Any):
        yield conn

    eval_order: list[str] = []

    async def _fake_eval(*args: Any, **kwargs: Any) -> set[str]:
        eval_order.append("eval")
        # Ensure UPDATE memories (setting valid_to) has not yet run
        assert not any("UPDATE memories" in sql for sql, _ in executed_calls)
        return set()

    append_ret = MagicMock(event_id=uuid4(), event_seq=1, occurred_at=datetime.now(timezone.utc))
    with (
        patch("nce.orchestrators.memory.scoped_pg_session", _scoped),
        patch("nce.atms.evaluate_atms_intervention", side_effect=_fake_eval),
        patch("nce.atms.persist_atms_invalidation", AsyncMock(return_value=0)),
        patch("nce.event_log.append_event", AsyncMock(return_value=append_ret)),
    ):
        await orch.shred_memory(str(mem_id), str(ns_id), "agent-1")

    assert eval_order == ["eval"]
    kg_sqls = [sql for sql, _ in executed_calls if "kg_" in sql]
    assert len(kg_sqls) == 3
    for sql in kg_sqls:
        assert "namespace_id = $2::uuid" in sql
    # Duplicate Python N+1 loop query must not exist
    conn.fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_p4_semantic_search_uses_read_pool_and_redis() -> None:
    from nce.orchestrators.memory import MemoryOrchestrator

    rw_pool = MagicMock()
    ro_pool = MagicMock()
    redis_cli = MagicMock()
    mongo_cli = MagicMock()

    orch = MemoryOrchestrator(rw_pool, mongo_cli, redis_cli, pg_read_pool=ro_pool)
    with patch("nce.semantic_search.semantic_search", AsyncMock(return_value=[])) as m_search:
        await orch.semantic_search("hello", str(uuid4()), "agent-1")

    m_search.assert_awaited_once()
    assert m_search.call_args.kwargs["pg_pool"] is ro_pool
    assert m_search.call_args.kwargs["redis_client"] is redis_cli


# ---------------------------------------------------------------------------
# 3. S6 Envelope Crypto & Key Invalidation Reconnect
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_s6_crypto_shred_subject_fails_closed_on_db_or_worm_error() -> None:
    from nce.crypto.envelope import EnvelopeCryptoManager, crypto_shred_subject

    mgr = EnvelopeCryptoManager()
    ns_id = str(uuid4())

    conn_db_fail = MagicMock()
    conn_db_fail.fetch = AsyncMock(side_effect=RuntimeError("pg error"))
    with pytest.raises(RuntimeError, match="pg error"):
        await crypto_shred_subject(conn_db_fail, ns_id, "subj-1", manager=mgr)

    conn_worm_fail = MagicMock()
    conn_worm_fail.fetch = AsyncMock(return_value=[{"dek_id": "dek-1"}])
    conn_worm_fail.is_in_transaction = MagicMock(return_value=True)
    with patch("nce.event_log.append_event", AsyncMock(side_effect=RuntimeError("worm fail"))):
        with pytest.raises(RuntimeError, match="worm fail"):
            await crypto_shred_subject(conn_worm_fail, ns_id, "subj-2", manager=mgr)


@pytest.mark.asyncio
async def test_s6_subscribe_key_invalidation_reconnects_and_cancels() -> None:
    from nce.crypto.envelope import EnvelopeCryptoManager

    mgr = EnvelopeCryptoManager()
    handled: list[Any] = []
    mgr.handle_invalidation_message = lambda data: handled.append(data)  # type: ignore[method-assign]

    listen_calls = 0

    class _FakePubSub:
        async def subscribe(self, _ch: str) -> None:
            return None

        async def aclose(self) -> None:
            return None

        async def listen(self):
            nonlocal listen_calls
            listen_calls += 1
            if listen_calls == 1:
                raise ConnectionError("redis dropped")
            yield {"type": "message", "data": json.dumps({"dek_id": "d1"})}
            await asyncio.sleep(60)

    redis_cli = MagicMock()
    redis_cli.pubsub = MagicMock(side_effect=lambda: _FakePubSub())

    real_sleep = asyncio.sleep
    with patch("nce.crypto.envelope.asyncio.sleep", AsyncMock()) as m_sleep:

        async def _fast_or_wait(delay: float) -> None:
            if delay >= 60:
                await asyncio.Future()
            await real_sleep(0)

        m_sleep.side_effect = _fast_or_wait
        await mgr.subscribe_key_invalidation(redis_cli)
        for _ in range(20):
            if handled:
                break
            await real_sleep(0.01)
        await mgr.stop_subscription()

    assert listen_calls >= 2
    assert len(handled) == 1


# ---------------------------------------------------------------------------
# 4. R2 Cron, GC, TaskRegistry.drain, NCEEngine.connect/disconnect
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cron_saga_recovery_marks_recovery_needed_and_sweeps_mongo() -> None:
    from nce import cron

    saga_missing = uuid4()
    saga_rec = uuid4()
    ns_id = uuid4()
    mem_id = uuid4()
    mongo_oid = ObjectId()

    executed: list[tuple[str, tuple[Any, ...]]] = []

    class _FakeConn:
        async def fetch(self, sql: str, *args: Any):
            if "WHERE state = 'pg_committed'" in sql:
                return [
                    {
                        "id": saga_missing,
                        "namespace_id": ns_id,
                        "agent_id": "a1",
                        "payload": json.dumps({"memory_id": str(mem_id)}),
                    }
                ]
            if "WHERE state = 'recovery_needed'" in sql:
                return [
                    {
                        "id": saga_rec,
                        "namespace_id": ns_id,
                        "agent_id": "a1",
                        "payload": json.dumps({"inserted_mongo_id": str(mongo_oid)}),
                    }
                ]
            return []

        async def fetchrow(self, sql: str, *args: Any):
            return None  # memory missing

        async def execute(self, sql: str, *args: Any):
            executed.append((sql, args))
            return "UPDATE 1"

    fake_conn = _FakeConn()

    @asynccontextmanager
    async def _unmanaged(_pool: Any, *, site: str = ""):
        yield fake_conn

    @asynccontextmanager
    async def _scoped(_pool: Any, _ns: Any):
        yield fake_conn

    mongo_cli = MagicMock()
    mongo_cli.memory_archive.episodes.delete_one = AsyncMock()

    with (
        patch("nce.cron.acquire_cron_lock", AsyncMock(return_value=MagicMock())),
        patch("nce.cron.release_cron_lock", AsyncMock()),
        patch("nce.cron.unmanaged_pg_connection", _unmanaged),
        patch("nce.cron.scoped_pg_session", _scoped),
        patch("nce.cron._dispatch_throttled_alert", AsyncMock()) as m_alert,
    ):
        await cron._saga_recovery_tick(MagicMock(), mongo_cli)

    # Never sets invalid 'failed' state; marks 'recovery_needed' and alerts once
    assert not any("'failed'" in sql for sql, _ in executed)
    assert any("SET state = 'recovery_needed'" in sql for sql, _ in executed)
    m_alert.assert_awaited()

    # Sweeps recovery_needed -> deletes Mongo doc and updates state = 'rolled_back'
    mongo_cli.memory_archive.episodes.delete_one.assert_awaited_once_with(
        {"_id": mongo_oid, "namespace_id": str(ns_id)}
    )
    assert any("SET state = 'rolled_back'" in sql for sql, _ in executed)


@pytest.mark.asyncio
async def test_cron_actor_trust_set_based_and_chain_verify_gin_query() -> None:
    from nce import cron

    ns_id = uuid4()
    seen_queries: list[str] = []

    class _FakeConn:
        async def fetch(self, sql: str, *args: Any):
            seen_queries.append(sql)
            if "SELECT id FROM namespaces" in sql:
                return [{"id": ns_id}]
            return [{"actor_id": "agent-1"}]

        async def fetchval(self, sql: str, *args: Any):
            seen_queries.append(sql)
            if "max(event_seq)" in sql:
                return 5
            return True

    fake_conn = _FakeConn()

    @asynccontextmanager
    async def _ctx(*args: Any, **kwargs: Any):
        yield fake_conn

    with (
        patch("nce.cron.acquire_cron_lock", AsyncMock(return_value=MagicMock())),
        patch("nce.cron.release_cron_lock", AsyncMock()),
        patch("nce.cron.unmanaged_pg_connection", _ctx),
        patch("nce.cron.scoped_pg_session", _ctx),
        patch("nce.cron._dispatch_throttled_alert", AsyncMock()),
        patch(
            "nce.event_log.verify_merkle_chain",
            AsyncMock(return_value={"valid": False, "first_break": 2, "reason": "bad"}),
        ),
    ):
        await cron._actor_trust_tick(MagicMock())
        await cron._chain_verification_tick(MagicMock())

    assert any("WITH event_agg AS" in q and "INSERT INTO actor_trust" in q for q in seen_queries)
    assert any("params @> jsonb_build_object('first_break', $2::text)" in q for q in seen_queries)


@pytest.mark.asyncio
async def test_gc_loop_retries_connect_with_capped_backoff() -> None:
    from nce import garbage_collector as gc

    attempts = 0

    async def _connect():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RuntimeError("transient outage")
        raise asyncio.CancelledError()

    sleeps: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    with (
        patch("nce.garbage_collector._connect_with_retry", side_effect=_connect),
        patch("nce.garbage_collector.asyncio.sleep", side_effect=_fake_sleep),
    ):
        with pytest.raises(asyncio.CancelledError):
            await gc.run_gc_loop()

    assert attempts == 3
    assert sleeps == [gc.CONNECT_BASE_DELAY, min(gc.CONNECT_BASE_DELAY * 2, 60)]


@pytest.mark.asyncio
async def test_task_registry_drain_and_engine_connect_disconnect() -> None:
    from nce.background_task_manager import TaskRegistry, TrackedTask
    from nce.orchestrator import NCEEngine

    reg = TaskRegistry()

    async def _never_ends() -> None:
        await asyncio.sleep(60)

    t = asyncio.create_task(_never_ends())
    reg.register(TrackedTask(name="test-bg", task=t))
    await reg.drain(timeout=1.0)
    assert t.cancelled()

    engine = NCEEngine()
    with (
        patch("nce.orchestrator.cfg.validate"),
        patch("nce.orchestrator.AsyncIOMotorClient", return_value=MagicMock()),
        patch("nce.orchestrator.Minio", return_value=MagicMock()),
        patch("nce.orchestrator.asyncpg.create_pool", AsyncMock(return_value=MagicMock())),
        patch("nce._client_factory.aioredis.from_url", return_value=MagicMock()),
        patch("nce._client_factory.redis.from_url", return_value=MagicMock()),
        patch.object(engine, "_init_pg_schema", AsyncMock()) as m_schema,
        patch.object(engine, "_apply_pg_migrations", AsyncMock()) as m_mig,
        patch.object(engine, "_verify_worm_enforcement", AsyncMock()) as m_worm,
        patch.object(engine, "_verify_rls_enforcement", AsyncMock()) as m_rls,
        patch.object(engine, "_seed_node_ownership_all", AsyncMock()) as m_seed,
        patch.object(engine, "_init_mongo_indexes", AsyncMock()) as m_idx,
        patch.object(engine, "_init_minio_buckets") as m_buckets,
    ):
        await engine.connect(run_startup_checks=False)

    m_schema.assert_not_awaited()
    m_mig.assert_not_awaited()
    m_worm.assert_not_awaited()
    m_rls.assert_not_awaited()
    m_seed.assert_not_awaited()
    m_idx.assert_not_awaited()
    m_buckets.assert_not_called()

    # disconnect() cancels memory._bg_cleanup_tasks and terminates pool on close timeout
    bg_task = asyncio.create_task(_never_ends())
    engine.memory._bg_cleanup_tasks.add(bg_task)

    slow_pool = MagicMock()

    async def _slow_close() -> None:
        await asyncio.sleep(60)

    slow_pool.close = _slow_close
    slow_pool.terminate = MagicMock()
    engine.pg_pool = slow_pool
    engine.pg_read_pool = None
    engine.redis_client = AsyncMock()
    engine.redis_sync_client = MagicMock()

    async def _fast_wait_for(coro: Any, timeout: float) -> Any:
        if hasattr(coro, "close"):
            coro.close()
        raise asyncio.TimeoutError()

    with patch("nce.orchestrator.asyncio.wait_for", side_effect=_fast_wait_for):
        await engine.disconnect()

    assert bg_task.cancelled()
    slow_pool.terminate.assert_called_once()
