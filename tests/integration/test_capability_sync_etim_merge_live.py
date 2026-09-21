"""
tests/integration/test_capability_sync_etim_merge_live.py
============================================================
Live-Postgres sibling for `do_sync_device_capabilities`'s ETIM-merge
(`nce/vertical_modules/system_design/capability_sync.py`). Dispatched
from E's fourteen-site jsonb-decode sweep, assigned to Lane F.

THE BUG, EXACTLY
------------------
`product_catalog.etim_specs` is jsonb; no jsonb codec is registered on
this project's pool (confirmed: nce/semantic_search.py's own comment
states the same fact), so asyncpg always hands the column back as a raw
JSON string, never a dict. The pre-fix code was
`etim_specs = row["etim_specs"] if isinstance(row["etim_specs"], dict)
else {}` -- against a real row this is unconditionally False, so the
product's EXISTING etim_specs were silently discarded and replaced with
an empty dict before merging in the caller's own override, on every
single call.

NOT A DATA-LOSS INCIDENT -- established live, before fixing anything,
because it changes what this PR is
--------------------------------------------------------------------------
`product_catalog` is only ever SELECTed in this module (grepped: zero
UPDATE/INSERT/DELETE against it anywhere in capability_sync.py), matching
this module's own docstring ("without modifying Product files"). The
discarded value is never written back -- confirmed live: created a real
product_catalog row with real etim_specs, called do_sync_device_
capabilities with a partial override, and read product_catalog.etim_specs
back afterward: byte-identical to what was inserted. The blast radius is
entirely in the OUTPUT this function computes for a DIFFERENT table
(system_design_device_capabilities), not in the source data.

THE ACTUAL CONSEQUENCE, reproduced live
------------------------------------------
Before the fix, syncing a device from a product with real, populated
etim_specs (device_category, power_draw_watts, heat_btu_hr, poe_watts)
plus a partial caller override (e.g. just `{"poe_class": 6}`) produced a
system_design_device_capabilities row missing every field that should
have come from the catalog -- device_category/power_draw_watts/
heat_btu_hr/poe_watts all null, only poe_class (the override) and
manufacturer/model_number (passed as SEPARATE parameters, not through
the merge) survived. Every real sync call with a partial override
silently threw away the rest of the product's known specs.
"""

from __future__ import annotations

import json
import uuid

import asyncpg
import pytest

from nce.orchestrator import NCEEngine
from nce.vertical_modules.system_design.capability_sync import do_sync_device_capabilities

pytestmark = pytest.mark.integration

_REAL_ETIM_SPECS = {
    "manufacturer": "Extron",
    "device_category": "switcher",
    "power_draw_watts": 45,
    "heat_btu_hr": 153,
    "poe_class": 4,
    "poe_watts": 25.5,
}


@pytest.fixture
def engine(pg_pool: asyncpg.Pool) -> NCEEngine:
    eng = NCEEngine()
    eng.pg_pool = pg_pool
    return eng


async def _create_product(pg_pool: asyncpg.Pool, model_number: str) -> uuid.UUID:
    specs = dict(_REAL_ETIM_SPECS, model_number=model_number)
    async with pg_pool.acquire() as conn:
        return await conn.fetchval(
            "INSERT INTO product_catalog (manufacturer, mfr_part_no, product_source_id, etim_specs) "
            "VALUES ($1, $2, $3, $4::jsonb) RETURNING id",
            "Extron",
            model_number,
            f"SRC-{model_number}",
            json.dumps(specs),
        )


async def _create_kg_node(pg_pool: asyncpg.Pool, namespace_id: uuid.UUID, label: str) -> None:
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO kg_nodes (label, entity_type, namespace_id) VALUES ($1, 'DEVICE', $2)",
            label,
            namespace_id,
        )


@pytest.mark.asyncio
async def test_sync_merges_catalog_etim_specs_with_caller_override(
    engine: NCEEngine, namespace_id: uuid.UUID, pg_pool: asyncpg.Pool
) -> None:
    """The regression: a partial caller override must MERGE with, not
    replace, the product's real stored specs. Every field the fix removes
    the silent-discard for is asserted individually, not just "some
    capabilities came back."
    """
    model_number = f"DTP-{uuid.uuid4().hex[:8]}"
    product_id = await _create_product(pg_pool, model_number)
    device_label = f"probe-etim-merge-{uuid.uuid4().hex[:8]}"
    await _create_kg_node(pg_pool, namespace_id, device_label)

    result = await do_sync_device_capabilities(
        engine,
        {
            "namespace_id": str(namespace_id),
            "device_label": device_label,
            "product_id": str(product_id),
            "etim_specs": {"poe_class": 6},
        },
    )
    cap = result["device_capabilities"]

    assert cap["manufacturer"] == "Extron"
    assert cap["model_number"] == model_number
    assert cap["device_category"] == "switcher", (
        "device_category from the catalog's stored specs was lost -- the exact "
        "discard-before-merge defect this test exists to catch"
    )
    assert cap["power_draw_watts"] == 45, "power_draw_watts from the catalog was lost"
    assert cap["heat_btu_hr"] == 153, "heat_btu_hr from the catalog was lost"
    assert cap["poe_watts"] == 25.5, "poe_watts from the catalog was lost"
    assert cap["poe_class"] == 6, "the caller's override must still win over the catalog value"

    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT device_category, power_draw_watts, heat_btu_hr, poe_class, poe_watts "
            "FROM system_design_device_capabilities WHERE namespace_id = $1 AND node_label = $2",
            namespace_id,
            device_label,
        )
    assert row is not None
    assert row["device_category"] == "switcher"
    assert row["power_draw_watts"] == 45
    assert row["heat_btu_hr"] == 153
    assert row["poe_watts"] == 25.5
    assert row["poe_class"] == 6


@pytest.mark.asyncio
async def test_sync_does_not_write_back_to_product_catalog(
    engine: NCEEngine, namespace_id: uuid.UUID, pg_pool: asyncpg.Pool
) -> None:
    """Pins the finding that closed the incident question: product_catalog
    itself is never mutated by this sync, regardless of what the caller
    supplies as an override. If this ever starts failing, the severity of
    every future finding in this module changes -- from "wrong output" to
    "corrupted source data" -- and must be re-escalated accordingly.
    """
    model_number = f"DTP-{uuid.uuid4().hex[:8]}"
    product_id = await _create_product(pg_pool, model_number)
    device_label = f"probe-etim-nowrite-{uuid.uuid4().hex[:8]}"
    await _create_kg_node(pg_pool, namespace_id, device_label)

    async with pg_pool.acquire() as conn:
        before = await conn.fetchval(
            "SELECT etim_specs FROM product_catalog WHERE id = $1", product_id
        )

    await do_sync_device_capabilities(
        engine,
        {
            "namespace_id": str(namespace_id),
            "device_label": device_label,
            "product_id": str(product_id),
            "etim_specs": {"poe_class": 6, "power_draw_watts": 999},
        },
    )

    async with pg_pool.acquire() as conn:
        after = await conn.fetchval(
            "SELECT etim_specs FROM product_catalog WHERE id = $1", product_id
        )
    assert after == before, (
        "product_catalog.etim_specs changed after a sync call -- this module is documented "
        "and was measured as read-only against product_catalog; a write here is a new, "
        "more severe defect than the one this file exists to fix"
    )
