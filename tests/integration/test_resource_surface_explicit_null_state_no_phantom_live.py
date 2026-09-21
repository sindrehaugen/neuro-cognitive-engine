"""
tests/integration/test_resource_surface_explicit_null_state_no_phantom_live.py
==================================================================================
Live-Postgres proof for a second, distinct instance of the phantom
``system_design_node_state`` row defect (found 2026-09-21, while enumerating
every writer of that table before authorizing a cleanup sweep -- see
``_internal/work-docs/mlv16-orchestration/FILED_phantom_node_state_rows.md``).

THE BUG, EXACTLY
------------------
``#394``/``#400`` closed the AGGREGATE-dict version of this defect: a caller
touching a DIFFERENT secondary table's field (e.g. ``signal_format`` on
``system_design_device_capabilities``) made the combined ``sec_data`` dict
non-empty, which then also injected ``node_type`` into
``system_design_node_state`` even though the caller never touched that
table.  ``compute_secondary_write_data`` (``rest.py``) fixed that by gating
the injection PER secondary table.

That fix checks whether the table's OTHER fields are PRESENT in the
caller's payload (``any(f in sec_data for f in sec.fields if f !=
"node_type")``) -- never whether the values are non-``None``.  So a caller
who sends ``{"status": null, "revision": null, "salience": null}``
explicitly -- not omitted, but present with a JSON ``null`` -- for a node
with no existing ``system_design_node_state`` row reaches
``upsert_secondary_tables``'s INSERT branch with ``sec_data`` still
non-empty (``node_type`` plus three explicit nulls), and writes a fresh
phantom row: ``node_type`` populated, every state column NULL.  This is a
SEPARATE, still-open defect in the same function family -- not historical
residue from before ``#394``/``#400``, reachable today, on all three
surfaces that share ``upsert_secondary_tables``: REST create, REST PATCH,
and the MCP ``handle_upsert`` tool.

Why a phantom row matters (unchanged from the original finding): ``read.py``
's ``_fetch_node_state_by_labels`` documents three distinguishable facts --
no row / row-with-NULL-status / row-with-status -- and both
``do_get_topology``'s response shape and ``retire.py``'s machine-readable
deny reasons (``state_row_absent`` vs ``status_undeclared``) depend on the
distinction staying real.  A phantom row asserts the middle fact about a
node that never had any of its lifecycle declared.

THE FIX, AND ITS SCOPE
-------------------------
``upsert_secondary_tables`` (``rest.py``) now refuses to INSERT a secondary
row whose only content is its own ``node_type`` -- checked once, shared by
all three surfaces, no new round trip (the ``existing`` check the INSERT
branch already runs unconditionally is what decides whether this is even
a candidate).  Scoped to the INSERT branch only: an EXISTING row is a
different question entirely, and an explicit ``null`` there is a
legitimate CLEAR of a previously-set value that must still write --
``test_rest_patch_explicit_null_on_existing_row_still_clears_the_field``
below is the positive control that proves the fix did not also break that.
"""

from __future__ import annotations

import json
import uuid

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
    entity="devices-explicitnull-probe",
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


@pytest_asyncio.fixture
async def engine(pg_pool: asyncpg.Pool, namespace_id: uuid.UUID) -> NCEEngine:
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


async def _state_row(engine: NCEEngine, namespace_id: uuid.UUID, node_label: str):
    async with engine.pg_pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT status, revision, salience FROM system_design_node_state "
            "WHERE namespace_id = $1 AND node_label = $2",
            namespace_id,
            node_label,
        )


@pytest.mark.asyncio
async def test_rest_create_with_explicit_null_state_fields_writes_no_state_row(
    engine: NCEEngine, namespace_id: uuid.UUID
) -> None:
    """The actual regression: a CREATE that names status/revision/salience
    explicitly as JSON null (not omitted) for a brand-new node must leave
    NO system_design_node_state row -- the same "no row" fact a bare
    create (nothing named at all) already produces.
    """
    node_label = f"probe-explicitnull-create-{uuid.uuid4().hex[:8]}"
    async with _rest_client(engine, DEVICE_PROBE_SPEC) as client:
        r = await client.post(
            "/api/system_design/devices-explicitnull-probe",
            json={
                "namespace_id": str(namespace_id),
                "node_label": node_label,
                "status": None,
                "revision": None,
                "salience": None,
            },
        )
    assert r.status_code == 201, r.text

    row = await _state_row(engine, namespace_id, node_label)
    assert row is None, (
        f"an explicit-null CREATE wrote a phantom system_design_node_state row: {dict(row)!r}"
    )


@pytest.mark.asyncio
async def test_rest_patch_first_touch_with_explicit_null_state_fields_writes_no_state_row(
    engine: NCEEngine, namespace_id: uuid.UUID
) -> None:
    """Same defect, PATCH-as-first-touch shape: a bare create leaves no
    state row, and a subsequent PATCH naming all three fields as explicit
    null -- the node's first-ever contact with system_design_node_state --
    must also leave no row, not create one holding nothing.
    """
    node_label = f"probe-explicitnull-patch1st-{uuid.uuid4().hex[:8]}"
    async with _rest_client(engine, DEVICE_PROBE_SPEC) as client:
        r1 = await client.post(
            "/api/system_design/devices-explicitnull-probe",
            json={"namespace_id": str(namespace_id), "node_label": node_label},
        )
        assert r1.status_code == 201, r1.text

        r2 = await client.patch(
            f"/api/system_design/devices-explicitnull-probe/{node_label}",
            json={
                "namespace_id": str(namespace_id),
                "status": None,
                "revision": None,
                "salience": None,
            },
        )
    assert r2.status_code == 200, r2.text

    row = await _state_row(engine, namespace_id, node_label)
    assert row is None, (
        f"an explicit-null first-touch PATCH wrote a phantom system_design_node_state "
        f"row: {dict(row)!r}"
    )


@pytest.mark.asyncio
async def test_rest_patch_explicit_null_on_existing_row_still_clears_the_field(
    engine: NCEEngine, namespace_id: uuid.UUID
) -> None:
    """Positive control -- the direction a naive fix breaks: a node with a
    REAL, already-declared status must still be clearable by an explicit
    null PATCH. The fix is scoped to the INSERT branch only; this proves
    the UPDATE branch (an existing row) was not also gated shut.
    """
    node_label = f"probe-explicitnull-clear-{uuid.uuid4().hex[:8]}"
    async with _rest_client(engine, DEVICE_PROBE_SPEC) as client:
        r1 = await client.post(
            "/api/system_design/devices-explicitnull-probe",
            json={
                "namespace_id": str(namespace_id),
                "node_label": node_label,
                "status": "planned",
                "revision": "rev-1",
            },
        )
        assert r1.status_code == 201, r1.text

    row_before = await _state_row(engine, namespace_id, node_label)
    assert row_before is not None, "setup failed: no state row after a real-content create"
    assert row_before["status"] == "planned"

    async with _rest_client(engine, DEVICE_PROBE_SPEC) as client:
        r2 = await client.patch(
            f"/api/system_design/devices-explicitnull-probe/{node_label}",
            json={"namespace_id": str(namespace_id), "status": None},
        )
    assert r2.status_code == 200, r2.text

    row_after = await _state_row(engine, namespace_id, node_label)
    assert row_after is not None, (
        "the fix over-reached: an explicit-null PATCH on an EXISTING row must still "
        "write (a legitimate clear), not be treated as a phantom-prevention skip"
    )
    assert row_after["status"] is None, (
        f"explicit-null clear did not take effect: status is still {row_after['status']!r}"
    )
    assert row_after["revision"] == "rev-1", (
        "clearing status must not also clear a field the caller did not touch"
    )


@pytest.mark.asyncio
async def test_mcp_upsert_with_explicit_null_state_fields_writes_no_state_row(
    engine: NCEEngine, namespace_id: uuid.UUID
) -> None:
    """Same regression, MCP surface: handle_upsert shares
    upsert_secondary_tables with the REST handlers, but builds its own
    `data` dict independently (`{f: arguments[f] for f in
    spec.writable_fields if f in arguments}`) -- a genuinely separate code
    path that needs its own proof, not assumed from the REST result.
    """
    tools = build_mcp_tool_specs(DEVICE_PROBE_SPEC)
    upsert = tools[f"system_design_upsert_{DEVICE_PROBE_SPEC.mcp_slug}"]
    node_label = f"probe-explicitnull-mcp-{uuid.uuid4().hex[:8]}"

    result = json.loads(
        await upsert.handler(
            engine,
            {
                "namespace_id": str(namespace_id),
                "id": node_label,
                "status": None,
                "revision": None,
                "salience": None,
            },
        )
    )
    assert result["status"] == "ok", result

    row = await _state_row(engine, namespace_id, node_label)
    assert row is None, (
        f"an explicit-null MCP upsert wrote a phantom system_design_node_state row: {dict(row)!r}"
    )
