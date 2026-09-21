"""
tests/integration/test_resource_surface_archive_restore_live.py
==================================================================
Live-Postgres sibling of
``tests/unit/test_resource_surface_generated_controls.py::test_generated_archive_and_restore``.

WHY THIS EXISTS
----------------
That unit test is parametrised over every registered ``ResourceSpec`` and
asserts 200 for archive/restore -- but its client (``_client_for_spec``)
builds a bare ``Starlette`` app with ``admin_state.engine`` never set, so
``rest.py``'s own gate (``if admin_state.engine and
getattr(admin_state.engine, "pg_pool", None):``) is always false and every
write takes the in-memory-dict branch, which applies no type checking and
has no real ``is_archived``-shaped column to be missing. Found live
2026-09-20: 25 specs advertised archive/restore against a soft-delete
column their real table did not have -- roughly 50 routes that would 500 in
production -- and the unit test above could not have caught it no matter
how the specs were declared, because it never reaches a database. Fixed in
PR #368 (``excluded_verbs={"archive"}`` on the 23 specs lacking the column;
2 more already excluded upsert/archive for unrelated reasons).

This file is the sibling that actually reaches Postgres, so the same class
of bug fails here if it recurs.

POPULATION -- MEASURED, NOT ASSUMED
-------------------------------------
Sized in ``_internal/work-docs/mlv16-orchestration/ARCHIVE_INTEGRATION_SIZING.md``
before this file was written (a read-only sizing pass, per dispatch). Of
the 56 registered specs, exactly 17 did not exclude ``archive``/``upsert``
and declared a ``soft_delete_field`` once PR #368 was in
(``main``@``fe7fd48``, confirmed by direct query below, not by re-reading
the sizing doc's numbers).

2026-09-21: down to 16 -- ``product:product-skus`` dropped out of the
population when ``excluded_verbs`` excluded ``"archive"`` for it (PR #404,
``spec.py``'s ``excluded_verbs`` reason (4): ``product_catalog`` has no
namespace/owner column, so a caller-scoped soft-delete had no per-row
authorization model to check against). Every one of the remaining 16 has a
real ``table_name`` -- zero are graph/kg_nodes-primary, so
``assert_owner``/``_seed_ownership`` (needed by a kg_nodes writer, see
``tests/test_agreements_sla.py``) does not apply to any of them: ``rest.py``'s
kg_nodes write path is gated behind ``if is_graph:`` (table_name is None),
never reached by this population.

Discovered building this file, NOT part of the sizing pass: ``rest.py``'s
``handle_create``/``handle_patch`` never parse a date/timestamp string from
the JSON request body into a real ``datetime``/``date`` before binding it to
Postgres (only ``version_field`` gets that treatment, ``rest.py:809``).
Reproduced directly: binding a plain ISO string to a ``timestamptz`` column
raises ``asyncpg.exceptions.DataError``. Of the (then-17, now-16) population,
this blocks exactly one -- ``notifications:reminders`` (``remind_at
TIMESTAMPTZ NOT NULL``, no
default: omitting it violates NOT NULL, supplying it raises DataError, so no
client payload can create this row today, through this route, at all). That
is a real, separate, wider bug (6 of the 56 registered specs cannot create a
row at all; 25 more silently 500 if a caller sets an otherwise-optional
date/timestamp field) -- reported upstream, not fixed here, since it is
outside archive/restore and outside this dispatch. ``reminders`` is skipped
below with that reason attached, not silently dropped from the parametrize
list.
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import Any

import asyncpg
import httpx
import pytest
from starlette.applications import Starlette

from nce import admin_state
from nce.orchestrator import NCEEngine
from nce.resource_surface import (
    ResourceSpec,
    get_all_resource_specs,
    load_all_engine_resources,
)
from nce.resource_surface.rest import make_resource_routes

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Population
# ---------------------------------------------------------------------------

load_all_engine_resources()
_ALL_SPECS = sorted(get_all_resource_specs(), key=lambda s: (s.engine, s.entity))
_ARCHIVE_ELIGIBLE = [
    s
    for s in _ALL_SPECS
    if "archive" not in s.excluded_verbs
    and "upsert" not in s.excluded_verbs
    and s.soft_delete_field
]

# Known-blocked by the date/timestamp-coercion bug this file's module
# docstring reports -- not an archive/restore defect, so not this file's to
# fix, but silently excluding it from the parametrize list would hide a real
# gap the same way the unit test's vacuous mock hid the original one.
_KNOWN_BLOCKED: dict[str, str] = {
    "notifications:reminders": (
        "remind_at TIMESTAMPTZ NOT NULL has no default, and rest.py's "
        "handle_create never parses a JSON date string into a real datetime "
        "before binding it to Postgres (only version_field gets that "
        "treatment, rest.py:809) -- asyncpg.exceptions.DataError either way. "
        "No client payload can create this row today. Reported upstream "
        "2026-09-20, not an archive/restore defect."
    ),
}

_SPEC_IDS = [f"{s.engine}:{s.entity}" for s in _ARCHIVE_ELIGIBLE]

# Enum-CHECK-constrained TEXT columns a generated string would violate.
# Each has a DEFAULT/nullability that satisfies the CHECK on omission --
# see nce/schema.sql's procurement_deal_registrations_status_check,
# sales_quotes_billing_method_check, and product_catalog_lifecycle_status_check
# (the third found only by running this file against real Postgres --
# reading the CREATE TABLE block alone missed it, since the CHECK is added
# by a later ALTER TABLE, nce/schema.sql:1431-1434).
_ENUM_CHECK_SKIP: dict[str, set[str]] = {
    "procurement_deal_registrations": {"status"},
    "sales_quotes": {"billing_method"},
    "product_catalog": {"lifecycle_status"},
}

_TEXT_TYPES = {"text", "character varying"}


def test_discovery_floor_matches_the_sized_population() -> None:
    """Guard-the-guard: the sizing doc measured exactly 17 on main@fe7fd48.
    2026-09-21: the floor moved to 16 -- product:product-skus dropped out
    when excluded_verbs excluded "archive" for it (PR #404, spec.py's
    excluded_verbs reason (4)), a real, deliberate exclusion, not a drift.
    A silent drop below 16 (a spec losing its soft_delete_field, or gaining
    an exclusion with no matching update here) would shrink parametrize
    coverage with no red anywhere else. An increase is fine and expected as
    new archivable specs land -- only a decrease below the measured floor is
    suspicious."""
    assert len(_ARCHIVE_ELIGIBLE) >= 16, (
        f"Only {len(_ARCHIVE_ELIGIBLE)} archive-eligible specs found, expected "
        f"at least 16 (was 17 at main@fe7fd48/PR #368; product:product-skus "
        f"dropped out 2026-09-21 via PR #404's excluded_verbs reason (4)). A "
        f"spec was excluded or lost its soft_delete_field since -- check "
        f"_internal/work-docs/mlv16-orchestration/ARCHIVE_INTEGRATION_SIZING.md."
    )
    assert all(s.table_name is not None for s in _ARCHIVE_ELIGIBLE), (
        "A graph/kg_nodes-primary spec entered the archive-eligible population -- "
        "this file's ownership-seeding exemption (module docstring) no longer "
        "holds, and assert_owner/_seed_ownership setup must be added."
    )


@pytest.fixture
def engine(pg_pool: asyncpg.Pool) -> NCEEngine:
    eng = NCEEngine()
    eng.pg_pool = pg_pool
    return eng


async def _text_columns(conn: asyncpg.Connection, table_name: str) -> dict[str, dict[str, Any]]:
    rows = await conn.fetch(
        "SELECT column_name, data_type, is_nullable, column_default, "
        "character_maximum_length "
        "FROM information_schema.columns WHERE table_schema='public' AND table_name=$1",
        table_name,
    )
    return {r["column_name"]: dict(r) for r in rows}


async def _enable_engine(pg_pool: asyncpg.Pool, namespace_id: uuid.UUID, engine_name: str) -> None:
    """Flip the namespace opt-in flag ``require_<engine>_enabled`` reads.

    ``tests/conftest.py``'s ``_insert_namespace`` sets no ``metadata`` at
    all, so a spec with an ``enabled_guard`` (agreements x3, product x2, and
    -- for the positive control below -- inventory) would 409 before
    ``handle_create`` ever runs.

    First cut of this used a two-element ``jsonb_set`` path
    (``ARRAY['<engine>', 'enabled']``) and silently no-opped: Postgres's
    ``jsonb_set`` only creates the FINAL path element when
    ``create_missing=true`` -- an absent intermediate object (here, the
    top-level ``<engine>`` key on a fresh ``{}``) is never created, so the
    call returned the input unchanged with no error. Caught by reading the
    row back after the update, not by trusting the 0-row-affected count
    (``UPDATE 1`` was reported -- the statement ran, it just built the wrong
    JSON). Fixed by setting the single top-level ``<engine>`` key directly,
    merged with whatever was already there so a real ``sources`` sibling
    (see ``require_nettailer_source_enabled``) is never clobbered.
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


async def _typed_sample_payload(
    conn: asyncpg.Connection, spec: ResourceSpec, namespace_id: uuid.UUID
) -> dict[str, object]:
    """Build a create payload asyncpg will actually accept against real
    Postgres, per ARCHIVE_INTEGRATION_SIZING.md's verified strategy: every
    non-text writable column on this population's tables is nullable or has
    a DEFAULT, so filtering to TEXT/VARCHAR columns and omitting everything
    else is sufficient -- not a per-spec guess, confirmed by inspecting
    every one of the (then-17, now-16) tables' real DDL before writing this
    function.
    """
    columns = await _text_columns(conn, spec.table_name)  # type: ignore[arg-type]
    skip = _ENUM_CHECK_SKIP.get(spec.table_name, set())
    payload: dict[str, object] = {"namespace_id": str(namespace_id)}
    for f in spec.writable_fields:
        if f in skip:
            continue
        col = columns.get(f)
        if col is not None and col["data_type"] in _TEXT_TYPES:
            value = f"generated_{f}_value"
            max_len = col["character_maximum_length"]
            # A bounded VARCHAR (legal_entities.country VARCHAR(8),
            # sales_deals/sales_quotes.currency VARCHAR(3)) raises
            # asyncpg.exceptions.StringDataRightTruncationError on the
            # unbounded generated string -- reproduced empirically, not
            # assumed. Truncating keeps every not-blank CHECK satisfied
            # (still non-empty) without hand-listing which columns are short.
            if max_len is not None and len(value) > max_len:
                value = value[:max_len]
            payload[f] = value

    if spec.table_name == "product_catalog":
        # Only global-scope spec in the population (namespace_id column is
        # dropped, ARCHIVE_INTEGRATION_SIZING.md) -- UNIQUE(manufacturer,
        # mfr_part_no) is not protected by per-test namespace isolation the
        # way every other spec's uniqueness constraints are.
        suffix = uuid.uuid4().hex[:12]
        payload["manufacturer"] = f"generated-mfr-{suffix}"
        payload["mfr_part_no"] = f"generated-part-{suffix}"

    return payload


def _rest_app(spec: ResourceSpec) -> Starlette:
    return Starlette(routes=make_resource_routes(spec))


async def _create(
    client: httpx.AsyncClient, spec: ResourceSpec, payload: dict[str, object]
) -> dict[str, Any]:
    resp = await client.post(spec.rest_collection_path, json=payload)
    assert resp.status_code == 201, (
        f"{spec.engine}:{spec.entity} create failed against real Postgres: {resp.text}"
    )
    return resp.json()


async def _seed_parent_id(
    conn: asyncpg.Connection,
    spec: ResourceSpec,
    namespace_id: uuid.UUID,
) -> dict[str, str]:
    """The 2 of 16 with a NOT NULL, no-default FK need a real parent row
    first (2 of 17 before product:product-skus dropped out of the population,
    2026-09-21; it needed no parent row, so the trivial side of this split
    shrank, not this one). Both parents (agreements, sales_deals) are
    themselves in the trivial-14 set, so this reuses the same typed-payload
    builder rather than a second fixture design
    (ARCHIVE_INTEGRATION_SIZING.md)."""
    if spec.table_name == "agreement_parties":
        parent_spec = next(s for s in _ALL_SPECS if s.table_name == "agreements")
        parent_payload = await _typed_sample_payload(conn, parent_spec, namespace_id)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_rest_app(parent_spec)), base_url="http://test"
        ) as parent_client:
            created = await _create(parent_client, parent_spec, parent_payload)
        return {"agreement_id": created["id"]}
    if spec.table_name == "sales_deal_participants":
        parent_spec = next(s for s in _ALL_SPECS if s.table_name == "sales_deals")
        parent_payload = await _typed_sample_payload(conn, parent_spec, namespace_id)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_rest_app(parent_spec)), base_url="http://test"
        ) as parent_client:
            created = await _create(parent_client, parent_spec, parent_payload)
        return {"deal_id": created["id"]}
    return {}


@pytest.mark.asyncio
@pytest.mark.parametrize("spec", _ARCHIVE_ELIGIBLE, ids=_SPEC_IDS)
async def test_archive_and_restore_against_real_postgres(
    spec: ResourceSpec,
    engine: NCEEngine,
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """The live counterpart of the unit test's vacuous version: creates a
    real row, archives it, restores it, and reads the ACTUAL column back
    from Postgres after each step -- not just the HTTP status code, which
    is all the original incident's own route would have needed to fake this
    green (see PR #368: 25 specs returned 200 from the mocked test while
    500ing against a real column that did not exist).
    """
    spec_id = f"{spec.engine}:{spec.entity}"
    if spec_id in _KNOWN_BLOCKED:
        pytest.skip(_KNOWN_BLOCKED[spec_id])

    if spec.enabled_guard is not None:
        await _enable_engine(pg_pool, namespace_id, spec.engine)

    previous_engine = admin_state.engine
    admin_state.engine = engine
    try:
        async with pg_pool.acquire() as conn:
            payload = await _typed_sample_payload(conn, spec, namespace_id)
            payload.update(await _seed_parent_id(conn, spec, namespace_id))

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_rest_app(spec)), base_url="http://test"
        ) as client:
            created = await _create(client, spec, payload)
            item_id = created[spec.id_field]

            archive_resp = await client.post(
                f"{spec.rest_item_path.format(id=item_id)}/archive",
                json={"namespace_id": str(namespace_id)},
            )
            assert archive_resp.status_code == 200, (
                f"{spec_id} archive failed against real Postgres: {archive_resp.text}"
            )

            async with pg_pool.acquire() as conn:
                row = await conn.fetchrow(
                    f"SELECT {spec.soft_delete_field} FROM {spec.table_name} "
                    f"WHERE {spec.id_field} = $1",
                    uuid.UUID(item_id),
                )
            assert row is not None, f"{spec_id}: row vanished after archive"
            assert row[spec.soft_delete_field] is True, (
                f"{spec_id}: archive route returned 200 but "
                f"{spec.table_name}.{spec.soft_delete_field} was not actually "
                f"set to true in Postgres -- the exact silent-success shape "
                f"PR #368 fixed for the other 23 specs."
            )

            restore_resp = await client.post(
                f"{spec.rest_item_path.format(id=item_id)}/restore",
                json={"namespace_id": str(namespace_id)},
            )
            assert restore_resp.status_code == 200, (
                f"{spec_id} restore failed against real Postgres: {restore_resp.text}"
            )

            async with pg_pool.acquire() as conn:
                row = await conn.fetchrow(
                    f"SELECT {spec.soft_delete_field} FROM {spec.table_name} "
                    f"WHERE {spec.id_field} = $1",
                    uuid.UUID(item_id),
                )
            assert row[spec.soft_delete_field] is False, (
                f"{spec_id}: restore route returned 200 but "
                f"{spec.table_name}.{spec.soft_delete_field} was not actually "
                f"reset to false in Postgres."
            )
    finally:
        admin_state.engine = previous_engine


# ---------------------------------------------------------------------------
# Positive control (U18): prove this sweep is sensitive to the EXACT defect
# class PR #368 fixed, against a REAL database -- the property the original
# mocked unit test could never have, no matter how it was written.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_positive_control_missing_column_is_caught_against_real_postgres(
    engine: NCEEngine, pg_pool: asyncpg.Pool, namespace_id: uuid.UUID
) -> None:
    """Reconstructs the exact tonight's-incident shape: a spec whose
    soft_delete_field names a column its real table does not have.
    `stock_locations` was one of the 23 PR #368 excluded (no is_archived
    column) -- this synthetically clears excluded_verbs on a copy of that
    real spec (same technique #368's own unit-level control uses,
    `dataclasses.replace(STOCK_LOCATION_SPEC, excluded_verbs=frozenset())`,
    per that PR's diff) and proves that, unlike the mocked unit test, THIS
    sweep's mechanism actually returns a 500 against a real Postgres
    connection -- i.e. this file's passing 200s above are not passing for
    the same vacuous reason the original test's were.
    """
    real_spec = next(
        s for s in _ALL_SPECS if s.engine == "inventory" and s.entity == "stock-locations"
    )
    assert real_spec.soft_delete_field is None, (
        "stock_locations no longer lacks a soft_delete_field -- this control's "
        "premise (one of PR #368's 23) is stale; pick a different excluded spec."
    )
    broken_spec = dataclasses.replace(real_spec, excluded_verbs=frozenset())
    assert broken_spec.enabled_guard is not None, (
        "stock_locations lost its enabled_guard -- drop this _enable_engine call "
        "if so, it would otherwise silently no-op."
    )
    await _enable_engine(pg_pool, namespace_id, broken_spec.engine)

    # Hand-built rather than the general _typed_sample_payload builder:
    # stock_locations is not one of the 16 real archive-eligible specs (it's
    # a borrowed real spec for this control only), and its
    # stock_locations_hierarchy_shape CHECK (nce/schema.sql:2597) requires a
    # real ('warehouse'|'van') `kind` with parent_id NULL and level = 0 --
    # the general builder's "generated_<field>_value" strategy correctly
    # does not special-case this one non-population table's constraint.
    payload = {
        "namespace_id": str(namespace_id),
        "kind": "warehouse",
        "name": "generated_name_value",
    }

    previous_engine = admin_state.engine
    admin_state.engine = engine
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_rest_app(broken_spec)), base_url="http://test"
        ) as client:
            created = await _create(client, broken_spec, payload)
            item_id = created[broken_spec.id_field]

            archive_resp = await client.post(
                f"{broken_spec.rest_item_path.format(id=item_id)}/archive",
                json={"namespace_id": str(namespace_id)},
            )
    finally:
        admin_state.engine = previous_engine

    assert archive_resp.status_code == 500, (
        f"Expected the synthetic missing-column spec to 500 against real Postgres "
        f"(reproducing the tonight's-incident shape), got {archive_resp.status_code}: "
        f"{archive_resp.text}. If this now passes, either stock_locations gained an "
        f"is_archived column (update the control) or this sweep stopped reaching a "
        f"real database at all (the exact silent regression this control exists to "
        f"catch)."
    )
    assert "is_archived" in archive_resp.text or "column" in archive_resp.text.lower(), (
        f"Got a 500 but not the expected UndefinedColumnError shape: {archive_resp.text}"
    )
