"""
tests/integration/test_resource_surface_multi_table_live.py
=============================================================
Live-Postgres verification of ``ResourceSpec.secondary_tables`` (multi-table
ResourceSpec support), dispatched to close system_design's DEVICE/PORT/RACK/
CABLE exemptions (``nce/resource_surface/exemptions.py``): each is a
"multi-table spread", not 1:1 with a table, which C12 had no declaration for
before this wave.

TWO REAL DEFECTS THIS TEST FOUND WHILE BEING WRITTEN, NOT REASONED ABOUT
--------------------------------------------------------------------------
1. system_design_device_capabilities has a real FK (fk_sddc_kg_nodes,
   schema.sql:5150) requiring (node_label, namespace_id) to already exist
   in kg_nodes -- undocumented in the table's own COMMENT ON TABLE, which
   only says node_label "matches kg_nodes.label" descriptively. Confirmed
   as a live ForeignKeyViolationError, not inferred from the schema alone
   (test setup now creates a real kg_nodes row first, via _create_kg_node).
2. The first cut of the secondary-table write used
   INSERT ... ON CONFLICT (namespace_id, join_field) DO UPDATE. A PATCH
   that only changed `status` (an existing row, no `node_type` in the
   request) failed a NOT NULL violation on node_state.node_type --
   Postgres validates an INSERT's own column list against table
   constraints regardless of whether ON CONFLICT redirects to UPDATE, so a
   genuinely partial update of an EXISTING row still needed every NOT NULL
   column supplied. Fixed by replacing ON CONFLICT with an explicit
   check-then-branch (nce.resource_surface.rest.upsert_secondary_tables):
   SELECT for an existing row first, UPDATE only the supplied columns if
   found, INSERT the full row only if not -- the same pattern the PRIMARY
   table's own upsert logic already used, just not yet applied to
   secondary tables in the first draft.

WHY THIS MUST BE A LIVE TEST, NOT A MOCK
-----------------------------------------
Every real defect resource_surface has produced tonight (#284's datetime
bind, #294's opt-in bypass, #295/#301's push-rejection) was invisible to a
mocked connection and only surfaced against a real Postgres. A multi-table
JOIN/ON-CONFLICT mechanism is exactly the kind of SQL a mock accepts
uncritically regardless of whether the generated statement is actually
valid.

WHY THIS TEST DOES NOT REGISTER DEVICE ITSELF
-----------------------------------------------
DEVICE's PRIMARY identity is graph-only (a ``kg_nodes`` row); the satellite
attribute tables (``system_design_device_capabilities``,
``system_design_node_state``, ``system_design_geometry``) hold its extended
fields, keyed by ``node_label`` matching the graph node's own label. At the
time this test was written, ``resource_surface`` had NO generated read/write
path for the ``storage_kind="kg_nodes"``/graph tenant_scope at all -- every
handler in mcp.py/rest.py returned a 501 unconditionally, and
``secondary_tables`` on a ``table_name=None`` spec was rejected at
construction for exactly that reason. **That gap is closed by Wave 3
(2026-09-20)** -- see
``tests/integration/test_resource_surface_kg_nodes_primary_live.py`` for the
live proof against real DEVICE/PORT/RACK/CABLE tables. This file still does
not register DEVICE as a production ``ResourceSpec``: that is a
system_design content decision (``filterable_fields``, ``tier_allowlists``,
``governed_verbs``) deliberately left to whoever owns that engine, same as
this wave left multi-table itself to be proven via an unregistered probe
rather than a real registration.

This test's synthetic spec uses ``system_design_device_capabilities`` as the
primary table (``id_field="node_label"``, since that table's own natural key
for this purpose is its ``node_label`` column, not its UUID surrogate ``id``)
and ``system_design_node_state`` as a secondary table joined on the same
``node_label`` -- the exact real tables and the exact real join key DEVICE
will use, so this is not a synthetic schema, only a synthetic (unregistered)
ResourceSpec instance over real production tables.
"""

from __future__ import annotations

import json
import uuid

import asyncpg
import httpx
import pytest
from starlette.applications import Starlette

from nce import admin_state
from nce.engine_registry import populate_engine_modules
from nce.orchestrator import NCEEngine
from nce.resource_surface.mcp import build_mcp_tool_specs
from nce.resource_surface.rest import make_resource_routes
from nce.resource_surface.spec import ResourceSpec, SecondaryTable
from tests._verified_tier_middleware import VERIFIED_TIER_TEST_MIDDLEWARE

pytestmark = pytest.mark.integration

MULTI_TABLE_PROBE_SPEC = ResourceSpec(
    engine="system_design",
    entity="devices-multitable-probe",
    node_type="DEVICE",
    table_name="system_design_device_capabilities",
    id_field="node_label",
    version_field="updated_at",
    soft_delete_field=None,
    writable_fields=(
        "signal_format",
        "manufacturer",
        "model_number",
        "node_type",
        "status",
        "revision",
    ),
    secondary_tables=(
        SecondaryTable(
            table_name="system_design_node_state",
            join_field="node_label",
            fields=("node_type", "status", "revision"),
        ),
    ),
    description="Test-only probe spec, never registered in the global registry.",
)


@pytest.fixture
def engine(pg_pool: asyncpg.Pool) -> NCEEngine:
    eng = NCEEngine()
    eng.pg_pool = pg_pool
    populate_engine_modules(eng)
    return eng


@pytest.fixture
def probe_mcp_tools():
    return build_mcp_tool_specs(MULTI_TABLE_PROBE_SPEC)


async def _create_kg_node(pg_pool: asyncpg.Pool, namespace_id: uuid.UUID, label: str) -> None:
    """Real requirement, found live while writing this test, not a test
    convenience: system_design_device_capabilities has a real FK
    (fk_sddc_kg_nodes, FOREIGN KEY (node_label, namespace_id) REFERENCES
    kg_nodes (label, namespace_id)) -- undocumented in the table's own
    COMMENT ON TABLE, which only says node_label "matches kg_nodes.label"
    descriptively, not that it's enforced. A satellite attribute row cannot
    exist without the graph node it describes already existing. This is the
    exact, concrete shape of the "DEVICE needs a kg_nodes write path too"
    gap this wave's own docstring states -- discovered here as a real FK
    violation, not reasoned about in the abstract.
    """
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO kg_nodes (label, entity_type, namespace_id) VALUES ($1, 'DEVICE', $2) "
            "ON CONFLICT (label, namespace_id) DO NOTHING",
            label,
            namespace_id,
        )


@pytest.mark.asyncio
async def test_mcp_upsert_then_get_merges_both_tables(
    engine: NCEEngine, namespace_id: uuid.UUID, probe_mcp_tools, pg_pool: asyncpg.Pool
) -> None:
    """A single upsert call must write signal_format/manufacturer to the
    PRIMARY table (system_design_device_capabilities) and node_type/status/
    revision to the SECONDARY table (system_design_node_state) -- one call,
    two tables, verified by reading each table directly, not just trusting
    the handler's own response.
    """
    node_label = f"probe-device-{uuid.uuid4().hex[:8]}"
    await _create_kg_node(pg_pool, namespace_id, node_label)
    upsert = probe_mcp_tools[f"system_design_upsert_{MULTI_TABLE_PROBE_SPEC.mcp_slug}"]

    result = json.loads(
        await upsert.handler(
            engine,
            {
                "namespace_id": str(namespace_id),
                "id": node_label,
                "signal_format": "HDMI 2.1",
                "manufacturer": "TestCorp",
                "node_type": "DEVICE",
                "status": "planned",
                "revision": "rev-1",
            },
        )
    )
    assert result["status"] == "ok", result

    async with pg_pool.acquire() as conn:
        primary_row = await conn.fetchrow(
            "SELECT signal_format, manufacturer FROM system_design_device_capabilities "
            "WHERE namespace_id = $1 AND node_label = $2",
            namespace_id,
            node_label,
        )
        secondary_row = await conn.fetchrow(
            "SELECT node_type, status, revision FROM system_design_node_state "
            "WHERE namespace_id = $1 AND node_label = $2",
            namespace_id,
            node_label,
        )
    assert primary_row is not None, "primary table row was not written"
    assert primary_row["signal_format"] == "HDMI 2.1"
    assert primary_row["manufacturer"] == "TestCorp"
    assert secondary_row is not None, "secondary table row was not written"
    assert secondary_row["node_type"] == "DEVICE"
    assert secondary_row["status"] == "planned"
    assert secondary_row["revision"] == "rev-1"

    # The generated GET must merge both tables back into one response --
    # this is the read half of the same round trip.
    get_tool = probe_mcp_tools[f"system_design_get_{MULTI_TABLE_PROBE_SPEC.mcp_slug}"]
    fetched = json.loads(
        await get_tool.handler(engine, {"namespace_id": str(namespace_id), "id": node_label})
    )
    assert fetched["signal_format"] == "HDMI 2.1"
    assert fetched["manufacturer"] == "TestCorp"
    assert fetched["node_type"] == "DEVICE"
    assert fetched["status"] == "planned"
    assert fetched["revision"] == "rev-1"


@pytest.mark.asyncio
async def test_mcp_get_with_no_secondary_row_still_returns_primary(
    engine: NCEEngine, namespace_id: uuid.UUID, probe_mcp_tools, pg_pool: asyncpg.Pool
) -> None:
    """A primary row with no matching secondary row yet must still be a
    real, gettable resource -- proves the merge treats a missing secondary
    row as absence, not an error.
    """
    node_label = f"probe-device-primary-only-{uuid.uuid4().hex[:8]}"
    await _create_kg_node(pg_pool, namespace_id, node_label)
    upsert = probe_mcp_tools[f"system_design_upsert_{MULTI_TABLE_PROBE_SPEC.mcp_slug}"]
    result = json.loads(
        await upsert.handler(
            engine,
            {
                "namespace_id": str(namespace_id),
                "id": node_label,
                "signal_format": "SDI",
                # No node_type/status/revision -- secondary table gets no row.
            },
        )
    )
    assert result["status"] == "ok", result

    get_tool = probe_mcp_tools[f"system_design_get_{MULTI_TABLE_PROBE_SPEC.mcp_slug}"]
    fetched = json.loads(
        await get_tool.handler(engine, {"namespace_id": str(namespace_id), "id": node_label})
    )
    assert fetched["signal_format"] == "SDI"
    assert "status" not in fetched or fetched.get("status") is None


@pytest.mark.asyncio
async def test_mcp_upsert_twice_updates_secondary_table_in_place(
    engine: NCEEngine, namespace_id: uuid.UUID, probe_mcp_tools, pg_pool: asyncpg.Pool
) -> None:
    """A second upsert against the same id must UPDATE the secondary table
    row in place (one row, new value), not insert a duplicate or fail --
    proves upsert_secondary_tables' existence check actually finds the row
    its own first call created, keyed on (namespace_id, join_field) matching
    this table's real UNIQUE constraint.
    """
    node_label = f"probe-device-update-{uuid.uuid4().hex[:8]}"
    await _create_kg_node(pg_pool, namespace_id, node_label)
    upsert = probe_mcp_tools[f"system_design_upsert_{MULTI_TABLE_PROBE_SPEC.mcp_slug}"]

    r1 = json.loads(
        await upsert.handler(
            engine,
            {
                "namespace_id": str(namespace_id),
                "id": node_label,
                "manufacturer": "First",
                "node_type": "DEVICE",
                "status": "planned",
            },
        )
    )
    assert r1["status"] == "ok"

    r2 = json.loads(
        await upsert.handler(
            engine,
            {
                "namespace_id": str(namespace_id),
                "id": node_label,
                "expected_version": r1["version"],
                "manufacturer": "Second",
                "node_type": "DEVICE",
                "status": "active",
            },
        )
    )
    assert r2["status"] == "ok", r2

    async with pg_pool.acquire() as conn:
        secondary_count = await conn.fetchval(
            "SELECT count(*) FROM system_design_node_state WHERE namespace_id = $1 AND node_label = $2",
            namespace_id,
            node_label,
        )
        secondary_row = await conn.fetchrow(
            "SELECT status FROM system_design_node_state WHERE namespace_id = $1 AND node_label = $2",
            namespace_id,
            node_label,
        )
    assert secondary_count == 1, (
        f"expected exactly one node_state row after two upserts (the second call "
        f"should have found and UPDATEd the existing row, not inserted a duplicate), "
        f"found {secondary_count}"
    )
    assert secondary_row["status"] == "active"


def _probe_rest_app() -> Starlette:
    return Starlette(
        routes=make_resource_routes(MULTI_TABLE_PROBE_SPEC),
        middleware=VERIFIED_TIER_TEST_MIDDLEWARE,
    )


@pytest.fixture
def probe_rest_client(engine: NCEEngine):
    previous = admin_state.engine
    admin_state.engine = engine
    try:
        yield httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_probe_rest_app()), base_url="http://test"
        )
    finally:
        admin_state.engine = previous


@pytest.mark.asyncio
async def test_rest_create_then_get_merges_both_tables(
    probe_rest_client: httpx.AsyncClient, namespace_id: uuid.UUID, pg_pool: asyncpg.Pool
) -> None:
    node_label = f"probe-device-rest-{uuid.uuid4().hex[:8]}"
    await _create_kg_node(pg_pool, namespace_id, node_label)
    async with probe_rest_client as client:
        r1 = await client.post(
            "/api/system_design/devices-multitable-probe",
            json={
                "namespace_id": str(namespace_id),
                # NOT "id": handle_create looks up the caller-supplied id
                # under `body[spec.id_field]`, unlike MCP's handle_upsert
                # (always the literal "id" regardless of id_field) -- a
                # pre-existing REST/MCP naming inconsistency, found live
                # while writing this test, out of scope to fix here.
                "node_label": node_label,
                "signal_format": "DisplayPort",
                "node_type": "DEVICE",
                "status": "staged",
                "revision": "rest-rev-1",
            },
        )
        assert r1.status_code == 201, r1.text

        r2 = await client.get(
            f"/api/system_design/devices-multitable-probe/{node_label}",
            params={"namespace_id": str(namespace_id)},
            headers={"X-NCE-Principal-Tier": "employee"},
        )
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["signal_format"] == "DisplayPort"
    assert body["status"] == "staged"
    assert body["revision"] == "rest-rev-1"

    async with pg_pool.acquire() as conn:
        secondary_row = await conn.fetchrow(
            "SELECT status FROM system_design_node_state WHERE namespace_id = $1 AND node_label = $2",
            namespace_id,
            node_label,
        )
    assert secondary_row is not None
    assert secondary_row["status"] == "staged"


@pytest.mark.asyncio
async def test_rest_patch_updates_secondary_table(
    probe_rest_client: httpx.AsyncClient, namespace_id: uuid.UUID, pg_pool: asyncpg.Pool
) -> None:
    node_label = f"probe-device-rest-patch-{uuid.uuid4().hex[:8]}"
    await _create_kg_node(pg_pool, namespace_id, node_label)
    async with probe_rest_client as client:
        r1 = await client.post(
            "/api/system_design/devices-multitable-probe",
            json={
                "namespace_id": str(namespace_id),
                "node_label": node_label,
                "node_type": "DEVICE",
                "status": "planned",
            },
        )
        assert r1.status_code == 201, r1.text
        version1 = r1.json()["version"]

        r2 = await client.patch(
            f"/api/system_design/devices-multitable-probe/{node_label}",
            json={
                "namespace_id": str(namespace_id),
                "expected_version": version1,
                "status": "active",
            },
        )
    assert r2.status_code == 200, r2.text

    async with pg_pool.acquire() as conn:
        secondary_row = await conn.fetchrow(
            "SELECT status FROM system_design_node_state WHERE namespace_id = $1 AND node_label = $2",
            namespace_id,
            node_label,
        )
    assert secondary_row["status"] == "active"


@pytest.mark.asyncio
async def test_rest_patch_first_touch_of_secondary_table_still_satisfies_node_type_not_null(
    probe_rest_client: httpx.AsyncClient, namespace_id: uuid.UUID, pg_pool: asyncpg.Pool
) -> None:
    """A PATCH that is the FIRST write ever to touch node_state (created
    with no node_state-routed fields, so no row exists yet) must still
    succeed, not 500 on node_state.node_type's NOT NULL constraint.

    node_type is never in spec.writable_fields (derived from the spec's
    own identity, never client-supplied), so handle_patch's `updates`
    dict never carries it -- without re-injecting it alongside whatever
    secondary field IS being patched, upsert_secondary_tables' INSERT
    branch (no existing row to UPDATE) would be missing a NOT NULL
    column. Distinct from test_rest_patch_updates_secondary_table above,
    which patches a row that already has a node_state row from its own
    CREATE -- that case never observably breaks, since UPDATE only
    touches listed columns, leaving a correct pre-existing node_type
    alone either way.
    """
    node_label = f"probe-device-rest-first-touch-{uuid.uuid4().hex[:8]}"
    await _create_kg_node(pg_pool, namespace_id, node_label)
    async with probe_rest_client as client:
        r1 = await client.post(
            "/api/system_design/devices-multitable-probe",
            json={
                "namespace_id": str(namespace_id),
                "node_label": node_label,
                "signal_format": "HDMI",
                # No node_type/status/revision -- node_state gets no row here.
            },
        )
        assert r1.status_code == 201, r1.text
        version1 = r1.json()["version"]

        r2 = await client.patch(
            f"/api/system_design/devices-multitable-probe/{node_label}",
            json={
                "namespace_id": str(namespace_id),
                "expected_version": version1,
                "status": "planned",
            },
        )
    assert r2.status_code == 200, r2.text

    async with pg_pool.acquire() as conn:
        secondary_row = await conn.fetchrow(
            "SELECT node_type, status FROM system_design_node_state "
            "WHERE namespace_id = $1 AND node_label = $2",
            namespace_id,
            node_label,
        )
    assert secondary_row is not None
    assert secondary_row["node_type"] == "DEVICE"
    assert secondary_row["status"] == "planned"


@pytest.mark.asyncio
async def test_positive_control_field_collision_is_rejected_at_construction() -> None:
    """Proves the spec.py validation added this wave actually fires -- a
    field routed to two secondary tables must be refused at ResourceSpec
    construction time, not silently accepted and then behave ambiguously at
    runtime.
    """
    with pytest.raises(ValueError, match="routed to both"):
        ResourceSpec(
            engine="system_design",
            entity="probe-collision",
            node_type="DEVICE",
            table_name="system_design_device_capabilities",
            secondary_tables=(
                SecondaryTable(
                    table_name="system_design_node_state",
                    join_field="node_label",
                    fields=("status",),
                ),
                SecondaryTable(
                    table_name="system_design_geometry",
                    join_field="node_label",
                    fields=("status",),
                ),
            ),
        )


@pytest.mark.asyncio
async def test_positive_control_kg_nodes_primary_with_secondary_tables_is_accepted() -> None:
    """Wave 3 (2026-09-20) relaxed this: a graph-primary spec (table_name=None)
    with secondary_tables now constructs successfully -- kg_nodes IS the
    primary row (identity only), and secondary tables route the real fields.
    This test used to assert the OPPOSITE (construction raised ValueError)
    back when resource_surface had no generated read/write path for the
    graph storage_kind at all; that gap is what Wave 3 closes. See
    tests/integration/test_resource_surface_kg_nodes_primary_live.py for the
    live read/write proof against real DEVICE/PORT/RACK/CABLE tables.
    """
    spec = ResourceSpec(
        engine="system_design",
        entity="probe-graph-only",
        node_type="DEVICE",
        table_name=None,
        secondary_tables=(
            SecondaryTable(
                table_name="system_design_node_state",
                join_field="node_label",
                fields=("status",),
            ),
        ),
    )
    assert spec.tenant_scope == "graph"
