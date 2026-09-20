"""
tests/integration/test_resource_surface_mcp_archive_live.py
==============================================================
Live-Postgres sibling for MCP's `handle_archive` (`nce/resource_surface/
mcp.py:736-813`). Filled a gap found while extending BRANCH_COVERAGE_MAP.md
to the MCP surface: MCP `list` and `get` already have live siblings, MCP
`archive` had none -- real code, never once run against Postgres. REST's
own `handle_archive` has full live coverage
(`test_resource_surface_archive_restore_live.py`); this is its MCP twin.

WHAT MAKES THIS HANDLER WORTH A DEDICATED TEST, NOT JUST "ANOTHER ARCHIVE"
---------------------------------------------------------------------------
`handle_archive`'s only failure detection is `res.endswith("0")` on the raw
`UPDATE ...` command tag asyncpg returns (e.g. ``"UPDATE 0"`` vs
``"UPDATE 1"``) -- there is no ``RETURNING`` clause, no re-SELECT, nothing
that confirms which row (if any) actually changed. Its success response,
``{"status": "ok", "id": item_id, "archived": True}``, is built
unconditionally whenever the command tag doesn't end in "0" -- a test that
only checks this envelope would pass just as well against an UPDATE that
matched and flipped a DIFFERENT row (a WHERE-clause bug), since the
envelope carries no information about which row Postgres actually touched.
Every test below reads the actual column back from Postgres directly,
and the "wrong row" class is covered explicitly by archiving one of two
rows and confirming the other is untouched, not by trusting the response.

POPULATION -- measured, not assumed. 17 relational, archive-eligible specs
declare a `soft_delete_field` (same population `test_resource_surface_
archive_restore_live.py` measured for the REST side). Of those 17, exactly
1 is tenant_scope="global" (`product:product-skus`) -- the rest are
"tenant". Both branches of `handle_archive`'s `spec.table_name` case
(`is_global` true/false, `rest.py`-mirrored WHERE clause) are exercised
here: `agreements:templates` (tenant, simple schema, no CHECK constraints,
already used by this session's list sibling) and `product:product-skus`
(the one global spec, needs its own `enabled_guard` opt-in).
"""

from __future__ import annotations

import json
import uuid

import asyncpg
import pytest
import pytest_asyncio

from nce.engine_registry import populate_engine_modules
from nce.orchestrator import NCEEngine
from nce.resource_surface import get_all_resource_specs, load_all_engine_resources
from nce.resource_surface.mcp import build_mcp_tool_specs
from nce.resource_surface.spec import ResourceSpec, SecondaryTable
from nce.tool_registry import TOOL_REGISTRY

pytestmark = pytest.mark.integration

load_all_engine_resources()
_ALL_SPECS = {(s.engine, s.entity): s for s in get_all_resource_specs()}

_RELATIONAL_ARCHIVE_ELIGIBLE = [
    s
    for s in get_all_resource_specs()
    if s.table_name is not None and "archive" not in s.excluded_verbs and s.soft_delete_field
]
_GLOBAL_SCOPE = [s for s in _RELATIONAL_ARCHIVE_ELIGIBLE if s.tenant_scope == "global"]

TEMPLATE_SPEC = _ALL_SPECS[("agreements", "templates")]
SKU_SPEC = _ALL_SPECS[("product", "product-skus")]

# A graph-primary probe, same shape as test_resource_surface_kg_nodes_
# primary_live.py's own DEVICE_PROBE_SPEC -- self-contained per this
# session's convention (each live sibling does not cross-import another
# test file's fixtures/specs).
GRAPH_PROBE_SPEC = ResourceSpec(
    engine="system_design",
    entity="devices-mcp-archive-probe",
    node_type="DEVICE",
    table_name=None,
    id_field="node_label",
    soft_delete_field=None,
    writable_fields=("change_origin", "signal_format"),
    secondary_tables=(
        SecondaryTable(
            table_name="system_design_device_capabilities",
            join_field="node_label",
            fields=("signal_format",),
        ),
    ),
    description="Test-only probe spec, never registered in the global registry.",
)


def test_discovery_floor_matches_the_measured_population() -> None:
    assert len(_RELATIONAL_ARCHIVE_ELIGIBLE) >= 17, (
        f"Only {len(_RELATIONAL_ARCHIVE_ELIGIBLE)} relational, archive-eligible, soft-delete "
        f"specs found -- expected at least 17 (same population REST's archive/restore sibling "
        f"measured)."
    )
    assert len(_GLOBAL_SCOPE) == 1 and _GLOBAL_SCOPE[0] is SKU_SPEC, (
        f"Expected exactly one global-scope archive-eligible spec (product:product-skus), "
        f"found {[f'{s.engine}:{s.entity}' for s in _GLOBAL_SCOPE]}"
    )


@pytest.fixture
def engine(pg_pool: asyncpg.Pool) -> NCEEngine:
    eng = NCEEngine()
    eng.pg_pool = pg_pool
    populate_engine_modules(eng)
    return eng


async def _enable_engine(pg_pool: asyncpg.Pool, namespace_id: uuid.UUID, engine_name: str) -> None:
    # Single-element jsonb_set path -- a two-element path on a fresh {}
    # silently no-ops (found and fixed identically in every other live
    # sibling tonight that needs enabled_guard).
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE namespaces SET metadata = jsonb_set("
            "coalesce(metadata, '{}'::jsonb), ARRAY[$2], "
            "coalesce(metadata->$2, '{}'::jsonb) || jsonb_build_object('enabled', true), "
            "true) WHERE id = $1",
            namespace_id,
            engine_name,
        )


@pytest_asyncio.fixture(autouse=True)
async def _enable_guarded_engines(pg_pool: asyncpg.Pool, namespace_id: uuid.UUID) -> None:
    await _enable_engine(pg_pool, namespace_id, "agreements")
    await _enable_engine(pg_pool, namespace_id, "product")


@pytest.mark.asyncio
async def test_mcp_archive_flips_the_row_for_a_tenant_scoped_spec(
    engine: NCEEngine, namespace_id: uuid.UUID, pg_pool: asyncpg.Pool
) -> None:
    """tenant branch (`is_global` false): `WHERE namespace_id = $1 AND id = $2`."""
    upsert = TOOL_REGISTRY["agreements_upsert_templates"]
    archive = TOOL_REGISTRY["agreements_archive_templates"]

    created = json.loads(
        await upsert.handler(
            engine, {"namespace_id": str(namespace_id), "name": "mcp-archive-probe-template"}
        )
    )
    assert created["status"] == "ok", created
    item_id = created["id"]

    result = json.loads(
        await archive.handler(engine, {"namespace_id": str(namespace_id), "id": item_id})
    )
    assert result["status"] == "ok", result
    assert result["archived"] is True

    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT is_archived FROM agreement_templates WHERE namespace_id = $1 AND id = $2",
            namespace_id,
            uuid.UUID(item_id),
        )
    assert row is not None, "row disappeared -- archive must not delete"
    assert row["is_archived"] is True, (
        "handler returned status=ok/archived=true but the actual column was not flipped"
    )


@pytest.mark.asyncio
async def test_mcp_archive_flips_the_row_for_a_global_scoped_spec(
    engine: NCEEngine, namespace_id: uuid.UUID, pg_pool: asyncpg.Pool
) -> None:
    """global branch (`is_global` true): `WHERE id = $1`, no namespace_id column
    on `product_catalog` at all -- `namespace_id` in the call is only for
    the `enabled_guard` opt-in check, never bound into the UPDATE.
    """
    upsert = TOOL_REGISTRY["product_upsert_product_skus"]
    archive = TOOL_REGISTRY["product_archive_product_skus"]

    unique = uuid.uuid4().hex[:10]
    created = json.loads(
        await upsert.handler(
            engine,
            {
                "namespace_id": str(namespace_id),
                "manufacturer": f"mcp-archive-mfr-{unique}",
                "mfr_part_no": f"PN-{unique}",
                "product_source_id": f"SRC-{unique}",
            },
        )
    )
    assert created["status"] == "ok", created
    item_id = created["id"]

    result = json.loads(
        await archive.handler(engine, {"namespace_id": str(namespace_id), "id": item_id})
    )
    assert result["status"] == "ok", result
    assert result["archived"] is True

    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT is_deleted FROM product_catalog WHERE id = $1", uuid.UUID(item_id)
        )
    assert row is not None
    assert row["is_deleted"] is True, (
        "handler returned status=ok/archived=true but the actual column was not flipped"
    )


@pytest.mark.asyncio
async def test_mcp_archive_touches_only_the_targeted_row_not_a_sibling(
    engine: NCEEngine, namespace_id: uuid.UUID, pg_pool: asyncpg.Pool
) -> None:
    """The response envelope alone cannot distinguish "the right row flipped"
    from "some row flipped" -- two rows exist, only one is archived, and
    BOTH are read back directly to prove the WHERE clause discriminated
    between them rather than the assertion just getting lucky on a single
    row.
    """
    upsert = TOOL_REGISTRY["agreements_upsert_templates"]
    archive = TOOL_REGISTRY["agreements_archive_templates"]

    target = json.loads(
        await upsert.handler(
            engine, {"namespace_id": str(namespace_id), "name": "mcp-archive-target"}
        )
    )
    sibling = json.loads(
        await upsert.handler(
            engine, {"namespace_id": str(namespace_id), "name": "mcp-archive-sibling"}
        )
    )
    target_id, sibling_id = target["id"], sibling["id"]

    result = json.loads(
        await archive.handler(engine, {"namespace_id": str(namespace_id), "id": target_id})
    )
    assert result["status"] == "ok", result

    async with pg_pool.acquire() as conn:
        rows = {
            r["id"]: r["is_archived"]
            for r in await conn.fetch(
                "SELECT id, is_archived FROM agreement_templates "
                "WHERE namespace_id = $1 AND id = ANY($2::uuid[])",
                namespace_id,
                [uuid.UUID(target_id), uuid.UUID(sibling_id)],
            )
        }
    assert rows[uuid.UUID(target_id)] is True, "the targeted row was not archived"
    assert rows[uuid.UUID(sibling_id)] is False, (
        "a sibling row was archived alongside the target -- the WHERE clause is not "
        "discriminating between rows"
    )


@pytest.mark.asyncio
async def test_mcp_archive_reports_not_found_for_a_missing_id(
    engine: NCEEngine, namespace_id: uuid.UUID
) -> None:
    """Pins the mechanism ML-orch flagged directly: the ONLY failure
    detection this handler has is `res.endswith("0")` on the UPDATE
    command tag. An id that matches zero rows must surface as an error,
    not silently return status=ok.
    """
    archive = TOOL_REGISTRY["agreements_archive_templates"]
    result = json.loads(
        await archive.handler(engine, {"namespace_id": str(namespace_id), "id": str(uuid.uuid4())})
    )
    assert "error" in result, (
        f"archiving a nonexistent id returned {result!r} -- the zero-rows-updated case must "
        f"surface as an error, not a silent status=ok"
    )


@pytest.mark.asyncio
async def test_mcp_archive_refused_for_graph_primary_spec_with_specific_reason(
    engine: NCEEngine, namespace_id: uuid.UUID
) -> None:
    """Same shape as REST's own graph-primary archive refusal
    (`test_resource_surface_kg_nodes_primary_live.py::
    test_archive_refused_with_specific_reason_not_generic_501`) -- asserted
    by name here for MCP, which had never been pinned at all.
    """
    tools = build_mcp_tool_specs(GRAPH_PROBE_SPEC)
    archive = tools[f"system_design_archive_{GRAPH_PROBE_SPEC.mcp_slug}"]
    result = json.loads(
        await archive.handler(engine, {"namespace_id": str(namespace_id), "id": "some-label"})
    )
    assert result.get("status_code") == 501
    assert "soft-delete" in result.get("error", ""), result
    assert "not supported yet for" not in result.get("error", ""), (
        "must not fall back to the generic (and now false) storage-kind message"
    )
