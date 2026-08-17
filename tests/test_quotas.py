"""Unit tests for Phase 3.2 resource quotas (async consume + rollback)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac as _hmac
import importlib.util
import json
import sys
import time
import types
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.testclient import TestClient

import nce.quotas as quotas
from nce.auth import HMACAuthMiddleware
from nce.quotas import QuotaExceededError


def _register_mcp_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Allow ``import server`` in environments without the MCP SDK wheel."""

    def _passthrough_decorator(fn):
        return fn

    class _FakeServer:
        def __init__(self, _name: str) -> None:
            pass

        def list_tools(self):
            return _passthrough_decorator

        def call_tool(self):
            return _passthrough_decorator

    @asynccontextmanager
    async def _fake_stdio():
        yield (MagicMock(), MagicMock())

    mcp = types.ModuleType("mcp")
    mcp_server = types.ModuleType("mcp.server")
    mcp_server_stdio = types.ModuleType("mcp.server.stdio")
    mcp_types = types.ModuleType("mcp.types")

    mcp_server.Server = _FakeServer
    mcp_server_stdio.stdio_server = _fake_stdio
    mcp_types.Tool = MagicMock()
    mcp_types.TextContent = MagicMock()

    monkeypatch.setitem(sys.modules, "mcp", mcp)
    monkeypatch.setitem(sys.modules, "mcp.server", mcp_server)
    monkeypatch.setitem(sys.modules, "mcp.server.stdio", mcp_server_stdio)
    monkeypatch.setitem(sys.modules, "mcp.types", mcp_types)


@pytest.mark.asyncio
async def test_consume_skips_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)
    pool = MagicMock()
    ns = uuid.uuid4()
    res = await quotas.consume_resources(
        pool,
        namespace_id=ns,
        agent_id="a1",
        amounts={quotas.RESOURCE_LLM_TOKENS: 100},
    )
    assert res.is_empty
    pool.acquire.assert_not_called()


@pytest.mark.asyncio
async def test_consume_updates_matching_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", True)

    ns_id = uuid.uuid4()
    qrow_ns = uuid.uuid4()

    conn = AsyncMock()
    conn.fetch.return_value = [{"id": qrow_ns, "agent_id": None}]
    conn.fetchrow.return_value = {"id": qrow_ns}

    tx = AsyncMock()
    conn.transaction = MagicMock(return_value=tx)
    tx.__aenter__.return_value = None
    tx.__aexit__.return_value = None

    pool = MagicMock()
    acq = AsyncMock()
    acq.__aenter__.return_value = conn
    acq.__aexit__.return_value = None
    pool.acquire = MagicMock(return_value=acq)

    res = await quotas.consume_resources(
        pool,
        namespace_id=ns_id,
        agent_id="agent-x",
        amounts={quotas.RESOURCE_LLM_TOKENS: 10},
    )
    assert len(res.steps) == 1
    assert res.steps[0] == (qrow_ns, 10)
    conn.fetch.assert_called()
    conn.fetchrow.assert_called_once()


@pytest.mark.asyncio
async def test_consume_raises_when_limit_hit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", True)

    ns_id = uuid.uuid4()
    qrow_ns = uuid.uuid4()

    conn = AsyncMock()
    conn.fetch.return_value = [{"id": qrow_ns, "agent_id": None}]
    conn.fetchrow.return_value = None

    tx = AsyncMock()
    conn.transaction = MagicMock(return_value=tx)
    tx.__aenter__.return_value = None
    tx.__aexit__.return_value = None

    pool = MagicMock()
    acq = AsyncMock()
    acq.__aenter__.return_value = conn
    acq.__aexit__.return_value = None
    pool.acquire = MagicMock(return_value=acq)

    with pytest.raises(QuotaExceededError):
        await quotas.consume_resources(
            pool,
            namespace_id=ns_id,
            agent_id="a",
            amounts={quotas.RESOURCE_LLM_TOKENS: 999},
        )


def test_tool_quota_plan_store_memory_shapes() -> None:
    ns = str(uuid.uuid4())
    plan = quotas.tool_quota_plan(
        "store_memory",
        {
            "namespace_id": ns,
            "agent_id": "bot",
            "content": "hello",
            "summary": "hi",
            "heavy_payload": "x" * 40,
        },
    )
    assert plan is not None
    n, agent, amounts = plan
    assert str(n) == ns
    assert agent == "bot"
    assert quotas.RESOURCE_LLM_TOKENS in amounts
    assert quotas.RESOURCE_STORAGE_BYTES in amounts
    assert amounts[quotas.RESOURCE_MEMORY_COUNT] == 1


def test_tool_quota_plan_unknown_returns_none() -> None:
    assert quotas.tool_quota_plan("graph_search", {}) is None


@pytest.mark.asyncio
async def test_reservation_rollback_skips_when_no_pool() -> None:
    r = quotas.null_reservation()
    await r.rollback()


@pytest.mark.asyncio
async def test_reservation_rollback_executes_decrements() -> None:
    conn = AsyncMock()
    tx = AsyncMock()
    conn.transaction = MagicMock(return_value=tx)
    tx.__aenter__.return_value = None
    tx.__aexit__.return_value = None

    pool = MagicMock()
    acq = AsyncMock()
    acq.__aenter__.return_value = conn
    acq.__aexit__.return_value = None
    pool.acquire = MagicMock(return_value=acq)

    qid = uuid.uuid4()
    r = quotas.QuotaReservation(pool=pool, steps=[(qid, 5)])
    await r.rollback()
    assert r.is_empty
    conn.execute.assert_called_once()


# ---------------------------------------------------------------------------
# Concurrent quota consumption — race-condition regression tests
# ---------------------------------------------------------------------------


def _build_multi_conn_pool(
    qid: uuid.UUID,
    agent_id: str | None,
    limit_amount: int,
    *,
    initial_used: int = 0,
) -> tuple[MagicMock, dict]:
    """
    Build a mock pool where each ``acquire()`` returns an *independent*
    connection that shares state via a mutable dict.

    This simulates real Postgres behaviour: each concurrent task gets its
    own connection, and the ``FOR UPDATE`` + ``UPDATE WHERE used + delta <= limit``
    gate is the only coordination point.
    """
    state: dict = {
        "used": initial_used,
        "limit": limit_amount,
        "qid": qid,
    }

    pool = MagicMock()

    def _make_conn() -> AsyncMock:
        conn = AsyncMock()
        conn.fetch.return_value = [{"id": qid, "agent_id": agent_id}]

        async def _fetchrow_update(sql: str, delta: int, row_id: uuid.UUID) -> dict | None:
            """Simulate the atomicity of ``UPDATE ... WHERE used + delta <= limit``."""
            # This is called under the "FOR UPDATE lock" (which we simulate by
            # having this function be the single point of mutation).
            new_used = state["used"] + delta
            if new_used <= state["limit"]:
                state["used"] = new_used
                return {"id": row_id}
            return None

        conn.fetchrow = _fetchrow_update

        tx = AsyncMock()
        conn.transaction = MagicMock(return_value=tx)
        tx.__aenter__.return_value = None
        tx.__aexit__.return_value = None
        return conn

    # ``pool.acquire()`` must return synchronously (not a coroutine) —
    # ``async with pool.acquire() as conn:`` calls ``__aenter__`` on the
    # return value directly.
    def _acquire(*_args: object, **_kwargs: object) -> MagicMock:
        acq = MagicMock()
        acq.__aenter__ = AsyncMock(return_value=_make_conn())
        acq.__aexit__ = AsyncMock(return_value=None)
        return acq

    pool.acquire = _acquire
    return pool, state


@pytest.mark.asyncio
async def test_concurrent_multi_connection_overallocation_prevented(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    N concurrent workers, each with its OWN connection, drain a shared quota.

    This is the real production scenario: workers get independent connections
    from an ``asyncpg.Pool``, and ``SELECT ... FOR UPDATE`` serialises access
    to the quota row.  The test uses per-task connections with shared state
    to simulate the Postgres row-level lock.
    """
    monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", True)

    ns_id = uuid.uuid4()
    qid = uuid.uuid4()
    LIMIT = 100
    DELTA = 30  # 3 workers fit (90), 4th is rejected → 3 success, 3 fail

    pool, state = _build_multi_conn_pool(qid, None, LIMIT)

    tasks = [
        quotas.consume_resources(
            pool,
            namespace_id=ns_id,
            agent_id="w1",
            amounts={quotas.RESOURCE_LLM_TOKENS: DELTA},
        )
        for _ in range(6)
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    successes = [r for r in results if isinstance(r, quotas.QuotaReservation)]
    failures = [r for r in results if isinstance(r, QuotaExceededError)]

    assert len(successes) == 3, f"expected 3 successes, got {len(successes)}"
    assert len(failures) == 3, f"expected 3 failures, got {len(failures)}"

    total = sum(s.steps[0][1] for s in successes if s.steps)
    assert total == 3 * DELTA
    assert total <= LIMIT, f"overallocated! consumed={total} limit={LIMIT}"
    assert state["used"] == 90, f"DB state = {state['used']}, expected 90"


@pytest.mark.asyncio
async def test_concurrent_multi_conn_exact_fill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    When the quota limit divides evenly, exactly N workers succeed with
    zero remaining capacity — the (N+1)th worker is rejected.
    """
    monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", True)

    ns_id = uuid.uuid4()
    qid = uuid.uuid4()
    LIMIT = 100
    DELTA = 25  # Exactly 4 workers fit (4 × 25 = 100)

    pool, state = _build_multi_conn_pool(qid, None, LIMIT)

    tasks = [
        quotas.consume_resources(
            pool,
            namespace_id=ns_id,
            agent_id="w1",
            amounts={quotas.RESOURCE_LLM_TOKENS: DELTA},
        )
        for _ in range(5)
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    successes = [r for r in results if isinstance(r, quotas.QuotaReservation)]
    failures = [r for r in results if isinstance(r, QuotaExceededError)]

    assert len(successes) == 4, f"expected 4 successes, got {len(successes)}"
    assert len(failures) == 1, f"expected 1 failure, got {len(failures)}"
    assert state["used"] == 100


@pytest.mark.asyncio
async def test_concurrent_multi_conn_last_unit_consumed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Boundary case: only 1 unit of quota remains. One worker consumes it,
    all others are rejected.
    """
    monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", True)

    ns_id = uuid.uuid4()
    qid = uuid.uuid4()
    LIMIT = 100
    INITIAL = 99  # only 1 unit remaining

    pool, state = _build_multi_conn_pool(qid, None, LIMIT, initial_used=INITIAL)

    tasks = [
        quotas.consume_resources(
            pool,
            namespace_id=ns_id,
            agent_id="w1",
            amounts={quotas.RESOURCE_LLM_TOKENS: 1},
        )
        for _ in range(5)
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    successes = [r for r in results if isinstance(r, quotas.QuotaReservation)]
    failures = [r for r in results if isinstance(r, QuotaExceededError)]

    assert len(successes) == 1, f"expected 1 success, got {len(successes)}"
    assert len(failures) == 4, f"expected 4 failures, got {len(failures)}"
    assert state["used"] == 100


@pytest.mark.asyncio
async def test_concurrent_multi_conn_namespace_and_agent_quotas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    When both namespace-level and agent-specific quota rows exist, concurrent
    workers should respect both limits.
    """
    monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", True)

    ns_id = uuid.uuid4()
    ns_qid = uuid.uuid4()
    agent_qid = uuid.uuid4()
    NS_LIMIT = 200
    AGENT_LIMIT = 80

    # Shared state for both quota rows
    ns_used = 0
    ag_used = 0

    pool = MagicMock()

    def _make_conn() -> AsyncMock:
        conn = AsyncMock()

        # fetch returns BOTH quota rows (namespace-level first, agent-specific second)
        async def _fetch(*args: object) -> list[dict]:
            return [
                {"id": agent_qid, "agent_id": "agent-x"},
                {"id": ns_qid, "agent_id": None},
            ]

        conn.fetch = _fetch

        async def _fetchrow(sql: str, delta: int, row_id: uuid.UUID) -> dict | None:
            nonlocal ns_used, ag_used
            if row_id == ns_qid:
                new = ns_used + delta
                if new <= NS_LIMIT:
                    ns_used = new
                    return {"id": row_id}
            elif row_id == agent_qid:
                new = ag_used + delta
                if new <= AGENT_LIMIT:
                    ag_used = new
                    return {"id": row_id}
            return None

        conn.fetchrow = _fetchrow

        tx = AsyncMock()
        conn.transaction = MagicMock(return_value=tx)
        tx.__aenter__.return_value = None
        tx.__aexit__.return_value = None
        return conn

    def _acquire(*_args: object, **_kwargs: object) -> MagicMock:
        acq = MagicMock()
        acq.__aenter__ = AsyncMock(return_value=_make_conn())
        acq.__aexit__ = AsyncMock(return_value=None)
        return acq

    pool.acquire = _acquire

    DELTA = 30
    tasks = [
        quotas.consume_resources(
            pool,
            namespace_id=ns_id,
            agent_id="agent-x",
            amounts={quotas.RESOURCE_LLM_TOKENS: DELTA},
        )
        for _ in range(8)
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    successes = [r for r in results if isinstance(r, quotas.QuotaReservation)]
    failures = [r for r in results if isinstance(r, QuotaExceededError)]

    # Agent limit (80) is tighter than namespace limit (200).
    # 80 / 30 = 2 full consumptions, 3rd is rejected at the agent level.
    assert len(successes) == 2, f"expected 2 successes (agent limit), got {len(successes)}"
    assert len(failures) == 6, f"expected 6 failures, got {len(failures)}"
    assert ag_used == 2 * DELTA


@pytest.mark.asyncio
async def test_concurrent_multi_conn_partial_near_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    When remaining quota is less than one delta but greater than zero,
    the consume should still be rejected (no partial consumption).
    """
    monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", True)

    ns_id = uuid.uuid4()
    qid = uuid.uuid4()
    LIMIT = 100
    INITIAL = 95  # 5 remaining, but delta is 10

    pool, state = _build_multi_conn_pool(qid, None, LIMIT, initial_used=INITIAL)

    tasks = [
        quotas.consume_resources(
            pool,
            namespace_id=ns_id,
            agent_id="w1",
            amounts={quotas.RESOURCE_LLM_TOKENS: 10},
        )
        for _ in range(3)
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    successes = [r for r in results if isinstance(r, quotas.QuotaReservation)]
    failures = [r for r in results if isinstance(r, QuotaExceededError)]

    assert len(successes) == 0, f"expected 0 successes, got {len(successes)}"
    assert len(failures) == 3, f"expected 3 failures, got {len(failures)}"
    assert state["used"] == INITIAL, "quota was partially consumed — should be rejected entirely"


# ---------------------------------------------------------------------------
# Original concurrent tests (kept for backward coverage of different paths)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_quota_consumption_no_overallocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Simulate N concurrent workers draining a shared quota pool.

    Without ``SELECT ... FOR UPDATE``, two workers could both read
    the same remaining quota and both succeed, causing overallocation.
    With row-level locking, only K workers should succeed where K
    is the number of times the quota fits into the limit.
    """
    monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", True)

    ns_id = uuid.uuid4()
    qid = uuid.uuid4()
    LIMIT = 100
    DELTA = 30  # each worker consumes 30 — 3 workers fit (90), 4th is rejected

    # Shared counter simulating the DB row's used_amount
    used = 0

    async def _fake_fetchrow(sql: str, delta: int, row_id: uuid.UUID) -> dict | None:
        nonlocal used
        new_used = used + delta
        if new_used <= LIMIT:
            used = new_used
            return {"id": row_id}
        return None

    # Build a conn mock that shares state across concurrent callers
    conn = AsyncMock()
    conn.fetch.return_value = [{"id": qid, "agent_id": None}]
    conn.fetchrow = _fake_fetchrow

    tx = AsyncMock()
    conn.transaction = MagicMock(return_value=tx)
    tx.__aenter__.return_value = None
    tx.__aexit__.return_value = None

    pool = MagicMock()
    acq = AsyncMock()
    acq.__aenter__.return_value = conn
    acq.__aexit__.return_value = None
    pool.acquire = MagicMock(return_value=acq)

    # Fire 6 concurrent consumers — only 3 should succeed (3 * 30 = 90 <= 100)
    # The other 3 should get QuotaExceededError
    tasks = [
        quotas.consume_resources(
            pool,
            namespace_id=ns_id,
            agent_id="a1",
            amounts={quotas.RESOURCE_LLM_TOKENS: DELTA},
        )
        for _ in range(6)
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    successes = [r for r in results if isinstance(r, quotas.QuotaReservation)]
    failures = [r for r in results if isinstance(r, QuotaExceededError)]

    # Exactly 3 should succeed, 3 should fail
    assert len(successes) == 3, f"expected 3 successes, got {len(successes)}"
    assert len(failures) == 3, f"expected 3 failures, got {len(failures)}"

    # Total consumed should be 90, not exceeding 100
    total_consumed = sum(s.steps[0][1] for s in successes if s.steps)
    assert total_consumed == 3 * DELTA
    assert total_consumed <= LIMIT, f"overallocated! consumed={total_consumed} limit={LIMIT}"


@pytest.mark.asyncio
async def test_concurrent_quota_no_deadlock_on_exceeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    When the very first worker exceeds the quota, the FOR UPDATE lock
    should not cause a deadlock — all workers should get QuotaExceededError.
    """
    monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", True)

    ns_id = uuid.uuid4()
    qid = uuid.uuid4()

    async def _always_exceeded(*_a: object, **_k: object) -> None:
        return None

    conn = AsyncMock()
    conn.fetch.return_value = [{"id": qid, "agent_id": None}]
    conn.fetchrow = _always_exceeded

    tx = AsyncMock()
    conn.transaction = MagicMock(return_value=tx)
    tx.__aenter__.return_value = None
    tx.__aexit__.return_value = None

    pool = MagicMock()
    acq = AsyncMock()
    acq.__aenter__.return_value = conn
    acq.__aexit__.return_value = None
    pool.acquire = MagicMock(return_value=acq)

    tasks = [
        quotas.consume_resources(
            pool,
            namespace_id=ns_id,
            agent_id="a1",
            amounts={quotas.RESOURCE_LLM_TOKENS: 999},
        )
        for _ in range(4)
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    failures = [r for r in results if isinstance(r, QuotaExceededError)]
    assert len(failures) == 4, f"expected all 4 to fail, got {len(failures)}"


@pytest.mark.asyncio
async def test_concurrent_quota_single_consumer_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single consumer within quota should always succeed."""
    monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", True)

    ns_id = uuid.uuid4()
    qid = uuid.uuid4()

    conn = AsyncMock()
    conn.fetch.return_value = [{"id": qid, "agent_id": None}]
    conn.fetchrow.return_value = {"id": qid}

    tx = AsyncMock()
    conn.transaction = MagicMock(return_value=tx)
    tx.__aenter__.return_value = None
    tx.__aexit__.return_value = None

    pool = MagicMock()
    acq = AsyncMock()
    acq.__aenter__.return_value = conn
    acq.__aexit__.return_value = None
    pool.acquire = MagicMock(return_value=acq)

    res = await quotas.consume_resources(
        pool,
        namespace_id=ns_id,
        agent_id="a1",
        amounts={quotas.RESOURCE_LLM_TOKENS: 10},
    )
    assert len(res.steps) == 1, "expected 1 quota step"
    assert res.steps[0][1] == 10


@pytest.mark.asyncio
async def test_concurrent_quota_sql_contains_for_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Verify the SQL sent to Postgres contains ``FOR UPDATE``.
    This is a structural assertion that the fix is applied.
    """
    monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", True)

    ns_id = uuid.uuid4()
    qid = uuid.uuid4()

    captured_sql: list[str] = []

    async def _capture_fetch(sql: str, *args: object) -> list[dict]:
        captured_sql.append(sql)
        return [{"id": qid, "agent_id": None}]

    conn = AsyncMock()
    conn.fetch = _capture_fetch
    conn.fetchrow.return_value = {"id": qid}

    tx = AsyncMock()
    conn.transaction = MagicMock(return_value=tx)
    tx.__aenter__.return_value = None
    tx.__aexit__.return_value = None

    pool = MagicMock()
    acq = AsyncMock()
    acq.__aenter__.return_value = conn
    acq.__aexit__.return_value = None
    pool.acquire = MagicMock(return_value=acq)

    await quotas.consume_resources(
        pool,
        namespace_id=ns_id,
        agent_id="a1",
        amounts={quotas.RESOURCE_LLM_TOKENS: 10},
    )

    assert len(captured_sql) >= 1, "No SQL captured"
    # The fetch SQL should contain FOR UPDATE
    assert any("FOR UPDATE" in sql for sql in captured_sql), (
        f"FOR UPDATE not found in captured SQL: {captured_sql!r}"
    )


@pytest.mark.asyncio
async def test_concurrent_quota_multi_resource_no_deadlock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    When consuming multiple resource types in the same call, no deadlock
    should occur (the loop iterates deterministically over sorted keys).
    """
    monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", True)

    ns_id = uuid.uuid4()
    qid_tokens = uuid.uuid4()
    qid_storage = uuid.uuid4()

    used_tokens = 0
    used_storage = 0

    async def _multi_fetchrow(sql: str, delta: int, row_id: uuid.UUID) -> dict | None:
        nonlocal used_tokens, used_storage
        if row_id == qid_tokens:
            new = used_tokens + delta
            if new <= 200:
                used_tokens = new
                return {"id": row_id}
        elif row_id == qid_storage:
            new = used_storage + delta
            if new <= 500:
                used_storage = new
                return {"id": row_id}
        return None

    conn = AsyncMock()

    async def _multi_fetch(sql: str, *args: object) -> list[dict]:
        # args: (namespace_id, resource_type, agent_id)
        resource_type = str(args[1]) if len(args) > 1 else ""
        if resource_type == quotas.RESOURCE_LLM_TOKENS:
            return [{"id": qid_tokens, "agent_id": None}]
        if resource_type == quotas.RESOURCE_STORAGE_BYTES:
            return [{"id": qid_storage, "agent_id": None}]
        return []

    conn.fetch = _multi_fetch
    conn.fetchrow = _multi_fetchrow

    tx = AsyncMock()
    conn.transaction = MagicMock(return_value=tx)
    tx.__aenter__.return_value = None
    tx.__aexit__.return_value = None

    pool = MagicMock()
    acq = AsyncMock()
    acq.__aenter__.return_value = conn
    acq.__aexit__.return_value = None
    pool.acquire = MagicMock(return_value=acq)

    res = await quotas.consume_resources(
        pool,
        namespace_id=ns_id,
        agent_id="a1",
        amounts={
            quotas.RESOURCE_LLM_TOKENS: 50,
            quotas.RESOURCE_STORAGE_BYTES: 100,
        },
    )
    assert len(res.steps) == 2
    consumed = dict(res.steps)
    assert consumed[qid_tokens] == 50
    assert consumed[qid_storage] == 100


# ---------------------------------------------------------------------------
# HTTP 429 (admin API) + MCP tool path (-32013) — no real Postgres / Redis
# ---------------------------------------------------------------------------

_HMAC_KEY = "pytest-quota-hmac-secret-not-for-prod"


def _signed_post(path: str, body: dict) -> tuple[bytes, dict[str, str]]:
    raw = json.dumps(body).encode("utf-8")
    ts = int(time.time())
    parts = ["POST", path, str(ts), hashlib.sha256(raw).hexdigest()]
    canonical = "\n".join(parts)
    sig = _hmac.new(_HMAC_KEY.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    headers = {
        "X-NCE-Timestamp": str(ts),
        "Authorization": f"HMAC-SHA256 {sig}",
        "Content-Type": "application/json",
    }
    return raw, headers


def test_tool_quota_plan_a2a_query_shared() -> None:
    consumer_ns = str(uuid.uuid4())
    plan = quotas.tool_quota_plan(
        "a2a_query_shared",
        {
            "consumer_namespace_id": consumer_ns,
            "consumer_agent_id": "agent-b",
            "query": "what did we decide?",
        },
    )
    assert plan is not None
    ns, agent, amounts = plan
    assert str(ns) == consumer_ns
    assert agent == "agent-b"
    assert quotas.RESOURCE_LLM_TOKENS in amounts


def test_admin_api_search_returns_429_when_quota_exceeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import admin_server as adm

    async def _boom(*_a, **_k):
        raise QuotaExceededError("namespace cap reached")

    monkeypatch.setattr("nce.quotas.consume_for_tool", _boom)
    mock_engine = MagicMock()
    mock_engine.pg_pool = MagicMock()
    mock_engine.redis_client = MagicMock()
    mock_engine.semantic_search = AsyncMock(return_value=[])
    monkeypatch.setattr("nce.admin_state.engine", mock_engine)
    monkeypatch.setattr(adm, "engine", mock_engine)

    app = Starlette(
        middleware=[
            Middleware(
                HMACAuthMiddleware,
                protected_prefix="/api/",
                api_key=_HMAC_KEY,
            )
        ],
        routes=[Route("/api/search", endpoint=adm.api_search, methods=["POST"])],
    )

    body = {
        "namespace_id": str(uuid.uuid4()),
        "agent_id": "alpha",
        "query": "hello",
    }
    raw, hdrs = _signed_post("/api/search", body)
    client = TestClient(app, raise_server_exceptions=True)
    r = client.post("/api/search", content=raw, headers=hdrs)
    assert r.status_code == 429
    payload = r.json()
    assert "error" in payload
    assert "cap" in payload["error"].lower() or "quota" in payload["error"].lower()


@pytest.mark.asyncio
async def test_mcp_call_tool_surfaces_quota_as_32013(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MCP stdio path: quota exhaustion maps to ValueError with -32013 (rate limit / quota)."""

    monkeypatch.delitem(sys.modules, "server", raising=False)
    if importlib.util.find_spec("mcp") is None:
        _register_mcp_stubs(monkeypatch)
    import server as srv

    eng = MagicMock()
    eng.pg_pool = MagicMock()
    eng.redis_client = MagicMock()
    eng.redis_client.get = AsyncMock(return_value=None)
    eng.redis_client.incr = AsyncMock(return_value=1)
    eng.redis_client.setex = AsyncMock()
    eng.semantic_search = AsyncMock(return_value=[])
    monkeypatch.setattr(srv, "engine", eng)

    async def _boom(*_a, **_k):
        raise QuotaExceededError("no capacity")

    monkeypatch.setattr("nce.quotas.consume_for_tool", _boom)

    result = await srv.call_tool(
        "semantic_search",
        {
            "namespace_id": str(uuid.uuid4()),
            "agent_id": "agent-a",
            "query": "find the spec",
        },
    )
    # call_tool now returns JSON-RPC 2.0 error responses as TextContent
    # instead of raising ValueError — verify the error code in the payload.
    assert len(result) == 1
    payload = json.loads(result[0].text)
    assert payload["jsonrpc"] == "2.0"
    assert payload["error"]["code"] == -32013
    assert "no capacity" in payload["error"]["data"]["detail"]


# ---------------------------------------------------------------------------
# Phase 3.2 hardening — delta bounds, Redis Lua, flush GREATEST, rollback
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("result", "expected_failed"),
    [
        (-1, True),
        (b"-1", True),
        ("-1", True),
        (100, False),
        (b"100", False),
        (None, True),
    ],
)
def test_quota_incr_failed_contract(result: object, expected_failed: bool) -> None:
    assert quotas._quota_incr_failed(result) is expected_failed


@pytest.mark.asyncio
async def test_consume_rejects_delta_above_max_pg_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", True)
    monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTA_REDIS_COUNTERS", False)

    pool = MagicMock()
    ns = uuid.uuid4()
    over = quotas._MAX_QUOTA_DELTA + 1

    with pytest.raises(ValueError, match="exceeds maximum"):
        await quotas.consume_resources(
            pool,
            namespace_id=ns,
            agent_id="a1",
            amounts={quotas.RESOURCE_LLM_TOKENS: over},
        )
    pool.acquire.assert_not_called()


@pytest.mark.asyncio
async def test_consume_resources_redis_rejects_delta_above_max() -> None:
    pool = MagicMock()
    redis = MagicMock()
    ns = uuid.uuid4()
    over = quotas._MAX_QUOTA_DELTA + 1

    with pytest.raises(ValueError, match="exceeds maximum"):
        await quotas._consume_resources_redis(
            pool,
            redis,
            namespace_id=ns,
            agent_id=None,
            amounts={quotas.RESOURCE_LLM_TOKENS: over},
        )
    pool.acquire.assert_not_called()
    redis.eval.assert_not_called()


@pytest.mark.asyncio
async def test_consume_skips_zero_and_negative_delta_without_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", True)
    monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTA_REDIS_COUNTERS", False)

    pool = MagicMock()
    ns = uuid.uuid4()

    for amounts in (
        {quotas.RESOURCE_LLM_TOKENS: 0},
        {quotas.RESOURCE_LLM_TOKENS: -50},
        {quotas.RESOURCE_LLM_TOKENS: 0, quotas.RESOURCE_STORAGE_BYTES: -1},
    ):
        res = await quotas.consume_resources(
            pool,
            namespace_id=ns,
            agent_id="a1",
            amounts=amounts,
        )
        assert res.steps == []
        pool.acquire.assert_not_called()


def _redis_consume_pool_mock(
    qid: uuid.UUID,
    *,
    pg_used: int = 0,
    limit_amount: int = 1_000_000,
) -> tuple[MagicMock, AsyncMock]:
    conn = AsyncMock()
    conn.fetch.return_value = [
        {
            "id": qid,
            "agent_id": None,
            "used_amount": pg_used,
            "limit_amount": limit_amount,
        }
    ]
    tx = AsyncMock()
    conn.transaction = MagicMock(return_value=tx)
    tx.__aenter__.return_value = None
    tx.__aexit__.return_value = None

    pool = MagicMock()
    acq = AsyncMock()
    acq.__aenter__.return_value = conn
    acq.__aexit__.return_value = None
    pool.acquire = MagicMock(return_value=acq)
    return pool, conn


@pytest.mark.asyncio
async def test_consume_resources_redis_eval_passes_ttl_as_fifth_argv() -> None:
    ns = uuid.uuid4()
    qid = uuid.uuid4()
    pool, _conn = _redis_consume_pool_mock(qid, pg_used=10, limit_amount=500)

    redis = MagicMock()
    redis.eval = AsyncMock(return_value=42)

    await quotas._consume_resources_redis(
        pool,
        redis,
        namespace_id=ns,
        agent_id=None,
        amounts={quotas.RESOURCE_LLM_TOKENS: 5},
    )

    redis.eval.assert_awaited_once()
    _lua, numkeys, key, base, delta, limit_s, ttl_s = redis.eval.await_args.args
    assert numkeys == 1
    assert key == quotas.quota_redis_key(ns, qid)
    assert ttl_s == str(quotas._QUOTA_REDIS_KEY_TTL_S)


@pytest.mark.asyncio
async def test_consume_resources_redis_eval_timeout_propagates() -> None:
    ns = uuid.uuid4()
    qid = uuid.uuid4()
    pool, _conn = _redis_consume_pool_mock(qid)

    async def _slow_eval(*_a: object, **_k: object) -> int:
        await asyncio.sleep(quotas._REDIS_CALL_TIMEOUT_S + 0.5)
        return 100

    redis = MagicMock()
    redis.eval = _slow_eval

    with pytest.raises(asyncio.TimeoutError):
        await quotas._consume_resources_redis(
            pool,
            redis,
            namespace_id=ns,
            agent_id=None,
            amounts={quotas.RESOURCE_LLM_TOKENS: 5},
        )


@pytest.mark.asyncio
async def test_consume_resources_redis_quota_exceeded_message_format() -> None:
    ns = uuid.uuid4()
    qid = uuid.uuid4()
    pool, _conn = _redis_consume_pool_mock(qid, pg_used=90, limit_amount=100)

    redis = MagicMock()
    redis.eval = AsyncMock(return_value=-1)
    redis.decrby = AsyncMock()

    with pytest.raises(QuotaExceededError) as exc_info:
        await quotas._consume_resources_redis(
            pool,
            redis,
            namespace_id=ns,
            agent_id=None,
            amounts={quotas.RESOURCE_LLM_TOKENS: 20},
        )

    msg = str(exc_info.value)
    assert f"namespace={ns}" in msg
    assert f"resource={quotas.RESOURCE_LLM_TOKENS!r}" in msg


@pytest.mark.asyncio
async def test_consume_resources_redis_partial_rollback_on_eval_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ns = uuid.uuid4()
    qid1 = uuid.uuid4()
    qid2 = uuid.uuid4()

    conn = AsyncMock()

    async def _fetch_by_resource(
        _sql: str, _ns: uuid.UUID, resource_type: str, _agent: str | None
    ) -> list[dict]:
        if resource_type == quotas.RESOURCE_LLM_TOKENS:
            return [
                {
                    "id": qid1,
                    "agent_id": None,
                    "used_amount": 0,
                    "limit_amount": 1_000,
                }
            ]
        if resource_type == quotas.RESOURCE_STORAGE_BYTES:
            return [
                {
                    "id": qid2,
                    "agent_id": None,
                    "used_amount": 0,
                    "limit_amount": 50,
                }
            ]
        return []

    conn.fetch = _fetch_by_resource
    tx = AsyncMock()
    conn.transaction = MagicMock(return_value=tx)
    tx.__aenter__.return_value = None
    tx.__aexit__.return_value = None

    pool = MagicMock()
    acq = AsyncMock()
    acq.__aenter__.return_value = conn
    acq.__aexit__.return_value = None
    pool.acquire = MagicMock(return_value=acq)

    redis = MagicMock()
    redis.eval = AsyncMock(side_effect=[10, -1])
    redis.decrby = AsyncMock()

    with caplog.at_level("ERROR"):
        with pytest.raises(QuotaExceededError):
            await quotas._consume_resources_redis(
                pool,
                redis,
                namespace_id=ns,
                agent_id=None,
                amounts={
                    quotas.RESOURCE_LLM_TOKENS: 10,
                    quotas.RESOURCE_STORAGE_BYTES: 100,
                },
            )

    redis.decrby.assert_awaited_once_with(quotas.quota_redis_key(ns, qid1), 10)


@pytest.mark.asyncio
async def test_reservation_redis_rollback_attempts_all_steps_on_decrby_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ns = uuid.uuid4()
    q1, q2, q3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    redis = AsyncMock()
    redis.decrby = AsyncMock(
        side_effect=[None, RuntimeError("redis down"), None],
    )

    r = quotas.QuotaReservation(
        pool=None,
        redis_client=redis,
        namespace_id=ns,
        steps=[(q1, 10), (q2, 20), (q3, 30)],
    )

    with caplog.at_level("ERROR"):
        await r.rollback()

    assert r.is_empty
    assert redis.decrby.await_count == 3
    assert any("Quota rollback step failed" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_flush_quota_counters_sql_uses_greatest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ns_id = uuid.uuid4()
    qid = uuid.uuid4()
    key = quotas.quota_redis_key(ns_id, qid)

    async def _scan_iter(*_a: object, **_k: object):
        yield key.encode()

    redis = MagicMock()
    redis.scan_iter = _scan_iter
    redis.get = AsyncMock(return_value=b"200")

    captured_sql: list[str] = []
    conn = AsyncMock()

    async def _execute(sql: str, used: int, row_id: uuid.UUID) -> None:
        captured_sql.append(sql)
        assert used == 200
        assert row_id == qid

    conn.execute = _execute

    @asynccontextmanager
    async def _fake_scoped(pool: object, namespace_id: object):
        assert pool is not None
        assert namespace_id == ns_id
        yield conn

    monkeypatch.setattr("nce.db_utils.scoped_pg_session", _fake_scoped)

    pool = MagicMock()
    flushed = await quotas.flush_quota_counters_to_postgres(redis, pool)

    assert flushed == 1
    assert len(captured_sql) == 1
    assert "GREATEST(used_amount" in captured_sql[0]


@pytest.mark.asyncio
async def test_flush_greatest_prevents_regressing_higher_pg_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flush passes Redis value; SQL GREATEST keeps PG row if it already exceeds mirror."""
    ns_id = uuid.uuid4()
    qid = uuid.uuid4()
    key = quotas.quota_redis_key(ns_id, qid)
    stale_redis_used = 200

    async def _scan_iter(*_a: object, **_k: object):
        yield key

    redis = MagicMock()
    redis.scan_iter = _scan_iter
    redis.get = AsyncMock(return_value=str(stale_redis_used).encode())

    bound_used: list[int] = []
    conn = AsyncMock()

    async def _execute(sql: str, used: int, row_id: uuid.UUID) -> None:
        bound_used.append(used)
        assert "GREATEST(used_amount" in sql
        assert row_id == qid

    conn.execute = _execute

    @asynccontextmanager
    async def _fake_scoped(pool: object, namespace_id: object):
        yield conn

    monkeypatch.setattr("nce.db_utils.scoped_pg_session", _fake_scoped)

    await quotas.flush_quota_counters_to_postgres(redis, MagicMock())

    assert bound_used == [stale_redis_used]


# ---------------------------------------------------------------------------
# Quota and Embedding Degradation Observability (Batch 19)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quota_metrics_updated_on_consume(monkeypatch: pytest.MonkeyPatch) -> None:
    from nce.observability import QUOTA_CONSUMED, QUOTA_REMAINING

    monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", True)

    ns_id = uuid.uuid4()
    qrow_ns = uuid.uuid4()

    conn = AsyncMock()
    conn.fetch.return_value = [{"id": qrow_ns, "agent_id": None}]
    conn.fetchrow.return_value = {"id": qrow_ns, "used_amount": 15, "limit_amount": 100}

    tx = AsyncMock()
    conn.transaction = MagicMock(return_value=tx)
    tx.__aenter__.return_value = None
    tx.__aexit__.return_value = None

    pool = MagicMock()
    acq = AsyncMock()
    acq.__aenter__.return_value = conn
    acq.__aexit__.return_value = None
    pool.acquire = MagicMock(return_value=acq)

    mock_consumed_set = MagicMock()
    mock_remaining_set = MagicMock()

    monkeypatch.setattr(
        QUOTA_CONSUMED, "labels", MagicMock(return_value=MagicMock(set=mock_consumed_set))
    )
    monkeypatch.setattr(
        QUOTA_REMAINING, "labels", MagicMock(return_value=MagicMock(set=mock_remaining_set))
    )

    await quotas.consume_resources(
        pool,
        namespace_id=ns_id,
        agent_id="agent-x",
        amounts={quotas.RESOURCE_LLM_TOKENS: 10},
    )

    QUOTA_CONSUMED.labels.assert_called_once_with(
        namespace_id=str(ns_id),
        resource_type=quotas.RESOURCE_LLM_TOKENS,
        agent_id="global",
    )
    QUOTA_REMAINING.labels.assert_called_once_with(
        namespace_id=str(ns_id),
        resource_type=quotas.RESOURCE_LLM_TOKENS,
        agent_id="global",
    )
    mock_consumed_set.assert_called_once_with(15)
    mock_remaining_set.assert_called_once_with(85)


@pytest.mark.asyncio
async def test_quota_metrics_updated_on_consume_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    from nce.observability import QUOTA_CONSUMED, QUOTA_REMAINING

    monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", True)

    ns_id = uuid.uuid4()
    qrow_ns = uuid.uuid4()

    conn = AsyncMock()
    conn.fetch.return_value = [
        {"id": qrow_ns, "agent_id": None, "used_amount": 5, "limit_amount": 100}
    ]

    redis_client = AsyncMock()
    redis_client.eval.return_value = 15

    pool = MagicMock()
    acq = AsyncMock()
    acq.__aenter__.return_value = conn
    acq.__aexit__.return_value = None
    pool.acquire = MagicMock(return_value=acq)

    mock_consumed_set = MagicMock()
    mock_remaining_set = MagicMock()

    monkeypatch.setattr(
        QUOTA_CONSUMED, "labels", MagicMock(return_value=MagicMock(set=mock_consumed_set))
    )
    monkeypatch.setattr(
        QUOTA_REMAINING, "labels", MagicMock(return_value=MagicMock(set=mock_remaining_set))
    )

    monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTA_REDIS_COUNTERS", True)

    await quotas.consume_resources(
        pool,
        namespace_id=ns_id,
        agent_id="agent-x",
        amounts={quotas.RESOURCE_LLM_TOKENS: 10},
        redis_client=redis_client,
    )

    QUOTA_CONSUMED.labels.assert_called_once_with(
        namespace_id=str(ns_id),
        resource_type=quotas.RESOURCE_LLM_TOKENS,
        agent_id="global",
    )
    QUOTA_REMAINING.labels.assert_called_once_with(
        namespace_id=str(ns_id),
        resource_type=quotas.RESOURCE_LLM_TOKENS,
        agent_id="global",
    )
    mock_consumed_set.assert_called_once_with(15)
    mock_remaining_set.assert_called_once_with(85)


@pytest.mark.asyncio
async def test_embedding_fallback_increments_counter_and_alerts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nce.embeddings import CPUBackend
    from nce.observability import EMBEDDING_FALLBACKS

    mock_dispatch_alert = AsyncMock()
    monkeypatch.setattr("nce.notifications.dispatcher.dispatch_alert", mock_dispatch_alert)

    mock_inc = MagicMock()
    monkeypatch.setattr(EMBEDDING_FALLBACKS, "inc", mock_inc)

    backend = CPUBackend()
    monkeypatch.setattr(backend, "_sync_embed_batch", MagicMock(return_value=([[0.0] * 768], True)))

    res = await backend.embed(["test text"])

    assert len(res) == 1
    mock_inc.assert_called_once()
    mock_dispatch_alert.assert_called_once()
    title, msg = mock_dispatch_alert.call_args[0]
    assert "Embedding Fallback Active" in title
    assert "hash-stub fallback" in msg
