"""
tests/integration/test_field_tech_jsonb_decode_live.py
==========================================================
Live-Postgres regression tests for the field_tech jsonb cluster: two
columns (`work_orders.raw`, `checklists.items`), same root cause as
tests/integration/test_jsonb_string_decode_live.py (#411) -- this pool
registers no jsonb codec, so every jsonb column round-trips through
asyncpg as raw JSON TEXT, never an automatically-decoded dict/list.

THE SIX CALL SITES
----------------------
Named: `work_orders.py`'s `do_get_work_order`, `do_query_work_order`,
`do_assign` (raw); `work_orders.py`'s `do_get_work_order` checklist
sub-list and `partner_view.py`'s `do_partner_view` (items).

Found while fixing the named sites, same file family, same shape, fixed
in the same PR: `work_orders.py`'s `do_create_work_order` also returns
`raw` undecoded (E's own claim -- "zero json.loads anywhere in that file"
-- covers this function too, not just the three individually named).
`checklist.py`'s `do_complete_checklist` returns both `items` and `raw`
undecoded from its own `RETURNING` clause.

NOT A CRASH -- A SILENT SHAPE DEFECT
-----------------------------------------
Unlike #411's three bugs, none of these six raise. Each function returns
successfully with `raw`/`items` as a raw JSON string embedded in an
otherwise-correct response. The defect surfaces one level up: a caller
reading `response["raw"]["origin"]` or iterating `response["items"]` as
the schema's own documented type (dict / list) gets a `TypeError` -- string
indices must be integers, or an iteration over characters instead of
checklist entries. Every test below asserts the real type, not just the
absence of an exception.

`checklists.items` HAS AN IN-REPO PRECEDENT THAT ALREADY WORKS
--------------------------------------------------------------------
`nce/vertical_modules/resources/field_schedule.py` already decodes this
exact column correctly: `json.loads(cl["items"]) if isinstance(cl["items"],
str) else cl["items"]`. Every fix here mirrors that shape exactly rather
than inventing a third one for a column that already has a right answer
in this codebase.

`do_partner_view` IS A PARTNER-FACING SURFACE
--------------------------------------------------
`checklists.items` in a partner's own response was a JSON string before
this fix; a real JSON array after. This is a response-shape change a
partner integration could observe directly, named explicitly here and in
the PR body rather than left implicit in a shared helper.
"""

from __future__ import annotations

import uuid

import asyncpg
import pytest
import pytest_asyncio

from nce.vertical_modules.field_tech.checklist import do_complete_checklist
from nce.vertical_modules.field_tech.partner_view import do_partner_view
from nce.vertical_modules.field_tech.work_orders import (
    do_assign,
    do_create_work_order,
    do_get_work_order,
    do_query_work_order,
)

pytestmark = pytest.mark.integration


class _Engine:
    def __init__(self, pg_pool: asyncpg.Pool) -> None:
        self.pg_pool = pg_pool


@pytest.fixture
def engine(pg_pool: asyncpg.Pool) -> _Engine:
    return _Engine(pg_pool)


@pytest_asyncio.fixture
async def work_order_id(engine: _Engine, namespace_id: uuid.UUID, pg_pool: asyncpg.Pool) -> str:
    from nce.entity_resolution.ownership_seed import seed_node_ownership_registry

    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await seed_node_ownership_registry(conn, namespace_id)

    wo_id = f"WO-JSONB-{uuid.uuid4().hex[:8]}"
    await do_create_work_order(
        engine,
        {
            "namespace_id": str(namespace_id),
            "work_order_id": wo_id,
            "source_kind": "manual",
            "source_ref": "jsonb-probe-ref",
            "raw": {"origin": "test", "n": 1},
        },
    )
    return wo_id


@pytest.mark.asyncio
async def test_create_work_order_raw_round_trips_a_real_dict(
    engine: _Engine, namespace_id: uuid.UUID
) -> None:
    """Found while fixing the three named sites below -- do_create_work_order
    has the identical dict(row) pattern and was equally undecoded.
    """
    wo_id = f"WO-JSONB-{uuid.uuid4().hex[:8]}"
    from nce.entity_resolution.ownership_seed import seed_node_ownership_registry

    async with engine.pg_pool.acquire() as conn:
        async with conn.transaction():
            await seed_node_ownership_registry(conn, namespace_id)

    created = await do_create_work_order(
        engine,
        {
            "namespace_id": str(namespace_id),
            "work_order_id": wo_id,
            "source_kind": "manual",
            "source_ref": "jsonb-create-ref",
            "raw": {"origin": "test", "n": 1},
        },
    )
    assert isinstance(created["raw"], dict), f"raw was not decoded: {created['raw']!r}"
    assert created["raw"] == {"origin": "test", "n": 1}


@pytest.mark.asyncio
async def test_get_work_order_raw_round_trips_a_real_dict(
    engine: _Engine, namespace_id: uuid.UUID, work_order_id: str
) -> None:
    got = await do_get_work_order(
        engine, {"namespace_id": str(namespace_id), "work_order_id": work_order_id}
    )
    assert isinstance(got["raw"], dict), f"raw was not decoded: {got['raw']!r}"
    assert got["raw"] == {"origin": "test", "n": 1}


@pytest.mark.asyncio
async def test_get_work_order_checklist_items_round_trips_a_real_list(
    engine: _Engine, namespace_id: uuid.UUID, work_order_id: str
) -> None:
    await do_complete_checklist(
        engine,
        {
            "namespace_id": str(namespace_id),
            "work_order_id": work_order_id,
            "items": [{"id": "step1", "label": "Step 1", "ticked": True}],
        },
    )
    got = await do_get_work_order(
        engine, {"namespace_id": str(namespace_id), "work_order_id": work_order_id}
    )
    assert got["checklists"], "expected at least one checklist"
    items = got["checklists"][0]["items"]
    assert isinstance(items, list), f"items was not decoded: {items!r}"
    assert items == [{"id": "step1", "label": "Step 1", "ticked": True}]


@pytest.mark.asyncio
async def test_query_work_order_raw_round_trips_a_real_dict(
    engine: _Engine, namespace_id: uuid.UUID, work_order_id: str
) -> None:
    queried = await do_query_work_order(engine, {"namespace_id": str(namespace_id)})
    matching = [w for w in queried["work_orders"] if w["work_order_id"] == work_order_id]
    assert matching, "expected the fixture work order in the query results"
    assert isinstance(matching[0]["raw"], dict), f"raw was not decoded: {matching[0]['raw']!r}"


@pytest.mark.asyncio
async def test_assign_raw_round_trips_a_real_dict(
    engine: _Engine, namespace_id: uuid.UUID, work_order_id: str
) -> None:
    assigned = await do_assign(
        engine,
        {
            "namespace_id": str(namespace_id),
            "work_order_id": work_order_id,
            "assignee_id": "EMP-JSONB",
            "assignee_kind": "employee",
        },
    )
    assert isinstance(assigned["raw"], dict), f"raw was not decoded: {assigned['raw']!r}"
    assert assigned["raw"] == {"origin": "test", "n": 1}


@pytest.mark.asyncio
async def test_complete_checklist_items_and_raw_round_trip_real_types(
    engine: _Engine, namespace_id: uuid.UUID, work_order_id: str
) -> None:
    """do_complete_checklist itself -- found while fixing the named sites,
    same RETURNING-clause pattern, same shape.
    """
    completed = await do_complete_checklist(
        engine,
        {
            "namespace_id": str(namespace_id),
            "work_order_id": work_order_id,
            "items": [{"id": "step1", "label": "Step 1", "ticked": True}],
            "raw": {"note": "test"},
        },
    )
    assert isinstance(completed["items"], list), f"items was not decoded: {completed['items']!r}"
    assert isinstance(completed["raw"], dict), f"raw was not decoded: {completed['raw']!r}"
    assert completed["raw"] == {"note": "test"}


@pytest.mark.asyncio
async def test_partner_view_checklist_items_round_trips_a_real_list(
    engine: _Engine, namespace_id: uuid.UUID, work_order_id: str, pg_pool: asyncpg.Pool
) -> None:
    """Partner-facing surface: before this fix, a partner integration
    received checklists.items as a JSON-encoded string instead of a real
    array -- a caller-visible response-shape change, named explicitly.
    """
    await do_complete_checklist(
        engine,
        {
            "namespace_id": str(namespace_id),
            "work_order_id": work_order_id,
            "items": [{"id": "step1", "label": "Step 1", "ticked": True}],
        },
    )
    partner_id = uuid.uuid4()
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE work_orders SET partner_scope_id = $1 "
            "WHERE work_order_id = $2 AND namespace_id = $3",
            partner_id,
            work_order_id,
            namespace_id,
        )
        await conn.execute(
            "UPDATE checklists SET partner_scope_id = $1 "
            "WHERE work_order_id = $2 AND namespace_id = $3",
            partner_id,
            work_order_id,
            namespace_id,
        )

    view = await do_partner_view(
        engine, {"namespace_id": str(namespace_id), "partner_scope_id": str(partner_id)}
    )
    assert view["work_orders"], "expected the fixture work order in the partner view"
    items = view["work_orders"][0]["checklists"][0]["items"]
    assert isinstance(items, list), f"items was not decoded for the partner view: {items!r}"
    assert items == [{"id": "step1", "label": "Step 1", "ticked": True}]
