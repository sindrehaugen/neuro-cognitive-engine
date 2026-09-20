"""
tests/integration/test_field_tech_identifier_writable_fields_live.py
========================================================================
Live-Postgres acceptance test for the fix documented in
``_internal/work-docs/mlv16-orchestration/UNCREATABLE_SPECS.md``:
``field_tech:checklists``/``time-entries``/``work-orders`` each had a real
identifier column (``checklist_id``/``time_entry_id``/``work_order_id``)
that was ``NOT NULL`` with no default and absent from the spec's own
``writable_fields`` -- no caller could ever create a row through the
generated surface, unconditionally (reproduced live in that doc before
this fix: ``NotNullViolationError``).

All three real hand-written writers (``field_tech/checklist.py``,
``time_entry.py``, ``work_orders.py``) treat their identifier as
caller-optional with a server-generated ``<PREFIX>-<hex>`` fallback on
omission. The generated surface has no mechanism to replicate that
fallback (only ``id_field`` gets a ``uuid4()`` default) -- the fix adds
each identifier to its spec's ``writable_fields`` so a caller CAN supply
it, matching the identical, already-shipped precedent
``hr:absences.absence_id`` already uses. This does not restore the
hand-written path's auto-generate-on-omission convenience; that asymmetry
is accepted, not new, and not this fix's job to close.

Population: exactly 3, all in ``field_tech``. ``checklists`` and
``time-entries`` also carry a real composite FK to
``work_orders(work_order_id, namespace_id)`` (verified against
``information_schema`` before writing this fixture, not assumed) -- both
need a real work order created first.
"""

from __future__ import annotations

import uuid

import asyncpg
import httpx
import pytest
import pytest_asyncio
from starlette.applications import Starlette

from nce import admin_state
from nce.orchestrator import NCEEngine
from nce.resource_surface import get_all_resource_specs, load_all_engine_resources
from nce.resource_surface.rest import make_resource_routes

pytestmark = pytest.mark.integration

load_all_engine_resources()
_WORK_ORDER_SPEC = next(
    s for s in get_all_resource_specs() if s.engine == "field_tech" and s.entity == "work-orders"
)
_TIME_ENTRY_SPEC = next(
    s for s in get_all_resource_specs() if s.engine == "field_tech" and s.entity == "time-entries"
)
_CHECKLIST_SPEC = next(
    s for s in get_all_resource_specs() if s.engine == "field_tech" and s.entity == "checklists"
)


@pytest.fixture
def engine(pg_pool: asyncpg.Pool) -> NCEEngine:
    eng = NCEEngine()
    eng.pg_pool = pg_pool
    return eng


@pytest_asyncio.fixture(autouse=True)
async def _enable_field_tech(pg_pool: asyncpg.Pool, namespace_id: uuid.UUID) -> None:
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE namespaces SET metadata = jsonb_set("
            "coalesce(metadata, '{}'::jsonb), ARRAY['field_tech'], "
            "jsonb_build_object('enabled', true), true) WHERE id = $1",
            namespace_id,
        )


async def _create(engine: NCEEngine, spec, payload: dict) -> dict:
    previous = admin_state.engine
    admin_state.engine = engine
    try:
        app = Starlette(routes=make_resource_routes(spec))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(spec.rest_collection_path, json=payload)
        assert resp.status_code == 201, f"{spec.engine}:{spec.entity} create failed: {resp.text}"
        return resp.json()
    finally:
        admin_state.engine = previous


@pytest.mark.asyncio
async def test_work_order_create_now_succeeds_with_an_explicit_work_order_id(
    engine: NCEEngine, pg_pool: asyncpg.Pool, namespace_id: uuid.UUID
) -> None:
    work_order_id = f"WO-LIVE-{uuid.uuid4().hex[:8]}"
    created = await _create(
        engine,
        _WORK_ORDER_SPEC,
        {
            "namespace_id": str(namespace_id),
            "work_order_id": work_order_id,
            "kind": "install",
            "source_ref": "test-source-ref",
        },
    )
    assert created["work_order_id"] == work_order_id

    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT work_order_id FROM work_orders WHERE id = $1", uuid.UUID(created["id"])
        )
    assert row is not None and row["work_order_id"] == work_order_id


@pytest.mark.asyncio
async def test_time_entry_create_now_succeeds_with_an_explicit_time_entry_id(
    engine: NCEEngine, pg_pool: asyncpg.Pool, namespace_id: uuid.UUID
) -> None:
    work_order_id = f"WO-LIVE-{uuid.uuid4().hex[:8]}"
    await _create(
        engine,
        _WORK_ORDER_SPEC,
        {
            "namespace_id": str(namespace_id),
            "work_order_id": work_order_id,
            "kind": "install",
            "source_ref": "test-source-ref",
        },
    )

    time_entry_id = f"TE-LIVE-{uuid.uuid4().hex[:8]}"
    created = await _create(
        engine,
        _TIME_ENTRY_SPEC,
        {
            "namespace_id": str(namespace_id),
            "time_entry_id": time_entry_id,
            "work_order_id": work_order_id,
            "started_at": "2026-09-21T08:00:00+00:00",
        },
    )
    assert created["time_entry_id"] == time_entry_id

    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT time_entry_id FROM time_entries WHERE id = $1", uuid.UUID(created["id"])
        )
    assert row is not None and row["time_entry_id"] == time_entry_id


@pytest.mark.asyncio
async def test_checklist_create_now_succeeds_with_an_explicit_checklist_id(
    engine: NCEEngine, pg_pool: asyncpg.Pool, namespace_id: uuid.UUID
) -> None:
    work_order_id = f"WO-LIVE-{uuid.uuid4().hex[:8]}"
    await _create(
        engine,
        _WORK_ORDER_SPEC,
        {
            "namespace_id": str(namespace_id),
            "work_order_id": work_order_id,
            "kind": "install",
            "source_ref": "test-source-ref",
        },
    )

    checklist_id = f"CL-LIVE-{uuid.uuid4().hex[:8]}"
    created = await _create(
        engine,
        _CHECKLIST_SPEC,
        {
            "namespace_id": str(namespace_id),
            "checklist_id": checklist_id,
            "work_order_id": work_order_id,
            "template_id": "test-template-id",
        },
    )
    assert created["checklist_id"] == checklist_id

    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT checklist_id FROM checklists WHERE id = $1", uuid.UUID(created["id"])
        )
    assert row is not None and row["checklist_id"] == checklist_id
