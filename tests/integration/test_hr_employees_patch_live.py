"""
tests/integration/test_hr_employees_patch_live.py
=====================================================
Live-Postgres proof for a specific claim, not a defect: ML-orch asserted
tonight, without evidence, that "real deactivation is already reachable via
PATCH" for ``hr:employees``. H's audit separately found there is ZERO real-
Postgres PATCH coverage for ANY C12 resource -- the one generic PATCH
assertion (``tests/unit/test_resource_surface_generated_controls.py::
test_generated_patch``) runs with ``admin_state.engine`` unset, so it never
reaches ``rest.py``'s table-backed UPDATE branch at all.

This file closes that specific gap for one spec (``hr:employees``,
``active`` column) using the scratch Postgres already stood up for the
archive/restore (#375) and write-coercion (#378) work tonight -- not a new
container, not a new investigation.
"""

from __future__ import annotations

import uuid

import asyncpg
import httpx
import pytest
import pytest_asyncio
from starlette.applications import Starlette

from nce import admin_state
from nce.orchestrator import NCEEngine
from nce.resource_surface import get_all_resource_specs, load_all_engine_resources
from nce.resource_surface.rest import make_resource_routes

pytestmark = pytest.mark.integration

load_all_engine_resources()
_EMPLOYEES_SPEC = next(
    s for s in get_all_resource_specs() if s.engine == "hr" and s.entity == "employees"
)


@pytest.fixture
def engine(pg_pool: asyncpg.Pool) -> NCEEngine:
    eng = NCEEngine()
    eng.pg_pool = pg_pool
    return eng


@pytest_asyncio.fixture(autouse=True)
async def _enable_hr(pg_pool: asyncpg.Pool, namespace_id: uuid.UUID) -> None:
    """hr:employees carries require_hr_enabled -- a bare make_namespace()
    row has no metadata at all (tests/conftest.py's _insert_namespace), so
    every call below would 409 before handle_create/handle_patch ever ran.
    Single-element jsonb_set path, not two: Postgres's jsonb_set only
    creates the FINAL path element, even with create_missing=true -- a
    two-element path on a fresh {} silently no-ops (found and fixed the
    same way in test_resource_surface_archive_restore_live.py and
    test_resource_surface_write_coercion_live.py tonight)."""
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE namespaces SET metadata = jsonb_set("
            "coalesce(metadata, '{}'::jsonb), ARRAY[$2], "
            "coalesce(metadata->$2, '{}'::jsonb) || jsonb_build_object('enabled', true), "
            "true) WHERE id = $1",
            namespace_id,
            "hr",
        )


def _rest_app() -> Starlette:
    return Starlette(routes=make_resource_routes(_EMPLOYEES_SPEC))


async def _rest(engine: NCEEngine, method: str, path: str, json_body: dict) -> httpx.Response:
    previous = admin_state.engine
    admin_state.engine = engine
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_rest_app()), base_url="http://test"
        ) as client:
            return await client.request(method, path, json=json_body)
    finally:
        admin_state.engine = previous


@pytest.mark.asyncio
async def test_rest_patch_deactivates_and_reactivates_employee_against_real_postgres(
    engine: NCEEngine, pg_pool: asyncpg.Pool, namespace_id: uuid.UUID
) -> None:
    """The exact claim under test: a real PATCH of `active` reaches the
    table-backed UPDATE branch and the real column changes -- not just a
    200, which the in-memory branch would fake identically (see module
    docstring)."""
    create_resp = await _rest(
        engine,
        "POST",
        "/api/hr/employees",
        {
            "namespace_id": str(namespace_id),
            "employee_id": "EMP-PATCH-1",
            "name": "Deactivation Target",
            "active": True,
        },
    )
    assert create_resp.status_code == 201, create_resp.text
    item_id = create_resp.json()["id"]
    version1 = create_resp.json()["version"]

    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT active FROM employees WHERE id = $1", uuid.UUID(item_id))
    assert row["active"] is True, "create did not actually persist active=true"

    deactivate_resp = await _rest(
        engine,
        "PATCH",
        f"/api/hr/employees/{item_id}",
        {"namespace_id": str(namespace_id), "active": False, "expected_version": version1},
    )
    assert deactivate_resp.status_code == 200, (
        f"PATCH active=False failed against real Postgres: {deactivate_resp.text}"
    )
    # handle_patch's response has no separate top-level "version" key the
    # way handle_create's does -- the current version lives under its own
    # column name (version_field == "updated_at" for this spec) inside the
    # returned item itself.
    version2 = deactivate_resp.json()[_EMPLOYEES_SPEC.version_field]

    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT active FROM employees WHERE id = $1", uuid.UUID(item_id))
    assert row["active"] is False, (
        "PATCH reported 200 but employees.active was not actually set to "
        "false in Postgres -- the exact silent-success shape a mocked "
        "in-memory PATCH test could never catch."
    )

    reactivate_resp = await _rest(
        engine,
        "PATCH",
        f"/api/hr/employees/{item_id}",
        {"namespace_id": str(namespace_id), "active": True, "expected_version": version2},
    )
    assert reactivate_resp.status_code == 200, (
        f"PATCH active=True (reactivation) failed: {reactivate_resp.text}"
    )

    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT active FROM employees WHERE id = $1", uuid.UUID(item_id))
    assert row["active"] is True, "reactivation PATCH did not actually persist active=true"


@pytest.mark.asyncio
async def test_positive_control_stale_expected_version_still_rejected_on_real_postgres(
    engine: NCEEngine, pg_pool: asyncpg.Pool, namespace_id: uuid.UUID
) -> None:
    """U18: proves the PATCH round-trip above is exercising real optimistic
    concurrency, not silently ignoring expected_version -- a genuinely
    stale version must still be a real 409 against the real Postgres row.
    """
    create_resp = await _rest(
        engine,
        "POST",
        "/api/hr/employees",
        {
            "namespace_id": str(namespace_id),
            "employee_id": "EMP-PATCH-2",
            "name": "Concurrency Target",
            "active": True,
        },
    )
    assert create_resp.status_code == 201, create_resp.text
    item_id = create_resp.json()["id"]
    stale_version = create_resp.json()["version"]

    first_patch = await _rest(
        engine,
        "PATCH",
        f"/api/hr/employees/{item_id}",
        {"namespace_id": str(namespace_id), "active": False, "expected_version": stale_version},
    )
    assert first_patch.status_code == 200, first_patch.text

    second_patch = await _rest(
        engine,
        "PATCH",
        f"/api/hr/employees/{item_id}",
        {
            "namespace_id": str(namespace_id),
            "active": True,
            "expected_version": stale_version,  # stale: first_patch already moved it on
        },
    )
    assert second_patch.status_code == 409, (
        f"A genuinely stale expected_version was NOT rejected against real "
        f"Postgres: {second_patch.text}"
    )
    assert second_patch.json().get("reason") == "version_conflict"

    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT active FROM employees WHERE id = $1", uuid.UUID(item_id))
    assert row["active"] is False, (
        "the rejected stale PATCH must not have applied its value -- "
        "active should still be false from the first, accepted PATCH"
    )
