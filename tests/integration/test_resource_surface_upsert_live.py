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
"""

from __future__ import annotations

import json
import uuid

import asyncpg
import pytest

from nce.engine_registry import populate_engine_modules
from nce.orchestrator import NCEEngine
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
