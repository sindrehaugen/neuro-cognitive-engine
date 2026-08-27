"""Regression guard: boot-time ownership seeding must not scale with tenant count.

``NCEEngine.connect`` calls :meth:`NCEEngine._seed_node_ownership_all` on every
startup.  It once looped over every namespace and called
``seed_node_ownership_registry`` for each, which issues one round trip per
ownership entry -- ``N_namespaces * N_entries`` sequential statements per boot.
On a database with 4,818 namespaces that was 178,266 statements and ~777 s of
startup, all of them inserting zero rows.

These tests pin the shape of the fix rather than its wording: the number of
database round trips the startup path makes is asserted to be *identical* for
one namespace and for many.  Restoring any per-namespace loop makes them fail.
"""

from __future__ import annotations

import pytest

from nce.entity_resolution.ownership_seed import _OWNERSHIP_ENTRIES
from nce.orchestrator import NCEEngine


class _RecordingConnection:
    """asyncpg-shaped connection that counts round trips instead of making them."""

    def __init__(self, namespace_ids: list[str], calls: list[tuple[str, str]]) -> None:
        self._namespace_ids = namespace_ids
        self.calls = calls

    async def execute(self, sql: str, *args):  # noqa: ANN002, ANN201
        self.calls.append(("execute", sql))
        self.last_execute_args = args
        return "INSERT 0 0"

    async def fetch(self, sql: str, *args):  # noqa: ANN002, ANN201
        self.calls.append(("fetch", sql))
        # Any query against namespaces yields the full tenant list, so a
        # per-namespace loop would fan out over it exactly as it used to.
        return [{"id": ns} for ns in self._namespace_ids]

    async def fetchval(self, sql: str, *args):  # noqa: ANN002, ANN201
        self.calls.append(("fetchval", sql))
        return None

    async def fetchrow(self, sql: str, *args):  # noqa: ANN002, ANN201
        self.calls.append(("fetchrow", sql))
        return None

    def transaction(self):  # noqa: ANN201
        return _NullAsyncContext()


class _NullAsyncContext:
    async def __aenter__(self):  # noqa: ANN204
        return None

    async def __aexit__(self, *exc):  # noqa: ANN002, ANN204
        return False


class _AcquireContext:
    def __init__(self, pool: _RecordingPool) -> None:
        self._pool = pool

    async def __aenter__(self) -> _RecordingConnection:
        self._pool.acquires += 1
        return self._pool.conn

    async def __aexit__(self, *exc):  # noqa: ANN002, ANN204
        return False


class _RecordingPool:
    """asyncpg-pool-shaped stand-in that records acquires and statements."""

    def __init__(self, namespace_count: int) -> None:
        self.calls: list[tuple[str, str]] = []
        self.acquires = 0
        self.conn = _RecordingConnection(
            [f"00000000-0000-4000-8000-{i:012d}" for i in range(namespace_count)],
            self.calls,
        )

    def acquire(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        return _AcquireContext(self)


async def _run_startup_seed(namespace_count: int) -> _RecordingPool:
    engine = NCEEngine()
    pool = _RecordingPool(namespace_count)
    engine.pg_pool = pool
    await engine._seed_node_ownership_all()
    return pool


@pytest.mark.asyncio
async def test_startup_seed_cost_is_independent_of_tenant_count() -> None:
    """One tenant and five hundred tenants must cost the same round trips."""
    small = await _run_startup_seed(1)
    large = await _run_startup_seed(500)

    assert large.acquires == small.acquires, (
        f"startup seeding acquired {large.acquires} connections for 500 namespaces "
        f"but {small.acquires} for 1 -- the cost scales with tenant count again"
    )
    assert len(large.calls) == len(small.calls), (
        f"startup seeding issued {len(large.calls)} statements for 500 namespaces "
        f"but {len(small.calls)} for 1 -- the cost scales with tenant count again"
    )


@pytest.mark.asyncio
async def test_startup_seed_issues_exactly_one_statement() -> None:
    """The whole backfill is a single set-based INSERT on a single connection."""
    pool = await _run_startup_seed(500)

    assert pool.acquires == 1, f"expected 1 pool acquire, got {pool.acquires}"
    assert len(pool.calls) == 1, f"expected 1 statement, got {pool.calls}"
    kind, sql = pool.calls[0]
    assert kind == "execute"
    assert "INSERT INTO node_ownership_registry" in sql
    assert "NOT EXISTS" in sql, "the bulk INSERT lost its idempotency guard"


@pytest.mark.asyncio
async def test_startup_seed_passes_every_ownership_entry() -> None:
    """The single statement carries the full ownership map, not a subset."""
    pool = await _run_startup_seed(3)
    node_types, transitions, owner_engines = pool.conn.last_execute_args

    assert node_types == [e["node_type"] for e in _OWNERSHIP_ENTRIES]
    assert transitions == [e.get("transition") for e in _OWNERSHIP_ENTRIES]
    assert owner_engines == [e["owner_engine"] for e in _OWNERSHIP_ENTRIES]
    assert len(node_types) == len(_OWNERSHIP_ENTRIES) > 0
