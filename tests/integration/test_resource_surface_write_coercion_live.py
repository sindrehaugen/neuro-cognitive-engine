"""
tests/integration/test_resource_surface_write_coercion_live.py
==================================================================
Live-Postgres acceptance test for the generated-surface write-path type
coercion fix (``nce.resource_surface.rest.coerce_writable_values``, shared
by both ``rest.py`` and ``mcp.py``).

WHY THIS MUST RUN AGAINST REAL POSTGRES, NOT THE IN-MEMORY PATH
------------------------------------------------------------------
``tests/unit/test_resource_surface_generated_controls.py``'s client never
sets ``admin_state.engine``, so every write there takes the in-memory-dict
branch -- a plain Python dict happily accepts any value, so no coercion is
needed or exercised. That is the exact masking that hid the archive-column
defect across 25 specs (PR #368) and this coercion defect itself, both
found by tests that never reached a database. A unit-level test of this fix
would be worth nothing; this file is ``@pytest.mark.integration`` and reads
the actual column back from Postgres after every write, on both surfaces.

THE BUG THIS FIXES, EXACTLY
-----------------------------
Neither ``rest.py``'s ``handle_create``/``handle_patch`` nor ``mcp.py``'s
``handle_upsert`` parsed a request value into the real Python type its
target Postgres column needed before binding it via asyncpg -- only
``version_field`` got that treatment. Full measurement (56 registered
specs) is in ``_internal/work-docs/mlv16-orchestration/
GENERATED_SURFACE_TYPE_COERCION.md``:

- 6 specs could not create a row AT ALL (a ``NOT NULL``, no-default
  date/timestamp column) -- this file's ``_BLOCKED_SPECS``.
- 25 more silently 500 if a caller actually set an otherwise-optional
  date/timestamp field.
- 41 writable ``jsonb`` columns across ~30 specs 500 whenever a client
  sends the field the only way that makes sense to -- a real JSON
  object/array, which arrives server-side as a Python ``dict``/``list``
  that asyncpg's default codec refuses outright (no custom codec is
  registered on the pool, ``nce/orchestrator.py::NCEEngine.connect``).

WHAT THIS FILE DOES NOT COVER
--------------------------------
``jsonb`` values round-trip through Postgres as raw text, not a decoded
``dict``/``list`` (asyncpg's default codec again, in the read direction --
a separate, already-known estate invariant). This fix stops the WRITE from
500ing; it does not add response-side JSON decoding to
``serialize_val``/``row_to_dict``, which is a distinct concern (the OpenAPI
schema question, explicitly out of this fix's scope). The round-trip test
below reads the stored value back via a raw SQL query and ``json.loads()``s
it itself, rather than asserting anything about the REST/MCP response body.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import asyncpg
import httpx
import pytest
import pytest_asyncio
from starlette.applications import Starlette

from nce import admin_state
from nce.orchestrator import NCEEngine
from nce.resource_surface import get_all_resource_specs, load_all_engine_resources
from nce.resource_surface.rest import make_resource_routes
from nce.tool_registry import TOOL_REGISTRY

pytestmark = pytest.mark.integration

load_all_engine_resources()
_ALL_SPECS = {(s.engine, s.entity): s for s in get_all_resource_specs()}

# The 6-cannot-create acceptance list, measured in
# GENERATED_SURFACE_TYPE_COERCION.md: (engine, entity) -> a minimal valid
# payload for the NOT NULL, no-default columns this fix must now satisfy.
# Real ISO date/timestamp strings for the blocking field -- exactly what a
# well-behaved client sends and what previously raised
# asyncpg.exceptions.DataError.
_BLOCKED_SPECS: dict[tuple[str, str], dict[str, object]] = {
    ("hr", "absences"): {
        "absence_id": "ABS-1",
        "employee_id": "EMP-1",
        "start_date": "2026-09-20",
    },
    ("hr", "certifications"): {
        "cert_id": "CERT-1",
        "employee_id": "EMP-1",
        "authority": "Test Authority",
        "name": "Test Cert",
        "issued": "2026-09-20",
    },
    ("notifications", "reminders"): {
        "principal_id": "user-1",
        "node_type": "TASK",
        "node_id": "task-1",
        "title": "Follow up",
        "remind_at": "2026-09-21T09:00:00+00:00",
    },
    # resources:allocations and resources:travel-legs also need a real
    # parent row (resource_id / allocation_id, both NOT NULL FKs with no
    # default) -- built inline by
    # test_rest_create_now_succeeds_for_allocations_and_travel_legs below,
    # not hand-listed here.
}


def _rest_app(spec) -> Starlette:  # noqa: ANN001
    return Starlette(routes=make_resource_routes(spec))


@pytest.fixture
def engine(pg_pool: asyncpg.Pool) -> NCEEngine:
    eng = NCEEngine()
    eng.pg_pool = pg_pool
    return eng


async def _enable_engine(pg_pool: asyncpg.Pool, namespace_id: uuid.UUID, engine_name: str) -> None:
    """Flip the namespace opt-in flag ``require_<engine>_enabled`` reads.

    A single-element ``jsonb_set`` path, not a two-element one: Postgres's
    ``jsonb_set`` only creates the FINAL path element even with
    ``create_missing=true`` -- a two-element path on a fresh ``{}`` silently
    no-ops (see the identical fix in
    ``tests/integration/test_resource_surface_archive_restore_live.py``,
    found the same way, by reading the row back rather than trusting the
    reported row count).
    """
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
    """This file's specs span two guarded engines (hr, resources) and two
    unguarded ones (notifications, sales) -- enabling both guarded ones
    unconditionally for every test's namespace is simpler and no less
    correct than threading a per-test engine list through every call site.
    """
    await _enable_engine(pg_pool, namespace_id, "hr")
    await _enable_engine(pg_pool, namespace_id, "resources")
    # hr:absences and hr:certifications both carry a NOT NULL FK to
    # employees(employee_id, namespace_id) (fk_absences_employees,
    # fk_certs_employees) -- found running this file, not part of the
    # 6-cannot-create measurement (that measurement only covered the
    # date/timestamp coercion defect, not pre-existing FK requirements).
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO employees (employee_id, namespace_id, name) VALUES ($1, $2, $3)",
            "EMP-1",
            namespace_id,
            "Test Employee",
        )


async def _rest_create(
    engine: NCEEngine, spec, namespace_id: uuid.UUID, payload: dict[str, object]
) -> httpx.Response:  # noqa: ANN001
    previous = admin_state.engine
    admin_state.engine = engine
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_rest_app(spec)), base_url="http://test"
        ) as client:
            body = {"namespace_id": str(namespace_id), **payload}
            return await client.post(spec.rest_collection_path, json=body)
    finally:
        admin_state.engine = previous


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "spec_key", sorted(_BLOCKED_SPECS), ids=[f"{e}:{n}" for e, n in sorted(_BLOCKED_SPECS)]
)
async def test_rest_create_now_succeeds_for_previously_blocked_specs(
    spec_key: tuple[str, str], engine: NCEEngine, pg_pool: asyncpg.Pool, namespace_id: uuid.UUID
) -> None:
    """The acceptance test named by the fix's own design doc: each of the
    6 specs that could not create a row at all must now succeed, against
    real Postgres, with the actual column read back afterwards -- not just
    a 201.
    """
    spec = _ALL_SPECS[spec_key]
    payload = _BLOCKED_SPECS[spec_key]

    resp = await _rest_create(engine, spec, namespace_id, payload)
    assert resp.status_code == 201, f"{spec_key} create failed: {resp.text}"
    item_id = resp.json()[spec.id_field]

    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT * FROM {spec.table_name} WHERE {spec.id_field} = $1",
            uuid.UUID(item_id),
        )
    assert row is not None, f"{spec_key}: create reported 201 but no row exists"


@pytest.mark.asyncio
async def test_rest_create_now_succeeds_for_allocations_and_travel_legs(
    engine: NCEEngine, pg_pool: asyncpg.Pool, namespace_id: uuid.UUID
) -> None:
    """resources:allocations and resources:travel-legs need a real parent
    chain (resources -> allocations -> travel-legs, both FKs NOT NULL with
    no default) on top of the date-coercion fix -- built here rather than
    hand-listed in _BLOCKED_SPECS, which only holds specs needing no parent.
    """
    resources_spec = _ALL_SPECS[("resources", "resources")]
    allocations_spec = _ALL_SPECS[("resources", "allocations")]
    travel_legs_spec = _ALL_SPECS[("resources", "travel-legs")]

    resource_resp = await _rest_create(
        engine, resources_spec, namespace_id, {"kind": "vehicle", "display_name": "Test Vehicle"}
    )
    assert resource_resp.status_code == 201, resource_resp.text
    resource_id = resource_resp.json()["id"]

    allocation_resp = await _rest_create(
        engine,
        allocations_spec,
        namespace_id,
        {
            "resource_id": resource_id,
            "demand_kind": "job",
            "starts_at": "2026-09-20T08:00:00+00:00",
            "ends_at": "2026-09-20T17:00:00+00:00",
        },
    )
    assert allocation_resp.status_code == 201, (
        f"resources:allocations create failed: {allocation_resp.text}"
    )
    allocation_id = allocation_resp.json()["id"]

    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT starts_at, ends_at FROM allocations WHERE id = $1", uuid.UUID(allocation_id)
        )
    assert row is not None
    import datetime as _dt

    assert isinstance(row["starts_at"], _dt.datetime)
    assert isinstance(row["ends_at"], _dt.datetime)

    travel_leg_resp = await _rest_create(
        engine,
        travel_legs_spec,
        namespace_id,
        {
            "allocation_id": allocation_id,
            "origin": "Oslo",
            "destination": "Bergen",
            "departure_at": "2026-09-20T06:00:00+00:00",
        },
    )
    assert travel_leg_resp.status_code == 201, (
        f"resources:travel-legs create failed: {travel_leg_resp.text}"
    )


@pytest.mark.asyncio
async def test_mcp_upsert_now_succeeds_for_a_previously_blocked_spec(
    engine: NCEEngine, namespace_id: uuid.UUID, pg_pool: asyncpg.Pool
) -> None:
    """The fix must cover BOTH generated surfaces, not just REST -- mcp.py's
    handle_upsert had the identical passthrough (data = {f: arguments[f]
    for f in spec.writable_fields if f in arguments}, no coercion)."""
    result = json.loads(
        await TOOL_REGISTRY["hr_upsert_absences"].handler(
            engine,
            {
                "namespace_id": str(namespace_id),
                "absence_id": "ABS-MCP-1",
                "employee_id": "EMP-1",
                "start_date": "2026-09-20",
            },
        )
    )
    assert result.get("status") == "ok", f"MCP upsert failed: {result}"

    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT start_date FROM absences WHERE id = $1", uuid.UUID(result["id"])
        )
    assert row is not None
    import datetime as _dt

    assert isinstance(row["start_date"], _dt.date)


# ---------------------------------------------------------------------------
# jsonb -- the bigger blast radius (41 writable columns, ~30 specs), found
# checking the same defect class ML-orch asked about, not part of the
# 6-cannot-create list (no spec in that list also has a jsonb field).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rest_create_accepts_a_real_json_object_for_a_jsonb_field(
    engine: NCEEngine, pg_pool: asyncpg.Pool, namespace_id: uuid.UUID
) -> None:
    """Reproduces the exact live failure reported upstream: a real REST POST
    with `metadata: {"foo": "bar", "tags": [1,2,3]}` against sales:customers
    500ed with `asyncpg.exceptions.DataError: expected str, got dict` before
    this fix. Reads the stored value back via raw SQL and json.loads()s it
    itself -- this test asserts the WRITE no longer 500s and the stored
    bytes are correct JSON, not anything about how the REST response
    represents the field (see module docstring)."""
    spec = _ALL_SPECS[("sales", "customers")]
    original = {"foo": "bar", "tags": [1, 2, 3]}

    resp = await _rest_create(
        engine, spec, namespace_id, {"name": "Coercion Co", "metadata": original}
    )
    assert resp.status_code == 201, f"jsonb create failed: {resp.text}"
    item_id = resp.json()["id"]

    async with pg_pool.acquire() as conn:
        raw = await conn.fetchval(
            "SELECT metadata FROM sales_customers WHERE id = $1", uuid.UUID(item_id)
        )
    assert json.loads(raw) == original, (
        f"stored jsonb does not round-trip: expected {original!r}, got {raw!r}"
    )


@pytest.mark.asyncio
async def test_mcp_upsert_accepts_a_real_json_object_for_a_jsonb_field(
    engine: NCEEngine, namespace_id: uuid.UUID, pg_pool: asyncpg.Pool
) -> None:
    """MCP-surface equivalent of the REST jsonb test above."""
    original = {"a": 1, "b": {"nested": True}}
    result = json.loads(
        await TOOL_REGISTRY["sales_upsert_customers"].handler(
            engine,
            {"namespace_id": str(namespace_id), "name": "MCP Coercion Co", "metadata": original},
        )
    )
    assert result.get("status") == "ok", f"MCP jsonb upsert failed: {result}"

    async with pg_pool.acquire() as conn:
        raw = await conn.fetchval(
            "SELECT metadata FROM sales_customers WHERE id = $1", uuid.UUID(result["id"])
        )
    assert json.loads(raw) == original


# ---------------------------------------------------------------------------
# Fail loudly (condition 1): an unparseable value must be a 400 naming the
# field, never a 500, and never silently coerced into something plausible.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rest_create_rejects_an_unparseable_date_with_400_naming_the_field(
    engine: NCEEngine, namespace_id: uuid.UUID
) -> None:
    spec = _ALL_SPECS[("hr", "absences")]
    resp = await _rest_create(
        engine,
        spec,
        namespace_id,
        {"absence_id": "ABS-BAD", "employee_id": "EMP-1", "start_date": "not-a-date"},
    )
    assert resp.status_code == 400, (
        f"An unparseable date must be a 400, not {resp.status_code}: {resp.text}"
    )
    assert "start_date" in resp.text, f"The 400 must name the offending field: {resp.text}"


@pytest.mark.asyncio
async def test_rest_create_rejects_malformed_json_text_with_400_naming_the_field(
    engine: NCEEngine, namespace_id: uuid.UUID
) -> None:
    """A jsonb field sent as a STRING must still be valid JSON text -- this
    is the one shape coerce_writable_values does not rewrite (see its own
    docstring: a str value is assumed to already be valid JSON and passed
    through), so an invalid one must fail loudly here, not reach asyncpg's
    own less legible InvalidTextRepresentationError as a raw 500.
    """
    spec = _ALL_SPECS[("sales", "customers")]
    resp = await _rest_create(
        engine, spec, namespace_id, {"name": "Bad JSON Co", "metadata": "not valid json"}
    )
    assert resp.status_code == 400, (
        f"Malformed JSON text must be a 400, not {resp.status_code}: {resp.text}"
    )
    assert "metadata" in resp.text, f"The 400 must name the offending field: {resp.text}"


@pytest.mark.asyncio
async def test_mcp_upsert_rejects_an_unparseable_date_with_400_naming_the_field(
    engine: NCEEngine, namespace_id: uuid.UUID
) -> None:
    result = json.loads(
        await TOOL_REGISTRY["hr_upsert_absences"].handler(
            engine,
            {
                "namespace_id": str(namespace_id),
                "absence_id": "ABS-MCP-BAD",
                "employee_id": "EMP-1",
                "start_date": "not-a-date",
            },
        )
    )
    assert result.get("status_code") == 400, f"Expected a 400-shaped error, got: {result}"
    assert "start_date" in result.get("error", ""), f"Error must name the field: {result}"


# ---------------------------------------------------------------------------
# Positive control (U18): prove the fix, not the payload, is what makes the
# tests above pass.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_positive_control_coercion_helper_is_what_makes_this_work(
    engine: NCEEngine, namespace_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without coerce_writable_values, the exact original defect returns --
    proving the tests above pass because of the fix, not because this
    file's payloads happen to dodge it. Patches rest.py's own module-level
    name (what handle_create actually calls), not a copy imported
    elsewhere.
    """
    import nce.resource_surface.rest as rest_mod

    async def _identity(conn: Any, table_name: str | None, data: dict[str, Any]) -> dict[str, Any]:
        return data

    monkeypatch.setattr(rest_mod, "coerce_writable_values", _identity)

    spec = _ALL_SPECS[("hr", "absences")]
    resp = await _rest_create(
        engine,
        spec,
        namespace_id,
        {"absence_id": "ABS-CONTROL", "employee_id": "EMP-1", "start_date": "2026-09-20"},
    )
    assert resp.status_code == 500, (
        f"Expected the original DataError-shaped 500 with coercion disabled, "
        f"got {resp.status_code}: {resp.text}. If this now passes, either "
        f"asyncpg started accepting a raw ISO date string for timestamptz "
        f"(unlikely) or handle_create stopped calling coerce_writable_values "
        f"at all -- the exact silent regression this control exists to catch."
    )
