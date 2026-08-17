"""R-C (NCE_MASTER_PLAN §VI.6a): in-flight RQ jobs must survive worker death.

These tests prove that a job which was *running* when its worker died (i.e. an
abandoned entry in the lane's ``StartedJobRegistry``) is requeued onto its
origin lane by ``start_worker.requeue_abandoned_jobs`` — not silently lost and
not merely moved to the failed registry.

They run against the live Redis from the local Docker stack and are marked
``integration`` so unit-only runs skip them. The assertions check the *real*
RQ requeue contract (the job is back, queued, on its origin lane), not that a
mock was called.
"""

from __future__ import annotations

import uuid

import pytest
import start_worker
from redis import from_url
from rq import Queue
from rq.job import Job, JobStatus
from rq.registry import StartedJobRegistry

REDIS_URL = "redis://localhost:6379/0"


def _noop_job() -> str:
    """A re-runnable, idempotent job body (stands in for code-indexing)."""
    return "ok"


@pytest.fixture
def redis_conn():
    conn = from_url(REDIS_URL)
    try:
        conn.ping()
    except Exception:  # pragma: no cover - skip when stack is down
        pytest.skip("local Redis not reachable")
    yield conn


@pytest.fixture
def lane(redis_conn):
    """A throwaway queue lane, cleaned up after the test."""
    name = f"test_recovery_{uuid.uuid4().hex}"
    queue = Queue(name, connection=redis_conn)
    yield queue
    # Tear down: drop the queue and its started registry.
    redis_conn.delete(queue.key)
    redis_conn.delete(StartedJobRegistry(name=name, connection=redis_conn).key)


def _make_abandoned_started_job(queue: Queue, redis_conn) -> Job:
    """Create a job, mark it STARTED, and place it in the started registry with
    an already-expired score — exactly the state left behind by a worker that
    was killed mid-execution."""
    job = queue.enqueue(_noop_job)
    # It is enqueued; simulate the worker having dequeued and started it, then
    # dying without finishing: remove from the ready queue, mark STARTED, and
    # register it as in-flight with an expiry in the past.
    redis_conn.lrem(queue.key, 0, job.id)
    job.set_status(JobStatus.STARTED)
    registry = StartedJobRegistry(name=queue.name, connection=redis_conn)
    # score = 1 → unix epoch 1970, i.e. expired relative to now ⇒ "abandoned".
    redis_conn.zadd(registry.key, {job.id: 1})
    return job


@pytest.mark.integration
def test_abandoned_inflight_job_is_requeued_onto_origin_lane(lane, redis_conn):
    queue = lane
    job = _make_abandoned_started_job(queue, redis_conn)
    registry = StartedJobRegistry(name=queue.name, connection=redis_conn)

    # Precondition: job is abandoned (in started registry, expired) and is NOT
    # on the ready queue.
    assert job.id in registry.get_expired_job_ids()
    assert job.id not in queue.get_job_ids()

    requeued = start_worker.requeue_abandoned_jobs(queue, redis_conn)

    # Real requeue contract, not a mock assertion:
    # 1. our job id is reported as requeued
    assert requeued == [job.id]
    # 2. it is back on its origin lane's ready queue
    assert job.id in queue.get_job_ids()
    # 3. it is no longer parked in the started registry
    assert job.id not in StartedJobRegistry(name=queue.name, connection=redis_conn).get_job_ids()
    # 4. its status is QUEUED again (re-runnable), proving it was not just
    #    dropped into the failed registry
    refreshed = Job.fetch(job.id, connection=redis_conn)
    assert refreshed.get_status() == JobStatus.QUEUED
    # 5. it kept its origin lane (did not migrate to "default")
    assert refreshed.origin == queue.name


@pytest.mark.integration
def test_sweep_across_lanes_requeues_only_abandoned(lane, redis_conn):
    """A live (non-expired) started job is left alone; only abandoned ones move."""
    queue = lane
    abandoned = _make_abandoned_started_job(queue, redis_conn)

    # A second job that is "started" but still being actively renewed by a live
    # worker: score far in the future ⇒ NOT abandoned.
    live = queue.enqueue(_noop_job)
    redis_conn.lrem(queue.key, 0, live.id)
    live.set_status(JobStatus.STARTED)
    registry = StartedJobRegistry(name=queue.name, connection=redis_conn)
    redis_conn.zadd(registry.key, {live.id: 9_999_999_999})  # year 2286

    requeued = start_worker.maintain_started_registries([queue], redis_conn)

    assert requeued == [abandoned.id]
    # the live job stays in-flight, untouched
    assert live.id in StartedJobRegistry(name=queue.name, connection=redis_conn).get_job_ids()
    assert live.id not in queue.get_job_ids()
