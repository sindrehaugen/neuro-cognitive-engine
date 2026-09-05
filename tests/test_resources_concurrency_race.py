"""
tests/test_resources_concurrency_race.py
========================================
RS-3: Database-enforced double-booking exclusion concurrency race test.
Proves that two concurrent allocation requests over overlapping time windows
for the same resource are rejected at the database level via PostgreSQL btree_gist
exclusion constraint EXCLUDE USING gist (resource_id WITH =, tstzrange(starts_at, ends_at) WITH &&) WHERE (status <> 'released').

Sequential tests here are vacuous. This test executes concurrent transactions
using asyncio.gather across separate database connections in pg_pool.
Includes a positive control proving RED when the constraint is dropped.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import asyncpg
import pytest

from nce.vertical_modules.resources._guard import ResourceConcurrencyError
from nce.vertical_modules.resources.allocations import do_reserve
from nce.vertical_modules.resources.registry import do_create_resource


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_overlapping_reservations_race(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """
    Two concurrent do_reserve calls for the SAME resource over an overlapping window:
      Reservation 1: 09:00 - 13:00 UTC
      Reservation 2: 11:00 - 15:00 UTC (overlaps by 2 hours)
    Dispatched simultaneously via asyncio.gather across real pool connections.
    Exactly one must commit and succeed; exactly one must fail with ResourceConcurrencyError.
    """
    engine: dict[str, Any] = {"pg_pool": pg_pool}

    # 1. Create a test resource
    res = await do_create_resource(
        engine,
        {
            "namespace_id": namespace_id,
            "kind": "employee",
            "display_name": "Lead Technician Test",
        },
    )
    resource_id = res["id"]

    # 2. Define two overlapping reservation requests
    req_1 = {
        "namespace_id": namespace_id,
        "resource_id": resource_id,
        "demand_kind": "project",
        "starts_at": "2026-09-10T09:00:00Z",
        "ends_at": "2026-09-10T13:00:00Z",
        "attrs": {"request_id": "REQ-A"},
    }
    req_2 = {
        "namespace_id": namespace_id,
        "resource_id": resource_id,
        "demand_kind": "service",
        "starts_at": "2026-09-10T11:00:00Z",
        "ends_at": "2026-09-10T15:00:00Z",
        "attrs": {"request_id": "REQ-B"},
    }

    # 3. Fire both concurrently
    results = await asyncio.gather(
        do_reserve(engine, req_1),
        do_reserve(engine, req_2),
        return_exceptions=True,
    )

    successes = [r for r in results if isinstance(r, dict) and r.get("status") == "reserved"]
    concurrency_errors = [r for r in results if isinstance(r, ResourceConcurrencyError)]
    other_errors = [
        r
        for r in results
        if isinstance(r, Exception) and not isinstance(r, ResourceConcurrencyError)
    ]

    # Exactly one succeeded and exactly one failed with ResourceConcurrencyError
    assert len(other_errors) == 0, f"Unexpected errors during concurrent race: {other_errors}"
    assert len(successes) == 1, f"Expected exactly 1 success, got {len(successes)}: {results}"
    assert len(concurrency_errors) == 1, (
        f"Expected exactly 1 ResourceConcurrencyError, got {len(concurrency_errors)}: {results}"
    )

    winner = successes[0]
    assert winner["resource_id"] == resource_id
    assert winner["status"] == "reserved"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_consecutive_non_overlapping_reservations_succeed(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """
    Two consecutive reservations touching at boundary (09:00-12:00 and 12:00-15:00)
    must BOTH succeed because half-open range [09:00, 12:00) and [12:00, 15:00) do NOT overlap.
    """
    engine: dict[str, Any] = {"pg_pool": pg_pool}

    res = await do_create_resource(
        engine,
        {
            "namespace_id": namespace_id,
            "kind": "vehicle",
            "display_name": "Service Van Non-Overlap",
        },
    )
    resource_id = res["id"]

    alloc_1 = await do_reserve(
        engine,
        {
            "namespace_id": namespace_id,
            "resource_id": resource_id,
            "demand_kind": "project",
            "starts_at": "2026-09-11T09:00:00Z",
            "ends_at": "2026-09-11T12:00:00Z",
        },
    )
    alloc_2 = await do_reserve(
        engine,
        {
            "namespace_id": namespace_id,
            "resource_id": resource_id,
            "demand_kind": "service",
            "starts_at": "2026-09-11T12:00:00Z",
            "ends_at": "2026-09-11T15:00:00Z",
        },
    )

    assert alloc_1["status"] == "reserved"
    assert alloc_2["status"] == "reserved"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_positive_control_dropping_constraint_allows_race(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """
    Positive control (Charter §5 RS-3): Prove RED by dropping the exclusion constraint.
    When the constraint is absent, two concurrent overlapping reserves both succeed
    (double-booking occurs), proving that the DB constraint is strictly load-bearing.
    """
    engine: dict[str, Any] = {"pg_pool": pg_pool}

    res = await do_create_resource(
        engine,
        {
            "namespace_id": namespace_id,
            "kind": "contractor",
            "display_name": "Positive Control Electrician",
        },
    )
    resource_id = res["id"]

    # 1. Drop constraint temporarily
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "ALTER TABLE allocations DROP CONSTRAINT IF EXISTS exclude_resource_double_booking;"
        )

    try:
        req_1 = {
            "namespace_id": namespace_id,
            "resource_id": resource_id,
            "demand_kind": "project",
            "starts_at": "2026-09-12T09:00:00Z",
            "ends_at": "2026-09-12T13:00:00Z",
        }
        req_2 = {
            "namespace_id": namespace_id,
            "resource_id": resource_id,
            "demand_kind": "service",
            "starts_at": "2026-09-12T11:00:00Z",
            "ends_at": "2026-09-12T15:00:00Z",
        }

        # 2. Both overlapping requests run concurrently
        results = await asyncio.gather(
            do_reserve(engine, req_1),
            do_reserve(engine, req_2),
            return_exceptions=True,
        )

        successes = [r for r in results if isinstance(r, dict) and r.get("status") == "reserved"]
        # Without constraint, BOTH succeed (proving RED condition: double-booking happens)
        assert len(successes) == 2, f"Expected both to succeed without constraint, got: {results}"
    finally:
        # 3. Clean up overlapping rows so constraint restoration succeeds
        async with pg_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM allocations WHERE resource_id = $1;", uuid.UUID(resource_id)
            )
            # Restore constraint
            await conn.execute(
                """
                ALTER TABLE allocations
                ADD CONSTRAINT exclude_resource_double_booking
                EXCLUDE USING gist (
                    resource_id WITH =,
                    tstzrange(starts_at, ends_at) WITH &&
                ) WHERE (status <> 'released');
                """
            )
