"""TASK-12: Event log concurrency and Merkle chain integrity tests."""

import asyncio

import pytest

from nce.event_log import append_event, verify_merkle_chain
from tests.fixtures.event_log_params import minimal_store_memory_params


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_append_event_no_gaps(pg_pool, namespace_id):
    async def write_one(i: int):
        async with pg_pool.acquire(timeout=10.0) as conn:
            async with conn.transaction():
                return await append_event(
                    conn=conn,
                    namespace_id=namespace_id,
                    agent_id="test-agent",
                    event_type="store_memory",
                    params=minimal_store_memory_params(i=i),
                )

    results = await asyncio.gather(*(write_one(i) for i in range(50)))
    seqs = sorted(r.event_seq for r in results)
    assert seqs == list(range(1, 51)), f"Gaps or duplicates in sequence: {seqs}"

    async with pg_pool.acquire(timeout=10.0) as conn:
        chain = await verify_merkle_chain(conn, namespace_id=namespace_id)

    assert chain["valid"] is True
    assert chain["checked"] == 50


@pytest.mark.integration
@pytest.mark.asyncio
async def test_event_sequences_are_independent_per_namespace(pg_pool, make_namespace):
    ns_a = await make_namespace()
    ns_b = await make_namespace()

    async def write(ns):
        async with pg_pool.acquire(timeout=10.0) as conn:
            async with conn.transaction():
                return await append_event(
                    conn=conn,
                    namespace_id=ns,
                    agent_id="test-agent",
                    event_type="store_memory",
                    params=minimal_store_memory_params(),
                )

    results = await asyncio.gather(
        *[write(ns_a) for _ in range(20)],
        *[write(ns_b) for _ in range(20)],
    )

    a_seqs = sorted(r.event_seq for r in results[:20])
    b_seqs = sorted(r.event_seq for r in results[20:])
    assert a_seqs == list(range(1, 21))
    assert b_seqs == list(range(1, 21))
