"""
Integration tests for procurement bid-price resolution (Module 1, Wave 5).

Validates:
  - procurement_bid_prices table exists with correct schema
  - FORCE RLS is enabled and the tenant_isolation_policy exists
  - upsert_bid_projection() writes rows into the consumer cache
  - do_resolve_bids() returns the best (lowest pris) BID per artnr
  - RLS isolates rows across namespaces
  - No Nettailer/CSV feed-parsing path is reachable from this module
"""

from __future__ import annotations

import sys
import uuid

import asyncpg  # type: ignore[import-untyped]
import pytest
import pytest_asyncio

from nce.auth import set_namespace_context
from nce.config import cfg
from nce.vertical_modules.procurement.bids import do_resolve_bids, upsert_bid_projection

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="function")
async def pg_pool() -> asyncpg.Pool:  # type: ignore[type-arg]
    """Live connection pool for integration tests."""
    pool = await asyncpg.create_pool(cfg.PG_DSN, min_size=1, max_size=3)
    yield pool
    await pool.close()


@pytest_asyncio.fixture(scope="function")
async def app_pool() -> asyncpg.Pool:  # type: ignore[type-arg]
    """Connection pool using the nce_app role (RLS-enforced)."""
    from urllib.parse import urlparse, urlunparse

    primary = cfg.PG_DSN or ""
    parsed = urlparse(primary)
    netloc = parsed.hostname or ""
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    app_pass = cfg.NCE_APP_PASSWORD or "nce_app_secret"
    netloc = f"nce_app:{app_pass}@{netloc}"
    app_dsn = urlunparse(parsed._replace(netloc=netloc))

    try:
        pool = await asyncpg.create_pool(app_dsn, min_size=1, max_size=2)
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"nce_app pool not reachable: {exc}")
    yield pool
    await pool.close()


class _FakeEngine:
    """Minimal stand-in for NCEEngine used by do_resolve_bids."""

    def __init__(self, pool: asyncpg.Pool) -> None:  # type: ignore[type-arg]
        self.pg_pool = pool


async def _make_namespace() -> uuid.UUID:
    """Create a namespace as the OWNER role.

    The ``nce_app`` role (used by ``app_pool``) has no INSERT privilege on
    ``namespaces`` — only the owner/superuser DSN (``cfg.PG_DSN``) may create
    them. Bid-cache operations still run as ``nce_app`` so RLS is genuinely
    exercised.
    """
    slug = f"pytest-bids-{uuid.uuid4().hex}"
    conn = await asyncpg.connect(cfg.PG_DSN)
    try:
        ns_id: uuid.UUID = await conn.fetchval(
            "INSERT INTO namespaces (slug) VALUES ($1) RETURNING id",
            slug,
        )
    finally:
        await conn.close()
    return ns_id


# ---------------------------------------------------------------------------
# Schema & RLS catalog tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_table_exists(pg_pool: asyncpg.Pool) -> None:  # type: ignore[type-arg]
    """procurement_bid_prices table must exist after migrations."""
    async with pg_pool.acquire() as conn:
        exists = await conn.fetchval(
            """
            SELECT EXISTS(
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name   = 'procurement_bid_prices'
            )
            """
        )
    assert exists, "procurement_bid_prices table does not exist"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_required_columns(pg_pool: asyncpg.Pool) -> None:  # type: ignore[type-arg]
    """All expected columns must be present with correct types."""
    async with pg_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT column_name, data_type
            FROM   information_schema.columns
            WHERE  table_name = 'procurement_bid_prices'
            ORDER  BY ordinal_position
            """
        )
    col_map = {r["column_name"]: r["data_type"] for r in rows}

    expected = {
        "id": "uuid",
        "namespace_id": "uuid",
        "artnr": "text",
        "leverandor": "text",
        "bid_id": "text",
        "prodid": "text",
        "pris": "numeric",
        "valid_to": "timestamp with time zone",
        "raw": "jsonb",
        "synced_at": "timestamp with time zone",
    }
    for col, dtype in expected.items():
        assert col in col_map, f"Column '{col}' not found in procurement_bid_prices"
        assert col_map[col] == dtype, (
            f"Column '{col}' has type '{col_map[col]}', expected '{dtype}'"
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_force_rls_enabled(pg_pool: asyncpg.Pool) -> None:  # type: ignore[type-arg]
    """FORCE ROW LEVEL SECURITY must be set on procurement_bid_prices."""
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT relrowsecurity, relforcerowsecurity
            FROM   pg_class
            WHERE  relname = 'procurement_bid_prices'
              AND  relnamespace = (SELECT oid FROM pg_namespace WHERE nspname = 'public')
            """
        )
    assert row is not None, "pg_class row for procurement_bid_prices not found"
    assert row["relrowsecurity"], "RLS is not enabled on procurement_bid_prices"
    assert row["relforcerowsecurity"], "FORCE RLS is not enabled on procurement_bid_prices"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rls_policy_references_get_nce_namespace(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
) -> None:
    """tenant_isolation_policy must reference get_nce_namespace()."""
    async with pg_pool.acquire() as conn:
        policies = await conn.fetch(
            """
            SELECT policyname, qual, with_check
            FROM   pg_policies
            WHERE  schemaname = 'public'
              AND  tablename  = 'procurement_bid_prices'
            """
        )
    assert policies, "No RLS policies found on procurement_bid_prices"
    combined = " ".join(f"{p['qual'] or ''} {p['with_check'] or ''}" for p in policies).lower()
    assert "get_nce_namespace" in combined or "nce.namespace_id" in combined, (
        "tenant_isolation_policy does not reference get_nce_namespace()"
    )


# ---------------------------------------------------------------------------
# Functional: upsert + resolve
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upsert_and_resolve_best_bid(app_pool: asyncpg.Pool) -> None:  # type: ignore[type-arg]
    """Seed the cache via upsert_bid_projection; do_resolve_bids returns lowest pris."""
    ns_id = await _make_namespace()
    engine = _FakeEngine(app_pool)

    projection_rows = [
        {
            "artnr": "ART-001",
            "leverandor": "SupplierA",
            "bid_id": "BID-A1",
            "prodid": "PROD-001",
            "pris": 150.00,
        },
        {
            "artnr": "ART-001",
            "leverandor": "SupplierB",
            "bid_id": "BID-B1",
            "prodid": "PROD-001",
            "pris": 120.00,  # best price for ART-001
        },
        {
            "artnr": "ART-002",
            "leverandor": "SupplierA",
            "bid_id": "BID-A2",
            "prodid": "PROD-002",
            "pris": 80.00,
        },
    ]

    # Seed cache directly (simulates Product's projection push).
    async with app_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, ns_id)
            upserted = await upsert_bid_projection(conn, ns_id, projection_rows)
    assert upserted == 3

    # Resolve best BID per artnr.
    result = await do_resolve_bids(
        engine,
        {"namespace_id": str(ns_id), "artnrs": ["ART-001", "ART-002"]},
    )

    results = result["results"]
    by_artnr = {r["artnr"]: r for r in results}

    assert "ART-001" in by_artnr, "ART-001 not resolved"
    assert "ART-002" in by_artnr, "ART-002 not resolved"

    # Best BID for ART-001 is SupplierB at 120.
    art001 = by_artnr["ART-001"]
    assert art001["leverandor"] == "SupplierB", (
        f"Expected SupplierB (lowest pris), got {art001['leverandor']}"
    )
    assert float(art001["pris"]) == pytest.approx(120.00)

    # Only one supplier for ART-002.
    assert by_artnr["ART-002"]["pris"] is not None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upsert_is_idempotent(app_pool: asyncpg.Pool) -> None:  # type: ignore[type-arg]
    """ON CONFLICT DO UPDATE: upserting the same natural key updates pris in place."""
    ns_id = await _make_namespace()

    row_v1 = {"artnr": "ART-100", "leverandor": "SupX", "bid_id": "B1", "pris": 200.00}
    row_v2 = {"artnr": "ART-100", "leverandor": "SupX", "bid_id": "B1", "pris": 175.00}

    async with app_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, ns_id)
            await upsert_bid_projection(conn, ns_id, [row_v1])
        async with conn.transaction():
            await set_namespace_context(conn, ns_id)
            await upsert_bid_projection(conn, ns_id, [row_v2])
        # Should be exactly one row (upserted, not duplicated).
        async with conn.transaction():
            await set_namespace_context(conn, ns_id)
            count = await conn.fetchval(
                "SELECT count(*) FROM procurement_bid_prices "
                "WHERE namespace_id = $1 AND artnr = 'ART-100'",
                ns_id,
            )
            pris = await conn.fetchval(
                "SELECT pris FROM procurement_bid_prices "
                "WHERE namespace_id = $1 AND artnr = 'ART-100'",
                ns_id,
            )
    assert count == 1, f"Expected 1 row after upsert, found {count}"
    assert float(pris) == pytest.approx(175.00), f"Expected updated pris 175.00, got {pris}"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_artnr_cap(app_pool: asyncpg.Pool) -> None:  # type: ignore[type-arg]
    """do_resolve_bids silently caps the artnr list at 500."""
    ns_id = await _make_namespace()
    engine = _FakeEngine(app_pool)

    # Pass 600 artnrs — function must not raise and cap to 500 internally.
    artnrs = [f"ART-{i:04d}" for i in range(600)]
    result = await do_resolve_bids(
        engine,
        {"namespace_id": str(ns_id), "artnrs": artnrs},
    )
    # Cache is empty for this namespace — zero results; no exception.
    assert "results" in result


# ---------------------------------------------------------------------------
# RLS isolation across namespaces
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rls_isolates_namespaces(app_pool: asyncpg.Pool) -> None:  # type: ignore[type-arg]
    """Namespace A's bid rows must not be visible under namespace B."""
    ns_a = await _make_namespace()
    ns_b = await _make_namespace()
    engine_a = _FakeEngine(app_pool)
    engine_b = _FakeEngine(app_pool)

    row_a = {"artnr": "RLS-TEST", "leverandor": "SupA", "bid_id": "BA", "pris": 99.00}

    async with app_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, ns_a)
            await upsert_bid_projection(conn, ns_a, [row_a])

    # Namespace A resolves the row.
    result_a = await do_resolve_bids(
        engine_a,
        {"namespace_id": str(ns_a), "artnrs": ["RLS-TEST"]},
    )
    assert len(result_a["results"]) == 1, "Namespace A should see its own row"

    # Namespace B must not see it.
    result_b = await do_resolve_bids(
        engine_b,
        {"namespace_id": str(ns_b), "artnrs": ["RLS-TEST"]},
    )
    assert len(result_b["results"]) == 0, (
        "RLS isolation failed: namespace B can see namespace A's row"
    )


# ---------------------------------------------------------------------------
# Negative: no Nettailer / CSV feed-parsing path reachable
# ---------------------------------------------------------------------------


def test_no_nettailer_import_in_bids_module() -> None:
    """bids.py must not import any Nettailer / CSV streaming client.

    The contract: Procurement never touches the raw feed (§9.1).

    Uses a subprocess so the check is immune to test-ordering: other tests in
    the same session may have already imported nettailer, which would pollute
    sys.modules and produce a false failure.  The subprocess starts with a
    clean interpreter that imports *only* the bids module.
    """
    import inspect
    import subprocess

    feed_module_names = [
        "nce.vertical_modules.product.sources.nettailer",
        "nce.vertical_modules.product.sources",
    ]

    # Build a small Python snippet that imports the bids module in isolation
    # and prints "CLEAN" only when none of the feed modules appear in sys.modules.
    probe_lines = [
        "import sys",
        "import nce.vertical_modules.procurement.bids",
        "feed = [m for m in sys.modules if any(f in m for f in ["
        + ", ".join(repr(n) for n in feed_module_names)
        + "])]",
        "print('CONTAMINATED:' + ','.join(feed) if feed else 'CLEAN')",
    ]
    probe = "; ".join(probe_lines)

    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"Probe subprocess failed:\n{result.stderr}"
    output = result.stdout.strip()
    assert output == "CLEAN", (
        f"Feed-parsing module was imported by the bids module in a fresh "
        f"interpreter — Procurement must not touch the Nettailer feed. "
        f"Subprocess output: {output!r}"
    )

    # bids.py source must not contain any csv or feed-streaming identifiers.
    import nce.vertical_modules.procurement.bids as bids_mod

    src = inspect.getsource(bids_mod)
    forbidden_patterns = ["nettailer", "csv.reader", "csv.DictReader", "StreamingCSV"]
    for pattern in forbidden_patterns:
        assert pattern not in src, (
            f"Forbidden pattern {pattern!r} found in bids.py source — "
            "Procurement must not contain any feed-parsing path"
        )
