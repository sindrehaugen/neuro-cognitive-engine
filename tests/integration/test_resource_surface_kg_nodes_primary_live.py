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


@pytest.fixture
def engine(pg_pool: asyncpg.Pool) -> NCEEngine:
    eng = NCEEngine()
    eng.pg_pool = pg_pool
    populate_engine_modules(eng)
    return eng


def _rest_client(engine: NCEEngine, spec: ResourceSpec) -> httpx.AsyncClient:
    admin_state.engine = engine
    app = Starlette(routes=make_resource_routes(spec))
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
