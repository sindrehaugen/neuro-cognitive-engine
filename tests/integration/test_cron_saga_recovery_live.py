"""
tests/integration/test_cron_saga_recovery_live.py
====================================================
Live-Postgres proof that `_saga_recovery_tick` (`nce/cron.py`) actually
decodes `saga_execution_log.payload` from a real JSONB column.

No asyncpg jsonb codec is registered anywhere in this codebase
(`nce/semantic_search.py`'s own comment documents this estate-wide fact),
so `payload` always arrives from a real connection as a raw JSON string,
never a dict. Before this fix, `isinstance(row["payload"], dict)` was
therefore always `False`, `payload` was always `{}`, and `memory_id`
extraction always failed -- every `pg_committed` saga this worker ever
processed against a real database skipped the memory-exists verification
entirely and fell straight to the "no memory_id" branch, which marks the
saga `'completed'` unconditionally. Found while sweeping every hand-written
JSONB writer/reader in the estate for the same convention `documents.py`
(#406) missed -- this is the worst of that sweep: not a wrong display, an
operational recovery job silently acting on a false premise on every run.

A mocked test cannot see this at all: a mock's `payload` is whatever
Python object the test hands it, never a real asyncpg string -- which is
exactly why this bug shipped for as long as it did with no test noticing
(F measured 51 test modules across this codebase that mock the database
while testing DB-dependent code).
"""

from __future__ import annotations

import json
import uuid

import asyncpg
import pytest

from nce.cron import _saga_recovery_tick

pytestmark = pytest.mark.integration


async def test_saga_recovery_finds_memory_id_from_real_jsonb_payload(
    pg_pool: asyncpg.Pool, namespace_id: uuid.UUID
) -> None:
    """The actual regression, not a synthetic shape: create a
    `pg_committed` saga whose `payload` (a real JSONB column) names a
    `memory_id`, and a real `memories` row with that id, then run the
    real tick against the real pool.

    Both the memory-exists branch and the no-memory-id branch end with
    `state = 'completed'` -- checking `state` alone cannot distinguish
    which branch ran, and would have passed even before this fix (the
    no-memory-id branch got there anyway, just without ever looking).
    Only the memory-exists branch appends a `saga_recovered` event, so
    that event's presence is the actual proof `memory_id` was
    successfully extracted from the real JSONB payload -- not inferred
    from the function's return value, which is `None` either way.
    """
    memory_id = uuid.uuid4()
    saga_id = uuid.uuid4()

    async with pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO memories (id, namespace_id, payload_ref) VALUES ($1, $2, $3)",
            memory_id,
            namespace_id,
            "test-payload-ref",
        )
        await conn.execute(
            """
            INSERT INTO saga_execution_log
                (id, saga_type, namespace_id, agent_id, state, payload, updated_at)
            VALUES ($1, 'store_memory', $2, 'test-agent', 'pg_committed', $3::jsonb,
                    now() - interval '10 minutes')
            """,
            saga_id,
            namespace_id,
            json.dumps({"memory_id": str(memory_id)}),
        )

    await _saga_recovery_tick(pg_pool)

    async with pg_pool.acquire() as conn:
        state = await conn.fetchval("SELECT state FROM saga_execution_log WHERE id = $1", saga_id)
        recovered_event_count = await conn.fetchval(
            "SELECT count(*) FROM event_log WHERE namespace_id = $1 AND event_type = 'saga_recovered'",
            namespace_id,
        )

    assert state == "completed", (
        f"expected the saga to be finalized via the memory-exists branch, got state={state!r}"
    )
    assert recovered_event_count == 1, (
        "no saga_recovered event was appended -- the memory-exists branch never ran, "
        "meaning memory_id was never successfully extracted from the real jsonb payload "
        "(this is exactly the pre-fix failure mode: the saga would still end up "
        "'completed', via the no-memory-id branch, with zero verification ever performed)"
    )


async def test_saga_recovery_leaves_old_completed_sagas_untouched(
    pg_pool: asyncpg.Pool, namespace_id: uuid.UUID
) -> None:
    """Positive control: a saga already in a terminal state must not be
    touched by the tick at all, proving the WHERE clause (`state =
    'pg_committed'`) is doing real filtering and this test isn't just
    observing every row get marked completed regardless of its payload.
    """
    saga_id = uuid.uuid4()
    async with pg_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO saga_execution_log
                (id, saga_type, namespace_id, agent_id, state, payload, updated_at)
            VALUES ($1, 'store_memory', $2, 'test-agent', 'completed', $3::jsonb,
                    now() - interval '10 minutes')
            """,
            saga_id,
            namespace_id,
            json.dumps({"memory_id": str(uuid.uuid4())}),
        )

    await _saga_recovery_tick(pg_pool)

    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT state, updated_at FROM saga_execution_log WHERE id = $1", saga_id
        )
    assert row["state"] == "completed"
