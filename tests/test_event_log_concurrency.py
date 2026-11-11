"""TASK-12: Event log concurrency and Merkle chain integrity tests."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import asyncpg
import pytest

from nce.event_log import append_event, verify_merkle_chain
from tests.fixtures.event_log_params import minimal_store_memory_params


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_append_event_no_gaps(pg_pool, namespace_id):
    async def write_one(i: int):
        async with pg_pool.acquire(timeout=10.0) as conn:
            async with conn.transaction():
                return await append_event(
                    conn=conn,
                    namespace_id=namespace_id,
                    agent_id="test-agent",
                    event_type="store_memory",
                    params=minimal_store_memory_params(i=i),
                )

    results = await asyncio.gather(*(write_one(i) for i in range(50)))
    seqs = sorted(r.event_seq for r in results)
    assert seqs == list(range(1, 51)), f"Gaps or duplicates in sequence: {seqs}"

    async with pg_pool.acquire(timeout=10.0) as conn:
        chain = await verify_merkle_chain(conn, namespace_id=namespace_id)

    assert chain["valid"] is True
    assert chain["checked"] == 50


@pytest.mark.integration
@pytest.mark.asyncio
async def test_event_sequences_are_independent_per_namespace(pg_pool, make_namespace):
    ns_a = await make_namespace()
    ns_b = await make_namespace()

    async def write(ns):
        async with pg_pool.acquire(timeout=10.0) as conn:
            async with conn.transaction():
                return await append_event(
                    conn=conn,
                    namespace_id=ns,
                    agent_id="test-agent",
                    event_type="store_memory",
                    params=minimal_store_memory_params(),
                )

    results = await asyncio.gather(
        *[write(ns_a) for _ in range(20)],
        *[write(ns_b) for _ in range(20)],
    )

    a_seqs = sorted(r.event_seq for r in results[:20])
    b_seqs = sorted(r.event_seq for r in results[20:])
    assert a_seqs == list(range(1, 21))
    assert b_seqs == list(range(1, 21))


class _SimulatedSharedDB:
    """In-memory PostgreSQL state simulator with genuine asyncio per-key advisory locks."""

    def __init__(self) -> None:
        self.sequences: dict[uuid.UUID, int] = {}
        self.events: dict[uuid.UUID, list[dict[str, Any]]] = {}
        self.advisory_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self.lock_acquisitions: list[tuple[str, str]] = []

    def get_lock(self, key1: str, key2: str) -> asyncio.Lock:
        pair = (key1, key2)
        if pair not in self.advisory_locks:
            self.advisory_locks[pair] = asyncio.Lock()
        return self.advisory_locks[pair]


class _SimulatedAsyncpgConn:
    """Simulated asyncpg connection that yields control between sequence allocation and insert."""

    def __init__(self, db: _SimulatedSharedDB, *, enforce_advisory_lock: bool = True) -> None:
        self._db = db
        self._enforce_advisory_lock = enforce_advisory_lock
        self._in_tx = False
        self._held_locks: list[asyncio.Lock] = []

    def is_in_transaction(self) -> bool:
        return self._in_tx

    @asynccontextmanager
    async def transaction(self):
        self._in_tx = True
        try:
            yield
        finally:
            self._in_tx = False
            for lk in reversed(self._held_locks):
                if lk.locked():
                    lk.release()
            self._held_locks.clear()

    async def execute(self, sql: str, *args: Any) -> str:
        if "pg_advisory_xact_lock" in sql and "nce_worm_event_log" in sql:
            ns_text = str(args[0])
            self._db.lock_acquisitions.append(("nce_worm_event_log", ns_text))
            if self._enforce_advisory_lock:
                lk = self._db.get_lock("nce_worm_event_log", ns_text)
                await lk.acquire()
                self._held_locks.append(lk)
        await asyncio.sleep(0)
        return "SELECT 1"

    async def fetchval(self, sql: str, *args: Any) -> Any:
        await asyncio.sleep(0)
        if "clock_timestamp()" in sql:
            return datetime.now(UTC)
        if "SELECT seq FROM event_sequences" in sql:
            ns_id = args[0]
            return self._db.sequences.get(ns_id)
        if "SELECT max(event_seq) FROM event_log" in sql:
            ns_id = args[0]
            rows = self._db.events.get(ns_id, [])
            return max((r["event_seq"] for r in rows), default=None)
        return None

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        await asyncio.sleep(0)
        if "INSERT INTO event_sequences" in sql:
            ns_id: uuid.UUID = args[0]
            nxt = self._db.sequences.get(ns_id, 0) + 1
            await asyncio.sleep(0)
            self._db.sequences[ns_id] = nxt
            return {"seq": nxt}

        if "SELECT chain_hash" in sql and "FROM   event_log" in sql:
            ns_id = args[0]
            rows = self._db.events.get(ns_id, [])
            await asyncio.sleep(0)
            if "event_seq = $2" in sql:
                target_seq = int(args[1])
                for r in rows:
                    if r["event_seq"] == target_seq:
                        return {"chain_hash": r["chain_hash"]}
                return None
            if not rows:
                return None
            latest = max(rows, key=lambda r: r["event_seq"])
            return {"chain_hash": latest["chain_hash"]}

        if "INSERT INTO event_log" in sql:
            (
                event_id,
                ns_id,
                agent_id,
                event_type,
                event_seq,
                occurred_at,
                params_json,
                result_summary_json,
                parent_event_id,
                llm_payload_uri,
                llm_payload_hash,
                signature,
                key_id,
                chain_hash,
                correlation_id,
            ) = args
            await asyncio.sleep(0)
            bucket = self._db.events.setdefault(ns_id, [])
            for existing in bucket:
                if existing["event_seq"] == event_seq:
                    raise asyncpg.UniqueViolationError("duplicate (namespace_id, event_seq)")
            bucket.append(
                {
                    "id": event_id,
                    "namespace_id": ns_id,
                    "agent_id": agent_id,
                    "event_type": event_type,
                    "event_seq": event_seq,
                    "occurred_at": occurred_at,
                    "params": json.loads(params_json)
                    if isinstance(params_json, str)
                    else params_json,
                    "result_summary": result_summary_json,
                    "parent_event_id": parent_event_id,
                    "llm_payload_uri": llm_payload_uri,
                    "llm_payload_hash": llm_payload_hash,
                    "signature": signature,
                    "signature_key_id": key_id,
                    "signature_version": 2,
                    "chain_hash": chain_hash,
                    "correlation_id": correlation_id,
                }
            )
            return {"id": event_id, "event_seq": event_seq, "occurred_at": occurred_at}

        return None

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        await asyncio.sleep(0)
        if "FROM   event_log" in sql:
            ns_id: uuid.UUID = args[0]
            start_seq = int(args[1])
            rows = [r for r in self._db.events.get(ns_id, []) if r["event_seq"] >= start_seq]
            rows.sort(key=lambda r: r["event_seq"])
            return rows
        return []


@pytest.mark.asyncio
async def test_25_concurrent_append_events_serialized_by_worm_advisory_lock() -> None:
    """25 concurrent writers on one namespace must serialize via pg_advisory_xact_lock with zero Merkle forks."""
    db = _SimulatedSharedDB()
    ns_primary = uuid.uuid4()
    ns_other = uuid.uuid4()
    raw_signing_key = b"k" * 32

    async def _fake_active_key(_conn: Any) -> tuple[str, bytes]:
        await asyncio.sleep(0)
        return ("test-key-v34", raw_signing_key)

    async def _writer(ns: uuid.UUID, idx: int):
        conn = _SimulatedAsyncpgConn(db, enforce_advisory_lock=True)
        async with conn.transaction():
            return await append_event(
                conn=conn,  # type: ignore[arg-type]
                namespace_id=ns,
                agent_id=f"worker-{idx}",
                event_type="config_reload",
                params={
                    "actor": f"worker-{idx}",
                    "domain": "worm-concurrency",
                    "message": f"concurrent-write-{idx}",
                },
            )

    with patch("nce.event_log.get_active_key", side_effect=_fake_active_key):
        results_primary, results_other = await asyncio.gather(
            asyncio.gather(*(_writer(ns_primary, i) for i in range(25))),
            asyncio.gather(*(_writer(ns_other, i) for i in range(5))),
        )

    seqs = sorted(r.event_seq for r in results_primary)
    assert seqs == list(range(1, 26))
    assert sorted(r.event_seq for r in results_other) == [1, 2, 3, 4, 5]

    primary_locks = [
        ns
        for key, ns in db.lock_acquisitions
        if key == "nce_worm_event_log" and ns == str(ns_primary)
    ]
    assert len(primary_locks) == 25

    verifier_conn = _SimulatedAsyncpgConn(db)
    report_primary = await verify_merkle_chain(verifier_conn, namespace_id=ns_primary)  # type: ignore[arg-type]
    assert report_primary["valid"] is True
    assert report_primary["checked"] == 25
    assert report_primary["first_break"] is None
    assert report_primary["last_verified_seq"] == 25

    report_other = await verify_merkle_chain(verifier_conn, namespace_id=ns_other)  # type: ignore[arg-type]
    assert report_other["valid"] is True
    assert report_other["checked"] == 5


@pytest.mark.integration
@pytest.mark.asyncio
async def test_25_concurrent_append_events_live_postgres_no_merkle_forks(
    pg_pool: asyncpg.Pool,
    make_namespace: Any,
) -> None:
    """Live PostgreSQL 16 integration test: 25 concurrent wide-pool transactions on one namespace."""
    dsn = (
        os.getenv("NCE_INTEGRATION_PG_DSN")
        or os.getenv("PG_DSN")
        or os.getenv("DATABASE_URL")
        or ""
    ).strip()
    assert dsn, "Expected live DSN when pg_pool fixture is active"

    ns_id = await make_namespace()

    wide_pool = await asyncpg.create_pool(dsn, min_size=5, max_size=28, command_timeout=60)
    try:
        barrier = asyncio.Event()

        async def _tx_append(idx: int):
            async with wide_pool.acquire() as conn:
                await barrier.wait()
                async with conn.transaction():
                    return await append_event(
                        conn=conn,
                        namespace_id=ns_id,
                        agent_id=f"live-worker-{idx}",
                        event_type="store_memory",
                        params=minimal_store_memory_params(i=idx),
                    )

        tasks = [asyncio.create_task(_tx_append(i)) for i in range(25)]
        await asyncio.sleep(0.02)
        barrier.set()
        results = await asyncio.gather(*tasks)

        assert sorted(r.event_seq for r in results) == list(range(1, 26))

        async with wide_pool.acquire() as verify_conn:
            merkle_report = await verify_merkle_chain(verify_conn, namespace_id=ns_id)
            assert merkle_report["valid"] is True, f"Merkle chain broken: {merkle_report}"
            assert merkle_report["checked"] == 25
            assert merkle_report["first_break"] is None
            assert merkle_report["last_verified_seq"] == 25
    finally:
        await wide_pool.close()
