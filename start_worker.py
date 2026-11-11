"""
NCE RQ Worker Launcher
Starts an RQ worker to handle background indexing tasks.

Priority lanes (§5.4): the worker dequeues ``high_priority`` before
``batch_processing`` so that user-facing API extractions never wait behind
large batch uploads.  The ``default`` queue is retained for backward
compatibility with any older enqueue sites that haven't been migrated.

In-flight job recovery (R-C, NCE_MASTER_PLAN §VI.6a)
----------------------------------------------------
A bare ``Worker(...).work()`` silently loses any job that was *running* when
the worker died (power loss, OOM, ``SIGKILL``): the job sits forever in the
queue's ``StartedJobRegistry`` and is never re-executed, because the app-level
dead-letter queue only catches Python *exceptions*, not process death.

This launcher closes that gap two ways:

* ``with_scheduler=True`` — the worker runs RQ's scheduler in-process so
  scheduled / retried jobs fire even with a single worker.
* a periodic ``StartedJobRegistry`` sweep (``maintain_started_registries``)
  runs on every worker maintenance tick.  Abandoned started jobs (whose
  monitoring TTL has expired — i.e. no live worker is renewing them) are
  **requeued onto their origin lane** rather than being dropped or only
  moved to the failed registry.

Safe-to-lose vs must-requeue
----------------------------
All NCE job classes are idempotent / re-derivable, so requeue (rather than
fail) is the correct default:

* **must-requeue (re-runnable, this sweep requeues them):**
  ``process_code_indexing`` (re-triggerable from source), bridge cursor /
  d365 sync polls (re-pollable from the stored cursor), webhook re-indexes.
  Re-running them at-most reproduces the same content — WORM/idempotency
  upstream dedupes, so a duplicate run is safe.
* **safe-to-lose (NOT requeued by this sweep):** none today.  If a future
  job class is *not* idempotent it must be enqueued with an explicit retry
  policy and excluded here; document it before adding.

``RESULT_TTL`` / ``FAILURE_TTL`` bound how long finished/failed job metadata
lingers in Redis so the registries don't grow unbounded between sweeps.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from redis import Redis, from_url
from rq import Queue, Worker
from rq.registry import StartedJobRegistry

from nce.config import cfg

if TYPE_CHECKING:
    from rq.job import Job

logging.basicConfig(level=logging.INFO, format="%(asctime)s [Worker] %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# Canonical priority queues in strict dequeue order:
# Dequeued left-to-right so high-priority / critical work is serviced first.
PRIORITY_QUEUES: list[str] = [
    "nce-critical",
    "nce-default",
    "nce-bulk",
    "high_priority",
    "batch_processing",
    "diag_ingest",
    "default",
]

# Lane ordering is load-bearing (§5.4): high-priority first, legacy last.
# Used as the default queue order when RQ_QUEUES env var is not explicitly overridden.
QUEUE_NAMES: tuple[str, ...] = tuple(PRIORITY_QUEUES)

# Retention for finished/failed job metadata (seconds). Bounds Redis growth so
# the registries stay small enough to sweep cheaply.
RESULT_TTL = 24 * 60 * 60  # keep successful results 24h for debugging/idempotency
FAILURE_TTL = 7 * 24 * 60 * 60  # keep failures 7d for post-mortem


def requeue_abandoned_jobs(queue: Queue, connection: Redis) -> list[str]:
    """Requeue every abandoned in-flight job for *queue*'s started registry.

    An "abandoned" job is one whose entry in the ``StartedJobRegistry`` has an
    expiry score earlier than now: a live worker renews that score while it
    holds the job, so an expired score means the worker that owned it died
    mid-execution. We re-enqueue each such job onto its origin lane via the
    real RQ ``Job.requeue`` contract, so it is picked up again instead of
    vanishing.

    Returns the list of job ids that were requeued.
    """
    registry = StartedJobRegistry(name=queue.name, connection=connection)
    expired_ids = registry.get_expired_job_ids()
    requeued: list[str] = []
    for job_id in expired_ids:
        try:
            job: Job = registry.job_class.fetch(job_id, connection=connection)
        except Exception:  # NoSuchJobError or a half-written job — drop from registry
            connection.zrem(registry.key, job_id)
            continue
        # Drop the stale started-registry entry (StartedJobRegistry.remove is a
        # no-op in RQ, so we zrem the sorted-set member directly), then re-enqueue
        # onto the job's *origin* lane. This mirrors RQ's own
        # ``FailedJobRegistry.requeue`` contract — reset the run timestamps and
        # push the same Job object back via ``Queue.enqueue_job`` — so the job is
        # picked up again from the lane it came from instead of vanishing.
        connection.zrem(registry.key, job_id)
        origin = Queue(job.origin, connection=connection)
        job.started_at = None
        job.ended_at = None
        job._exc_info = ""
        origin.enqueue_job(job)
        requeued.append(job_id)
        log.warning(
            "Requeued abandoned in-flight job %s onto lane %r (worker death recovery)",
            job_id,
            job.origin,
        )
    return requeued


def maintain_started_registries(queues: list[Queue], connection: Redis) -> list[str]:
    """Sweep the started registry of every lane, requeuing abandoned jobs.

    Returns the flat list of requeued job ids across all lanes.
    """
    requeued: list[str] = []
    for queue in queues:
        requeued.extend(requeue_abandoned_jobs(queue, connection))
    return requeued


class RecoveringWorker(Worker):
    """RQ worker that requeues abandoned in-flight jobs on each maintenance tick.

    ``Worker.run_maintenance_tasks`` is invoked periodically by ``work()`` (and
    once at startup); hooking it means crashed-worker recovery happens without a
    separate process while keeping standard ``work()`` semantics.
    """

    def run_maintenance_tasks(self) -> None:
        super().run_maintenance_tasks()
        try:
            maintain_started_registries(list(self.queues), self.connection)
        except Exception:  # never let recovery crash the worker loop
            log.exception("started-registry maintenance sweep failed")


def start_worker() -> None:
    redis_conn = from_url(cfg.REDIS_URL)
    raw_env_queues = os.environ.get("RQ_QUEUES", "").strip()
    if raw_env_queues:
        queue_names = [q.strip() for q in raw_env_queues.split(",") if q.strip()]
    else:
        queue_names = list(QUEUE_NAMES)
    queues = [Queue(name, connection=redis_conn) for name in queue_names]
    worker = RecoveringWorker(queues, connection=redis_conn)
    # Recover anything abandoned by a previous worker crash before we start.
    maintain_started_registries(queues, redis_conn)
    try:
        worker.work(with_scheduler=True)
    finally:
        redis_conn.close()


if __name__ == "__main__":
    start_worker()
