"""Integration tests for the set-based startup backfill (``seed_node_ownership_all_namespaces``).

The boot path replaced a per-namespace loop with one statement covering every
namespace.  These tests prove the replacement is *equivalent* to the
per-namespace seeder it stands in for, and idempotent, against a real Postgres:

  a. A namespace with no ownership rows gets exactly the full ownership map.
  b. A second call inserts nothing (the NOT EXISTS guard holds).
  c. The rows written are byte-identical to what ``seed_node_ownership_registry``
     writes for the same namespace -- the bulk statement is not a re-spelling
     that quietly drops ``transition`` or reorders columns.

Every test runs inside a transaction that is rolled back, so no namespace or
ownership rows survive the run.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator

import asyncpg  # type: ignore[import-untyped]
import pytest  # type: ignore[import-untyped]
import pytest_asyncio  # type: ignore[import-untyped]

from nce.entity_resolution.ownership_seed import (
    _OWNERSHIP_ENTRIES,
    seed_node_ownership_all_namespaces,
    seed_node_ownership_registry,
)

_ROWS_SQL = """
SELECT node_type, transition, owner_engine
FROM node_ownership_registry
WHERE namespace_id = $1
ORDER BY node_type, transition NULLS FIRST
"""


@pytest_asyncio.fixture
async def seed_pool() -> AsyncGenerator[asyncpg.Pool, None]:
    """Owner-role pool for this module only.

    Deliberately not the shared ``pg_pool`` fixture: that one also proves an
    active signing key decrypts, which these tests neither need nor touch, and
    which skips against a database holding another deployment's keys.
    """

    dsn = (
        os.getenv("NCE_INTEGRATION_PG_DSN")
        or os.getenv("PG_DSN")
        or os.getenv("DATABASE_URL")
        or ""
    ).strip()
    if not dsn:
        pytest.skip("Integration tests need NCE_INTEGRATION_PG_DSN, PG_DSN, or DATABASE_URL")
    try:
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2, command_timeout=60)
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"Postgres not reachable for integration tests: {exc}")
    try:
        yield pool
    finally:
        await pool.close()


async def _insert_namespace(conn: asyncpg.Connection) -> uuid.UUID:
    ns = await conn.fetchval(
        "INSERT INTO namespaces (slug) VALUES ($1) RETURNING id",
        f"pytest-bulkseed-{uuid.uuid4().hex}",
    )
    assert ns is not None
    return ns


@pytest.mark.integration
@pytest.mark.asyncio
async def test_bulk_seed_covers_a_fresh_namespace_and_is_idempotent(
    seed_pool: asyncpg.Pool,
) -> None:
    async with seed_pool.acquire() as conn:
        tr = conn.transaction()
        await tr.start()
        try:
            ns = await _insert_namespace(conn)

            first = await seed_node_ownership_all_namespaces(conn)
            rows = await conn.fetch(_ROWS_SQL, ns)
            assert len(rows) == len(_OWNERSHIP_ENTRIES)
            assert first >= len(_OWNERSHIP_ENTRIES)

            second = await seed_node_ownership_all_namespaces(conn)
            assert second == 0, "bulk seed is not idempotent -- it re-inserted rows"
            assert len(await conn.fetch(_ROWS_SQL, ns)) == len(_OWNERSHIP_ENTRIES)
        finally:
            await tr.rollback()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_bulk_seed_writes_the_same_rows_as_the_per_namespace_seeder(
    seed_pool: asyncpg.Pool,
) -> None:
    async with seed_pool.acquire() as conn:
        tr = conn.transaction()
        await tr.start()
        try:
            looped_ns = await _insert_namespace(conn)
            bulk_ns = await _insert_namespace(conn)

            await seed_node_ownership_registry(conn, looped_ns)
            await seed_node_ownership_all_namespaces(conn)

            looped = [tuple(r) for r in await conn.fetch(_ROWS_SQL, looped_ns)]
            bulk = [tuple(r) for r in await conn.fetch(_ROWS_SQL, bulk_ns)]

            assert bulk == looped
            assert len(bulk) == len(_OWNERSHIP_ENTRIES)
        finally:
            await tr.rollback()
