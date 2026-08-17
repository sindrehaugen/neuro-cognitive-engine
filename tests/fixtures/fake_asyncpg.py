"""
Fake asyncpg connection + pool mocks for Saga / event_log unit tests.

Implements only the SQL shapes exercised by nce.event_log.append_event —
no TCP, no Postgres.  Use RecordingFakePool when code expects ``pool.acquire``.

This is deterministic test scaffolding (replacement for unavailable local
PostgreSQL during CI smoke runs).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import asyncpg


class _FakeTransaction:
    def __init__(self, conn: RecordingFakeConnection) -> None:
        self._conn = conn

    async def __aenter__(self) -> None:
        self._conn._tx_depth += 1

    async def __aexit__(self, *_exc: object) -> None:
        self._conn._tx_depth -= 1


class _FakeAcquireContext:
    """
    Mirrors asyncpg ``pool.acquire()`` return value: awaitable and async context manager.

    Always pair ``await pool.acquire()`` with ``await pool.release(conn)``, or use
    ``async with pool.acquire() as conn`` so checkouts are balanced.
    """

    __slots__ = ("_pool",)

    def __init__(self, pool: RecordingFakePool) -> None:
        self._pool = pool

    def __await__(self):
        return self._pool._acquire_conn().__await__()

    async def __aenter__(self) -> RecordingFakeConnection:
        return await self._pool._acquire_conn()

    async def __aexit__(self, *_exc: object) -> None:
        await self._pool._release_conn()


class RecordingFakeConnection:
    """
    Records INSERT payloads and honours per-namespace event_sequences-style sequencing.

    asyncpg Compatibility
    ---------------------
    * ``transaction()`` async context matches Saga coordinator wrapping.
    * ``fetchval`` / ``execute`` / ``fetchrow`` route on SQL substring heuristics.
    """

    def __init__(
        self,
        *,
        db_clock: datetime | None = None,
        simulate_unique_violation_on_insert: bool = False,
    ) -> None:
        utc = timezone.utc
        self._db_clock = (
            db_clock or datetime(2026, 5, 5, 10, 0, 0, 123456, tzinfo=utc)
        ).astimezone(utc)
        self._seq_max: defaultdict[UUID, int] = defaultdict(int)
        self._seq_counters: dict[UUID, int] = {}
        self._tx_depth = 0
        self.event_inserts: list[dict[str, Any]] = []
        self.unique_violation = simulate_unique_violation_on_insert

    def is_in_transaction(self) -> bool:
        return self._tx_depth > 0

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction(self)

    async def fetchval(self, query: str, *args: Any) -> Any:
        q_compact = "".join(query.split()).lower()
        if "clock_timestamp()" in query.lower():
            return self._db_clock
        if "fromevent_sequences" in q_compact:
            namespace_id = args[0]
            ns_key = namespace_id if isinstance(namespace_id, UUID) else UUID(str(namespace_id))
            return self._seq_counters.get(ns_key)
        if "max(event_seq)" in q_compact and "fromevent_log" in q_compact:
            namespace_id = args[0]
            ns_key = namespace_id if isinstance(namespace_id, UUID) else UUID(str(namespace_id))
            rows = [r for r in self.event_inserts if r["namespace_id"] == ns_key]
            if not rows:
                return None
            return max(int(r["event_seq"]) for r in rows)
        raise AssertionError(f"Unexpected fetchval query: {query!r}")

    async def execute(self, query: str, *args: Any) -> str:
        raise AssertionError(f"Unexpected execute query: {query!r}")

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        """
        Minimal fetch() for Merkle chain verification queries.

        Handles ``SELECT ... FROM event_log WHERE namespace_id = $1
        ORDER BY event_seq ASC``.
        """
        q = "".join(query.split()).lower()
        if "fromevent_log" in q.replace(" ", "") and "chain_hash" in q.lower():
            namespace_id = args[0]
            start_seq = args[1] if len(args) > 1 else None
            rows = [r for r in self.event_inserts if r["namespace_id"] == namespace_id]
            rows.sort(key=lambda r: r.get("event_seq", 0))
            if start_seq is not None:
                rows = [r for r in rows if r["event_seq"] >= start_seq]
            return rows
        raise AssertionError(f"Unexpected fetch query: {query!r}")

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        q = "".join(query.split()).lower()

        if "intoevent_sequences" in q.replace(" ", "") and "onconflict" in q.replace(" ", ""):
            namespace_id = args[0]
            ns_key = namespace_id if isinstance(namespace_id, UUID) else UUID(str(namespace_id))
            nxt = self._seq_max[ns_key] + 1
            self._seq_counters[ns_key] = nxt
            return {"seq": nxt}

        # _fetch_previous_chain_hash: SELECT chain_hash FROM event_log
        # WHERE namespace_id = $1 ORDER BY event_seq DESC LIMIT 1
        if (
            "chain_hash" in q.lower()
            and "fromevent_log" in q.replace(" ", "")
            and "orderbyevent_seqdesc" in q.replace(" ", "").lower()
        ):
            namespace_id = args[0]
            rows = [r for r in self.event_inserts if r["namespace_id"] == namespace_id]
            if not rows:
                return None
            rows.sort(key=lambda r: r.get("event_seq", 0), reverse=True)
            latest = rows[0]
            return {"chain_hash": latest.get("chain_hash")}

        # chain hash anchor query (seq = N): SELECT chain_hash FROM event_log
        # WHERE namespace_id = $1 AND event_seq = $2
        if (
            "chain_hash" in q.lower()
            and "fromevent_log" in q.replace(" ", "")
            and "event_seq" in q.lower()
            and "orderbyevent_seqdesc" not in q.replace(" ", "").lower()
        ):
            namespace_id = args[0]
            target_seq = args[1]
            for r in self.event_inserts:
                if r["namespace_id"] == namespace_id and r.get("event_seq") == target_seq:
                    return {"chain_hash": r.get("chain_hash")}
            return None

        if "insertintoevent_log" in q.replace(" ", ""):
            (
                event_id,
                namespace_id,
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
                signature_key_id,
                chain_hash,
                correlation_id,
            ) = args

            record = {
                "id": event_id,
                "namespace_id": namespace_id,
                "agent_id": agent_id,
                "event_type": event_type,
                "event_seq": event_seq,
                "occurred_at": occurred_at,
                "params": params_json,
                "signature": signature,
                "signature_key_id": signature_key_id,
                "chain_hash": chain_hash,
                "correlation_id": correlation_id,
            }
            if self.unique_violation:
                raise asyncpg.UniqueViolationError("simulated collision")

            ns = namespace_id if isinstance(namespace_id, UUID) else UUID(str(namespace_id))
            self._seq_max[ns] = max(self._seq_max[ns], int(event_seq))
            self.event_inserts.append(record)

            es = event_seq if isinstance(event_seq, int) else int(event_seq)
            return {"id": event_id, "event_seq": es, "occurred_at": occurred_at}

        raise AssertionError(f"Unexpected fetchrow query: {query!r}")


class RecordingFakePool:
    """
    Minimal pool stand-in: ``await pool.acquire()`` returns a fixed connection.
    """

    __slots__ = ("_conn", "_closed", "_outstanding")

    def __init__(self, conn: RecordingFakeConnection) -> None:
        self._conn = conn
        self._closed = False
        self._outstanding = 0

    def acquire(self, *, timeout: float | None = None) -> _FakeAcquireContext:
        _ = timeout  # asyncpg API parity — fake checkout is immediate.
        if self._closed:
            raise RuntimeError("RecordingFakePool: pool is closed")
        return _FakeAcquireContext(self)

    async def _acquire_conn(self) -> RecordingFakeConnection:
        if self._closed:
            raise RuntimeError("RecordingFakePool: pool is closed")
        self._outstanding += 1
        return self._conn

    async def _release_conn(self) -> None:
        if self._outstanding > 0:
            self._outstanding -= 1

    async def release(self, conn: RecordingFakeConnection, *args: Any, **kwargs: Any) -> None:
        """asyncpg-compatible explicit release after ``await pool.acquire()``."""
        if conn is not self._conn:
            raise ValueError("RecordingFakePool.release: connection does not belong to this pool")
        await self._release_conn()

    async def close(self) -> None:
        if self._closed:
            return
        # Drop stray checkouts so teardown never carries state into the next test.
        self._outstanding = 0
        self._closed = True


def make_fake_pool(**kwargs: Any) -> tuple[RecordingFakePool, RecordingFakeConnection]:
    """Factory: ``pool, conn = make_fake_pool()``."""
    conn = RecordingFakeConnection(**kwargs)
    return RecordingFakePool(conn), conn
