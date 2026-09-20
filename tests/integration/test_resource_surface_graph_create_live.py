"""
tests/integration/test_resource_surface_graph_create_live.py
================================================================
Live-Postgres CREATE sibling for the 9 graph/kg_nodes-primary specs.
#388 gave this population a live PATCH sibling; #385/#391 cover relational
PATCH; the write-coercion sibling covers relational CREATE. Nothing
exercised graph-primary CREATE against real Postgres in its own right --
#388's PATCH test calls create only as setup, and never verifies that the
fields supplied AT CREATE TIME actually land in the secondary tables (it
immediately PATCHes a different field and only checks the post-PATCH
state). This file closes that gap.

WHY THIS PATH IS STRUCTURALLY DIFFERENT FROM EVERYTHING ELSE COVERED
------------------------------------------------------------------------
It writes ``kg_nodes`` plus secondary tables in one call. It is the only
generated-surface path where ``assert_owner`` (deny-by-default ownership)
fires at all -- confirmed by reading ``rest.py``: the call lives inside
``if is_graph:`` only, in both ``handle_create`` and ``mcp.py``'s
``handle_upsert``. And ``rest.py``'s own ``data[spec.id_field] = item_id``
(set unconditionally, before the graph/relational branch) is dead code on
this path: the graph branch's response comes from ``row_to_dict()`` over
kg_nodes' own ``RETURNING`` clause (``id, label, entity_type,
namespace_id, change_origin, created_at, updated_at``), which never
contains a key named after ``spec.id_field`` unless ``id_field`` happens
to literally be ``"label"`` -- for 6 of the 9 it is ``"node_label"``
instead. Pinned explicitly below (found and noted, not fixed, in #388).

TRANSITION-SPECIFIC OWNERSHIP -- CHECKED PER SPEC, NOT ASSUMED
--------------------------------------------------------------------
``assert_owner`` is called with no ``transition`` argument by the
generated surface (confirmed by reading both call sites), so it always
looks up the ``transition IS NULL`` row for a node type.
``node-ownership.json`` has real per-transition splits for ``PO_LINE``,
``MARGIN``, and ``BOM_LINE`` -- each explicitly WITHOUT a ``transition:
null`` row (`MARGIN`'s own note: "Do NOT add a transition:null row...
that would silently re-claim the whole node type"). Checked all 9 of this
population's node types against the live registry directly: every one has
exactly one entry, and it is a plain ``transition: null`` row. None of the
9 hit the per-transition shape, so ``seed_node_ownership_registry``
(which seeds every entry, including each node type's ``null`` row) is
sufficient for all 9 -- not assumed from the population being "probably
simple", verified against the same JSON file the three exceptions live in.
"""

from __future__ import annotations

import uuid
from typing import Any

import asyncpg
import httpx
import pytest
import pytest_asyncio
from starlette.applications import Starlette

from nce import admin_state
from nce.auth import set_namespace_context
from nce.engine_registry import populate_engine_modules
from nce.entity_resolution.ownership_seed import seed_node_ownership_registry
from nce.orchestrator import NCEEngine
from nce.resource_surface import ResourceSpec, get_all_resource_specs, load_all_engine_resources
from nce.resource_surface.rest import make_resource_routes

pytestmark = pytest.mark.integration

load_all_engine_resources()
_ALL_SPECS = {(s.engine, s.entity): s for s in get_all_resource_specs()}
_GRAPH_ELIGIBLE = [
    s
    for s in sorted(get_all_resource_specs(), key=lambda s: (s.engine, s.entity))
    if "upsert" not in s.excluded_verbs and s.writable_fields and s.table_name is None
]
_SPEC_IDS = [f"{s.engine}:{s.entity}" for s in _GRAPH_ELIGIBLE]

_TEXT_TYPES = {"text", "character varying"}

# Same skip list #388 measured and used -- enum-CHECK-constrained secondary
# columns where a generated string would violate the CHECK regardless of
# nullability. Re-derived here rather than imported: each live sibling
# tonight is self-contained by convention, not cross-importing another
# test file's internals.
_ENUM_CHECK_SKIP: dict[str, set[str]] = {
    "system_design_design_requests": {"status", "priority"},
    "system_design_device_capabilities": {"port_direction", "redundancy_role"},
    # status: enum-CHECK per node_type (system_design_node_state_status_
    # per_node_type). node_type: this is the ONE SecondaryTable.fields
    # entry the generated surface auto-injects itself
    # (handle_create/handle_upsert's `if "node_type" in secondary_field_
    # names: sec_data["node_type"] = spec.node_type`, see
    # UNCREATABLE_SPECS.md's silent-no-op finding from #388) -- it is
    # never read from the client's own supplied value, so asserting a
    # test-supplied placeholder against it would always fail regardless
    # of whether create is correct.
    "system_design_node_state": {"status", "node_type"},
}


@pytest.fixture
def engine(pg_pool: asyncpg.Pool) -> NCEEngine:
    eng = NCEEngine()
    eng.pg_pool = pg_pool
    populate_engine_modules(eng)
    return eng


@pytest_asyncio.fixture(autouse=True)
async def _seed_ownership(pg_pool: asyncpg.Pool, namespace_id: uuid.UUID) -> None:
    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, namespace_id)
            await seed_node_ownership_registry(conn, namespace_id)


async def _column_info(conn: asyncpg.Connection, table_name: str) -> dict[str, dict[str, Any]]:
    rows = await conn.fetch(
        "SELECT column_name, data_type, is_nullable, column_default, "
        "character_maximum_length FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=$1",
        table_name,
    )
    return {r["column_name"]: dict(r) for r in rows}


def _rest_app(spec: ResourceSpec) -> Starlette:
    return Starlette(routes=make_resource_routes(spec))


async def _typed_secondary_fields(
    conn: asyncpg.Connection, table_name: str, candidate_fields: tuple[str, ...]
) -> dict[str, str]:
    """Every real TEXT/VARCHAR column in `candidate_fields`, given a value
    distinct per field name so the create-time read-back assertion below
    can tell fields apart (not the same literal for every column)."""
    columns = await _column_info(conn, table_name)
    skip = _ENUM_CHECK_SKIP.get(table_name, set())
    payload: dict[str, str] = {}
    for f in candidate_fields:
        if f in skip:
            continue
        col = columns.get(f)
        if col is not None and col["data_type"] in _TEXT_TYPES:
            value = f"created_{f}_value"
            max_len = col["character_maximum_length"]
            if max_len is not None and len(value) > max_len:
                value = value[:max_len]
            payload[f] = value
    return payload


async def _create(
    engine: NCEEngine, spec: ResourceSpec, payload: dict[str, object]
) -> httpx.Response:
    previous = admin_state.engine
    admin_state.engine = engine
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_rest_app(spec)), base_url="http://test"
        ) as client:
            return await client.post(spec.rest_collection_path, json=payload)
    finally:
        admin_state.engine = previous


def test_discovery_floor_matches_the_measured_population() -> None:
    assert len(_GRAPH_ELIGIBLE) >= 9, (
        f"Only {len(_GRAPH_ELIGIBLE)} graph-primary, upsert-eligible, writable "
        f"specs found -- expected at least 9 (measured on main@0aaba82). A spec "
        f"was excluded, lost its writable_fields, or gained a table_name."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("spec", _GRAPH_ELIGIBLE, ids=_SPEC_IDS)
async def test_create_writes_kg_nodes_and_secondary_tables_correctly(
    spec: ResourceSpec, engine: NCEEngine, pg_pool: asyncpg.Pool, namespace_id: uuid.UUID
) -> None:
    """Creates with a real, distinct value for every secondary-table field
    the spec declares, then reads EVERY one of those values back directly
    from Postgres -- not just the identity, and not after a subsequent
    PATCH masks whether create itself ever wrote them.
    """
    node_label = f"probe-create-{spec.node_type.lower()}-{uuid.uuid4().hex[:8]}"
    create_payload: dict[str, object] = {
        "namespace_id": str(namespace_id),
        spec.id_field: node_label,
        "change_origin": "agent",
    }

    secondary_payloads: dict[str, dict[str, str]] = {}
    async with pg_pool.acquire() as conn:
        for sec in spec.secondary_tables:
            fields = await _typed_secondary_fields(conn, sec.table_name, sec.fields)
            secondary_payloads[sec.table_name] = fields
            create_payload.update(fields)

    resp = await _create(engine, spec, create_payload)
    spec_id = f"{spec.engine}:{spec.entity}"
    assert resp.status_code == 201, f"{spec_id} create failed against real Postgres: {resp.text}"
    created = resp.json()

    # Pinned per #388's finding, re-asserted here since this file's whole
    # job is CREATE correctness: the graph branch's response always keys
    # identity as literal "label" (kg_nodes' own RETURNING clause), never
    # spec.id_field's declared name.
    assert created["label"] == node_label, (
        f"{spec_id}: create response keyed identity as {created.get('label')!r} under "
        f"'label', expected {node_label!r} -- if this ever starts matching "
        f"spec.id_field instead, the response-key inconsistency #388 found was fixed "
        f"and this assertion should be updated to match, not deleted."
    )

    async with pg_pool.acquire() as conn:
        kg_row = await conn.fetchrow(
            "SELECT entity_type, change_origin FROM kg_nodes WHERE label = $1 AND namespace_id = $2",
            node_label,
            namespace_id,
        )
    assert kg_row is not None, f"{spec_id}: no kg_nodes row found for {node_label!r} after create"
    assert kg_row["entity_type"] == spec.node_type
    assert kg_row["change_origin"] == "agent", (
        f"{spec_id}: create reported 201 but kg_nodes.change_origin was not actually "
        f"persisted as supplied."
    )

    for sec in spec.secondary_tables:
        supplied = secondary_payloads[sec.table_name]
        if not supplied:
            continue
        async with pg_pool.acquire() as conn:
            sec_row = await conn.fetchrow(
                f"SELECT {', '.join(supplied.keys())} FROM {sec.table_name} "
                f"WHERE {sec.join_field} = $1 AND namespace_id = $2",
                node_label,
                namespace_id,
            )
        assert sec_row is not None, (
            f"{spec_id}: no {sec.table_name} row found for {node_label!r} after create "
            f"-- create reported 201 but the secondary-table write silently did not happen."
        )
        for field, expected_value in supplied.items():
            assert sec_row[field] == expected_value, (
                f"{spec_id}: create reported 201 but {sec.table_name}.{field} is "
                f"{sec_row[field]!r}, expected {expected_value!r} -- the value supplied "
                f"at CREATE time was not actually written."
            )


@pytest.mark.asyncio
async def test_positive_control_unowned_node_type_is_denied_on_create(
    engine: NCEEngine, pg_pool: asyncpg.Pool, namespace_id: uuid.UUID
) -> None:
    """U18: with the ownership registry fully seeded for every REAL node
    type, a synthetic, unregistered node_type must still be denied (403) --
    proving assert_owner's deny-by-default fires on create specifically,
    not merely that seeding this population happens to work.
    """
    import dataclasses

    real_spec = _ALL_SPECS[("project", "projects")]
    unowned_spec = dataclasses.replace(real_spec, node_type="PROBE_UNOWNED_CREATE_NODE_TYPE")

    resp = await _create(
        engine,
        unowned_spec,
        {
            "namespace_id": str(namespace_id),
            unowned_spec.id_field: f"probe-unowned-create-{uuid.uuid4().hex[:8]}",
            "change_origin": "agent",
        },
    )

    assert resp.status_code == 403, (
        f"An unowned node_type must be denied (403) on create even with the ownership "
        f"registry seeded for every REAL node type -- got {resp.status_code}: {resp.text}."
    )
