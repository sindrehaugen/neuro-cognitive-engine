"""Regression guard: the namespace fixtures must clean up after themselves.

``tests/conftest.py`` once defined ``namespace_id`` and ``make_namespace`` with
``return``, not ``yield``. A fixture that returns has no teardown phase, so every
test that asked for a namespace leaked one permanently. The live database had
accumulated 4,613 ``pytest-ns-*`` rows against 7 real tenants -- and boot-time
ownership seeding fanned out over all of them, which is how a 777-second server
startup happened.

Two layers here:

  a. A structural ratchet -- both fixtures must be async *generator* functions.
     Converting either back to ``return`` fails this immediately, in the unit
     job, with no database required.
  b. Behavioural tests for the teardown helper against a real Postgres,
     including the case it must survive: a namespace pinned by WORM rows.
"""

from __future__ import annotations

import inspect
import os
import uuid
from collections.abc import AsyncGenerator

import asyncpg  # type: ignore[import-untyped]
import pytest  # type: ignore[import-untyped]
import pytest_asyncio  # type: ignore[import-untyped]

import tests.conftest as conftest_mod


def _unwrap_fixture(fixture):  # noqa: ANN001, ANN202
    """Return the function a pytest fixture wraps."""
    func = getattr(fixture, "__wrapped__", None)
    if func is not None:
        return func
    marker = getattr(fixture, "_pytestfixturefunction", None)
    if marker is not None and getattr(marker, "name", None):
        pass
    return fixture


@pytest.mark.parametrize("name", ["namespace_id", "make_namespace"])
def test_namespace_fixtures_are_generators_so_they_have_teardown(name: str) -> None:
    func = _unwrap_fixture(getattr(conftest_mod, name))
    assert inspect.isasyncgenfunction(func), (
        f"tests/conftest.py::{name} is not an async generator, so pytest never runs "
        "a teardown phase for it and every test using it leaks a namespace row. "
        "Use `yield` plus a finally-block, not `return`."
    )


@pytest_asyncio.fixture
async def teardown_pool() -> AsyncGenerator[asyncpg.Pool, None]:
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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_drop_namespaces_removes_the_row(teardown_pool: asyncpg.Pool) -> None:
    ns = await conftest_mod._insert_namespace(teardown_pool)
    async with teardown_pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM namespaces WHERE id = $1", ns) == 1

    await conftest_mod._drop_namespaces(teardown_pool, [ns])

    async with teardown_pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM namespaces WHERE id = $1", ns) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_drop_namespaces_survives_a_worm_pinned_namespace(
    teardown_pool: asyncpg.Pool,
) -> None:
    """A namespace with event_log rows cannot be deleted -- teardown must not raise."""
    ns = await conftest_mod._insert_namespace(teardown_pool)
    try:
        async with teardown_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO event_log "
                "(namespace_id, agent_id, event_type, event_seq, params, signature, signature_key_id) "
                "VALUES ($1, 'a', 'teardown-probe', 1, '{}'::jsonb, $2, 'k1')",
                ns,
                b"\x00",
            )

        # Must not raise, and must leave the namespace in place.
        await conftest_mod._drop_namespaces(teardown_pool, [ns])

        async with teardown_pool.acquire() as conn:
            assert await conn.fetchval("SELECT count(*) FROM namespaces WHERE id = $1", ns) == 1
    finally:
        # event_log is WORM: this namespace is permanent. Rename it out of the
        # pytest-% sweep space so it is not mistaken for reclaimable debris.
        async with teardown_pool.acquire() as conn:
            await conn.execute(
                "UPDATE namespaces SET slug = $2 WHERE id = $1",
                ns,
                f"wormpinned-teardown-probe-{uuid.uuid4().hex}",
            )
