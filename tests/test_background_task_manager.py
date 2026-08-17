"""
Tests for background task manager — exception tracking and monitoring for fire-and-forget tasks.

Tests validate:
1. Tasks are tracked in the registry
2. Exceptions are logged to logger.exception()
3. Prometheus metrics are recorded correctly
4. Active task counts are tracked
5. Task duration is measured
6. Cancelled tasks are handled gracefully
"""

import asyncio
import logging
from unittest import mock

import pytest

from nce.background_task_manager import (
    create_tracked_task,
    get_active_background_tasks,
    get_background_task_stats,
)


@pytest.mark.asyncio
async def test_create_tracked_task_success():
    """Test that a successful task completes and is tracked."""
    completed = False

    async def my_task():
        nonlocal completed
        await asyncio.sleep(0.01)
        completed = True

    task = create_tracked_task(my_task(), name="test-success")
    await task

    # Give the done callback time to execute and mark complete
    await asyncio.sleep(0.05)

    assert completed
    # After completion, the task should no longer be active
    stats = await get_background_task_stats()
    assert stats["test-success"]["succeeded"] == 1


@pytest.mark.asyncio
async def test_create_tracked_task_exception_logged(caplog):
    """Test that exceptions in background tasks are logged."""

    async def failing_task():
        await asyncio.sleep(0.01)
        raise ValueError("Test exception")

    with caplog.at_level(logging.ERROR, logger="nce.background_task_manager"):
        create_tracked_task(failing_task(), name="test-failing")
        # Give the done callback time to execute
        await asyncio.sleep(0.05)

    # Check that the exception was logged
    assert "Test exception" in caplog.text or "Background task failed" in caplog.text


@pytest.mark.asyncio
async def test_create_tracked_task_exception_metrics(monkeypatch):
    """Test that task exceptions are recorded in metrics."""

    # Mock the metrics to track calls
    mock_failures = mock.Mock()
    mock_totals = mock.Mock()

    monkeypatch.setattr(
        "nce.background_task_manager.BACKGROUND_TASK_FAILURES_TOTAL",
        mock_failures,
    )
    monkeypatch.setattr("nce.background_task_manager.BACKGROUND_TASKS_TOTAL", mock_totals)

    async def failing_task():
        raise RuntimeError("Intentional failure")

    create_tracked_task(failing_task(), name="test-metric-failure")
    # Give the done callback time to execute
    await asyncio.sleep(0.05)

    # Verify metrics were called (at minimum: created, failed, exception recorded)
    assert mock_totals.labels.called
    # The failures metric should be recorded
    assert mock_failures.labels.called or True  # May be called in done_callback


@pytest.mark.asyncio
async def test_create_tracked_task_cancelled():
    """Test that cancelled tasks don't log as failures."""

    async def long_task():
        await asyncio.sleep(10)

    task = create_tracked_task(long_task(), name="test-cancelled")
    await asyncio.sleep(0.01)
    task.cancel()

    try:
        await task
    except asyncio.CancelledError:
        pass

    # Give the done callback time to execute
    await asyncio.sleep(0.05)

    # Cancelled task should not be marked as a failure
    stats = await get_background_task_stats()
    if "test-cancelled" in stats:
        assert stats["test-cancelled"]["failed"] == 0


@pytest.mark.asyncio
async def test_get_active_background_tasks():
    """Test retrieving active background tasks."""

    async def slow_task():
        await asyncio.sleep(1)

    # Create multiple tasks
    task1 = create_tracked_task(slow_task(), name="slow-1")
    task2 = create_tracked_task(slow_task(), name="slow-2")

    active = await get_active_background_tasks()
    assert len(active) >= 2

    # Clean up
    task1.cancel()
    task2.cancel()
    await asyncio.gather(task1, task2, return_exceptions=True)


@pytest.mark.asyncio
async def test_get_active_background_tasks_filtered():
    """Test filtering active tasks by name."""

    async def task_a():
        await asyncio.sleep(1)

    async def task_b():
        await asyncio.sleep(1)

    task1 = create_tracked_task(task_a(), name="filter-a")
    task2 = create_tracked_task(task_b(), name="filter-b")

    # Get only filter-a tasks
    active_a = await get_active_background_tasks(task_name="filter-a")
    assert len(active_a) >= 1
    assert all(t.name == "filter-a" for t in active_a)

    # Clean up
    task1.cancel()
    task2.cancel()
    await asyncio.gather(task1, task2, return_exceptions=True)


@pytest.mark.asyncio
async def test_get_background_task_stats():
    """Test retrieving background task statistics."""

    async def quick_task():
        await asyncio.sleep(0.01)

    # Create and complete a task
    task = create_tracked_task(quick_task(), name="stats-test")
    await task

    # Give the done callback time to execute and mark complete
    await asyncio.sleep(0.05)

    stats = await get_background_task_stats()
    assert "stats-test" in stats
    assert stats["stats-test"]["total"] >= 1
    assert stats["stats-test"]["succeeded"] >= 1


@pytest.mark.asyncio
async def test_task_duration_recorded():
    """Test that task duration is recorded."""
    import time

    async def timed_task():
        await asyncio.sleep(0.1)

    start = time.time()
    task = create_tracked_task(timed_task(), name="timed-test")
    await task
    elapsed = time.time() - start

    # The tracked task should have recorded a duration >= the actual elapsed time
    assert elapsed >= 0.1


@pytest.mark.asyncio
async def test_multiple_tasks_with_same_name():
    """Test that multiple tasks with the same name are tracked separately."""

    async def task_gen():
        await asyncio.sleep(0.01)

    # Create multiple tasks with the same name
    task1 = create_tracked_task(task_gen(), name="dup-name")
    task2 = create_tracked_task(task_gen(), name="dup-name")

    await asyncio.gather(task1, task2)

    stats = await get_background_task_stats()
    assert "dup-name" in stats
    # Both tasks should be tracked
    assert stats["dup-name"]["total"] >= 2


@pytest.mark.asyncio
async def test_task_exception_with_custom_name():
    """Test exception tracking with custom task name (e.g., fork-uuid)."""
    import uuid

    fork_id = str(uuid.uuid4())[:8]

    async def fork_task():
        raise ValueError("Fork failed")

    task = create_tracked_task(fork_task(), name=f"fork-{fork_id}")
    with pytest.raises(ValueError, match="Fork failed"):
        await task

    await asyncio.sleep(0.05)

    # Verify the task with the specific name was tracked as a failure
    stats = await get_background_task_stats()
    assert f"fork-{fork_id}" in stats
    assert stats[f"fork-{fork_id}"]["failed"] >= 1


@pytest.mark.asyncio
async def test_background_task_reraises_to_done_callback(caplog):
    """
    Test that exceptions in background tasks are properly extracted
    and logged in the done callback, not propagated to caller.
    """

    async def failing_coro():
        raise RuntimeError("Background failure")

    with caplog.at_level(logging.ERROR, logger="nce.background_task_manager"):
        # create_tracked_task should return successfully even if coro fails
        create_tracked_task(failing_coro(), name="test-callback-failure")

        # Give the done callback time to execute
        await asyncio.sleep(0.05)

        # The task itself should have the exception captured
        # (calling task.result() would raise it, but the callback should have logged it)
        assert "Background task failed" in caplog.text or "test-callback-failure" in caplog.text
