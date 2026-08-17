"""
Background Task Manager
=======================
Robust management of fire-and-forget async tasks with exception tracking,
logging, and Prometheus metrics. Ensures exceptions in background tasks are
surfaced to the monitoring layer instead of being silently swallowed.

Pattern: All `asyncio.create_task()` calls are routed through `create_tracked_task()`
which automatically attaches done callbacks to extract and log exceptions.

Usage:
    from nce.background_task_manager import create_tracked_task

    # Instead of: asyncio.create_task(my_coroutine())
    # Do this:
    create_tracked_task(my_coroutine(), name="my-task")

Exception Handling:
    Any exception raised inside a tracked task is:
    1. Logged to logger.exception() → visible in logs
    2. Recorded as metric nce_background_task_failures_total
    3. Exposed via Prometheus for alerting

This ensures failures in background jobs are never silent — they surface
through the central monitoring layer.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from nce.observability import (
    _safe_counter,
    _safe_gauge,
    _safe_histogram,
)

log = logging.getLogger("nce.background_task_manager")

# --- Prometheus Metrics ---

BACKGROUND_TASKS_TOTAL = _safe_counter(
    "nce_background_tasks_total",
    "Total count of background tasks created",
    ["task_name", "status"],  # status: created, completed, failed
)

BACKGROUND_TASK_DURATION = _safe_histogram(
    "nce_background_task_duration_seconds",
    "Duration of background tasks in seconds",
    ["task_name"],
    buckets=(0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, 300.0, float("inf")),
)

BACKGROUND_TASK_FAILURES_TOTAL = _safe_counter(
    "nce_background_task_failures_total",
    "Total count of background task failures (exceptions)",
    ["task_name", "exception_type"],
)

BACKGROUND_TASK_ACTIVE = _safe_gauge(
    "nce_background_task_active",
    "Current number of active background tasks",
    ["task_name"],
)

# Max completed entries retained per task name — prevents unbounded growth for
# long-running processes that spawn many short-lived tasks (e.g. token-refresh).
_MAX_COMPLETED_HISTORY: int = 200


# --- Task Registry ---


@dataclass
class TrackedTask:
    """Metadata for a tracked background task."""

    name: str
    task: asyncio.Task[Any]
    created_at: float = field(default_factory=time.time)
    completed_at: float | None = None
    exception: BaseException | None = None
    success: bool = False

    @property
    def duration(self) -> float:
        end_time = self.completed_at or time.time()
        return end_time - self.created_at

    def is_active(self) -> bool:
        return not self.task.done()


class TaskRegistry:
    """Global registry of tracked background tasks for lifecycle tracking.

    All methods are synchronous — asyncio is cooperative/single-threaded so
    plain dict operations are safe without an asyncio.Lock.
    """

    def __init__(self):
        self._tasks: dict[str, list[TrackedTask]] = {}

    def register(self, tracked_task: TrackedTask) -> None:
        """Register a new tracked task."""
        name = tracked_task.name
        if name not in self._tasks:
            self._tasks[name] = []
        self._tasks[name].append(tracked_task)
        BACKGROUND_TASK_ACTIVE.labels(task_name=name).inc()

    def mark_complete(
        self,
        tracked_task: TrackedTask,
        success: bool,
        exception: BaseException | None = None,
    ) -> None:
        """Mark a task as complete, record metrics, and prune old history."""
        tracked_task.completed_at = time.time()
        tracked_task.success = success
        tracked_task.exception = exception

        BACKGROUND_TASK_DURATION.labels(task_name=tracked_task.name).observe(tracked_task.duration)
        BACKGROUND_TASKS_TOTAL.labels(
            task_name=tracked_task.name, status="completed" if success else "failed"
        ).inc()
        if exception:
            BACKGROUND_TASK_FAILURES_TOTAL.labels(
                task_name=tracked_task.name,
                exception_type=type(exception).__name__,
            ).inc()
        BACKGROUND_TASK_ACTIVE.labels(task_name=tracked_task.name).dec()

        # Prune completed entries to prevent unbounded memory growth.
        tasks = self._tasks.get(tracked_task.name, [])
        completed = [t for t in tasks if not t.is_active()]
        if len(completed) > _MAX_COMPLETED_HISTORY:
            active = [t for t in tasks if t.is_active()]
            self._tasks[tracked_task.name] = active + completed[-_MAX_COMPLETED_HISTORY:]

    def get_active_tasks(self, task_name: str | None = None) -> list[TrackedTask]:
        """Get list of active tasks, optionally filtered by name."""
        if task_name:
            return [t for t in self._tasks.get(task_name, []) if t.is_active()]
        result: list[TrackedTask] = []
        for tasks in self._tasks.values():
            result.extend(t for t in tasks if t.is_active())
        return result

    def get_task_stats(self) -> dict[str, Any]:
        """Get per-name statistics about tracked tasks."""
        stats = {}
        for task_name, tasks in self._tasks.items():
            active = [t for t in tasks if t.is_active()]
            failed = [t for t in tasks if t.exception is not None]
            stats[task_name] = {
                "total": len(tasks),
                "active": len(active),
                "failed": len(failed),
                "succeeded": len([t for t in tasks if t.success]),
            }
        return stats


# Global registry instance
_registry = TaskRegistry()


def _mark_complete_sync(
    tracked_task: TrackedTask,
    success: bool,
    exception: BaseException | None = None,
) -> None:
    """Record completion on the global registry (sync; used by tests and edge paths)."""

    _registry.mark_complete(tracked_task, success, exception)


async def _mark_complete_async(
    tracked_task: TrackedTask,
    success: bool,
    exception: BaseException | None = None,
) -> None:
    """Async façade over :func:`_mark_complete_sync` for call sites that ``await`` bookkeeping."""

    _registry.mark_complete(tracked_task, success, exception)


def create_tracked_task(
    coro: Any,
    name: str,
) -> asyncio.Task[Any]:
    """
    Create a tracked background task with automatic exception logging.

    Synchronous — returns the asyncio.Task immediately without requiring
    ``await``.  Using ``await create_tracked_task(...)`` is an error (the
    return value is a Task, not a coroutine).

    Args:
        coro: The coroutine to run in the background.
        name: Stable low-cardinality name for the task (used in metrics).
              Use module-level constants such as ``"gc_loop"`` or
              ``"outbox_relay"`` — never embed IDs or namespace UUIDs here.

    Returns:
        The created asyncio.Task.

    Example:
        >>> task = create_tracked_task(my_long_job(), name="long-job")
    """
    task = asyncio.create_task(coro, name=name)
    tracked_task = TrackedTask(name=name, task=task)
    _registry.register(tracked_task)
    BACKGROUND_TASKS_TOTAL.labels(task_name=name, status="created").inc()

    def _done_callback(t: asyncio.Task[Any]) -> None:
        """Called when task completes (success, failure, or cancellation)."""
        exception = None
        success = False

        try:
            t.result()
            success = True
        except asyncio.CancelledError:
            log.info("Background task cancelled: name=%s", name)
        except Exception as exc:
            exception = exc
            log.exception(
                "Background task failed with exception: name=%s",
                name,
                exc_info=exc,
            )

        _registry.mark_complete(tracked_task, success, exception)

    task.add_done_callback(_done_callback)
    return task


async def get_active_background_tasks(
    task_name: str | None = None,
) -> list[TrackedTask]:
    """
    Get list of currently active background tasks.

    Args:
        task_name: Optional filter by specific task name.

    Returns:
        List of TrackedTask instances that are still running.
    """
    return _registry.get_active_tasks(task_name)


async def get_background_task_stats() -> dict[str, Any]:
    """
    Get statistics about all tracked background tasks.

    Returns:
        Dictionary with per-task-name stats:
        {
            "gc_loop": {"total": 1, "active": 1, "failed": 0, "succeeded": 0},
        }
    """
    return _registry.get_task_stats()
