"""Integration tests for Batch 099 (Module 4 Wave 6 - contractor-rls)."""

from __future__ import annotations

import os
import uuid
from typing import Any
from urllib.parse import urlparse, urlunparse

import asyncpg  # type: ignore[import-untyped]
import pytest

from nce.config import cfg
from nce.db_utils import scoped_pg_session
from nce.vertical_modules.vendors.contractors import do_get_contractor, do_upsert_contractor


class EngineStub:
    """Stub representing the core engine context passed to vertical modules."""

    def __init__(self, pg_pool: asyncpg.Pool) -> None:
        self.pg_pool = pg_pool
        self.mongo_client = None  # Contractors table does not rely on Mongo in this wave


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
async def test_contractor_upsert_and_rls_scoping(
    pg_pool: asyncpg.Pool, make_namespace: Any
) -> None:
    """Verify do_upsert_contractor works and RLS isolates rows by partner_scope_id."""
    ns_id = await make_namespace()

    # 1. Seed ownership for CONTRACTOR owned by vendors using the admin connection (pg_pool)
    async with scoped_pg_session(pg_pool, ns_id) as conn:
        await _seed_ownership(conn, ns_id, "CONTRACTOR", "vendors")

    # 2. Setup app_pool connecting as the restricted nce_app role
    app_dsn = _app_dsn()
    app_pool = await asyncpg.create_pool(app_dsn, min_size=1, max_size=2)
    engine = EngineStub(app_pool)

    try:
        contractor_id = "CONTRACTOR:BOB"
        partner_scope_a = uuid.uuid4()

        # 3. Upsert contractor Bob (runs as nce_app)
        res = await do_upsert_contractor(
            engine,
            {
                "namespace_id": ns_id,
                "contractor_id": contractor_id,
                "partner_scope_id": partner_scope_a,
                "profile": {"name": "Bob Vance"},
                "rates": {"hourly": 150.0},
                "skills": ["crossover", "dsp"],
                "availability": {"monday": True},
                "performance_score": 95.0,
            },
        )
        assert res["ok"] is True

        # 4. Retrieve contractor Bob using correct partner_scope_id (passes RLS)
        contractor = await do_get_contractor(
            engine,
            {
                "namespace_id": ns_id,
                "contractor_id": contractor_id,
                "partner_scope_id": partner_scope_a,
            },
        )
        assert contractor is not None
        assert contractor["contractor_id"] == contractor_id
        assert contractor["partner_scope_id"] == str(partner_scope_a)
        assert contractor["profile"]["name"] == "Bob Vance"
        assert contractor["rates"]["hourly"] == 150.0
        assert contractor["skills"] == ["crossover", "dsp"]
        assert contractor["performance_score"] == 95.0

        # 5. Attempt to retrieve Bob with a different partner_scope_id (denied by RLS)
        partner_scope_b = uuid.uuid4()
        contractor_denied = await do_get_contractor(
            engine,
            {
                "namespace_id": ns_id,
                "contractor_id": contractor_id,
                "partner_scope_id": partner_scope_b,
            },
        )
        assert contractor_denied is None

        # 6. Attempt to retrieve Bob with unset partner_scope_id (denied by RLS)
        contractor_unset = await do_get_contractor(
            engine,
            {
                "namespace_id": ns_id,
                "contractor_id": contractor_id,
            },
        )
        assert contractor_unset is None
    finally:
        await app_pool.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_contractor_rls_cross_tenant_isolation(
    pg_pool: asyncpg.Pool, make_namespace: Any
) -> None:
    """Verify tenant isolation prevents cross-tenant reads even if partner_scope_id matches."""
    ns_a = await make_namespace()
    ns_b = await make_namespace()

    # Seed ownership in both namespaces
    async with scoped_pg_session(pg_pool, ns_a) as conn:
        await _seed_ownership(conn, ns_a, "CONTRACTOR", "vendors")
    async with scoped_pg_session(pg_pool, ns_b) as conn:
        await _seed_ownership(conn, ns_b, "CONTRACTOR", "vendors")

    app_dsn = _app_dsn()
    app_pool = await asyncpg.create_pool(app_dsn, min_size=1, max_size=2)
    engine = EngineStub(app_pool)

    try:
        contractor_id = "CONTRACTOR:ALICE"
        partner_scope = uuid.uuid4()

        # 1. Upsert Alice in Namespace A
        await do_upsert_contractor(
            engine,
            {
                "namespace_id": ns_a,
                "contractor_id": contractor_id,
                "partner_scope_id": partner_scope,
                "profile": {"name": "Alice Cooper"},
            },
        )

        # 2. Try to query Alice under Namespace B with the correct partner_scope_id (denied by tenant isolation)
        contractor_cross = await do_get_contractor(
            engine,
            {
                "namespace_id": ns_b,
                "contractor_id": contractor_id,
                "partner_scope_id": partner_scope,
            },
        )
        assert contractor_cross is None
    finally:
        await app_pool.close()
