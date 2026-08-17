"""
Batch 121 — DLQ auto-triage integration tests.

Tests:
  1. Transient failure auto-replays up to cfg.NCE_DLQ_AUTO_REPLAY_MAX, then alerts
     and leaves the entry for manual handling.
  2. Three same-fingerprint deterministic failures open the circuit.
  3. Subsequent enqueue of that task type is rejected (CircuitOpenError).
  4. Admin close re-enables enqueue.

All tests are @pytest.mark.integration and require live Postgres + Redis.
"""

from __future__ import annotations

import os
import uuid
from unittest.mock import patch

import pytest
import pytest_asyncio

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def redis_client():
    """Live Redis client pointed at the integration stack."""
    import redis.asyncio as aioredis

    url = os.environ.get("REDIS_URL", "redis://127.0.0.1:6380/0")
    client = aioredis.from_url(url)
    try:
        await client.ping()
    except Exception as exc:
        pytest.skip(f"Redis not reachable for integration tests: {exc}")
    yield client
    await client.aclose()


@pytest_asyncio.fixture
def sync_redis():
    """Synchronous Redis client (used by tasks.check_circuit_before_enqueue)."""
    from redis import Redis

    url = os.environ.get("REDIS_URL", "redis://127.0.0.1:6380/0")
    client = Redis.from_url(url)
    try:
        client.ping()
    except Exception as exc:
        pytest.skip(f"Redis not reachable for integration tests: {exc}")
    yield client
    client.close()


@pytest_asyncio.fixture
async def clean_circuit(sync_redis):
    """Ensure the test task_name has a clean circuit state before + after.

    Each call generates a unique task name so cross-run DLQ row accumulation
    in the dead_letter_queue table is impossible (fingerprint includes task_name).
    """
    task = f"test_triage_task_{uuid.uuid4().hex[:8]}"
    sync_redis.delete(f"nce:dlq:quarantine:{task}")
    yield task
    sync_redis.delete(f"nce:dlq:quarantine:{task}")


@pytest_asyncio.fixture
async def clean_det_circuit(sync_redis):
    """Ensure the deterministic-failure test task_name is clean.

    Each call generates a unique task name so cross-run DLQ row accumulation
    in the dead_letter_queue table is impossible (fingerprint includes task_name).
    """
    task = f"test_det_task_{uuid.uuid4().hex[:8]}"
    sync_redis.delete(f"nce:dlq:quarantine:{task}")
    yield task
    sync_redis.delete(f"nce:dlq:quarantine:{task}")


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


async def _make_exc(msg: str = "simulated error") -> Exception:
    """Return a fresh exception with a real traceback attached."""
    try:
        raise TimeoutError(msg)
    except TimeoutError as exc:
        return exc


async def _make_det_exc(msg: str = "assertion failed") -> Exception:
    """Return a fresh deterministic exception with a real traceback attached."""
    try:
        raise AssertionError(msg)
    except AssertionError as exc:
        return exc


# ---------------------------------------------------------------------------
# Test 1: Transient auto-replay up to cap then alert
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_transient_auto_replay_then_alert(pg_pool, sync_redis, clean_circuit):
    """
    A transient failure (TimeoutError) is auto-replayed up to
    cfg.NCE_DLQ_AUTO_REPLAY_MAX times, then an alert fires and the entry
    is left for manual handling.
    """
    from nce.config import cfg
    from nce.dead_letter_queue import triage_dead_letter

    task_name = clean_circuit
    cfg.NCE_DLQ_AUTO_REPLAY_MAX = 2  # Low cap for test speed

    alerts_dispatched: list[str] = []

    async def _mock_dispatch(self, title: str, message: str) -> None:
        alerts_dispatched.append(title)

    with patch("nce.notifications.NotificationDispatcher.dispatch_alert", new=_mock_dispatch):
        # First attempt — should schedule auto-replay #1
        exc1 = await _make_exc("conn timeout attempt 1")
        result1 = await triage_dead_letter(
            pg_pool,
            sync_redis,
            task_name,
            str(uuid.uuid4()),
            {"filepath": "/a.py"},
            exc1,
            attempt_count=1,
        )
        assert result1["error_class"] == "transient"
        assert result1["triage_action"] == "auto_replay_scheduled"

        fingerprint = result1["fingerprint"]

        # Second attempt — should schedule auto-replay #2
        exc2 = await _make_exc("conn timeout attempt 2")
        result2 = await triage_dead_letter(
            pg_pool,
            sync_redis,
            task_name,
            str(uuid.uuid4()),
            {"filepath": "/a.py"},
            exc2,
            attempt_count=2,
        )
        assert result2["error_class"] == "transient"
        assert result2["triage_action"] == "auto_replay_scheduled"
        assert result2["fingerprint"] == fingerprint

        # Third attempt — cap (2) reached → manual_review_required + alert
        exc3 = await _make_exc("conn timeout attempt 3")
        result3 = await triage_dead_letter(
            pg_pool,
            sync_redis,
            task_name,
            str(uuid.uuid4()),
            {"filepath": "/a.py"},
            exc3,
            attempt_count=3,
        )
        assert result3["error_class"] == "transient"
        assert result3["triage_action"] == "manual_review_required"
        assert result3["fingerprint"] == fingerprint

    # At least one alert should have been dispatched (cap reached)
    assert any("cap" in t.lower() or "auto-replay" in t.lower() for t in alerts_dispatched), (
        f"Expected a cap-reached alert, got: {alerts_dispatched}"
    )

    # Verify DLQ rows were created in the DB
    async with pg_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT count(*) FROM dead_letter_queue WHERE task_name = $1",
            task_name,
        )
    assert count >= 3, f"Expected at least 3 DLQ rows, got {count}"


# ---------------------------------------------------------------------------
# Test 2: Three deterministic failures open the circuit
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_deterministic_failures_open_circuit(pg_pool, sync_redis, clean_det_circuit):
    """
    After cfg.NCE_DLQ_CIRCUIT_THRESHOLD (3) same-fingerprint deterministic
    failures the circuit flag is set in Redis.
    """
    from nce.config import cfg
    from nce.dead_letter_queue import is_circuit_open, triage_dead_letter

    task_name = clean_det_circuit
    cfg.NCE_DLQ_CIRCUIT_THRESHOLD = 3

    alerts_dispatched: list[str] = []

    async def _mock_dispatch(self, title: str, message: str) -> None:
        alerts_dispatched.append(title)

    with patch("nce.notifications.NotificationDispatcher.dispatch_alert", new=_mock_dispatch):
        for i in range(1, 4):
            exc = await _make_det_exc(f"assertion failed run {i}")
            result = await triage_dead_letter(
                pg_pool,
                sync_redis,
                task_name,
                str(uuid.uuid4()),
                {"key": "value"},
                exc,
                attempt_count=i,
            )
            assert result["error_class"] == "deterministic", (
                f"Expected deterministic, got {result['error_class']} on attempt {i}"
            )

            if i < 3:
                assert result["triage_action"] == "deterministic_pending"
                assert not is_circuit_open(sync_redis, task_name), (
                    f"Circuit should be closed after {i} entries"
                )
            else:
                assert result["triage_action"] == "circuit_opened"
                assert is_circuit_open(sync_redis, task_name), (
                    "Circuit should be open after reaching threshold"
                )

    # Alert should have been dispatched for circuit open
    assert any("circuit" in t.lower() for t in alerts_dispatched), (
        f"Expected a circuit-open alert, got: {alerts_dispatched}"
    )


# ---------------------------------------------------------------------------
# Test 3: Enqueue rejected while circuit is open
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_enqueue_rejected_while_circuit_open(sync_redis, clean_det_circuit):
    """
    With the circuit open, check_circuit_before_enqueue raises CircuitOpenError.
    """
    from nce.dead_letter_queue import open_circuit
    from nce.tasks import CircuitOpenError, check_circuit_before_enqueue

    task_name = clean_det_circuit
    open_circuit(sync_redis, task_name)

    with pytest.raises(CircuitOpenError, match="circuit-open"):
        check_circuit_before_enqueue(task_name, redis_client=sync_redis)


# ---------------------------------------------------------------------------
# Test 4: Admin close re-enables enqueue
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_admin_close_reenables_enqueue(sync_redis, clean_det_circuit):
    """
    After close_circuit, check_circuit_before_enqueue no longer raises.
    """
    from nce.dead_letter_queue import close_circuit, is_circuit_open, open_circuit
    from nce.tasks import check_circuit_before_enqueue

    task_name = clean_det_circuit
    open_circuit(sync_redis, task_name)
    assert is_circuit_open(sync_redis, task_name)

    close_circuit(sync_redis, task_name)
    assert not is_circuit_open(sync_redis, task_name)

    # Should not raise now
    check_circuit_before_enqueue(task_name, redis_client=sync_redis)


# ---------------------------------------------------------------------------
# Unit tests (no DB/Redis) — fingerprint + classification
# ---------------------------------------------------------------------------


def test_compute_error_fingerprint_stable():
    """Same inputs always produce the same fingerprint."""
    from nce.dead_letter_queue import compute_error_fingerprint

    fp1 = compute_error_fingerprint("my_task", "TimeoutError", top_frame="app/worker.py:42:run")
    fp2 = compute_error_fingerprint("my_task", "TimeoutError", top_frame="app/worker.py:42:run")
    assert fp1 == fp2
    assert len(fp1) == 64  # SHA-256 hex


def test_compute_error_fingerprint_differs_by_task():
    """Different task_names yield different fingerprints."""
    from nce.dead_letter_queue import compute_error_fingerprint

    fp1 = compute_error_fingerprint("task_a", "TimeoutError", top_frame="x.py:1:fn")
    fp2 = compute_error_fingerprint("task_b", "TimeoutError", top_frame="x.py:1:fn")
    assert fp1 != fp2


def test_classify_error_transient_timeout():
    """TimeoutError is classified as transient."""
    from nce.dead_letter_queue import classify_error

    assert classify_error("TimeoutError", "operation timed out") == "transient"


def test_classify_error_transient_429():
    """HTTP 429 in the message is classified as transient."""
    from nce.dead_letter_queue import classify_error

    assert classify_error("HTTPStatusError", "status code 429 Too Many Requests") == "transient"


def test_classify_error_transient_503():
    """HTTP 503 in the message is classified as transient."""
    from nce.dead_letter_queue import classify_error

    assert classify_error("RequestError", "got 503 Service Unavailable") == "transient"


def test_classify_error_deterministic_value_error():
    """ValueError is classified as deterministic."""
    from nce.dead_letter_queue import classify_error

    assert classify_error("ValueError", "invalid literal for int()") == "deterministic"


def test_classify_error_deterministic_assertion():
    """AssertionError is classified as deterministic."""
    from nce.dead_letter_queue import classify_error

    assert classify_error("AssertionError", "expected True") == "deterministic"
