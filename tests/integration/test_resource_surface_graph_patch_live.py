"""
tests/integration/test_resource_surface_graph_patch_live.py
===============================================================
Live-Postgres PATCH sibling for the 9 graph/kg_nodes-primary specs
explicitly out of scope in ``test_resource_surface_patch_live.py``
(``table_name is None``: ``project:projects``, ``sales:contacts``, and 7
``system_design`` specs). Sized in ``UNCREATABLE_SPECS.md`` before being
built here.

WHY THIS IS A SEPARATE FILE, NOT AN EXTENSION OF THE RELATIONAL ONE
----------------------------------------------------------------------
``rest.py``'s ``handle_create``/``handle_patch`` gate every kg_nodes write
behind ``assert_owner`` (deny-by-default ownership) -- a check that never
fires for any of the 40 relational specs (it lives inside the ``if
is_graph:`` branch only). This is the ONE place in the whole generated
surface where that gate is live, so the fixture needs
``seed_node_ownership_registry`` (the same helper
``tests/test_agreements_sla.py`` and
``tests/integration/test_resource_surface_kg_nodes_primary_live.py`` use)
that none of the relational live siblings tonight needed at all.

The kg_nodes identity row itself is deliberately thin (label, entity_type,
change_origin, timestamps -- kg_nodes has no attribute column of its own,
confirmed by every existing consumer, see
``test_resource_surface_kg_nodes_primary_live.py``'s own docstring). Every
real field routes to a secondary table via the already-shared
``upsert_secondary_tables()`` -- the same function
``test_resource_surface_patch_live.py`` and
``test_resource_surface_write_coercion_live.py`` already exercise, just
pointed at a different table.

POPULATION
------------
3 of 9 write only ``change_origin`` (kg_nodes' one real writable column,
nullable with a CHECK if set) and have no secondary table at all:
``project:projects``, ``system_design:designs``,
``system_design:functional-locations``. PATCH for these targets
``change_origin`` directly on the kg_nodes row.

6 of 9 also have a secondary table PATCH targets naturally route through:
``sales:contacts`` (``sales_contacts``), ``system_design:cables``
(``system_design_node_state``), ``system_design:design_requests``
(``system_design_design_requests``), ``system_design:devices`` (BOTH
``system_design_device_capabilities`` and ``system_design_node_state``),
``system_design:ports`` (``system_design_device_capabilities`` only --
``system_design_node_state``'s own CHECK,
``system_design_node_state_status_per_node_type``, has no ``WHEN 'PORT'``
branch and falls to ``ELSE false``, so a PORT row there is structurally
impossible, not merely undeclared -- confirmed by reading the constraint
definition, matching the precedent file's own documented finding),
``system_design:racks`` (both tables, like devices).

None of the 9 have an ``enabled_guard`` -- one fewer axis than the
relational population.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
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
_JSON_TYPES = {"json", "jsonb"}

# Enum-CHECK-constrained secondary-table columns with a DEFAULT -- omission
# (not the generic "generated_<field>_value" text pass) satisfies both
# NOT NULL and the CHECK. Verified against pg_get_constraintdef, not
# guessed (system_design_design_requests_status_check /
# _priority_check; system_design_device_capabilities' port_direction/
# redundancy_role CHECKs are nullable, but a generated string would still
# violate them if not skipped, regardless of nullability).
_ENUM_CHECK_SKIP: dict[str, set[str]] = {
    "system_design_design_requests": {"status", "priority"},
    "system_design_device_capabilities": {"port_direction", "redundancy_role"},
}

# system_design_node_state's status is validated by a CASE on node_type
# (system_design_node_state_status_per_node_type) -- nullable for every
# node_type this population reaches, so omitting it (rather than trying to
# thread a per-node-type valid value through this generic builder) is
# correct, not a shortcut.
_ENUM_CHECK_SKIP["system_design_node_state"] = {"status"}


@pytest.fixture
def engine(pg_pool: asyncpg.Pool) -> NCEEngine:
    eng = NCEEngine()
    eng.pg_pool = pg_pool
    populate_engine_modules(eng)
    return eng


@pytest_asyncio.fixture(autouse=True)
async def _seed_ownership(pg_pool: asyncpg.Pool, namespace_id: uuid.UUID) -> None:
    """The one universal prerequisite for this population (sized in
    UNCREATABLE_SPECS.md): assert_owner denies by default with no registry
    row, and it is the ONE place in the whole generated surface this gate
    is live (inside rest.py's `if is_graph:` branch only) -- no relational
    live sibling tonight needed this."""
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
) -> dict[str, object]:
    """A minimal, type-safe subset of `candidate_fields` to send on create,
    filtered to real TEXT/VARCHAR columns on `table_name` -- the same
    strategy every relational live sibling tonight used, applied here to a
    SECONDARY table instead of a spec's own primary one. Purpose is only to
    guarantee the secondary row actually gets created (see module
    docstring's PORT note: its device_capabilities has no auto-injected
    node_type, so it needs at least one real supplied field)."""
    columns = await _column_info(conn, table_name)
    skip = _ENUM_CHECK_SKIP.get(table_name, set())
    payload: dict[str, object] = {}
    for f in candidate_fields:
        if f in skip:
            continue
        col = columns.get(f)
        if col is not None and col["data_type"] in _TEXT_TYPES:
            value = f"generated_{f}_value"
            max_len = col["character_maximum_length"]
            if max_len is not None and len(value) > max_len:
                value = value[:max_len]
            payload[f] = value
    return payload


async def _create(engine: NCEEngine, spec: ResourceSpec, payload: dict[str, object]) -> dict:
    previous = admin_state.engine
    admin_state.engine = engine
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_rest_app(spec)), base_url="http://test"
        ) as client:
            resp = await client.post(spec.rest_collection_path, json=payload)
        assert resp.status_code == 201, f"{spec.engine}:{spec.entity} create failed: {resp.text}"
        return resp.json()
    finally:
        admin_state.engine = previous


def test_discovery_floor_matches_the_measured_population() -> None:
    assert len(_GRAPH_ELIGIBLE) >= 9, (
        f"Only {len(_GRAPH_ELIGIBLE)} graph-primary, upsert-eligible, writable "
        f"specs found -- expected at least 9 (measured on main@308020c, sized "
        f"in UNCREATABLE_SPECS.md). A spec was excluded, lost its writable_fields, "
        f"or gained a table_name."
    )
    assert all(s.enabled_guard is None for s in _GRAPH_ELIGIBLE), (
        "A graph-primary spec gained an enabled_guard -- this file's fixture "
        "does not enable any engine, per the measured premise that none of the "
        "9 have one."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("spec", _GRAPH_ELIGIBLE, ids=_SPEC_IDS)
async def test_graph_patch_reaches_real_postgres(
    spec: ResourceSpec, engine: NCEEngine, pg_pool: asyncpg.Pool, namespace_id: uuid.UUID
) -> None:
    node_label = f"probe-{spec.node_type.lower()}-{uuid.uuid4().hex[:8]}"
    create_payload: dict[str, object] = {
        "namespace_id": str(namespace_id),
        spec.id_field: node_label,
        "change_origin": "agent",
    }

    async with pg_pool.acquire() as conn:
        for sec in spec.secondary_tables:
            create_payload.update(await _typed_secondary_fields(conn, sec.table_name, sec.fields))

    created = await _create(engine, spec, create_payload)
    # The graph-branch create response always keys the identity value as
    # "label" (row_to_dict() over kg_nodes' own RETURNING clause: "id, label,
    # entity_type, ..."), never spec.id_field's own name ("label" or
    # "node_label" depending on the spec) -- found building this file, a
    # small but real response-key inconsistency: id_field's declared alias
    # governs the URL path parameter correctly, but not this response body key.
    assert created["label"] == node_label

    if spec.secondary_tables:
        # PATCH a real secondary-table field, not change_origin -- this is
        # the branch that did not exist before Wave 3(b) and is the one
        # this file's own 6-of-9 population exists to prove: handle_patch's
        # graph branch routes non-change_origin fields through
        # upsert_secondary_tables, never touching kg_nodes at all for them.
        sec = spec.secondary_tables[0]
        async with pg_pool.acquire() as conn:
            columns = await _column_info(conn, sec.table_name)
        # sec.fields may include a column (e.g. "node_type", auto-injected
        # by handle_create only, never client-writable) that is NOT in
        # spec.writable_fields -- handle_patch silently drops anything
        # outside writable_fields before it ever reaches
        # upsert_secondary_tables, so patch_field must come from the
        # intersection, not sec.fields alone. Found running this file: a
        # first cut picked "node_type" for CABLE and the PATCH silently
        # no-opped (200, unchanged column) for exactly this reason.
        patch_field = next(
            f
            for f in sec.fields
            if f in spec.writable_fields
            and f not in _ENUM_CHECK_SKIP.get(sec.table_name, set())
            and columns.get(f, {}).get("data_type") in _TEXT_TYPES
        )
        new_value = "patched_secondary_value"
        target_table = sec.table_name
        join_field = sec.join_field
    else:
        # The 3 change_origin-only specs: nothing else to patch, and this
        # IS the real generated contract for them, not a gap in this test.
        patch_field = "change_origin"
        new_value = "operator"
        target_table = "kg_nodes"
        join_field = "label"

    previous_engine = admin_state.engine
    admin_state.engine = engine
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_rest_app(spec)), base_url="http://test"
        ) as client:
            patch_resp = await client.patch(
                spec.rest_item_path.format(id=node_label),
                json={"namespace_id": str(namespace_id), patch_field: new_value},
            )
    finally:
        admin_state.engine = previous_engine

    spec_id = f"{spec.engine}:{spec.entity}"
    assert patch_resp.status_code == 200, (
        f"{spec_id} PATCH failed against real Postgres: {patch_resp.text}"
    )

    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT {patch_field} FROM {target_table} WHERE {join_field} = $1 "
            f"AND namespace_id = $2",
            node_label,
            namespace_id,
        )
    assert row is not None, (
        f"{spec_id}: {target_table} row for {node_label!r} not found after PATCH"
    )
    stored = row[patch_field]
    stored_compare = float(stored) if isinstance(stored, Decimal) else str(stored)
    expected_compare = float(new_value) if isinstance(stored, Decimal) else str(new_value)
    assert stored_compare == expected_compare, (
        f"{spec_id}: PATCH returned 200 but {target_table}.{patch_field} was not "
        f"actually updated in Postgres (expected {new_value!r}, got {stored!r})."
    )


@pytest.mark.asyncio
async def test_positive_control_unowned_node_type_is_denied(
    engine: NCEEngine, pg_pool: asyncpg.Pool, namespace_id: uuid.UUID
) -> None:
    """U18: proves the ownership gate this fixture seeds around is real,
    not vacuous -- a synthetic spec whose node_type has no
    node-ownership.json entry at all must be denied even with the registry
    fully seeded, since deny-by-default means "no row", not "any row"."""
    import dataclasses

    real_spec = _ALL_SPECS[("project", "projects")]
    unowned_spec = dataclasses.replace(real_spec, node_type="PROBE_UNOWNED_NODE_TYPE")

    previous_engine = admin_state.engine
    admin_state.engine = engine
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_rest_app(unowned_spec)), base_url="http://test"
        ) as client:
            resp = await client.post(
                unowned_spec.rest_collection_path,
                json={
                    "namespace_id": str(namespace_id),
                    unowned_spec.id_field: f"probe-unowned-{uuid.uuid4().hex[:8]}",
                    "change_origin": "agent",
                },
            )
    finally:
        admin_state.engine = previous_engine

    assert resp.status_code == 403, (
        f"An unowned node_type must be denied (403) even with the ownership "
        f"registry seeded for every REAL node_type -- got {resp.status_code}: "
        f"{resp.text}. If this now passes, either PROBE_UNOWNED_NODE_TYPE "
        f"collided with a real entry or assert_owner stopped being deny-by-default."
    )
