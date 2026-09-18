"""H-5: negative cross-tenant isolation tests for vendor domain functions (charter §9/§10).

H-5's raw test count (99, verified via ``pytest --collect-only``) already clears
the charter's "Vendors >= 80" floor -- this file is not lifting that count. It
closes a real coverage gap found *above* the floor: of the vendors engine's 18
tenant-scoped domain functions, only two (``do_upsert_contractor`` /
``do_get_contractor``, see ``tests/test_vendors_contractor_rls.py``) had a
negative cross-tenant-isolation test. Seven files' functions had none:
certs, feed, frontier, matching, partner_view, performance, tiers. The floor
was met by tests that do not test the thing the floor exists to protect
(charter §10 rule 1: "other namespace, other principal tier" negative
coverage, not just a happy-path count).

Per ML-orch's approval (2026-09-18): every test below extends the EXACT
template shape from ``test_vendors_contractor_rls.py::
test_contractor_rls_cross_tenant_isolation`` -- seed a row in namespace A via
the real ``nce_app``-role connection (proving RLS *policy* enforcement, not
just an application-level WHERE clause), then attempt to read/reference the
SAME identifier from namespace B and assert isolation. No test here invents
a different shape; where a function genuinely does not fit this shape, it is
named and left out below rather than covered with an invented variant this
file's author could not run against a live database to verify.

**Covered (5 of 7 flagged files):**
- certs.py + partner_view.py: ``do_upsert_cert`` (write) -> ``do_partner_view``
  (read), combined into one test since certs.py has no read function of its
  own and partner_view.py's ``do_partner_view`` is the only function among
  the 18 that does a direct ``kg_nodes`` lookup returning ``None`` on a miss.
  Uses the CERT node path (not the CONTRACTOR path), because the CONTRACTOR
  fallback in ``do_partner_view`` additionally depends on the C3
  ``partner_scope_id`` external-scope GUC (see
  ``do_upsert_contractor``/``set_external_scope``) -- a second RLS layer this
  file's author could not verify locally, so it is not exercised here to
  avoid asserting on a compound condition it cannot independently confirm.
- feed.py: ``do_detect_reliability_degradation`` and ``do_check_tier_at_risk``
  (the latter delegates to ``do_get_tier_status`` internally; both raise
  ``ValueError("Vendor not found: ...")`` for a vendor that exists only in
  another namespace, since ``kg_nodes`` lookups are namespace-scoped).
- tiers.py: ``do_get_tier_status`` directly (same isolation mechanism as the
  two feed.py functions, but this is the function whose own zero-coverage
  status was flagged, not just a downstream caller of it).
- performance.py: ``do_compute_performance``, seeded via
  ``do_record_outcome`` (tiers.py) -- isolation manifests as
  ``performance_score is None`` / ``insufficient_data is True`` rather than a
  bare ``None`` return, since the function always returns a result dict.

**Explicitly left out, with reasons (verified by reading the source, not
assumed):**
- certs.py ``do_check_cert_expiry`` -- no caller-supplied identifier
  parameter at all; it scans every CERT node in the namespace, so there is
  nothing to look up by the same key in another namespace.
- frontier.py ``do_reliability_radar`` and ``do_calibrate_weights`` --
  namespace-wide advisor/aggregate functions with no single-entity key
  parameter; ``do_calibrate_weights``' side effect is also a shared JSON
  config file on disk, not a namespaced database row.
- matching.py ``do_match_contractor`` -- ranks every contractor profile in
  the namespace against a job spec; the caller supplies no target
  identifier, so there is no single-ID read to assert isolation against (a
  contractor written in namespace A simply not appearing in namespace B's
  *list* is a materially weaker and different assertion than this file's
  template).
- performance.py ``do_recall_similar_jobs`` -- a namespace-filtered vector
  similarity search; no function anywhere in the vendors module writes to
  the ``memories`` table it reads, so there is no in-module write to pair it
  with.
- tiers.py ``do_record_outcome`` -- writes a row keyed by a server-generated
  UUID (``ledger_id``), not any caller-supplied identifier; it is used here
  as the seeding helper for ``do_compute_performance``'s test, not as the
  subject of its own dedicated pair.
"""

from __future__ import annotations

import os
import uuid
from typing import Any
from urllib.parse import urlparse, urlunparse

import asyncpg  # type: ignore[import-untyped]
import pytest

from nce.config import cfg
from nce.db_utils import scoped_pg_session
from nce.vertical_modules.vendors.certs import do_upsert_cert
from nce.vertical_modules.vendors.feed import (
    do_check_tier_at_risk,
    do_detect_reliability_degradation,
)
from nce.vertical_modules.vendors.partner_view import do_partner_view
from nce.vertical_modules.vendors.performance import do_compute_performance
from nce.vertical_modules.vendors.registry import do_upsert_vendor
from nce.vertical_modules.vendors.tiers import do_get_tier_status, do_record_outcome


class EngineStub:
    """Stub representing the core engine context passed to vertical modules."""

    def __init__(self, pg_pool: asyncpg.Pool) -> None:
        self.pg_pool = pg_pool
        self.mongo_client = None  # none of these functions require Mongo to be reachable


def _app_dsn() -> str:
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


async def _seed_ownership(
    conn: asyncpg.Connection, ns_id: uuid.UUID, node_type: str, owner_engine: str
) -> None:
    """Seed the node ownership registry for tests."""
    await conn.execute(
        """
        INSERT INTO node_ownership_registry (namespace_id, node_type, owner_engine)
        VALUES ($1, $2, $3)
        ON CONFLICT DO NOTHING
        """,
        ns_id,
        node_type,
        owner_engine,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cert_cross_tenant_isolation_via_partner_view(
    pg_pool: asyncpg.Pool, make_namespace: Any
) -> None:
    """A certification created in namespace A must not be visible via
    do_partner_view under namespace B, even with the identical cert label."""
    ns_a = await make_namespace()
    ns_b = await make_namespace()

    async with scoped_pg_session(pg_pool, ns_a) as conn:
        await _seed_ownership(conn, ns_a, "CERT", "vendors")
    async with scoped_pg_session(pg_pool, ns_b) as conn:
        await _seed_ownership(conn, ns_b, "CERT", "vendors")

    app_dsn = _app_dsn()
    app_pool = await asyncpg.create_pool(app_dsn, min_size=1, max_size=2)
    engine = EngineStub(app_pool)

    try:
        upsert_res = await do_upsert_cert(
            engine,
            {
                "namespace_id": ns_a,
                "contractor_id": "CONTRACTOR:DAVE",
                "cert_name": "SAFETY_101",
                "expiry_date": "2027-01-01",
            },
        )
        cert_label = upsert_res["cert_label"]

        cross_view = await do_partner_view(engine, {"namespace_id": ns_b, "node_id": cert_label})
        assert cross_view is None, (
            f"Cert {cert_label!r} created in one namespace must not be readable "
            "via do_partner_view from another namespace."
        )
    finally:
        await app_pool.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reliability_degradation_cross_tenant_isolation(
    pg_pool: asyncpg.Pool, make_namespace: Any
) -> None:
    """do_detect_reliability_degradation must raise 'Vendor not found' when
    the vendor was only ever registered in a different namespace."""
    ns_a = await make_namespace()
    ns_b = await make_namespace()

    async with scoped_pg_session(pg_pool, ns_a) as conn:
        await _seed_ownership(conn, ns_a, "VENDOR", "vendors")

    app_dsn = _app_dsn()
    app_pool = await asyncpg.create_pool(app_dsn, min_size=1, max_size=2)
    engine = EngineStub(app_pool)

    try:
        await do_upsert_vendor(
            engine, {"namespace_id": ns_a, "orgnr": "910111222", "name": "Erik Transport AS"}
        )

        with pytest.raises(ValueError, match="Vendor not found"):
            await do_detect_reliability_degradation(
                engine, {"namespace_id": ns_b, "vendor_id": "VENDOR:910111222"}
            )
    finally:
        await app_pool.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_tier_at_risk_cross_tenant_isolation(
    pg_pool: asyncpg.Pool, make_namespace: Any
) -> None:
    """do_check_tier_at_risk (delegates to do_get_tier_status) must raise
    'Vendor not found' for a vendor that exists only in another namespace."""
    ns_a = await make_namespace()
    ns_b = await make_namespace()

    async with scoped_pg_session(pg_pool, ns_a) as conn:
        await _seed_ownership(conn, ns_a, "VENDOR", "vendors")

    app_dsn = _app_dsn()
    app_pool = await asyncpg.create_pool(app_dsn, min_size=1, max_size=2)
    engine = EngineStub(app_pool)

    try:
        await do_upsert_vendor(
            engine, {"namespace_id": ns_a, "orgnr": "910333444", "name": "Frida Freight AS"}
        )

        with pytest.raises(ValueError, match="Vendor not found"):
            await do_check_tier_at_risk(
                engine, {"namespace_id": ns_b, "vendor_id": "VENDOR:910333444"}
            )
    finally:
        await app_pool.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_tier_status_cross_tenant_isolation(
    pg_pool: asyncpg.Pool, make_namespace: Any
) -> None:
    """do_get_tier_status must raise 'Vendor not found' for a vendor that
    exists only in another namespace -- the function whose own zero-coverage
    status this test closes, distinct from feed.py's callers of it above."""
    ns_a = await make_namespace()
    ns_b = await make_namespace()

    async with scoped_pg_session(pg_pool, ns_a) as conn:
        await _seed_ownership(conn, ns_a, "VENDOR", "vendors")

    app_dsn = _app_dsn()
    app_pool = await asyncpg.create_pool(app_dsn, min_size=1, max_size=2)
    engine = EngineStub(app_pool)

    try:
        await do_upsert_vendor(
            engine, {"namespace_id": ns_a, "orgnr": "910555666", "name": "Gunnar Logistikk AS"}
        )

        with pytest.raises(ValueError, match="Vendor not found"):
            await do_get_tier_status(
                engine, {"namespace_id": ns_b, "vendor_id": "VENDOR:910555666"}
            )
    finally:
        await app_pool.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_contractor_performance_cross_tenant_isolation(
    pg_pool: asyncpg.Pool, make_namespace: Any
) -> None:
    """Work-order ratings recorded in namespace A must not count toward a
    contractor's performance score computed under namespace B -- the score
    must come back None (insufficient_data), not the namespace-A average."""
    ns_a = await make_namespace()
    ns_b = await make_namespace()

    app_dsn = _app_dsn()
    app_pool = await asyncpg.create_pool(app_dsn, min_size=1, max_size=2)
    engine = EngineStub(app_pool)

    try:
        contractor_id = "CONTRACTOR:HELGA"
        min_sample = getattr(cfg, "NCE_VENDORS_SCORECARD_MIN_SAMPLE", 5)
        for _ in range(min_sample):
            await do_record_outcome(
                engine,
                {
                    "namespace_id": ns_a,
                    "event_type": "work_order_rating",
                    "contractor_id": contractor_id,
                    "rating": 4.5,
                },
            )

        result = await do_compute_performance(
            engine, {"namespace_id": ns_b, "contractor_id": contractor_id}
        )
        assert result["performance_score"] is None, (
            "Ratings recorded in namespace A must not be visible when "
            "computing performance under namespace B."
        )
        assert result["insufficient_data"] is True
        assert result["sample_n"] == 0
    finally:
        await app_pool.close()
