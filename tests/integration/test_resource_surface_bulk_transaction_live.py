"""
tests/integration/test_resource_surface_bulk_transaction_live.py
====================================================================
Live-Postgres proof of the Q-48 follow-on fix (2026-09-20):
``nce/resource_surface/rest.py::handle_bulk`` no longer half-commits a
table-backed bulk create on a mid-batch failure.

THE BUG, EXACTLY
------------------
Before this fix, the per-item loop opened a NEW ``scoped_pg_session``
(and therefore a new transaction) for EVERY item in the batch. Item N's
own INSERT committed on its own, independently of whether item N+1 ever
ran or succeeded. If item K raised (a real constraint violation, a bad
value), items 1..K-1 stayed committed, the exception propagated
uncaught out of ``handle_bulk``, and items K+1..N were never attempted.
Nobody decided this was the semantics -- Q-48's own brief names it
plainly: "an undocumented crash-and-partial-commit, not a decision
anyone made."

THE FIX
--------
One shared ``scoped_pg_session`` (one transaction) for the whole batch.
A failure on any item now rolls back every item already inserted in
that same call, restoring the all-or-nothing guarantee a caller of a
bulk-create endpoint already assumed. This does NOT decide Q-48's
separate, still-open reporting-semantics question (should a future
version report per-item success/failure instead of an atomic batch) --
it only removes the accidental partial-commit that was never a real
decision.

This is deliberately a LIVE test, not a mock: the whole point is a real
Postgres UNIQUE constraint violation and a real transaction rollback --
a mocked connection cannot prove either.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

import asyncpg  # type: ignore[import-untyped]
import httpx
import pytest
from starlette.applications import Starlette

from nce import admin_state
from nce.engine_registry import populate_engine_modules
from nce.orchestrator import NCEEngine
from nce.resource_surface import get_all_resource_specs, load_all_engine_resources
from nce.resource_surface.rest import make_resource_routes

pytestmark = pytest.mark.integration


def _sites_rest_app() -> Starlette:
    load_all_engine_resources()
    site_spec = next(s for s in get_all_resource_specs() if s.engine == "sites")
    return Starlette(routes=make_resource_routes(site_spec))


@pytest.fixture
def engine(pg_pool: asyncpg.Pool) -> NCEEngine:
    eng = NCEEngine()
    eng.pg_pool = pg_pool
    populate_engine_modules(eng)
    return eng


@asynccontextmanager
async def _sites_rest_client(engine: NCEEngine):
    previous = admin_state.engine
    admin_state.engine = engine
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_sites_rest_app()), base_url="http://test"
        ) as client:
            yield client
    finally:
        admin_state.engine = previous


@pytest.mark.asyncio
async def test_bulk_create_rolls_back_everything_on_a_mid_batch_failure(
    engine: NCEEngine, namespace_id: uuid.UUID, pg_pool: asyncpg.Pool
) -> None:
    """3-item batch: item 1 and item 3 share a cadastre_id, violating
    sites' real UNIQUE (namespace_id, cadastre_id) constraint on item 3.
    Item 1 and item 2 must NOT be findable afterward -- proving the whole
    batch rolled back, not just that item 3 failed.
    """
    dup_cadastre = f"DUP-{uuid.uuid4().hex[:8]}"
    unique_cadastre = f"UNIQUE-{uuid.uuid4().hex[:8]}"

    async with _sites_rest_client(engine) as client:
        r = await client.post(
            "/api/sites/sites/bulk",
            json={
                "namespace_id": str(namespace_id),
                "items": [
                    {"name": "Bulk Site A", "site_type": "building", "cadastre_id": dup_cadastre},
                    {
                        "name": "Bulk Site B",
                        "site_type": "building",
                        "cadastre_id": unique_cadastre,
                    },
                    {
                        "name": "Bulk Site C (duplicate cadastre_id)",
                        "site_type": "building",
                        "cadastre_id": dup_cadastre,
                    },
                ],
            },
        )

    assert r.status_code == 500, (
        f"expected the batch to fail on item 3's UNIQUE violation, got {r.status_code}: {r.text}"
    )

    async with pg_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT count(*) FROM sites WHERE namespace_id = $1 AND name LIKE 'Bulk Site%'",
            namespace_id,
        )
    assert count == 0, (
        f"all-or-nothing violated: {count} row(s) from the failed batch are still in the "
        "table -- item 1 and/or item 2 committed independently of item 3's failure"
    )


@pytest.mark.asyncio
async def test_bulk_create_still_succeeds_end_to_end_when_nothing_fails(
    engine: NCEEngine, namespace_id: uuid.UUID, pg_pool: asyncpg.Pool
) -> None:
    """Positive control: the fix must not have broken the ordinary
    success path -- same response shape as before (status/count/ids),
    all rows actually present.
    """
    async with _sites_rest_client(engine) as client:
        r = await client.post(
            "/api/sites/sites/bulk",
            json={
                "namespace_id": str(namespace_id),
                "items": [
                    {"name": "Clean Bulk Site A", "site_type": "building"},
                    {"name": "Clean Bulk Site B", "site_type": "building"},
                ],
            },
        )

    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["count"] == 2
    assert len(body["ids"]) == 2

    async with pg_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT count(*) FROM sites WHERE namespace_id = $1 AND name LIKE 'Clean Bulk Site%'",
            namespace_id,
        )
    assert count == 2
