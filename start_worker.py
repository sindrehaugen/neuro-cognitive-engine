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
import sys
import traceback
from typing import TYPE_CHECKING, Any

from redis import Redis, from_url
from rq import Queue, Worker
from rq.registry import StartedJobRegistry
from rq.timeouts import JobTimeoutException
from rq.utils import now as rq_now

from nce.config import cfg
from nce.ops.quarantine import (
    FAILURES_KEY_PREFIX,
    FAILURES_TTL_SECONDS,
    INFLIGHT_KEY_PREFIX,
    INFLIGHT_TTL_SECONDS,
    compute_job_fingerprint,
    quarantine_poison_job_sync,
)

if TYPE_CHECKING:
    from rq.executions import Execution
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
        #
        # IMPORTANT (v3.7.0 OOM / abrupt-death quarantine): intentionally do NOT
        # clear ``nce:rq_inflight:{job_fp}`` here; its presence on next pickup
        # tells RecoveringWorker that the prior attempt terminated abruptly.
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
    """RQ worker with crash recovery and poison-pill / OOM quarantine guards.

    ``Worker.run_maintenance_tasks`` requeues abandoned in-flight jobs on each
    maintenance tick, while :meth:`perform_job` enforces pre-execution checks on
    ``nce:rq_inflight:{job_fp}`` and ``nce:rq_failures:{job_fp}`` so repeated
    poison pills or abruptly terminated OOM jobs quarantine cleanly without
    crash-looping.
    """

    def __init__(self, *args: Any, quarantine_pg_pool: Any = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._quarantine_pg_pool = quarantine_pg_pool

    def run_maintenance_tasks(self) -> None:
        super().run_maintenance_tasks()
        try:
            maintain_started_registries(list(self.queues), self.connection)
        except Exception:  # never let recovery crash the worker loop
            log.exception("started-registry maintenance sweep failed")

    def _job_fingerprint(self, job: Job, queue: Queue) -> str:
        origin_name = getattr(job, "origin", None) or getattr(queue, "name", "")
        return compute_job_fingerprint(
            queue_name=str(origin_name),
            func_name=str(getattr(job, "func_name", "") or ""),
            args=getattr(job, "args", ()) or (),
            kwargs=getattr(job, "kwargs", None) or {},
        )

    def _re_enqueue_once(self, job: Job, queue: Queue) -> None:
        origin_name = getattr(job, "origin", None) or queue.name
        origin = Queue(origin_name, connection=self.connection)
        try:
            job.requeue()
            return
        except Exception as exc:
            log.debug("Job.requeue fallback to Queue.enqueue_job for %s: %s", job.id, exc)
        job.started_at = None
        job.ended_at = None
        job._exc_info = ""
        origin.enqueue_job(job)

    def handle_work_horse_killed(
        self,
        job: Job,
        retpid: int,
        ret_val: Any,
        rusage: Any,
    ) -> None:
        """Record abrupt work-horse death (e.g., OOM exit 137 / SIGKILL) and keep inflight."""
        super().handle_work_horse_killed(job, retpid, ret_val, rusage)
        try:
            origin_name = getattr(job, "origin", None) or (
                self.queues[0].name if self.queues else "default"
            )
            job_fp = compute_job_fingerprint(
                queue_name=str(origin_name),
                func_name=str(getattr(job, "func_name", "") or ""),
                args=getattr(job, "args", ()) or (),
                kwargs=getattr(job, "kwargs", None) or {},
            )
            quarantine_poison_job_sync(
                pg_pool=self._quarantine_pg_pool,
                redis_client=self.connection,
                queue_name=str(origin_name),
                func_name=str(getattr(job, "func_name", "") or ""),
                args=getattr(job, "args", ()) or (),
                kwargs=getattr(job, "kwargs", None) or {},
                failure_count=2,
                exit_reason="work_horse_killed",
                traceback_text=f"Work horse killed abruptly (pid={retpid}, status={ret_val})",
                job_id=str(getattr(job, "id", "") or ""),
                job_fingerprint=job_fp,
            )
        except Exception:
            log.exception("Failed recording killed work horse quarantine for job %s", job.id)

    def perform_job(
        self,
        job: Job,
        queue: Queue,
        execution: Execution | None = None,
    ) -> bool:
        """Perform a job with OOM inflight-detection and 2-strike poison quarantine."""
        if execution is None:
            execution = self.prepare_execution(job)

        origin_name = getattr(job, "origin", None) or queue.name
        job_fp = self._job_fingerprint(job, queue)
        inflight_key = f"{INFLIGHT_KEY_PREFIX}{job_fp}"
        failures_key = f"{FAILURES_KEY_PREFIX}{job_fp}"
        remove_from_intermediate_queue = len(self.queues) == 1

        raw_inflight = self.connection.get(inflight_key)
        raw_failures = self.connection.get(failures_key)
        prior_failures = (
            int(raw_failures.decode("utf-8") if isinstance(raw_failures, bytes) else raw_failures)
            if raw_failures is not None
            else 0
        )

        # Pre-execution guard: if inflight marker persisted from an abrupt exit
        # (OOM / SIGKILL) OR failure threshold already reached, quarantine BEFORE
        # calling the user callable.
        if raw_inflight is not None or prior_failures >= 2:
            was_abrupt = raw_inflight is not None
            exit_reason = "oom_or_abrupt_exit" if was_abrupt else "repeated_exception"
            tb_hint = str(getattr(job, "_exc_info", "") or "").strip() or (
                "Previous worker died abruptly mid-job (lingering inflight marker)"
                if was_abrupt
                else f"Repeated exception quarantine triggered (failures={prior_failures})"
            )
            self.prepare_job_execution(job, remove_from_intermediate_queue)
            quarantine_poison_job_sync(
                pg_pool=self._quarantine_pg_pool,
                redis_client=self.connection,
                queue_name=str(origin_name),
                func_name=str(getattr(job, "func_name", "") or ""),
                args=getattr(job, "args", ()) or (),
                kwargs=getattr(job, "kwargs", None) or {},
                failure_count=max(2, prior_failures + (1 if was_abrupt else 0)),
                exit_reason=exit_reason,
                traceback_text=tb_hint,
                job_id=str(getattr(job, "id", "") or ""),
                job_fingerprint=job_fp,
            )
            self.connection.delete(inflight_key)
            self._finalize_success(
                job,
                queue,
                execution,
                {"quarantined": True, "exit_reason": exit_reason, "job_fingerprint": job_fp},
            )
            return False

        self.connection.set(inflight_key, str(job.id), ex=INFLIGHT_TTL_SECONDS)
        try:
            self.prepare_job_execution(job, remove_from_intermediate_queue)
            job.started_at = rq_now()
            timeout = job.timeout or self.queue_class.DEFAULT_TIMEOUT
            with self.death_penalty_class(timeout, JobTimeoutException, job_id=job.id):
                self.log.debug("Worker %s: performing job %s ...", self.name, job.id)
                return_value = job.perform()
                self.log.debug("Worker %s: finished performing job %s", self.name, job.id)

            self._finalize_success(job, queue, execution, return_value)
            self.connection.delete(inflight_key, failures_key)
            return True
        except (KeyboardInterrupt, SystemExit):
            # Intentionally preserve inflight_key across abrupt termination so
            # subsequent pickup detects the interrupted run pre-execution.
            raise
        except Exception:
            exc_info = sys.exc_info()
            tb_text = "".join(traceback.format_exception(*exc_info))
            self.connection.delete(inflight_key)
            failures = int(self.connection.incr(failures_key))
            self.connection.expire(failures_key, FAILURES_TTL_SECONDS)
            self._finalize_failure(job, queue, execution, exc_info)

            if failures >= 2:
                quarantine_poison_job_sync(
                    pg_pool=self._quarantine_pg_pool,
                    redis_client=self.connection,
                    queue_name=str(origin_name),
                    func_name=str(getattr(job, "func_name", "") or ""),
                    args=getattr(job, "args", ()) or (),
                    kwargs=getattr(job, "kwargs", None) or {},
                    failure_count=failures,
                    exit_reason="repeated_exception",
                    traceback_text=tb_text,
                    job_id=str(getattr(job, "id", "") or ""),
                    job_fingerprint=job_fp,
                )
            elif failures == 1 and getattr(job, "retries_left", None) in (None, 0):
                self._re_enqueue_once(job, queue)
            return False


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
