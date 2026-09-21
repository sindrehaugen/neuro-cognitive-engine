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

POPULATION -- measured, not assumed. At this file's original measurement, 17
relational, archive-eligible specs declared a `soft_delete_field` (same
population `test_resource_surface_archive_restore_live.py` measured for the
REST side), and exactly 1 was tenant_scope="global" (`product:product-skus`).

2026-09-21: `product:product-skus` dropped out of the archive-eligible
population -- `excluded_verbs` now excludes `"archive"` for it (spec.py's
excluded_verbs reason (4)): `product_catalog` has no namespace/owner column,
so a caller-scoped soft-delete had no per-row authorization model to check
at that scope. The global (`is_global` true) branch of `handle_archive` is
no longer reachable from any registered spec's generated MCP surface;
`test_archive_is_not_registered_for_global_scope_specs` below asserts that
directly, against the tool registry, rather than testing a call that can no
longer be made. The tenant (`is_global` false) branch stays exercised by
`agreements:templates` (simple schema, no CHECK constraints, already used by
this session's list sibling).
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
    assert len(_RELATIONAL_ARCHIVE_ELIGIBLE) >= 16, (
        f"Only {len(_RELATIONAL_ARCHIVE_ELIGIBLE)} relational, archive-eligible, soft-delete "
        f"specs found -- expected at least 16 (was 17 at this file's original measurement; "
        f"product:product-skus dropped out when excluded_verbs excluded 'archive' for it, "
        f"spec.py's excluded_verbs reason (4))."
    )
    assert len(_GLOBAL_SCOPE) == 0, (
        f"Expected zero global-scope archive-eligible specs -- product:product-skus was the "
        f"one (excluded_verbs now excludes 'archive' for it; see spec.py's excluded_verbs "
        f"reason (4)), found {[f'{s.engine}:{s.entity}' for s in _GLOBAL_SCOPE]}"
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


def test_archive_is_not_registered_for_global_scope_specs() -> None:
    """`product:product-skus` was this file's own one global-scope archive-
    eligible spec (see the discovery floor above); it no longer is.
    `excluded_verbs` now excludes `"archive"` for it (spec.py's
    excluded_verbs reason (4)): the storage has no column expressing which
    caller owns a row, so a caller-scoped soft-delete has no per-row
    authorization model to check at this scope.

    Asserted against the generated tool registry, not a failed call: a tool
    that exists but errors and a tool that was never emitted look identical
    from the caller's side, and only the second is what this guards. This is
    a ratchet against the verb quietly coming back, not a behavior test --
    stronger than the live call it replaces, which could only prove the verb
    worked, never that it stays absent.
    """
    assert "archive" in SKU_SPEC.excluded_verbs
    assert "product_archive_product_skus" not in TOOL_REGISTRY
    generated = build_mcp_tool_specs(SKU_SPEC)
    assert not any(name.endswith("_archive_product_skus") for name in generated), generated


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
