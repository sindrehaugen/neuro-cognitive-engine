"""
tests/test_tenant_predicates.py

Tenant-isolation coverage for five queries that sit *inside*
``scoped_pg_session(pool, namespace_id)`` but omit ``namespace_id`` from their
``WHERE`` clause (RL-H15 census).  ``scoped_pg_session`` only issues
``SET LOCAL nce.namespace_id`` -- it does not ``SET ROLE`` -- and every DSN
connects as ``mcp_user``, which is both ``rolsuper`` and ``rolbypassrls``,
while all tenant policies target ``nce_app``.  RLS is therefore inert as
deployed and the predicate is the only thing separating tenants.

Sites covered:
  1. ``CatalogManager.suggest``         -- nce/query_catalog.py (query_templates ANN)
  2. ``CatalogManager.execute``         -- nce/query_catalog.py (query_templates by slug)
  3. ``CatalogManager.describe_schema`` -- nce/query_catalog.py (graph_schema_registry)
  4. ``do_get_product`` prices          -- product/mcp_handlers.py (product_prices)
  5. ``do_get_product`` edges           -- product/mcp_handlers.py (kg_edges)

Design rule, and it is the whole test: two namespaces that **collide on every
identifier** -- same template ``slug``, same ``mfr_part_no``, same
``subject_label``, same ``element_type``/``type_key`` -- differing only on
*content*.  A fixture whose namespaces differ on the lookup key would pass with
the predicate absent and prove nothing.

NOT covered here: ``product_catalog`` is deliberately **global** (shared across
tenants); its unpredicated queries are correct and are never asserted on.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

import nce.query_catalog as catalog_mod
from nce.query_catalog import CatalogManager
from nce.vertical_modules.product.mcp_handlers import do_get_product

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

# ---------------------------------------------------------------------------
# Colliding identifiers -- identical in BOTH namespaces on purpose
# ---------------------------------------------------------------------------

_SLUG = "pytest-collide-tenant-predicate"
_SLUG_B_ONLY = "pytest-bravo-only-tenant-predicate"
_MFR = "PYTESTMFR"
_PART = "PYTEST-COLLIDE-PART-001"
_SUBJECT = f"PRODUCT:{_MFR}:{_PART}"
_SHARED_TYPE_KEY = "AAA_PYTEST_SHARED_PRED"
_BRAVO_TYPE_KEY = "AAA_PYTEST_BRAVO_PRED"
_EMBEDDING_DIM = 768


def _vec() -> list[float]:
    """Deterministic unit vector; stored on both templates so both rank first."""
    v = [0.0] * _EMBEDDING_DIM
    v[-1] = 1.0
    return v


class _FakeEngine:
    """Minimal NCEEngine stand-in -- ``do_get_product`` only reads ``pg_pool``."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pg_pool = pool


async def _seed(pool: asyncpg.Pool, ns: uuid.UUID, marker: str) -> None:
    """Insert one fully-colliding row set for ``ns``, tagged with ``marker``."""
    async with pool.acquire() as conn:
        # 1/2. query_templates -- same slug in both namespaces
        #      (UNIQUE (namespace_id, slug) permits it).
        await conn.execute(
            """
            INSERT INTO query_templates
                (slug, description, description_embedding, tools, param_schema,
                 pipeline, raw_template, target_engine, namespace_id, is_active)
            VALUES ($1, $2, $3::vector, '{}', '{}'::jsonb, '[]'::jsonb, $4,
                    'postgres', $5, TRUE)
            """,
            _SLUG,
            f"{marker}_DESCRIPTION",
            json.dumps(_vec()),
            f"SELECT '{marker}_TEMPLATE_RAN' AS marker",
            ns,
        )
        # 3. graph_schema_registry -- one colliding type_key in both namespaces.
        await conn.execute(
            """
            INSERT INTO graph_schema_registry (namespace_id, element_type, type_key)
            VALUES ($1, 'EDGE', $2)
            """,
            ns,
            _SHARED_TYPE_KEY,
        )
        # product_catalog is GLOBAL by design -- seeded in both namespaces only so
        # the master lookup inside do_get_product resolves. Never asserted on.
        # product_catalog is TENANT-scoped before migration 064 and GLOBAL after it
        # (Sindre's 2026-09-04 ruling, PR #205). This seed adapts to either shape so
        # the two PRs may merge in EITHER order; the catalogue row is only a
        # prerequisite for do_get_product's master lookup, never asserted on here.
        catalog_has_ns = await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'product_catalog'
                  AND column_name = 'namespace_id'
            )
            """
        )
        if catalog_has_ns:
            await conn.execute(
                """
                INSERT INTO product_catalog
                    (namespace_id, manufacturer, mfr_part_no, product_source_id)
                VALUES ($1, $2, $3, 'pytest-tenant-predicates')
                ON CONFLICT DO NOTHING
                """,
                ns,
                _MFR,
                _PART,
            )
        else:
            await conn.execute(
                """
                INSERT INTO product_catalog
                    (manufacturer, mfr_part_no, product_source_id)
                VALUES ($1, $2, 'pytest-tenant-predicates')
                ON CONFLICT DO NOTHING
                """,
                _MFR,
                _PART,
            )
        # 4. product_prices -- same mfr_part_no, content differs on supplier.
        await conn.execute(
            """
            INSERT INTO product_prices
                (namespace_id, mfr_part_no, supplier, bid_id, list_price, cost_price)
            VALUES ($1, $2, $3, 'pytest-bid', $4, 1)
            """,
            ns,
            _PART,
            f"{marker}_SUPPLIER",
            100,
        )
        # 5. kg_edges -- same subject_label, content differs on object_label.
        await conn.execute(
            """
            INSERT INTO kg_edges
                (subject_label, predicate, object_label, namespace_id, change_origin)
            VALUES ($1, 'PYTEST_PRED', $2, $3, 'operator')
            """,
            _SUBJECT,
            f"{marker}_OBJECT",
            ns,
        )


def _dsn() -> str:
    raw = (
        os.getenv("NCE_INTEGRATION_PG_DSN")
        or os.getenv("PG_DSN")
        or os.getenv("DATABASE_URL")
        or ""
    ).strip()
    if not raw:
        pytest.skip("Needs NCE_INTEGRATION_PG_DSN, PG_DSN or DATABASE_URL")
    return raw


@pytest_asyncio.fixture
async def tp_pool():
    """Own pool -- deliberately NOT tests/conftest.py's ``pg_pool``.

    ``pg_pool`` asserts up front that ``NCE_MASTER_KEY`` decrypts
    ``signing_keys`` and skips the whole test otherwise.  On the shared dev
    stack the deployed master key differs from the test key, and the documented
    escape hatch (``NCE_INTEGRATION_REFRESH_SIGNING_ON_DECRYPT_FAIL``) *rotates
    the active signing key* -- which would leave that stack holding a key it
    cannot decrypt.  None of the five queries under test touch signing, so this
    fixture declines the precondition rather than satisfying it destructively.
    """
    try:
        pool = await asyncpg.create_pool(_dsn(), min_size=1, max_size=4, command_timeout=60)
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"Postgres not reachable for integration tests: {exc}")
    try:
        yield pool
    finally:
        await pool.close()


@pytest_asyncio.fixture
async def tp_namespaces(tp_pool: asyncpg.Pool):
    """Factory handing out throwaway namespaces; removes them on teardown."""
    created: list[uuid.UUID] = []

    async def _make() -> uuid.UUID:
        async with tp_pool.acquire() as conn:
            ns = await conn.fetchval(
                "INSERT INTO namespaces (slug) VALUES ($1) RETURNING id",
                f"pytest-ns-{uuid.uuid4().hex}",
            )
        assert ns is not None
        created.append(ns)
        return ns

    try:
        yield _make
    finally:
        async with tp_pool.acquire() as conn:
            for ns in created:
                try:
                    await conn.execute("DELETE FROM namespaces WHERE id = $1", ns)
                except asyncpg.PostgresError:
                    pass


@pytest_asyncio.fixture
async def collided(tp_pool: asyncpg.Pool, tp_namespaces: Any):
    """Two namespaces holding identifier-identical, content-different rows.

    Namespace B is seeded **first** so an unpredicated sequential scan reaches
    B's row before A's -- otherwise a leaking ``fetchrow`` could return A's row
    by accident and the test would pass while the defect was present.
    """
    ns_b = await tp_namespaces()
    ns_a = await tp_namespaces()

    await _seed(tp_pool, ns_b, "BRAVO")
    await _seed(tp_pool, ns_a, "ALPHA")

    async with tp_pool.acquire() as conn:
        # A slug that exists ONLY in B: a deterministic companion assertion that
        # does not depend on physical row order.
        await conn.execute(
            """
            INSERT INTO query_templates
                (slug, description, description_embedding, tools, param_schema,
                 pipeline, raw_template, target_engine, namespace_id, is_active)
            VALUES ($1, 'bravo only', $2::vector, '{}', '{}'::jsonb, '[]'::jsonb,
                    $3, 'postgres', $4, TRUE)
            """,
            _SLUG_B_ONLY,
            json.dumps(_vec()),
            "SELECT 'BRAVO_ONLY_TEMPLATE_RAN' AS marker",
            ns_b,
        )
        # A type_key that exists ONLY in B.
        await conn.execute(
            """
            INSERT INTO graph_schema_registry (namespace_id, element_type, type_key)
            VALUES ($1, 'EDGE', $2)
            """,
            ns_b,
            _BRAVO_TYPE_KEY,
        )

    try:
        yield {"a": ns_a, "b": ns_b}
    finally:
        # kg_edges.namespace_id carries no FK, so namespace teardown does not
        # cascade it -- remove those rows explicitly.
        async with tp_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM kg_edges WHERE subject_label = $1 AND namespace_id = ANY($2::uuid[])",
                _SUBJECT,
                [ns_a, ns_b],
            )


@pytest.fixture
def stub_embed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the intent embedding so both seeded templates sit at cosine distance 0.

    The site under test is the SQL predicate, not the embedding model.
    """

    async def _fake_embed(_text: str, *_a: Any, **_kw: Any) -> list[float]:
        return _vec()

    monkeypatch.setattr(catalog_mod, "embed", _fake_embed)


# ---------------------------------------------------------------------------
# Site 1 -- query_catalog.py: suggest() ANN over query_templates
# ---------------------------------------------------------------------------


async def test_suggest_does_not_rank_other_tenants_templates(
    tp_pool: asyncpg.Pool,
    collided: dict[str, uuid.UUID],
    stub_embed: None,
) -> None:
    mgr = CatalogManager(pool=tp_pool)
    rows = await mgr.suggest("anything", namespace_id=collided["a"], limit=10)

    descriptions = [r.description for r in rows]
    slugs = [r.slug for r in rows]

    assert "BRAVO_DESCRIPTION" not in descriptions, (
        f"suggest() leaked namespace B's template description: {descriptions}"
    )
    assert _SLUG_B_ONLY not in slugs, (
        f"suggest() leaked a slug that exists only in namespace B: {slugs}"
    )
    assert "ALPHA_DESCRIPTION" in descriptions, (
        f"suggest() lost namespace A's own template: {descriptions}"
    )


# ---------------------------------------------------------------------------
# Site 2 -- query_catalog.py: execute() fetch of raw_template by slug
# ---------------------------------------------------------------------------


async def test_execute_runs_own_raw_template_on_colliding_slug(
    tp_pool: asyncpg.Pool,
    collided: dict[str, uuid.UUID],
) -> None:
    mgr = CatalogManager(pool=tp_pool)
    rows = await mgr.execute(_SLUG, {}, namespace_id=collided["a"])

    assert rows == [{"marker": "ALPHA_TEMPLATE_RAN"}], (
        f"execute() ran the wrong tenant's raw_template: {rows}"
    )


async def test_execute_cannot_reach_a_slug_owned_only_by_another_tenant(
    tp_pool: asyncpg.Pool,
    collided: dict[str, uuid.UUID],
) -> None:
    mgr = CatalogManager(pool=tp_pool)
    with pytest.raises(ValueError, match="not found or is inactive"):
        await mgr.execute(_SLUG_B_ONLY, {}, namespace_id=collided["a"])


# ---------------------------------------------------------------------------
# Site 3 -- query_catalog.py: describe_schema() over graph_schema_registry
# ---------------------------------------------------------------------------


async def test_describe_schema_returns_only_this_namespaces_types(
    tp_pool: asyncpg.Pool,
    collided: dict[str, uuid.UUID],
) -> None:
    mgr = CatalogManager(pool=tp_pool)
    schema = await mgr.describe_schema(namespace_id=collided["a"], limit=50)

    assert _BRAVO_TYPE_KEY not in schema.edge_predicates, (
        f"describe_schema() leaked namespace B's type_key: {schema.edge_predicates}"
    )
    assert schema.edge_predicates.count(_SHARED_TYPE_KEY) == 1, (
        "describe_schema() returned the colliding type_key once per namespace: "
        f"{schema.edge_predicates}"
    )


# ---------------------------------------------------------------------------
# Site 4 -- product/mcp_handlers.py: product_prices
# ---------------------------------------------------------------------------


async def test_get_product_prices_exclude_other_tenants_suppliers(
    tp_pool: asyncpg.Pool,
    collided: dict[str, uuid.UUID],
) -> None:
    result = await do_get_product(
        _FakeEngine(tp_pool),  # type: ignore[arg-type]
        {
            "namespace_id": str(collided["a"]),
            "mfr_part_no": _PART,
            "manufacturer": _MFR,
        },
    )

    suppliers = sorted(p["supplier"] for p in result["prices"])
    assert suppliers == ["ALPHA_SUPPLIER"], (
        f"product_prices leaked another tenant's negotiated pricing: {suppliers}"
    )


# ---------------------------------------------------------------------------
# Site 5 -- product/mcp_handlers.py: kg_edges
# ---------------------------------------------------------------------------


async def test_get_product_edges_exclude_other_tenants_edges(
    tp_pool: asyncpg.Pool,
    collided: dict[str, uuid.UUID],
) -> None:
    result = await do_get_product(
        _FakeEngine(tp_pool),  # type: ignore[arg-type]
        {
            "namespace_id": str(collided["a"]),
            "mfr_part_no": _PART,
            "manufacturer": _MFR,
        },
    )

    objects = sorted(e["object_label"] for e in result["edges"])
    assert objects == ["ALPHA_OBJECT"], f"kg_edges leaked another tenant's graph edges: {objects}"
