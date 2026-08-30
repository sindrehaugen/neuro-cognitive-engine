"""Tenant-isolation tests for the Assets surface's three Wave-3 (Batch 143)
DB functions -- ``nce/vertical_modules/assets/mcp_handlers.py``'s
``do_get_asset`` / ``do_list_assets`` / ``do_advance_lifecycle``.

Why this file exists
---------------------
``tests/unit/test_assets_surface.py`` (Batch 143's own acceptance test)
replaces ``scoped_pg_session`` with a pass-through over a fully mocked
``AsyncMock`` connection that returns its configured fixture regardless of
the SQL or arguments it is given. That is correct for proving *shape*, but
it cannot prove the one thing that actually matters here: that each
function's ``namespace_id`` predicate is load-bearing. Stripping every
tenant predicate from all three functions and running the unit suite still
gives ``33 passed`` -- confirmed by an out-of-tree audit build before this
file was written. This file is the fix: real Postgres, real rows, two real
tenants, and a proof that goes RED when the predicate is removed.

Precedent, and where this file must diverge from it
-------------------------------------------------------
``tests/test_assets_seed.py`` (Batch 142, this same ``assets`` table)
already built the two-level pattern this file follows:

  * ``test_nce_app_pool_isolates_namespaces_and_refuses_delete`` (:397) --
    the RLS **policy**, through a real restricted ``nce_app`` connection.
  * ``test_replay_readback_is_namespace_scoped_not_bom_line_scoped`` (:545)
    -- the **owner pool** level, using a genuine cross-namespace collision
    on ``bom_line_id`` (a per-namespace business key, so two tenants CAN
    pick the same value) to prove ``seed.py``'s own predicate -- not the
    RLS policy -- is what separates them.

That collision recipe does not transfer to ``do_get_asset`` /
``do_advance_lifecycle``: their identifier is ``assets.id``, migration 054's
plain ``PRIMARY KEY (id)`` -- **globally** unique, not scoped to
``(namespace_id, id)``. Two rows literally cannot share one ``id`` value (a
direct ``INSERT`` reusing an existing ``id`` raises
``UniqueViolationError`` before RLS or any Python predicate is even
reached), so "seed the same id in two namespaces" is not a scenario that
exists to test. The substitute used below is the practical equivalent --
arguably the more realistic attack shape -- of the same claim: call the
function with the WRONG tenant's ``namespace_id`` paired with the RIGHT,
real ``asset_id`` of another tenant's row (something an attacker would need
to have leaked or guessed -- exactly what the predicate exists to make
useless). ``do_list_assets`` has no such constraint on its optional
``functional_location_id`` filter (a free-text column, no uniqueness
constraint), so that one test below DOES use a genuine value collision,
mirroring the precedent exactly.

Which level "matters in production" -- verified, not assumed
------------------------------------------------------------------
``nce/orchestrator.py``'s ``NCEEngine.connect()`` builds ``self.pg_pool``
directly from ``cfg.PG_DSN`` (``nce/orchestrator.py:133-134``), and every
``do_*`` core in this codebase -- including all three under test here -- is
called with that same ``engine.pg_pool``. In this repository's own default
config (``nce/config.py:1153``) and in this integration job's ``ci.yml``
env, ``PG_DSN`` is the ``mcp_user`` role, verified against the live
container for this run: ``rolsuper=true, rolbypassrls=true``. ``nce_app``
is used in production ONLY for one thing --
``NCEEngine._verify_worm_enforcement``'s boot-time self-check
(``nce/orchestrator.py:483-520``) -- never for serving a ``do_get_asset`` /
``do_list_assets`` / ``do_advance_lifecycle`` call. The owner-pool tests
below are therefore the ones that reflect what this deployment actually
runs; the ``nce_app`` tests are a defense-in-depth proof of the RLS POLICY
itself, and -- exactly as the precedent's own docstring says of test (e) --
do NOT discriminate on this module's own SQL predicate. Stripping the
predicate and re-running only the ``nce_app`` tests would NOT go red; that
is expected, not a defect, and is why the RED proof in this wave's report
is taken only from the owner-pool tests.

Integration tests (``@pytest.mark.integration``) are picked up
automatically by ``.github/workflows/ci.yml``'s ``tests/test_assets_*.py``
prefix glob (Batch 152a) -- no workflow edit accompanies this file.
"""

from __future__ import annotations

import os
import uuid
from typing import Any
from urllib.parse import urlparse, urlunparse

import asyncpg  # type: ignore[import-untyped]
import pytest

from nce.config import cfg
from nce.vertical_modules.assets.lifecycle import load_lifecycle_config
from nce.vertical_modules.assets.mcp_handlers import (
    do_advance_lifecycle,
    do_get_asset,
    do_list_assets,
)
from nce.vertical_modules.assets.seed import do_seed_asset_from_bom

# ---------------------------------------------------------------------------
# Shared helpers -- duplicated per test_assets_*.py file convention (see
# test_assets_seed.py's own _app_dsn docstring: "Verbatim in shape from
# tests/test_inventory_rma.py::_app_dsn" -- one idiom, copied, not imported).
# ---------------------------------------------------------------------------


class _EngineStub:
    def __init__(self, pg_pool: asyncpg.Pool) -> None:  # type: ignore[type-arg]
        self.pg_pool = pg_pool


def _app_dsn() -> str:
    """Rewrite the integration DSN onto the restricted nce_app role.

    Verbatim in shape from tests/test_assets_seed.py::_app_dsn /
    tests/test_inventory_rma.py::_app_dsn.
    """
    primary = (
        os.environ.get("NCE_INTEGRATION_PG_DSN")
        or os.environ.get("PG_DSN")
        or os.environ.get("DATABASE_URL")
        or cfg.PG_DSN
    )
    parsed = urlparse(primary)
    netloc = parsed.hostname or ""
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    app_pass = cfg.NCE_APP_PASSWORD or "nce_app_secret"
    netloc = f"nce_app:{app_pass}@{netloc}"
    return urlunparse(parsed._replace(netloc=netloc))


async def _seed(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    *,
    bom_line_id: str,
    serial: str | None = None,
    functional_location_id: str | None = None,
) -> dict[str, Any]:
    """Seed one real assets row through the already-proven Wave-2 writer."""
    engine = _EngineStub(pg_pool)
    result = await do_seed_asset_from_bom(
        engine,
        {
            "namespace_id": namespace_id,
            "bom_line_id": bom_line_id,
            "serial": serial,
            "functional_location_id": functional_location_id,
        },
    )
    assert result["ok"] is True
    return result


# ===========================================================================
# OWNER-POOL LEVEL -- proves each function's OWN namespace_id predicate.
# This is the level that reflects production (see module docstring).
# ===========================================================================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_do_get_asset_refuses_another_tenants_real_id_at_owner_pool_level(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    make_namespace: Any,
) -> None:
    """do_get_asset(ns_b, real_id_of_ns_a's_asset) must return None.

    assets.id is a global PRIMARY KEY (migration 054) -- no two rows can
    share one, so this is the closest real equivalent of the seed.py
    collision test: a wrong tenant naming a right, foreign identifier.

    Goes RED if "AND id = $2::uuid" loses its "namespace_id = $1::uuid"
    partner in do_get_asset's WHERE clause -- the query would then find the
    row by id alone and return ns_a's data to ns_b's caller.
    """
    ns_a = await make_namespace()
    ns_b = await make_namespace()
    seeded = await _seed(pg_pool, ns_a, bom_line_id="BOM-ISO-GET-1", serial="SN-ISO-GET-1")
    asset_id = seeded["asset_id"]

    engine = _EngineStub(pg_pool)

    own = await do_get_asset(engine, {"namespace_id": ns_a, "asset_id": asset_id})
    assert own["asset"] is not None, "ns_a must see its own asset (sanity)"
    assert own["asset"]["asset_id"] == asset_id

    foreign = await do_get_asset(engine, {"namespace_id": ns_b, "asset_id": asset_id})
    assert foreign["ok"] is True
    assert foreign["asset"] is None, (
        "ns_b must NOT be able to fetch ns_a's asset by its real id -- "
        "the namespace_id predicate on do_get_asset's WHERE clause is missing or ineffective"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_do_list_assets_returns_only_the_callers_own_rows_at_owner_pool_level(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    make_namespace: Any,
) -> None:
    """Baseline: two tenants, two distinct rows, each list sees only its own.

    Goes RED if do_list_assets' mandatory "namespace_id = $1::uuid"
    condition is dropped from `conditions`.
    """
    ns_a = await make_namespace()
    ns_b = await make_namespace()
    asset_a = await _seed(pg_pool, ns_a, bom_line_id="BOM-ISO-LIST-A")
    asset_b = await _seed(pg_pool, ns_b, bom_line_id="BOM-ISO-LIST-B")

    engine = _EngineStub(pg_pool)

    result_a = await do_list_assets(engine, {"namespace_id": ns_a})
    ids_a = {item["asset_id"] for item in result_a["items"]}
    assert asset_a["asset_id"] in ids_a
    assert asset_b["asset_id"] not in ids_a, "ns_a's list must not contain ns_b's asset"

    result_b = await do_list_assets(engine, {"namespace_id": ns_b})
    ids_b = {item["asset_id"] for item in result_b["items"]}
    assert asset_b["asset_id"] in ids_b
    assert asset_a["asset_id"] not in ids_b, "ns_b's list must not contain ns_a's asset"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_do_list_assets_location_filter_does_not_leak_on_a_genuine_value_collision(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    make_namespace: Any,
) -> None:
    """The precedent's exact recipe, transplanted: unlike id,
    functional_location_id is a free-text column with NO uniqueness
    constraint, so two tenants genuinely CAN pick the same value. Seed both
    tenants with the SAME functional_location_id and filter by it, so a
    query that forgot the namespace_id AND-clause (but kept the
    functional_location_id filter) would still leak -- the filter alone is
    not a tenant boundary.

    Goes RED if `conditions` drops its namespace_id entry while keeping the
    optional filter entries.
    """
    ns_a = await make_namespace()
    ns_b = await make_namespace()
    shared_location = "ROOM-COLLISION-1"
    asset_a = await _seed(
        pg_pool, ns_a, bom_line_id="BOM-ISO-COLLIDE-A", functional_location_id=shared_location
    )
    asset_b = await _seed(
        pg_pool, ns_b, bom_line_id="BOM-ISO-COLLIDE-B", functional_location_id=shared_location
    )
    assert asset_a["asset_id"] != asset_b["asset_id"], (
        "this test needs a REAL collision: two distinct rows sharing one functional_location_id"
    )

    engine = _EngineStub(pg_pool)
    result = await do_list_assets(
        engine, {"namespace_id": ns_a, "functional_location_id": shared_location}
    )
    ids = {item["asset_id"] for item in result["items"]}
    assert asset_a["asset_id"] in ids
    assert asset_b["asset_id"] not in ids, (
        "ns_a's location-filtered list leaked ns_b's asset -- the functional_location_id "
        "filter is not a substitute for the namespace_id predicate"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_do_advance_lifecycle_cannot_move_another_tenants_asset_at_owner_pool_level(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    make_namespace: Any,
) -> None:
    """The write-side proof: ns_b naming ns_a's real asset_id must be
    refused as not_found, and ns_a's row's lifecycle_state must be
    UNCHANGED afterward -- not just that the response looks like a refusal.

    Goes RED if "AND id = $3::uuid" loses its "namespace_id = $2::uuid"
    partner in either the SELECT or the UPDATE: the SELECT would find the
    row and the UPDATE would move another tenant's lifecycle_state.
    """
    ns_a = await make_namespace()
    ns_b = await make_namespace()
    seeded = await _seed(pg_pool, ns_a, bom_line_id="BOM-ISO-ADV-1")
    asset_id = seeded["asset_id"]
    original_state = seeded["lifecycle_state"]

    engine = _EngineStub(pg_pool)
    config = load_lifecycle_config()
    legal_next = config["VALID_TRANSITIONS"][original_state][0]

    hijack = await do_advance_lifecycle(
        engine, {"namespace_id": ns_b, "asset_id": asset_id, "target_state": legal_next}
    )
    assert hijack["ok"] is False
    assert hijack["not_found"] is True

    after_hijack = await do_get_asset(engine, {"namespace_id": ns_a, "asset_id": asset_id})
    assert after_hijack["asset"]["lifecycle_state"] == original_state, (
        "ns_b's call must not have moved ns_a's asset's lifecycle_state -- "
        "the namespace_id predicate on do_advance_lifecycle's SELECT/UPDATE is missing "
        "or ineffective"
    )

    # Sanity: the legitimate owner can still advance it.
    legit = await do_advance_lifecycle(
        engine, {"namespace_id": ns_a, "asset_id": asset_id, "target_state": legal_next}
    )
    assert legit["ok"] is True
    assert legit["changed"] is True
    assert legit["new_state"] == legal_next


# ===========================================================================
# NCE_APP LEVEL -- proves the RLS POLICY, through a real restricted
# connection. Does NOT discriminate on this module's own SQL predicate (see
# module docstring) -- a defense-in-depth proof, not this module's gate.
# ===========================================================================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_do_get_asset_rls_policy_isolates_namespaces_via_nce_app(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    make_namespace: Any,
) -> None:
    """Through a REAL nce_app connection: RLS refuses ns_b's read of ns_a's
    asset regardless of what this module's WHERE clause says.
    """
    ns_a = await make_namespace()
    ns_b = await make_namespace()
    seeded = await _seed(pg_pool, ns_a, bom_line_id="BOM-ISO-APP-GET-1")
    asset_id = seeded["asset_id"]

    app_pool = await asyncpg.create_pool(_app_dsn(), min_size=1, max_size=2)
    engine = _EngineStub(app_pool)
    try:
        own = await do_get_asset(engine, {"namespace_id": ns_a, "asset_id": asset_id})
        assert own["asset"] is not None, "ns_a must see its own asset through nce_app"

        foreign = await do_get_asset(engine, {"namespace_id": ns_b, "asset_id": asset_id})
        assert foreign["asset"] is None, "RLS must refuse ns_b's read of ns_a's asset"
    finally:
        await app_pool.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_do_list_assets_rls_policy_isolates_namespaces_via_nce_app(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    make_namespace: Any,
) -> None:
    """Through a REAL nce_app connection: RLS restricts do_list_assets to
    the caller's own namespace, independent of the module's own predicate.
    """
    ns_a = await make_namespace()
    ns_b = await make_namespace()
    asset_a = await _seed(pg_pool, ns_a, bom_line_id="BOM-ISO-APP-LIST-A")
    asset_b = await _seed(pg_pool, ns_b, bom_line_id="BOM-ISO-APP-LIST-B")

    app_pool = await asyncpg.create_pool(_app_dsn(), min_size=1, max_size=2)
    engine = _EngineStub(app_pool)
    try:
        result_a = await do_list_assets(engine, {"namespace_id": ns_a})
        ids_a = {item["asset_id"] for item in result_a["items"]}
        assert asset_a["asset_id"] in ids_a
        assert asset_b["asset_id"] not in ids_a, "RLS must hide ns_b's asset from ns_a's list"
    finally:
        await app_pool.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_do_advance_lifecycle_rls_policy_isolates_namespaces_via_nce_app(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    make_namespace: Any,
) -> None:
    """Through a REAL nce_app connection: RLS makes ns_a's asset invisible
    to ns_b's SELECT, so do_advance_lifecycle reports not_found -- the row
    is never visible to be UPDATEd in the first place.
    """
    ns_a = await make_namespace()
    ns_b = await make_namespace()
    seeded = await _seed(pg_pool, ns_a, bom_line_id="BOM-ISO-APP-ADV-1")
    asset_id = seeded["asset_id"]
    original_state = seeded["lifecycle_state"]
    config = load_lifecycle_config()
    legal_next = config["VALID_TRANSITIONS"][original_state][0]

    app_pool = await asyncpg.create_pool(_app_dsn(), min_size=1, max_size=2)
    engine = _EngineStub(app_pool)
    try:
        hijack = await do_advance_lifecycle(
            engine, {"namespace_id": ns_b, "asset_id": asset_id, "target_state": legal_next}
        )
        assert hijack["not_found"] is True, "RLS must hide ns_a's asset from ns_b's SELECT"

        legit = await do_advance_lifecycle(
            engine, {"namespace_id": ns_a, "asset_id": asset_id, "target_state": legal_next}
        )
        assert legit["ok"] is True
        assert legit["changed"] is True
    finally:
        await app_pool.close()
