"""
tests/integration/test_resource_surface_kg_nodes_primary_live.py
==================================================================
Live-Postgres verification of kg_nodes-primary ``ResourceSpec`` support
(Wave 3(b), 2026-09-20) -- the generated read/write path for
``tenant_scope == "graph"`` specs (``table_name=None``), targeting
DEVICE/PORT/RACK/CABLE (system_design). Before this wave every handler in
mcp.py/rest.py returned an unconditional 501 for this branch; Wave 1
(multi-table ``SecondaryTable`` support, #304) built the satellite-table
write machinery but explicitly did not wire it to a graph primary.

WHAT THE PRIMARY ROW ACTUALLY IS
---------------------------------
kg_nodes has no attribute column of its own -- confirmed by reading every
existing consumer before writing a line of this wave's code (case_study.py:
`INSERT INTO kg_nodes (label, entity_type, namespace_id)`, nothing else,
ever; tasks.py's own comment: "the graph has no free-text status column on
kg_nodes"; fl_tree.py's `_format_node_dict` derives name/kind/as_built by
parsing `label`/`change_origin`, not from stored fields). So the kg_nodes
row this wave writes is deliberately thin: label, entity_type,
change_origin, timestamps. Every real field routes to a secondary table via
the same ``upsert_secondary_tables()`` Wave 1 built -- this wave's own
contribution is creating/reading the kg_nodes identity row first (every
system_design satellite table carries a real FK to
kg_nodes(label, namespace_id): fk_sddc_kg_nodes, fk_sdns_kg_nodes,
fk_sdg_kg_nodes) and wiring that identity row into list/get/create/patch.

TABLE-TO-TYPE MAPPING (verified against schema.sql, not assumed)
-------------------------------------------------------------------
- DEVICE: system_design_device_capabilities (AV/PoE fields) AND
  system_design_node_state (status/revision/salience).
- PORT: system_design_device_capabilities ONLY (port_direction) --
  system_design_node_state's own CHECK constraint
  (system_design_node_state_status_per_node_type) is
  ``CASE node_type WHEN 'DEVICE' ... WHEN 'CABLE' ... WHEN 'RACK' ... ELSE
  FALSE END`` -- PORT is not in it, so a PORT row there is structurally
  impossible, not merely undeclared.
- RACK / CABLE: system_design_node_state ONLY -- neither has AV capability
  columns.

REAL BUG FOUND AND FIXED WHILE BUILDING THIS, NOT REASONED ABOUT
--------------------------------------------------------------------
``requires_namespace`` (both backends) was ``is_tenant or enabled_guard is
not None`` -- never included graph-scoped specs, because no graph-primary
spec ever had a real read/write path before this wave (the whole branch was
a 501, so it never mattered). kg_nodes rows ARE namespace-scoped
(``UNIQUE (label, namespace_id)``, schema.sql), so a graph-primary spec is
exactly as namespace-bound as a tenant one. Without the fix, a caller
omitting ``namespace_id`` gets ``ns_uuid=None``, which binds
``WHERE namespace_id = NULL`` -- zero rows back, SILENTLY, instead of the
422 every other namespace-scoped spec gives. Fixed to
``is_tenant or is_graph or enabled_guard is not None``;
test_mcp_list_requires_namespace_id / test_rest_list_requires_namespace_id
below prove the fixed behaviour directly.

WHAT STAYS EXPLICITLY REFUSED THIS WAVE, AND WHY (not oversights)
------------------------------------------------------------------
- Archive/restore: kg_nodes and every system_design satellite table have no
  generic soft-delete/is_archived column anywhere. Inventing one (e.g.
  overloading node_state.status) is a system_design content decision.
- Bulk create: the per-item loop already gates its real INSERT on
  spec.table_name truthy and falls back to the in-memory mock bucket
  otherwise -- refusing gk_nodes-primary bulk up front prevents a silent
  wrong-backend write in production. Bulk-creating a kg_nodes identity row
  PLUS N secondary rows per item also raises a genuine partial-failure
  question (item 3 of 10 fails: roll back all, or report partial?) with no
  precedent in this generator, left for whoever builds it.
- system_design_geometry is not wired as a SecondaryTable for RACK/CABLE
  (rack_position, cable_length_m, etc.) in this wave: its writer,
  geometry.py's validate_geometry(), enforces half-U rack_position
  granularity, an exact-precision float-max bound, and rack_face/cable_type
  vocabularies that no DB CHECK captures -- its own docstring states the
  validation lives in exactly one place "so a caller that reaches this
  function directly ... cannot route around it. One place, not two." A
  generic SecondaryTable write has no validation hook (SecondaryTable's own
  docstring now documents this restriction) -- adding rack/cable geometry
  fields here would be a second, unvalidated path into that table.

NINE 501 GUARDS DELETED, NOT "GIVEN KG_NODES SUPPORT" -- THEY WERE DEAD
--------------------------------------------------------------------------
handle_events, handle_list_comments/add_comment, handle_list_tags/add_tag/
remove_tag, handle_list_documents/attach_document/detach_document never
touched spec.table_name at all: events/comments/tags are keyed on
engine/entity/item_id against v3_cognitive_ledger or event_log; documents
route through nce.vertical_modules.documents.service keyed on
spec.node_type + item_id. The kg_nodes-or-graph guard on these was
copy-pasted from the primary-CRUD handlers where it belongs and was never
reachable-but-wrong there -- it was just always wrong. Removing it is
deletion of dead code, not new capability; test_comments_work_for_a_kg_nodes_primary_spec
below proves one of the nine actually works now, not just that it stopped
501ing.
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import patch

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
from nce.resource_surface.mcp import build_mcp_tool_specs
from nce.resource_surface.rest import make_resource_routes
from nce.resource_surface.spec import ResourceSpec, SecondaryTable
from tests._verified_tier_middleware import VERIFIED_TIER_TEST_MIDDLEWARE

pytestmark = pytest.mark.integration

DEVICE_PROBE_SPEC = ResourceSpec(
    engine="system_design",
    entity="devices-kgprimary-probe",
    node_type="DEVICE",
    table_name=None,
    id_field="node_label",
    version_field="updated_at",
    soft_delete_field=None,
    writable_fields=(
        "change_origin",
        "signal_format",
        "manufacturer",
        "status",
        "revision",
        "salience",
    ),
    secondary_tables=(
        SecondaryTable(
            table_name="system_design_device_capabilities",
            join_field="node_label",
            fields=("signal_format", "manufacturer"),
        ),
        SecondaryTable(
            table_name="system_design_node_state",
            join_field="node_label",
            fields=("node_type", "status", "revision", "salience"),
        ),
    ),
    description="Test-only probe spec, never registered in the global registry.",
)

PORT_PROBE_SPEC = ResourceSpec(
    engine="system_design",
    entity="ports-kgprimary-probe",
    node_type="PORT",
    table_name=None,
    id_field="node_label",
    soft_delete_field=None,
    writable_fields=("change_origin", "port_direction"),
    secondary_tables=(
        SecondaryTable(
            table_name="system_design_device_capabilities",
            join_field="node_label",
            fields=("port_direction",),
        ),
    ),
    description="Test-only probe spec. PORT deliberately has no node_state "
    "SecondaryTable -- that table's own CHECK constraint refuses PORT rows.",
)

RACK_PROBE_SPEC = ResourceSpec(
    engine="system_design",
    entity="racks-kgprimary-probe",
    node_type="RACK",
    table_name=None,
    id_field="node_label",
    soft_delete_field=None,
    writable_fields=("change_origin", "status", "revision", "salience"),
    secondary_tables=(
        SecondaryTable(
            table_name="system_design_node_state",
            join_field="node_label",
            fields=("node_type", "status", "revision", "salience"),
        ),
    ),
    description="Test-only probe spec, never registered in the global registry.",
)

CABLE_PROBE_SPEC = ResourceSpec(
    engine="system_design",
    entity="cables-kgprimary-probe",
    node_type="CABLE",
    table_name=None,
    id_field="node_label",
    soft_delete_field=None,
    writable_fields=("change_origin", "status", "revision", "salience"),
    secondary_tables=(
        SecondaryTable(
            table_name="system_design_node_state",
            join_field="node_label",
            fields=("node_type", "status", "revision", "salience"),
        ),
    ),
    description="Test-only probe spec, never registered in the global registry.",
)


@pytest_asyncio.fixture
async def engine(pg_pool: asyncpg.Pool, namespace_id: uuid.UUID) -> NCEEngine:
    """Real NCEEngine over the live pool, with the ownership registry seeded.

    Fixed 2026-09-20: kg_nodes-primary create/patch now calls assert_owner
    (deny-by-default) before writing, matching every hand-written kg_nodes
    writer (system_design/devices.py, project/convert.py, ...) -- a real
    gap found and closed after Wave 3(b) shipped without it. DEVICE/PORT/
    RACK/CABLE all really are owned by system_design in
    nce/config_data/node-ownership.json, so seeding here is not a synthetic
    allowance -- it is what production already grants this engine.
    """
    eng = NCEEngine()
    eng.pg_pool = pg_pool
    populate_engine_modules(eng)
    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, namespace_id)
            await seed_node_ownership_registry(conn, namespace_id)
    return eng


def _rest_client(engine: NCEEngine, spec: ResourceSpec) -> httpx.AsyncClient:
    admin_state.engine = engine
    app = Starlette(routes=make_resource_routes(spec), middleware=VERIFIED_TIER_TEST_MIDDLEWARE)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_device_mcp_upsert_then_get_merges_both_tables(
    engine: NCEEngine, namespace_id: uuid.UUID
) -> None:
    """One upsert on a kg_nodes-primary DEVICE spec must: create the
    kg_nodes identity row, write AV fields to device_capabilities, write
    status/revision/salience to node_state -- and GET must merge all three
    back into one response.
    """
    tools = build_mcp_tool_specs(DEVICE_PROBE_SPEC)
    node_label = f"probe-kg-device-{uuid.uuid4().hex[:8]}"

    upsert = tools[f"system_design_upsert_{DEVICE_PROBE_SPEC.mcp_slug}"]
    result = json.loads(
        await upsert.handler(
            engine,
            {
                "namespace_id": str(namespace_id),
                "id": node_label,
                "signal_format": "HDMI 2.1",
                "manufacturer": "TestCorp",
                "status": "planned",
                "revision": "rev-1",
                "salience": 0.5,
            },
        )
    )
    assert result["status"] == "ok", result

    async with engine.pg_pool.acquire() as conn:
        kg_row = await conn.fetchrow(
            "SELECT entity_type, change_origin FROM kg_nodes WHERE namespace_id = $1 AND label = $2",
            namespace_id,
            node_label,
        )
        assert kg_row is not None, "kg_nodes identity row was not created"
        assert kg_row["entity_type"] == "DEVICE"

        cap_row = await conn.fetchrow(
            "SELECT signal_format, manufacturer FROM system_design_device_capabilities "
            "WHERE namespace_id = $1 AND node_label = $2",
            namespace_id,
            node_label,
        )
        assert cap_row is not None, "device_capabilities row was not written"
        assert cap_row["signal_format"] == "HDMI 2.1"

        state_row = await conn.fetchrow(
            "SELECT node_type, status, revision, salience FROM system_design_node_state "
            "WHERE namespace_id = $1 AND node_label = $2",
            namespace_id,
            node_label,
        )
        assert state_row is not None, "node_state row was not written"
        assert state_row["node_type"] == "DEVICE"
        assert state_row["status"] == "planned"

    get_tool = tools[f"system_design_get_{DEVICE_PROBE_SPEC.mcp_slug}"]
    fetched = json.loads(
        await get_tool.handler(engine, {"namespace_id": str(namespace_id), "id": node_label})
    )
    assert fetched["signal_format"] == "HDMI 2.1"
    assert fetched["manufacturer"] == "TestCorp"
    assert fetched["status"] == "planned"
    assert fetched["revision"] == "rev-1"
    assert fetched["entity_type"] == "DEVICE"


@pytest.mark.asyncio
async def test_device_rest_create_then_get(engine: NCEEngine, namespace_id: uuid.UUID) -> None:
    node_label = f"probe-kg-device-rest-{uuid.uuid4().hex[:8]}"
    async with _rest_client(engine, DEVICE_PROBE_SPEC) as client:
        r1 = await client.post(
            "/api/system_design/devices-kgprimary-probe",
            json={
                "namespace_id": str(namespace_id),
                "node_label": node_label,
                "signal_format": "DisplayPort",
                "status": "staged",
            },
        )
        assert r1.status_code == 201, r1.text

        r2 = await client.get(
            f"/api/system_design/devices-kgprimary-probe/{node_label}",
            params={"namespace_id": str(namespace_id)},
            headers={"X-NCE-Principal-Tier": "employee"},
        )
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["signal_format"] == "DisplayPort"
    assert body["status"] == "staged"


@pytest.mark.asyncio
async def test_device_rest_patch_updates_change_origin_and_secondary(
    engine: NCEEngine, namespace_id: uuid.UUID
) -> None:
    """PATCH must route `status` to node_state and `change_origin` to the
    kg_nodes identity row itself -- the ONE real writable column kg_nodes
    has beyond identity.
    """
    node_label = f"probe-kg-device-patch-{uuid.uuid4().hex[:8]}"
    async with _rest_client(engine, DEVICE_PROBE_SPEC) as client:
        r1 = await client.post(
            "/api/system_design/devices-kgprimary-probe",
            json={"namespace_id": str(namespace_id), "node_label": node_label, "status": "planned"},
        )
        assert r1.status_code == 201, r1.text

        r2 = await client.patch(
            f"/api/system_design/devices-kgprimary-probe/{node_label}",
            json={
                "namespace_id": str(namespace_id),
                "status": "active",
                "change_origin": "operator",
            },
        )
    assert r2.status_code == 200, r2.text

    async with engine.pg_pool.acquire() as conn:
        kg_row = await conn.fetchrow(
            "SELECT change_origin FROM kg_nodes WHERE namespace_id = $1 AND label = $2",
            namespace_id,
            node_label,
        )
        state_row = await conn.fetchrow(
            "SELECT status FROM system_design_node_state WHERE namespace_id = $1 AND node_label = $2",
            namespace_id,
            node_label,
        )
    assert kg_row["change_origin"] == "operator"
    assert state_row["status"] == "active"


@pytest.mark.asyncio
async def test_device_rest_patch_first_touch_of_secondary_table(
    engine: NCEEngine, namespace_id: uuid.UUID
) -> None:
    """GRAPH branch (`if is_graph:`, DEVICE_PROBE_SPEC has table_name=None).
    A separate test exercises the relational branch with the same shape:
    tests/integration/test_resource_surface_multi_table_live.py::
    test_rest_patch_first_touch_of_secondary_table_still_satisfies_node_type_not_null
    -- the two share the same underlying bug but are genuinely different
    code paths (`if is_graph:` vs `elif spec.table_name:` inside
    handle_patch), each needing its own fix and its own proof. An earlier
    version of this fix was verified only against the relational test
    above and shipped believing it covered both; it did not -- this test
    exists specifically because the failure at that mismatch produced a
    500 on `main` that would have gone uncaught.

    UPDATED -- what this test proves changed with the phantom-row fix
    (handle_create's graph branch is now gated the same conservative way
    as handle_patch's, see rest.py's own comment there). Before that fix,
    handle_create injected node_type unconditionally whenever it was
    declared on any secondary table, so the CREATE below (only
    `signal_format` supplied, routed to the *capabilities* table) would
    have pre-seeded a `system_design_node_state` row anyway, making the
    PATCH below an UPDATE rather than the first-ever INSERT this test's
    name describes -- this test could not have told the difference, and
    an earlier version of this docstring said so. Now that CREATE only
    writes a state row when a real state key is supplied, the CREATE below
    (no status/revision/salience) leaves NO row at all, so the PATCH IS
    genuinely the first write -- this test now proves what its name always
    claimed. See test_device_rest_create_gates_node_state_on_a_real_state_key
    below for the direct create-side proof of the same fix.
    """
    node_label = f"probe-kg-device-first-touch-{uuid.uuid4().hex[:8]}"
    async with _rest_client(engine, DEVICE_PROBE_SPEC) as client:
        r1 = await client.post(
            "/api/system_design/devices-kgprimary-probe",
            json={
                "namespace_id": str(namespace_id),
                "node_label": node_label,
                "signal_format": "DisplayPort",
                # No status/revision/salience -- leaves no node_state row at
                # all now (see docstring above), so the PATCH below really
                # is node_state's first-ever write for this node.
            },
        )
        assert r1.status_code == 201, r1.text

        r2 = await client.patch(
            f"/api/system_design/devices-kgprimary-probe/{node_label}",
            json={"namespace_id": str(namespace_id), "status": "planned"},
        )
    assert r2.status_code == 200, r2.text

    async with engine.pg_pool.acquire() as conn:
        state_row = await conn.fetchrow(
            "SELECT node_type, status FROM system_design_node_state "
            "WHERE namespace_id = $1 AND node_label = $2",
            namespace_id,
            node_label,
        )
    assert state_row is not None
    assert state_row["node_type"] == "DEVICE"
    assert state_row["status"] == "planned"


@pytest.mark.asyncio
async def test_rest_patch_by_true_kg_nodes_id_resolves_to_the_correct_row(
    engine: NCEEngine, namespace_id: uuid.UUID
) -> None:
    """Pins REST's `(label = $3 OR id::text = $3)` leniency in `handle_get`/
    `handle_patch` against kg_nodes' REAL surrogate id -- never exercised
    before this test. Every other test in this file that supplies an
    "id"-shaped value gets it from a GET response, and GET's own response
    merges each secondary table's row over kg_nodes' (`item.update(
    row_to_dict(sec_row))`), which for DEVICE_PROBE_SPEC silently replaces
    kg_nodes' real `id` with `system_design_device_capabilities.id` instead
    (found while measuring IDENTIFIER_ACCEPTANCE_MATRIX_F.md) -- so no
    existing test could have exercised the leniency against a value known
    to be correct. This one reads kg_nodes.id directly from the database,
    bypassing GET entirely, to answer the question the matrix left open.

    Measured live before writing this assertion: PATCHing by the true id
    resolves to the single existing row (200, no duplicate created) -- the
    leniency clause is correct, just never proven. If this regresses, the
    correct fix is almost certainly in `handle_get`'s response (stop a
    secondary table's own `id` column from shadowing kg_nodes' real one),
    not in this clause.
    """
    node_label = f"probe-kg-device-trueid-{uuid.uuid4().hex[:8]}"
    async with _rest_client(engine, DEVICE_PROBE_SPEC) as client:
        r1 = await client.post(
            "/api/system_design/devices-kgprimary-probe",
            json={
                "namespace_id": str(namespace_id),
                "node_label": node_label,
                "signal_format": "A",
            },
        )
        assert r1.status_code == 201, r1.text

        async with engine.pg_pool.acquire() as conn:
            true_id = await conn.fetchval(
                "SELECT id FROM kg_nodes WHERE namespace_id = $1 AND label = $2",
                namespace_id,
                node_label,
            )
        assert true_id is not None

        r2 = await client.patch(
            f"/api/system_design/devices-kgprimary-probe/{true_id}",
            json={"namespace_id": str(namespace_id), "status": "planned"},
        )
    assert r2.status_code == 200, (
        f"PATCH by kg_nodes' true surrogate id was refused: {r2.text} -- the "
        f"(label = $3 OR id::text = $3) leniency did not find the row"
    )

    async with engine.pg_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, label FROM kg_nodes WHERE namespace_id = $1 AND entity_type = 'DEVICE'",
            namespace_id,
        )
    assert len(rows) == 1, (
        f"expected exactly 1 kg_nodes row, found {len(rows)}: {[dict(r) for r in rows]} -- "
        f"PATCH by true id must resolve to the SAME row, not create a second one"
    )
    assert rows[0]["id"] == true_id
    assert rows[0]["label"] == node_label


@pytest.mark.asyncio
async def test_device_rest_create_gates_node_state_on_a_real_state_key(
    engine: NCEEngine, namespace_id: uuid.UUID
) -> None:
    """A bare create -- no status/revision/salience supplied -- must leave
    the node with NO system_design_node_state row at all, not a
    node_type-only row.

    Before this fix, handle_create's graph branch injected node_type
    unconditionally whenever it was declared on any of the spec's
    secondary tables, regardless of whether the caller supplied any other
    node_state field -- creating exactly the row read.py's own module
    docstring says must never happen by other means: a node silently
    moved from "no row" (nothing declared) to "row, status NULL" (data
    held, no lifecycle declared), the two facts do_get_topology is built
    and tested to keep apart. Two independent surfaces would have observed
    it: do_get_topology's own `state` map (a present key with null values
    instead of an absent key) and this generic surface's own handle_get
    (row_to_dict emits every column, so the secondary row's mere existence
    changes the response shape).

    A create that DOES supply a state key must still get a real row, with
    node_type populated -- this is the half that keeps #394/#396's NOT
    NULL fix from regressing: node_type is never in writable_fields, so it
    must still be injected whenever a real write to node_state happens.
    """
    bare_label = f"probe-kg-device-bare-create-{uuid.uuid4().hex[:8]}"
    stated_label = f"probe-kg-device-stated-create-{uuid.uuid4().hex[:8]}"
    async with _rest_client(engine, DEVICE_PROBE_SPEC) as client:
        r_bare = await client.post(
            "/api/system_design/devices-kgprimary-probe",
            json={
                "namespace_id": str(namespace_id),
                "node_label": bare_label,
                "signal_format": "DisplayPort",
                # No status/revision/salience -- must leave node_state untouched.
            },
        )
        assert r_bare.status_code == 201, r_bare.text

        r_stated = await client.post(
            "/api/system_design/devices-kgprimary-probe",
            json={
                "namespace_id": str(namespace_id),
                "node_label": stated_label,
                "signal_format": "HDMI 2.1",
                "status": "planned",
            },
        )
        assert r_stated.status_code == 201, r_stated.text

    async with engine.pg_pool.acquire() as conn:
        bare_row = await conn.fetchrow(
            "SELECT 1 FROM system_design_node_state WHERE namespace_id = $1 AND node_label = $2",
            namespace_id,
            bare_label,
        )
        stated_row = await conn.fetchrow(
            "SELECT node_type, status FROM system_design_node_state "
            "WHERE namespace_id = $1 AND node_label = $2",
            namespace_id,
            stated_label,
        )
    assert bare_row is None, (
        "a bare create (no state key supplied) wrote a system_design_node_state "
        "row anyway -- the exact phantom-row shape this fix removes"
    )
    assert stated_row is not None, "a create supplying a state key wrote no row at all"
    assert stated_row["node_type"] == "DEVICE"
    assert stated_row["status"] == "planned"


@pytest.mark.asyncio
async def test_device_mcp_upsert_gates_node_state_on_a_real_state_key(
    engine: NCEEngine, namespace_id: uuid.UUID
) -> None:
    """MCP counterpart of test_device_rest_create_gates_node_state_on_a_real_state_key
    above -- same gate, same shared reasoning, but a genuinely independent
    code path: mcp.py's handle_upsert graph branch builds its own
    INSERT/UPDATE against system_design_node_state, never sharing SQL
    construction with rest.py's handle_create (established while mapping
    archive/restore coverage for #394/#396 -- a fix or a regression on one
    side says nothing about the other).
    """
    tools = build_mcp_tool_specs(DEVICE_PROBE_SPEC)
    upsert = tools[f"system_design_upsert_{DEVICE_PROBE_SPEC.mcp_slug}"]
    bare_label = f"probe-kg-device-mcp-bare-{uuid.uuid4().hex[:8]}"
    stated_label = f"probe-kg-device-mcp-stated-{uuid.uuid4().hex[:8]}"

    bare_result = json.loads(
        await upsert.handler(
            engine,
            {
                "namespace_id": str(namespace_id),
                "id": bare_label,
                "signal_format": "DisplayPort",
                # No status/revision/salience -- must leave node_state untouched.
            },
        )
    )
    assert bare_result["status"] == "ok", bare_result

    stated_result = json.loads(
        await upsert.handler(
            engine,
            {
                "namespace_id": str(namespace_id),
                "id": stated_label,
                "signal_format": "HDMI 2.1",
                "status": "planned",
            },
        )
    )
    assert stated_result["status"] == "ok", stated_result

    async with engine.pg_pool.acquire() as conn:
        bare_row = await conn.fetchrow(
            "SELECT 1 FROM system_design_node_state WHERE namespace_id = $1 AND node_label = $2",
            namespace_id,
            bare_label,
        )
        stated_row = await conn.fetchrow(
            "SELECT node_type, status FROM system_design_node_state "
            "WHERE namespace_id = $1 AND node_label = $2",
            namespace_id,
            stated_label,
        )
    assert bare_row is None, (
        "a bare upsert (no state key supplied) wrote a system_design_node_state "
        "row anyway -- the exact phantom-row shape this fix removes"
    )
    assert stated_row is not None, "an upsert supplying a state key wrote no row at all"
    assert stated_row["node_type"] == "DEVICE"
    assert stated_row["status"] == "planned"


@pytest.mark.asyncio
async def test_device_rest_patch_capabilities_only_field_does_not_create_phantom_node_state(
    engine: NCEEngine, namespace_id: uuid.UUID
) -> None:
    """THE regression that hid #394's own defect, found live by F while
    verifying #400: a bare create (no state key, so no node_state row
    exists), then a PATCH that touches ONLY a capabilities field
    (`manufacturer`) -- never a node_state field. Before
    compute_secondary_write_data existed, `sec_updates` was built from the
    COMBINED dict across both of DEVICE's secondary tables (capabilities +
    node_state); `manufacturer` alone made that combined dict non-empty,
    the aggregate-level gate fired, node_type got injected into the same
    combined dict, and upsert_secondary_tables' own per-table filter then
    picked node_type back out for node_state -- writing a node_type-only
    phantom row to a table this PATCH never touched at all. Confirmed live
    on `main` with #394 merged and #400 not yet applied (F, 2026-09-21):
    this exact PATCH reproduces the phantom row on today's `main`.

    No prior test caught this because every existing PATCH test either
    supplies a real node_state field (masking the leak, since node_state
    would have gotten a row anyway) or supplies nothing at all (no leak to
    have). This is the one combination in between.
    """
    node_label = f"probe-kg-device-caps-only-patch-{uuid.uuid4().hex[:8]}"
    async with _rest_client(engine, DEVICE_PROBE_SPEC) as client:
        r1 = await client.post(
            "/api/system_design/devices-kgprimary-probe",
            json={
                "namespace_id": str(namespace_id),
                "node_label": node_label,
                "signal_format": "DisplayPort",
                # No status/revision/salience -- leaves no node_state row.
            },
        )
        assert r1.status_code == 201, r1.text

        r2 = await client.patch(
            f"/api/system_design/devices-kgprimary-probe/{node_label}",
            json={"namespace_id": str(namespace_id), "manufacturer": "TestCorp"},
        )
    assert r2.status_code == 200, r2.text

    async with engine.pg_pool.acquire() as conn:
        state_row = await conn.fetchrow(
            "SELECT 1 FROM system_design_node_state WHERE namespace_id = $1 AND node_label = $2",
            namespace_id,
            node_label,
        )
    assert state_row is None, (
        "a PATCH touching only a capabilities field wrote a phantom "
        "system_design_node_state row anyway -- the exact aggregate-"
        "granularity defect this fix removes"
    )


@pytest.mark.asyncio
async def test_device_mcp_upsert_capabilities_only_field_does_not_create_phantom_node_state(
    engine: NCEEngine, namespace_id: uuid.UUID
) -> None:
    """MCP counterpart of test_device_rest_patch_capabilities_only_field_
    does_not_create_phantom_node_state -- mcp.py's handle_upsert graph
    branch shares compute_secondary_write_data with rest.py, but the
    surrounding INSERT/UPDATE and existing-row lookup are independent code,
    so this is proven on its own rather than assumed from the REST result.
    """
    tools = build_mcp_tool_specs(DEVICE_PROBE_SPEC)
    upsert = tools[f"system_design_upsert_{DEVICE_PROBE_SPEC.mcp_slug}"]
    node_label = f"probe-kg-device-mcp-caps-only-{uuid.uuid4().hex[:8]}"

    bare_result = json.loads(
        await upsert.handler(
            engine,
            {
                "namespace_id": str(namespace_id),
                "id": node_label,
                "signal_format": "DisplayPort",
                # No status/revision/salience -- leaves no node_state row.
            },
        )
    )
    assert bare_result["status"] == "ok", bare_result

    caps_only_result = json.loads(
        await upsert.handler(
            engine,
            {
                "namespace_id": str(namespace_id),
                "id": node_label,
                "manufacturer": "TestCorp",
                # Still no status/revision/salience -- a second upsert
                # touching only capabilities must not create node_state.
            },
        )
    )
    assert caps_only_result["status"] == "ok", caps_only_result

    async with engine.pg_pool.acquire() as conn:
        state_row = await conn.fetchrow(
            "SELECT 1 FROM system_design_node_state WHERE namespace_id = $1 AND node_label = $2",
            namespace_id,
            node_label,
        )
    assert state_row is None, (
        "an upsert touching only a capabilities field wrote a phantom "
        "system_design_node_state row anyway -- the exact aggregate-"
        "granularity defect this fix removes"
    )


@pytest.mark.asyncio
async def test_rest_patch_rejects_a_version_that_predates_a_kg_nodes_only_write(
    engine: NCEEngine, namespace_id: uuid.UUID
) -> None:
    """A concurrency check must compare against the row it actually
    protects. Before this fix, `handle_get`/`handle_patch` merged each
    secondary table's row over kg_nodes' own (`item.update(row_to_dict(
    sec_row))`), which replaces kg_nodes' real `updated_at` with a
    secondary table's -- every graph-primary spec with a secondary table
    and `version_field="updated_at"` (6 of 9) inherited this. The
    sequence below is the ordinary, correct way to use optimistic
    concurrency -- GET, then PATCH with exactly what GET returned -- and
    it must be rejected here, because kg_nodes changed in between.

    Reproduced live before this fix: this exact sequence returned 200 and
    silently applied the write, because the version check compared the
    caller's (shadowed) value against a freshly-recomputed copy of the
    SAME shadowed value, never against kg_nodes' real, already-advanced
    clock -- a lost update with no error anywhere.
    """
    node_label = f"probe-kg-device-version-{uuid.uuid4().hex[:8]}"
    async with _rest_client(engine, DEVICE_PROBE_SPEC) as client:
        r1 = await client.post(
            "/api/system_design/devices-kgprimary-probe",
            json={
                "namespace_id": str(namespace_id),
                "node_label": node_label,
                "signal_format": "A",
            },
        )
        assert r1.status_code == 201, r1.text

        # A kg_nodes-only write -- the one thing that advances kg_nodes'
        # own clock without touching any secondary table.
        r2 = await client.patch(
            f"/api/system_design/devices-kgprimary-probe/{node_label}",
            json={"namespace_id": str(namespace_id), "change_origin": "operator"},
        )
        assert r2.status_code == 200, r2.text

        got = (
            await client.get(
                f"/api/system_design/devices-kgprimary-probe/{node_label}",
                params={"namespace_id": str(namespace_id)},
                headers={"X-NCE-Principal-Tier": "employee"},
            )
        ).json()

        async with engine.pg_pool.acquire() as conn:
            true_updated_at = await conn.fetchval(
                "SELECT updated_at FROM kg_nodes WHERE namespace_id = $1 AND label = $2",
                namespace_id,
                node_label,
            )
        assert str(got["updated_at"]).replace("T", " ") == str(true_updated_at), (
            "test fixture assumption broken: GET's 'updated_at' must reflect kg_nodes' "
            "real clock for this reproduction to mean anything"
        )

        r3 = await client.patch(
            f"/api/system_design/devices-kgprimary-probe/{node_label}",
            json={
                "namespace_id": str(namespace_id),
                "expected_version": got["updated_at"],
                "status": "planned",
            },
        )
    assert r3.status_code == 200, (
        f"a version taken from GET immediately after PATCH was refused as stale: {r3.text}"
    )

    # Now the real check: a SECOND writer who read the node BEFORE the
    # change_origin PATCH, and only now sends their write, must be
    # rejected -- not silently applied.
    async with _rest_client(engine, DEVICE_PROBE_SPEC) as client:
        r4 = await client.patch(
            f"/api/system_design/devices-kgprimary-probe/{node_label}",
            json={
                "namespace_id": str(namespace_id),
                "expected_version": r1.json()["updated_at"],
                "status": "should-not-land",
            },
        )
    assert r4.status_code == 409, (
        f"a PATCH using a version from before a kg_nodes-only write was NOT rejected: "
        f"{r4.status_code} {r4.text} -- this is a silent lost update"
    )

    async with engine.pg_pool.acquire() as conn:
        state_row = await conn.fetchrow(
            "SELECT status FROM system_design_node_state WHERE namespace_id = $1 AND node_label = $2",
            namespace_id,
            node_label,
        )
    assert state_row["status"] == "planned", "the rejected write must not have landed"


@pytest.mark.asyncio
async def test_rest_get_then_immediate_patch_of_a_secondary_field_still_succeeds(
    engine: NCEEngine, namespace_id: uuid.UUID
) -> None:
    """Must not break: the ordinary case, with no concurrent writer at
    all. A caller who creates, GETs, and immediately PATCHes a secondary
    field using exactly the version GET returned must succeed -- this is
    the spurious-409 failure mode a fix to the check above could
    introduce if it compared against the wrong row instead of the right
    one, and only a positive test like this one would catch it.
    """
    node_label = f"probe-kg-device-version-ok-{uuid.uuid4().hex[:8]}"
    async with _rest_client(engine, DEVICE_PROBE_SPEC) as client:
        r1 = await client.post(
            "/api/system_design/devices-kgprimary-probe",
            json={
                "namespace_id": str(namespace_id),
                "node_label": node_label,
                "signal_format": "A",
            },
        )
        assert r1.status_code == 201, r1.text

        got = (
            await client.get(
                f"/api/system_design/devices-kgprimary-probe/{node_label}",
                params={"namespace_id": str(namespace_id)},
                headers={"X-NCE-Principal-Tier": "employee"},
            )
        ).json()

        r2 = await client.patch(
            f"/api/system_design/devices-kgprimary-probe/{node_label}",
            json={
                "namespace_id": str(namespace_id),
                "expected_version": got["updated_at"],
                "status": "planned",
            },
        )
    assert r2.status_code == 200, (
        f"GET-then-immediate-PATCH with the exact version GET returned was rejected: {r2.text}"
    )


@pytest.mark.asyncio
async def test_port_upsert_writes_capabilities_only(
    engine: NCEEngine, namespace_id: uuid.UUID
) -> None:
    """PORT has no node_state SecondaryTable declared -- proves that
    absence is enough: no attempt is ever made to write a row there, so
    the real CHECK constraint (which would refuse a PORT row outright)
    never fires and never needs to.
    """
    tools = build_mcp_tool_specs(PORT_PROBE_SPEC)
    node_label = f"probe-kg-port-{uuid.uuid4().hex[:8]}"

    upsert = tools[f"system_design_upsert_{PORT_PROBE_SPEC.mcp_slug}"]
    result = json.loads(
        await upsert.handler(
            engine,
            {"namespace_id": str(namespace_id), "id": node_label, "port_direction": "input"},
        )
    )
    assert result["status"] == "ok", result

    async with engine.pg_pool.acquire() as conn:
        cap_row = await conn.fetchrow(
            "SELECT port_direction FROM system_design_device_capabilities "
            "WHERE namespace_id = $1 AND node_label = $2",
            namespace_id,
            node_label,
        )
        assert cap_row is not None
        assert cap_row["port_direction"] == "input"

        state_row = await conn.fetchrow(
            "SELECT 1 FROM system_design_node_state WHERE namespace_id = $1 AND node_label = $2",
            namespace_id,
            node_label,
        )
        assert state_row is None, "PORT must never get a node_state row"


@pytest.mark.asyncio
async def test_rack_and_cable_upsert_then_get(engine: NCEEngine, namespace_id: uuid.UUID) -> None:
    """RACK and CABLE share the same table shape (node_state only, no
    device_capabilities) -- proves both independently.
    """
    for spec, expected_status in ((RACK_PROBE_SPEC, "reserved"), (CABLE_PROBE_SPEC, "planned")):
        tools = build_mcp_tool_specs(spec)
        node_label = f"probe-kg-{spec.node_type.lower()}-{uuid.uuid4().hex[:8]}"
        upsert = tools[f"system_design_upsert_{spec.mcp_slug}"]
        result = json.loads(
            await upsert.handler(
                engine,
                {
                    "namespace_id": str(namespace_id),
                    "id": node_label,
                    "status": expected_status,
                },
            )
        )
        assert result["status"] == "ok", result

        get_tool = tools[f"system_design_get_{spec.mcp_slug}"]
        fetched = json.loads(
            await get_tool.handler(engine, {"namespace_id": str(namespace_id), "id": node_label})
        )
        assert fetched["status"] == expected_status
        assert fetched["entity_type"] == spec.node_type


@pytest.mark.asyncio
async def test_get_returns_not_found_for_missing_node(
    engine: NCEEngine, namespace_id: uuid.UUID
) -> None:
    tools = build_mcp_tool_specs(DEVICE_PROBE_SPEC)
    get_tool = tools[f"system_design_get_{DEVICE_PROBE_SPEC.mcp_slug}"]
    fetched = json.loads(
        await get_tool.handler(engine, {"namespace_id": str(namespace_id), "id": "does-not-exist"})
    )
    assert "error" in fetched


@pytest.mark.asyncio
async def test_list_returns_primary_rows_only(engine: NCEEngine, namespace_id: uuid.UUID) -> None:
    """Documented limitation, same as a postgres-primary multi-table spec's
    own list (see SecondaryTable's docstring): list returns kg_nodes
    identity rows, not merged with secondary-table data.
    """
    tools = build_mcp_tool_specs(RACK_PROBE_SPEC)
    node_label = f"probe-kg-rack-list-{uuid.uuid4().hex[:8]}"
    upsert = tools[f"system_design_upsert_{RACK_PROBE_SPEC.mcp_slug}"]
    await upsert.handler(
        engine, {"namespace_id": str(namespace_id), "id": node_label, "status": "available"}
    )

    list_tool = tools[f"system_design_list_{RACK_PROBE_SPEC.mcp_slug}"]
    listed = json.loads(await list_tool.handler(engine, {"namespace_id": str(namespace_id)}))
    matching = [it for it in listed["items"] if it["label"] == node_label]
    assert len(matching) == 1
    assert matching[0]["entity_type"] == "RACK"
    assert "status" not in matching[0], (
        "list must return primary (kg_nodes) rows only -- if this starts "
        "failing because status now appears, the documented limitation "
        "changed and this test (and the docstring citing it) needs updating"
    )


@pytest.mark.asyncio
async def test_mcp_list_requires_namespace_id(engine: NCEEngine) -> None:
    """The requires_namespace fix this wave made: a graph-primary spec
    without an enabled_guard used to have requires_namespace=False (only
    is_tenant was considered), so omitting namespace_id fell through to a
    silent, successful-looking empty list instead of a 422-equivalent
    error. kg_nodes rows are namespace-scoped
    (UNIQUE(label, namespace_id)) exactly like a tenant table.
    """
    tools = build_mcp_tool_specs(DEVICE_PROBE_SPEC)
    list_tool = tools[f"system_design_list_{DEVICE_PROBE_SPEC.mcp_slug}"]
    result = json.loads(await list_tool.handler(engine, {}))
    assert "error" in result, (
        f"omitting namespace_id must be refused, not silently return an "
        f"empty/partial list: got {result}"
    )


@pytest.mark.asyncio
async def test_rest_list_requires_namespace_id(engine: NCEEngine) -> None:
    async with _rest_client(engine, DEVICE_PROBE_SPEC) as client:
        r = await client.get("/api/system_design/devices-kgprimary-probe")
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_archive_refused_with_specific_reason_not_generic_501(
    engine: NCEEngine, namespace_id: uuid.UUID
) -> None:
    """The message must say WHY (no soft-delete column), not the generic
    "storage not supported yet" -- that generic message would be false:
    the storage works fine, there is simply nowhere to put an archived flag.
    """
    async with _rest_client(engine, DEVICE_PROBE_SPEC) as client:
        r = await client.post(
            "/api/system_design/devices-kgprimary-probe/some-label/archive",
            json={"namespace_id": str(namespace_id)},
        )
    assert r.status_code == 501
    assert "soft-delete" in r.text, r.text
    assert "not supported yet for" not in r.text, (
        "must not fall back to the generic (and now false) storage-kind message"
    )


@pytest.mark.asyncio
async def test_restore_refused_with_specific_reason_not_generic_501(
    engine: NCEEngine, namespace_id: uuid.UUID
) -> None:
    """Same shape as archive's own refusal test above -- restore is the
    identical 501 one function down (`rest.py`'s `handle_restore`), but
    nothing had ever pinned its message by name; only archive's had been
    asserted. Found while building BRANCH_COVERAGE_MAP.md.
    """
    async with _rest_client(engine, DEVICE_PROBE_SPEC) as client:
        r = await client.post(
            "/api/system_design/devices-kgprimary-probe/some-label/restore",
            json={"namespace_id": str(namespace_id)},
        )
    assert r.status_code == 501
    assert "soft-delete" in r.text, r.text
    assert "not supported yet for" not in r.text, (
        "must not fall back to the generic (and now false) storage-kind message"
    )


@pytest.mark.asyncio
async def test_bulk_refused_with_specific_reason(
    engine: NCEEngine, namespace_id: uuid.UUID
) -> None:
    async with _rest_client(engine, DEVICE_PROBE_SPEC) as client:
        r = await client.post(
            "/api/system_design/devices-kgprimary-probe/bulk",
            json={"namespace_id": str(namespace_id), "items": [{"node_label": "x"}]},
        )
    assert r.status_code == 501
    assert "partial-failure" in r.text, r.text


@pytest.mark.asyncio
async def test_comments_work_for_a_kg_nodes_primary_spec(
    engine: NCEEngine, namespace_id: uuid.UUID
) -> None:
    """Proves one of the nine deleted dead-guards actually works now, not
    just that it stopped 501ing -- comments are stored in
    v3_cognitive_ledger, keyed on entity_type/item_id, entirely independent
    of spec.table_name, so this was never blocked by anything real.
    """
    node_label = f"probe-kg-device-comment-{uuid.uuid4().hex[:8]}"
    async with _rest_client(engine, DEVICE_PROBE_SPEC) as client:
        r1 = await client.post(
            "/api/system_design/devices-kgprimary-probe",
            json={"namespace_id": str(namespace_id), "node_label": node_label},
        )
        assert r1.status_code == 201, r1.text

        r2 = await client.post(
            f"/api/system_design/devices-kgprimary-probe/{node_label}/comments",
            json={"namespace_id": str(namespace_id), "comment": "verified live"},
        )
        assert r2.status_code == 201, r2.text

        r3 = await client.get(
            f"/api/system_design/devices-kgprimary-probe/{node_label}/comments",
            params={"namespace_id": str(namespace_id)},
        )
    assert r3.status_code == 200, r3.text
    comments = r3.json()
    assert any(c.get("comment") == "verified live" for c in comments.get("comments", comments)), (
        comments
    )


@pytest.mark.asyncio
async def test_positive_control_geometry_is_not_a_secondary_table_target(
    namespace_id: uuid.UUID,
) -> None:
    """Standing guard against the exact regression this wave refused to
    introduce: system_design_geometry must never appear as a SecondaryTable
    target on any of these probe specs (its writer has application-level
    validation this generic mechanism cannot enforce -- see SecondaryTable's
    own docstring).
    """
    for spec in (DEVICE_PROBE_SPEC, PORT_PROBE_SPEC, RACK_PROBE_SPEC, CABLE_PROBE_SPEC):
        table_names = {sec.table_name for sec in spec.secondary_tables}
        assert "system_design_geometry" not in table_names, spec.entity


# ---------------------------------------------------------------------------
# Follow-up (2026-09-20): the generated write path skipped two invariants
# every hand-written kg_nodes writer respects -- assert_owner (deny-by-
# default ownership) and emit_graph_write (outbox event on write). Found
# while reading system_design/devices.py for an unrelated wave (C-6) and
# noticing its own docstring: "Guard every owned-node write with
# assert_owner + emit_graph_write." Neither call existed anywhere in
# resource_surface/rest.py or mcp.py. Fixed in both backends' create/patch
# (rest.py) and upsert (mcp.py) kg_nodes-primary branches; the `engine`
# fixture above now seeds the ownership registry for exactly that reason
# (DEVICE/PORT/RACK/CABLE really are system_design-owned in
# node-ownership.json, so this is not a synthetic test allowance).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rest_create_refused_for_an_unowned_node_type(
    engine: NCEEngine, namespace_id: uuid.UUID
) -> None:
    """Deny-by-default: a node_type with no node-ownership.json row must be
    refused with 403/ownership_denied, not silently written.
    """
    unowned_spec = ResourceSpec(
        engine="system_design",
        entity="unowned-kgprimary-probe",
        node_type="NOT_A_REAL_OWNED_TYPE",
        table_name=None,
        id_field="node_label",
        soft_delete_field=None,
        writable_fields=("change_origin",),
        description="Test-only probe spec for a node_type no engine owns.",
    )
    async with _rest_client(engine, unowned_spec) as client:
        r = await client.post(
            "/api/system_design/unowned-kgprimary-probe",
            json={"namespace_id": str(namespace_id), "node_label": "should-not-be-written"},
        )
    assert r.status_code == 403, r.text
    body = r.json()
    assert body["reason"] == "ownership_denied"

    async with engine.pg_pool.acquire() as conn:
        row = await conn.fetchval(
            "SELECT 1 FROM kg_nodes WHERE namespace_id = $1 AND label = $2",
            namespace_id,
            "should-not-be-written",
        )
    assert row is None, "refused write must not have reached kg_nodes"


@pytest.mark.asyncio
async def test_mcp_upsert_calls_assert_owner_and_emit_graph_write(
    engine: NCEEngine, namespace_id: uuid.UUID
) -> None:
    """Spy on both calls (still calling through to the real implementation)
    to prove the generated write path actually invokes them, not just that
    a real end-to-end write happens to succeed for an unrelated reason.
    """
    from nce.entity_resolution import ownership as ownership_module
    from nce.events import emit as emit_module

    owner_calls: list[tuple[Any, ...]] = []
    emit_calls: list[dict[str, Any]] = []
    real_assert_owner = ownership_module.assert_owner
    real_emit_graph_write = emit_module.emit_graph_write

    async def _owner_spy(*args: Any, **kwargs: Any) -> None:
        owner_calls.append(args)
        return await real_assert_owner(*args, **kwargs)

    async def _emit_spy(*args: Any, **kwargs: Any) -> None:
        emit_calls.append(kwargs)
        return await real_emit_graph_write(*args, **kwargs)

    tools = build_mcp_tool_specs(DEVICE_PROBE_SPEC)
    node_label = f"probe-kg-device-spy-{uuid.uuid4().hex[:8]}"
    with (
        patch("nce.resource_surface.mcp.assert_owner", side_effect=_owner_spy),
        patch("nce.resource_surface.mcp.emit_graph_write", side_effect=_emit_spy),
    ):
        upsert = tools[f"system_design_upsert_{DEVICE_PROBE_SPEC.mcp_slug}"]
        result = json.loads(
            await upsert.handler(
                engine, {"namespace_id": str(namespace_id), "id": node_label, "status": "planned"}
            )
        )

    assert result["status"] == "ok", result
    # assert_owner(conn, namespace_id, node_type, writer_engine) -- called
    # positionally in the generated handler, so asserted positionally here.
    assert len(owner_calls) == 1
    assert owner_calls[0][2] == "DEVICE"
    assert owner_calls[0][3] == "system_design"
    assert len(emit_calls) == 1
    assert emit_calls[0]["node_type"] == "DEVICE"
    assert emit_calls[0]["op"] == "upserted"
    assert emit_calls[0]["node_id"] == node_label
