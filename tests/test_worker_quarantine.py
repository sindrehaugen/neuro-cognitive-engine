"""Unit tests for v3.7.0 poison-pill & OOM (exit 137) RQ worker quarantine."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from rq import Queue
from start_worker import RecoveringWorker, requeue_abandoned_jobs

from nce.ops.quarantine import (
    FAILURES_KEY_PREFIX,
    FAILURES_TTL_SECONDS,
    INFLIGHT_KEY_PREFIX,
    INFLIGHT_TTL_SECONDS,
    compute_job_fingerprint,
    dismiss_quarantined_job,
    list_quarantined_jobs,
    quarantine_poison_job,
    requeue_quarantined_job,
    sanitize_traceback,
)


class _FakeSyncRedis:
    """Thread-safe minimal synchronous Redis test double for RQ worker tests."""

    def __init__(self) -> None:
        self.kv: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.zsets: dict[str, dict[str, float]] = {}

    def get(self, key: str) -> bytes | None:
        v = self.kv.get(key)
        return v.encode("utf-8") if v is not None else None

    def set(self, key: str, value: Any, ex: int | None = None) -> bool:
        self.kv[key] = str(value)
        if ex is not None:
            self.ttls[key] = int(ex)
        return True

    def delete(self, *keys: str) -> int:
        cnt = 0
        for k in keys:
            if k in self.kv:
                del self.kv[k]
                cnt += 1
        return cnt

    def incr(self, key: str) -> int:
        val = int(self.kv.get(key, "0")) + 1
        self.kv[key] = str(val)
        return val

    def expire(self, key: str, seconds: int) -> bool:
        self.ttls[key] = int(seconds)
        return True

    def zrem(self, key: str, *members: Any) -> int:
        z = self.zsets.get(key, {})
        cnt = 0
        for m in members:
            if str(m) in z:
                del z[str(m)]
                cnt += 1
        return cnt

    def rpush(self, key: str, *values: Any) -> int:
        return len(values)


class _InMemoryQuarantineConn:
    def __init__(self, rows: dict[str, dict[str, Any]]):
        self.rows = rows

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        s = " ".join(sql.split()).lower()
        if "insert into ops_quarantine" in s:
            fp = str(args[1])
            existing = self.rows.get(fp)
            now = datetime.now(timezone.utc)
            if existing:
                existing["queue_name"] = str(args[2])
                existing["func_name"] = str(args[3])
                if args[4] is not None:
                    existing["namespace_id"] = uuid.UUID(str(args[4]))
                existing["failure_count"] = max(existing["failure_count"], int(args[5]))
                existing["exit_reason"] = str(args[6])
                existing["sanitized_traceback"] = str(args[7])
                existing["job_args_preview"] = args[8]
                existing["status"] = "quarantined"
                existing["updated_at"] = now
                return existing
            row = {
                "quarantine_id": uuid.UUID(str(args[0])),
                "job_fingerprint": fp,
                "queue_name": str(args[2]),
                "func_name": str(args[3]),
                "namespace_id": uuid.UUID(str(args[4])) if args[4] else None,
                "failure_count": int(args[5]),
                "exit_reason": str(args[6]),
                "sanitized_traceback": str(args[7]),
                "job_args_preview": args[8],
                "status": "quarantined",
                "created_at": now,
                "updated_at": now,
            }
            self.rows[fp] = row
            return row
        if "select quarantine_id" in s and "from ops_quarantine" in s:
            target = str(args[0])
            for row in self.rows.values():
                if str(row["quarantine_id"]) == target or row["job_fingerprint"] == target:
                    return row
            return None
        if "update ops_quarantine" in s and "status = 'dismissed'" in s:
            target = str(args[0])
            for row in self.rows.values():
                if str(row["quarantine_id"]) == target or row["job_fingerprint"] == target:
                    row["status"] = "dismissed"
                    row["updated_at"] = datetime.now(timezone.utc)
                    return row
            return None
        return None

    async def execute(self, sql: str, *args: Any) -> str:
        s = " ".join(sql.split()).lower()
        if "update ops_quarantine" in s and "status = 'requeued'" in s:
            target = str(args[0])
            for row in self.rows.values():
                if str(row["quarantine_id"]) == target:
                    row["status"] = "requeued"
                    row["failure_count"] = 0
                    row["updated_at"] = datetime.now(timezone.utc)
                    return "UPDATE 1"
        return "OK"

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        s = " ".join(sql.split()).lower()
        if "from ops_quarantine" in s:
            rows = list(self.rows.values())
            if "where status = $1" in s:
                rows = [r for r in rows if r["status"] == str(args[0])]
            return rows
        return []


class _InMemoryQuarantinePool:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    @asynccontextmanager
    async def acquire(self, timeout: float | None = None):
        yield _InMemoryQuarantineConn(self.rows)


class _FakeJob:
    def __init__(
        self,
        job_id: str,
        origin: str,
        func_name: str,
        args: tuple[Any, ...] = (),
        kwargs: dict[str, Any] | None = None,
        fn: Any = None,
        retries_left: int | None = None,
    ) -> None:
        self.id = job_id
        self.origin = origin
        self.func_name = func_name
        self.args = args
        self.kwargs = kwargs or {}
        self.timeout = 30
        self.retries_left = retries_left
        self.started_at: datetime | None = None
        self.ended_at: datetime | None = None
        self._exc_info = ""
        self._fn = fn
        self._status = None

    def perform(self) -> Any:
        return self._fn(*self.args, **self.kwargs)

    def requeue(self) -> None:
        raise RuntimeError("not in failed registry")

    def send_webhooks(self, *_args: Any) -> None:
        return None


class _FakeQueue:
    DEFAULT_TIMEOUT = 180

    def __init__(self, name: str, connection: Any, enqueued: list[Any]) -> None:
        self.name = name
        self.connection = connection
        self._enqueued = enqueued
        self.started_job_registry = SimpleNamespace(key=f"rq:wip:{name}")

    def enqueue_job(self, job: Any) -> Any:
        self._enqueued.append((self.name, job))
        return job

    def enqueue(self, func_name: Any, *args: Any, **kwargs: Any) -> Any:
        j = _FakeJob(
            f"req-{len(self._enqueued) + 1}", self.name, str(func_name), args=args, kwargs=kwargs
        )
        self._enqueued.append((self.name, j))
        return j


class _TestableRecoveringWorker(RecoveringWorker):
    """Subclass stubbing only RQ registry book-keeping so perform_job runs without TCP Redis."""

    def __init__(
        self,
        queue: _FakeQueue,
        redis_conn: _FakeSyncRedis,
        pg_pool: _InMemoryQuarantinePool,
    ) -> None:
        self.queues = [queue]
        self.connection = redis_conn
        self._quarantine_pg_pool = pg_pool
        self.name = "test-worker"
        self.log = SimpleNamespace(
            debug=lambda *_a, **_kw: None,
            info=lambda *_a, **_kw: None,
            warning=lambda *_a, **_kw: None,
        )
        self.queue_class = _FakeQueue
        from rq.timeouts import TimerDeathPenalty

        self.death_penalty_class = TimerDeathPenalty
        self.prepared_executions = 0
        self.finalized_successes: list[Any] = []
        self.finalized_failures: list[Any] = []

    def prepare_execution(self, job: Any) -> SimpleNamespace:
        return SimpleNamespace(id=f"exec-{job.id}")

    def prepare_job_execution(self, job: Any, remove_from_intermediate_queue: bool = False) -> None:
        self.prepared_executions += 1

    def _finalize_success(self, job: Any, queue: Any, execution: Any, return_value: Any) -> None:
        self.finalized_successes.append((job.id, return_value))

    def _finalize_failure(self, job: Any, queue: Any, execution: Any, exc_info: Any) -> None:
        self.finalized_failures.append((job.id, str(exc_info[1])))


def test_fingerprint_determinism_and_traceback_sanitization() -> None:
    ns_id = uuid.uuid4()
    fp1 = compute_job_fingerprint(
        "nce-default",
        "nce.tasks.index_doc",
        args=(ns_id, [1, 2]),
        kwargs={"b": 2, "a": "x"},
    )
    fp2 = compute_job_fingerprint(
        "nce-default",
        "nce.tasks.index_doc",
        args=(str(ns_id), [1, 2]),
        kwargs={"a": "x", "b": 2},
    )
    fp3 = compute_job_fingerprint(
        "nce-bulk",
        "nce.tasks.index_doc",
        args=(str(ns_id), [1, 2]),
        kwargs={"a": "x", "b": 2},
    )
    assert fp1 == fp2
    assert fp1 != fp3

    raw_tb = (
        "Traceback (most recent call last):\n"
        "  connect('postgresql://admin:SuperSecretPass@pg:5432/nce')\n"
        "  headers={'Authorization': 'Bearer tok_abc123SECRET'}\n"
        "  NCE_MASTER_KEY=my_raw_master_key_val\n"
    )
    cleaned = sanitize_traceback(raw_tb)
    assert "SuperSecretPass" not in cleaned
    assert "tok_abc123SECRET" not in cleaned
    assert "my_raw_master_key_val" not in cleaned
    assert "[REDACTED]" in cleaned


def test_poison_pill_exception_quarantines_on_second_failure() -> None:
    redis = _FakeSyncRedis()
    pg_pool = _InMemoryQuarantinePool()
    enqueued: list[Any] = []
    queue = _FakeQueue("nce-critical", redis, enqueued)
    worker = _TestableRecoveringWorker(queue, redis, pg_pool)
    ns_id = str(uuid.uuid4())

    calls = {"count": 0}

    def _poison_fn(*_a: Any, **_kw: Any) -> None:
        calls["count"] += 1
        raise ValueError("boom postgresql://user:secretpw@pg/db")

    job = _FakeJob(
        "job-poison-1",
        "nce-critical",
        "nce.tasks.poison",
        kwargs={"namespace_id": ns_id, "api_key": "secret-val"},
        fn=_poison_fn,
        retries_left=None,
    )
    fp = compute_job_fingerprint("nce-critical", "nce.tasks.poison", job.args, job.kwargs)

    with patch(
        "start_worker.Queue",
        side_effect=lambda name, connection: _FakeQueue(name, connection, enqueued),
    ):
        # Attempt 1 fails -> inflight cleared, failures=1 (TTL 86400s), re-enqueued once!
        ok1 = worker.perform_job(job, queue)
        assert ok1 is False
        assert calls["count"] == 1
        assert redis.get(f"{INFLIGHT_KEY_PREFIX}{fp}") is None
        assert redis.get(f"{FAILURES_KEY_PREFIX}{fp}") == b"1"
        assert redis.ttls[f"{FAILURES_KEY_PREFIX}{fp}"] == FAILURES_TTL_SECONDS
        assert len(enqueued) == 1
        assert fp not in pg_pool.rows

        # Attempt 2 fails -> failures=2 -> quarantined immediately into ops_quarantine!
        ok2 = worker.perform_job(job, queue)
        assert ok2 is False
        assert calls["count"] == 2
        assert redis.get(f"{INFLIGHT_KEY_PREFIX}{fp}") is None
        assert redis.get(f"{FAILURES_KEY_PREFIX}{fp}") == b"2"
        assert len(enqueued) == 1  # NOT re-enqueued again!
        assert fp in pg_pool.rows
        row = pg_pool.rows[fp]
        assert row["status"] == "quarantined"
        assert row["failure_count"] == 2
        assert row["exit_reason"] == "repeated_exception"
        assert "secretpw" not in row["sanitized_traceback"]

        # Attempt 3 must intercept BEFORE invoking callable!
        ok3 = worker.perform_job(job, queue)
        assert ok3 is False
        assert calls["count"] == 2  # callable never ran!


def test_simulated_oom_exit_137_preserves_inflight_and_quarantines_pre_exec() -> None:
    redis = _FakeSyncRedis()
    pg_pool = _InMemoryQuarantinePool()
    enqueued: list[Any] = []
    queue = _FakeQueue("nce-default", redis, enqueued)
    worker = _TestableRecoveringWorker(queue, redis, pg_pool)

    calls = {"count": 0}

    def _oom_fn(*_a: Any, **_kw: Any) -> None:
        calls["count"] += 1
        raise SystemExit(137)

    job = _FakeJob("job-oom-1", "nce-default", "nce.tasks.oom", args=("doc-1",), fn=_oom_fn)
    fp = compute_job_fingerprint("nce-default", "nce.tasks.oom", job.args, job.kwargs)
    inflight_key = f"{INFLIGHT_KEY_PREFIX}{fp}"

    # Simulated abrupt worker death (exit 137) leaves inflight marker in Redis.
    with pytest.raises(SystemExit):
        worker.perform_job(job, queue)
    assert calls["count"] == 1
    assert redis.get(inflight_key) == b"job-oom-1"
    assert redis.ttls[inflight_key] == INFLIGHT_TTL_SECONDS

    # requeue_abandoned_jobs recovers from StartedJobRegistry WITHOUT clearing inflight_key!
    fake_registry = SimpleNamespace(
        key="rq:wip:nce-default",
        get_expired_job_ids=lambda: ["job-oom-1"],
        job_class=SimpleNamespace(fetch=lambda jid, connection: job),
    )
    with (
        patch("start_worker.StartedJobRegistry", return_value=fake_registry),
        patch(
            "start_worker.Queue",
            side_effect=lambda name, connection: _FakeQueue(name, connection, enqueued),
        ),
    ):
        requeued = requeue_abandoned_jobs(Queue("nce-default", connection=redis), redis)
    assert requeued == ["job-oom-1"]
    assert redis.get(inflight_key) == b"job-oom-1"

    # Next pickup detects lingering inflight marker and quarantines BEFORE executing callable!
    ok_retry = worker.perform_job(job, queue)
    assert ok_retry is False
    assert calls["count"] == 1  # Callable was NOT invoked again!
    assert redis.get(inflight_key) is None
    assert fp in pg_pool.rows
    assert pg_pool.rows[fp]["exit_reason"] == "oom_or_abrupt_exit"
    assert pg_pool.rows[fp]["status"] == "quarantined"


def test_success_clears_inflight_and_failures() -> None:
    redis = _FakeSyncRedis()
    pg_pool = _InMemoryQuarantinePool()
    enqueued: list[Any] = []
    queue = _FakeQueue("nce-default", redis, enqueued)
    worker = _TestableRecoveringWorker(queue, redis, pg_pool)

    job = _FakeJob(
        "job-ok-1", "nce-default", "nce.tasks.ok", args=("hello",), fn=lambda x: f"done:{x}"
    )
    fp = compute_job_fingerprint("nce-default", "nce.tasks.ok", job.args, job.kwargs)
    redis.set(f"{FAILURES_KEY_PREFIX}{fp}", "1", ex=FAILURES_TTL_SECONDS)

    ok = worker.perform_job(job, queue)
    assert ok is True
    assert redis.get(f"{INFLIGHT_KEY_PREFIX}{fp}") is None
    assert redis.get(f"{FAILURES_KEY_PREFIX}{fp}") is None


@pytest.mark.asyncio
async def test_quarantine_requeue_list_and_dismiss_actions() -> None:
    pg_pool = _InMemoryQuarantinePool()
    redis = _FakeSyncRedis()
    rec = await quarantine_poison_job(
        pg_pool=pg_pool,
        queue_name="nce-bulk",
        func_name="nce.tasks.bad",
        args=("a1",),
        kwargs={"password": "secret"},
        failure_count=2,
        exit_reason="repeated_exception",
        traceback_text="Bearer tok_hidden_abc\nValueError: oops",
        job_id="job-bad-1",
    )
    assert rec.job_args_preview["kwargs"]["password"] == "[REDACTED]"
    assert "tok_hidden_abc" not in rec.sanitized_traceback

    listed = await list_quarantined_jobs(pg_pool, status="quarantined")
    assert len(listed) == 1
    assert listed[0].quarantine_id == rec.quarantine_id

    redis.set(f"{FAILURES_KEY_PREFIX}{rec.job_fingerprint}", "2")
    redis.set(f"{INFLIGHT_KEY_PREFIX}{rec.job_fingerprint}", "job-bad-1")
    enqueued: list[Any] = []
    with patch(
        "nce.ops.quarantine.Queue",
        side_effect=lambda name, connection: _FakeQueue(name, connection, enqueued),
    ):
        req_res = await requeue_quarantined_job(pg_pool, redis, rec.quarantine_id)
    assert req_res["status"] == "requeued"
    assert redis.get(f"{FAILURES_KEY_PREFIX}{rec.job_fingerprint}") is None
    assert redis.get(f"{INFLIGHT_KEY_PREFIX}{rec.job_fingerprint}") is None
    assert pg_pool.rows[rec.job_fingerprint]["failure_count"] == 0

    dis_res = await dismiss_quarantined_job(pg_pool, rec.quarantine_id, redis_client=redis)
    assert dis_res["status"] == "dismissed"
    assert await list_quarantined_jobs(pg_pool, status="quarantined") == []
