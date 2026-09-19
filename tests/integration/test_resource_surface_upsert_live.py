"""
tests/integration/test_resource_surface_upsert_live.py
========================================================
Live-Postgres regression test for the C12 resource-surface generic
``handle_upsert`` datetime/version bug (found 2026-09-19 building H-9's
Golden Thread v2 extension, fixed same day per ML-orch's Q-46 ruling).

WHY THIS MUST BE A LIVE TEST, NOT A MOCK
-----------------------------------------
Every existing unit test for a C12 resource (``test_agreements_resources.py``,
``test_sites_address_registry.py``, etc.) mocks the connection. A mock accepts
a Python ``str`` wherever a real ``datetime`` is expected; only asyncpg's real
binary protocol against a real ``timestamptz`` column enforces the type. The
bug survived because the code and its proof were tested apart -- a mocked
test for this fix would be worthless by construction, per the same reasoning
that keeps ``tests/integration/test_golden_thread.py`` a live suite.

THE BUG, EXACTLY
-----------------
``nce/resource_surface/mcp.py::handle_upsert`` built ``data[spec.version_field]``
(``version_field`` defaults to ``"updated_at"`` for every ``ResourceSpec`` --
26 of 30 registered specs as of this wave) from
``datetime.now(timezone.utc).isoformat()`` -- a ``str`` -- then bound it
straight into a parameterized INSERT/UPDATE against the real ``timestamptz``
column. asyncpg's binary codec for ``timestamptz`` requires an actual
``datetime.datetime`` instance and raises ``asyncpg.exceptions.DataError`` on
a ``str``, every time, for every tenant-scoped C12 resource on the real
(non-in-memory) storage path. Every real POST to any such resource's
``upsert`` verb against a live Postgres-backed deployment was failing this
way before this fix.

THE SECOND DEFECT THE NAIVE FIX WOULD HAVE INTRODUCED
-------------------------------------------------------
Binding a real ``datetime`` for the column write is necessary but not
sufficient. ``handle_upsert`` also uses ``spec.version_field`` for optimistic
concurrency (``expected_version``) and returns it to the caller as
``now.isoformat()`` (a ``"T"``-separated string) so the client can echo it back
next time. A naive fix that binds a real ``datetime`` but leaves the
comparison as ``str(existing.get(spec.version_field, ""))`` compares
``str(a_datetime)`` (space-separated: ``"2026-09-19 18:18:49+00:00"``)
against the client's remembered ``isoformat()`` value (``"2026-09-19T18:18:
49+00:00"``) -- they can never match, so a correct expected_version is
rejected as a version conflict on every single upsert against a real
Postgres-backed resource. That is worse than the crash it replaces: it looks
like it works (the write succeeds) while silently breaking concurrency
control. ``test_upsert_then_correct_expected_version_succeeds`` below is the
test that catches exactly this -- a version-comparison bug a type-only test
would miss.

THE SAME DEFECT, TWICE MORE, IN THE OTHER GENERATED SURFACE
--------------------------------------------------------------
C12 generates TWO backends per resource: the MCP tool handlers
(``nce/resource_surface/mcp.py``, fixed above) and the REST routes
(``nce/resource_surface/rest.py``). ML-orch reviewed the MCP fix, went
looking for the same shape elsewhere, and found ``rest.py`` had it worse:

* ``handle_create`` and ``handle_patch`` had the identical
  string-instead-of-datetime bug for ``version_field``.
* ``handle_create`` additionally set ``data["version"] = now_iso``
  *unconditionally*, and that dict became the column list for a raw
  ``INSERT INTO <table> (...)`` -- no C12 table has a ``version`` column
  (checked: sites, assets, documents, legal_entities, notifications,
  product_catalog, contractor_profiles all lack one), so **every real REST
  ``POST`` to any C12 resource raised ``UndefinedColumnError`` before the
  datetime type error was ever reached.** The generated REST create path had
  never worked against a real Postgres, for any resource.
* Fixing the type binding alone reproduced a THIRD defect this investigation
  found: the in-memory (no ``pg_pool``) fallback for both handlers returns
  its response dict unserialized. Once ``version_field`` holds a real
  ``datetime`` instead of a string, Starlette's plain ``JSONResponse`` cannot
  encode it (``TypeError: Object of type datetime is not JSON serializable``)
  -- reproduced live via a direct ASGI call before being caught. Both
  handlers now wrap their response dict in ``serialize_val`` (the same
  helper the real-Postgres path already gets via ``row_to_dict``).
* ``handle_bulk`` was checked and is clean: its own ``data`` dict never sets
  ``version_field`` at all, built only from ``spec.writable_fields`` plus
  ``id_field``/``namespace_id``.

Extended in the same PR rather than opened separately, per ML-orch's
reasoning: splitting this would ship a half-fixed surface where the MCP
tools work and the REST routes still crash -- the exact asymmetry (fixed
here, proven there) that let the original bug survive undetected.
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
from nce.resource_surface import get_all_resource_specs, load_all_engine_resources
from nce.resource_surface.rest import make_resource_routes
from nce.tool_registry import TOOL_REGISTRY

pytestmark = pytest.mark.integration


@pytest.fixture
def engine(pg_pool: asyncpg.Pool) -> NCEEngine:
    eng = NCEEngine()
    eng.pg_pool = pg_pool
    populate_engine_modules(eng)
    return eng


@pytest.mark.asyncio
async def test_upsert_insert_binds_a_real_datetime_not_a_string(
    engine: NCEEngine, namespace_id: uuid.UUID, pg_pool: asyncpg.Pool
) -> None:
    """The exact crash this fix resolves: INSERT must not raise DataError."""
    result = json.loads(
        await TOOL_REGISTRY["sites_upsert_sites"].handler(
            engine,
            {"namespace_id": str(namespace_id), "name": "GT Live Site", "site_type": "building"},
        )
    )
    assert result["status"] == "ok", f"upsert INSERT failed: {result}"

    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT name, updated_at FROM sites WHERE id = $1", uuid.UUID(result["id"])
        )
    assert row is not None, "INSERT reported ok but no row was written"
    assert row["name"] == "GT Live Site"
    # asyncpg decodes timestamptz to a real datetime; this is the type the
    # broken code could never successfully write in the first place.
    import datetime

    assert isinstance(row["updated_at"], datetime.datetime)


@pytest.mark.asyncio
async def test_upsert_then_correct_expected_version_succeeds(
    engine: NCEEngine, namespace_id: uuid.UUID
) -> None:
    """The version the client is handed back must be the version the next
    upsert can successfully present as expected_version -- proving the two
    comparison sites (real-Postgres row read, and the response payload) agree
    on format. This is the check a type-only fix would still fail.
    """
    r1 = json.loads(
        await TOOL_REGISTRY["sites_upsert_sites"].handler(
            engine,
            {"namespace_id": str(namespace_id), "name": "Version Site", "site_type": "building"},
        )
    )
    assert r1["status"] == "ok"

    r2 = json.loads(
        await TOOL_REGISTRY["sites_upsert_sites"].handler(
            engine,
            {
                "namespace_id": str(namespace_id),
                "id": r1["id"],
                "name": "Version Site Renamed",
                "expected_version": r1["version"],
            },
        )
    )
    assert r2["status"] == "ok", (
        f"A correct expected_version was rejected as a conflict: {r2}. This is "
        "the silent-concurrency-failure shape a datetime-binding-only fix "
        "introduces: the write succeeds but every optimistic-concurrency "
        "check on this resource then fails forever."
    )
    assert r2["version"] != r1["version"]


@pytest.mark.asyncio
async def test_positive_control_stale_expected_version_is_still_rejected(
    engine: NCEEngine, namespace_id: uuid.UUID
) -> None:
    """Standing positive control: proves the fix did not silently disable
    optimistic concurrency altogether to make the test above pass -- a stale
    version must still be a real, reported 409.
    """
    r1 = json.loads(
        await TOOL_REGISTRY["sites_upsert_sites"].handler(
            engine,
            {"namespace_id": str(namespace_id), "name": "Stale Site", "site_type": "building"},
        )
    )
    r2 = json.loads(
        await TOOL_REGISTRY["sites_upsert_sites"].handler(
            engine,
            {
                "namespace_id": str(namespace_id),
                "id": r1["id"],
                "name": "Stale Site Renamed Once",
                "expected_version": r1["version"],
            },
        )
    )
    assert r2["status"] == "ok"

    r3 = json.loads(
        await TOOL_REGISTRY["sites_upsert_sites"].handler(
            engine,
            {
                "namespace_id": str(namespace_id),
                "id": r1["id"],
                "name": "Stale Site Renamed Twice",
                "expected_version": r1["version"],  # stale: r2 already moved it on
            },
        )
    )
    assert r3.get("reason") == "version_conflict", (
        f"A genuinely stale expected_version was NOT rejected: {r3} -- the "
        "concurrency check is vacuous, not merely fixed."
    )
    assert r3.get("status_code") == 409


def _sites_rest_app() -> Starlette:
    load_all_engine_resources()
    site_spec = next(s for s in get_all_resource_specs() if s.engine == "sites")
    return Starlette(routes=make_resource_routes(site_spec))


@pytest.fixture
def sites_rest_client(engine: NCEEngine):
    """A real ASGI client for the generated sites REST routes, wired to the
    same live engine as the other tests in this file. ``admin_state.engine``
    is what the REST handlers actually read (not a fixture-injected engine),
    so it is set for the duration of the test and restored after.
    """
    previous = admin_state.engine
    admin_state.engine = engine
    try:
        yield httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_sites_rest_app()), base_url="http://test"
        )
    finally:
        admin_state.engine = previous


@pytest.mark.asyncio
async def test_rest_create_does_not_raise_undefined_column(
    sites_rest_client: httpx.AsyncClient, namespace_id: uuid.UUID
) -> None:
    """The exact crash this fix resolves: POST must not raise UndefinedColumnError
    for a bogus 'version' column, and must not raise DataError for the
    version_field datetime either.
    """
    async with sites_rest_client as client:
        r = await client.post(
            "/api/sites/sites",
            json={"namespace_id": str(namespace_id), "name": "REST Site", "site_type": "building"},
        )
    assert r.status_code == 201, f"REST create failed: {r.text}"
    body = r.json()
    assert body["status"] == "ok"
    assert "version" in body, "version must still be present in the response, just not in the row"


@pytest.mark.asyncio
async def test_rest_patch_then_correct_expected_version_succeeds(
    sites_rest_client: httpx.AsyncClient, namespace_id: uuid.UUID
) -> None:
    """REST equivalent of the MCP round-trip test above -- the version handed
    back by POST must be usable as expected_version on the next PATCH.
    """
    async with sites_rest_client as client:
        r1 = await client.post(
            "/api/sites/sites",
            json={
                "namespace_id": str(namespace_id),
                "name": "REST Version Site",
                "site_type": "building",
            },
        )
        assert r1.status_code == 201, r1.text
        item_id = r1.json()["id"]
        version1 = r1.json()["version"]

        r2 = await client.patch(
            f"/api/sites/sites/{item_id}",
            json={
                "namespace_id": str(namespace_id),
                "name": "REST Version Site Renamed",
                "expected_version": version1,
            },
        )
    assert r2.status_code == 200, (
        f"A correct expected_version was rejected: {r2.text} -- the same "
        "silent-concurrency-failure shape as the MCP path."
    )


@pytest.mark.asyncio
async def test_positive_control_rest_stale_expected_version_is_still_rejected(
    sites_rest_client: httpx.AsyncClient, namespace_id: uuid.UUID
) -> None:
    """REST equivalent of the MCP positive control -- a genuinely stale
    expected_version must still be a real 409, proving concurrency control
    was fixed, not disabled.
    """
    async with sites_rest_client as client:
        r1 = await client.post(
            "/api/sites/sites",
            json={
                "namespace_id": str(namespace_id),
                "name": "REST Stale Site",
                "site_type": "building",
            },
        )
        item_id = r1.json()["id"]
        version1 = r1.json()["version"]

        r2 = await client.patch(
            f"/api/sites/sites/{item_id}",
            json={
                "namespace_id": str(namespace_id),
                "name": "REST Stale Site Renamed Once",
                "expected_version": version1,
            },
        )
        assert r2.status_code == 200, r2.text

        r3 = await client.patch(
            f"/api/sites/sites/{item_id}",
            json={
                "namespace_id": str(namespace_id),
                "name": "REST Stale Site Renamed Twice",
                "expected_version": version1,  # stale: r2 already moved it on
            },
        )
    assert r3.status_code == 409, (
        f"A genuinely stale expected_version was NOT rejected over REST: {r3.text}"
    )
