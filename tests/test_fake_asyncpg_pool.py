"""Sanity checks for tests/fixtures/fake_asyncpg.py pool shim."""

from __future__ import annotations

import gc

import pytest

from tests.fixtures.fake_asyncpg import RecordingFakeConnection, make_fake_pool


@pytest.fixture(autouse=True)
def _reap_asyncpg_pool_garbage() -> None:
    """Reduce cross-test flakiness from refcount chains + asyncio debug."""
    yield
    gc.collect()


@pytest.mark.asyncio
async def test_fake_pool_acquire_returns_same_connection() -> None:
    pool, conn = make_fake_pool()
    try:
        async with pool.acquire() as got:
            assert got is conn
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_fake_pool_manual_await_acquire_requires_release() -> None:
    pool, expected = make_fake_pool()
    try:
        got = await pool.acquire()
        try:
            assert got is expected
        finally:
            await pool.release(got)
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_fake_connection_transaction_depth() -> None:
    conn = RecordingFakeConnection()
    try:
        assert conn._tx_depth == 0
        async with conn.transaction():
            assert conn._tx_depth == 1
            async with conn.transaction():
                assert conn._tx_depth == 2
            assert conn._tx_depth == 1
        assert conn._tx_depth == 0
    finally:
        assert conn._tx_depth == 0
