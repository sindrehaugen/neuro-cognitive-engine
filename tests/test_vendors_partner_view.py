"""Integration tests for Batch 100 (partner-view)."""

from __future__ import annotations

import os
import uuid
from typing import Any
from unittest.mock import patch
from urllib.parse import urlparse, urlunparse

import asyncpg
import pytest

from nce.a2a import A2AScopeViolationError
from nce.a2a_server import _dispatch_skill
from nce.auth import NamespaceContext
from nce.config import cfg
from nce.db_utils import scoped_pg_session
from nce.vertical_modules.vendors.contractors import do_upsert_contractor
from nce.vertical_modules.vendors.partner_view import do_partner_view


class EngineStub:
    """Stub representing the core engine context passed to vertical modules."""

    def __init__(self, pg_pool: asyncpg.Pool) -> None:
        self.pg_pool = pg_pool
        self.mongo_client = None
        self.redis_client = None


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
async def test_do_partner_view_redaction_and_scoping(
    pg_pool: asyncpg.Pool, make_namespace: Any
) -> None:
    """Verify do_partner_view filters fields correctly and honors partner scope RLS."""
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
                "profile": {
                    "description": "Crossover/DSP Specialist",
                    "manufacturer": "Vance Refrigeration",
                },
                "rates": {"hourly": 150.0},
                "skills": ["crossover", "dsp"],
                "availability": {"monday": True},
                "performance_score": 95.0,
            },
        )
        assert res["ok"] is True

        # 4. Fetch the partner view using correct partner_scope_id
        partner_view = await do_partner_view(
            engine,
            {
                "namespace_id": ns_id,
                "node_id": contractor_id,
                "partner_scope_id": partner_scope_a,
            },
        )
        assert partner_view is not None

        # Verify allow-listed fields are present
        assert uuid.UUID(partner_view["id"])
        assert partner_view["label"] == contractor_id
        assert partner_view["node_type"] == "CONTRACTOR"
        assert partner_view["namespace_id"] == str(ns_id)
        assert partner_view["description"] == "Crossover/DSP Specialist"
        assert partner_view["manufacturer"] == "Vance Refrigeration"

        # Verify sensitive or non-allow-listed fields are redacted/dropped
        assert "skills" not in partner_view
        assert "availability" not in partner_view
        assert "rates" not in partner_view
        assert "performance_score" not in partner_view

        # 5. Fetch with a different partner_scope_id (should be isolated/blocked by RLS, returning None)
        partner_scope_b = uuid.uuid4()
        partner_view_denied = await do_partner_view(
            engine,
            {
                "namespace_id": ns_id,
                "node_id": contractor_id,
                "partner_scope_id": partner_scope_b,
            },
        )
        assert partner_view_denied is None

    finally:
        await app_pool.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a2a_server_contractor_scoping(pg_pool: asyncpg.Pool, make_namespace: Any) -> None:
    """Verify that contractor sessions can invoke vendors_partner_view but are blocked on other skills."""
    ns_id = await make_namespace()

    # Seed ownership
    async with scoped_pg_session(pg_pool, ns_id) as conn:
        await _seed_ownership(conn, ns_id, "CONTRACTOR", "vendors")

    app_dsn = _app_dsn()
    app_pool = await asyncpg.create_pool(app_dsn, min_size=1, max_size=2)
    engine = EngineStub(app_pool)

    try:
        contractor_id = "CONTRACTOR:ALICE"
        partner_scope = uuid.uuid4()

        # Seed Alice
        await do_upsert_contractor(
            engine,
            {
                "namespace_id": ns_id,
                "contractor_id": contractor_id,
                "partner_scope_id": partner_scope,
                "profile": {"description": "Electrician"},
                "rates": {"hourly": 120.0},
                "performance_score": 88.0,
            },
        )

        # Setup A2A server context with contractor caller
        caller_ctx = NamespaceContext(
            namespace_id=ns_id,
            agent_id="partner-agent",
            principal_kind="contractor",
            external_scope_id=partner_scope,
        )

        with patch("nce.a2a_server._engine", engine):
            # A. Invoke allowed skill (vendors_partner_view)
            res = await _dispatch_skill(
                "vendors_partner_view",
                {"namespace_id": str(ns_id), "node_id": contractor_id},
                caller_ctx,
            )
            assert res is not None
            assert uuid.UUID(res["id"])
            assert res["label"] == contractor_id
            assert "rates" not in res
            assert "performance_score" not in res
            assert "skills" not in res
            assert "availability" not in res
            assert res["description"] == "Electrician"

            # B. Invoke unauthorized skill (recall_relevant_context) -> should raise A2AScopeViolationError
            with pytest.raises(A2AScopeViolationError) as exc_info:
                await _dispatch_skill(
                    "recall_relevant_context",
                    {"query": "test", "namespace_id": str(ns_id)},
                    caller_ctx,
                )
            assert "not authorized for contractor sessions" in str(exc_info.value)

            # C. Invoke unauthorized skill (archive_session) -> should raise A2AScopeViolationError
            with pytest.raises(A2AScopeViolationError):
                await _dispatch_skill(
                    "archive_session",
                    {
                        "namespace_id": str(ns_id),
                        "agent_id": "test",
                        "memories": [{"content": "x"}],
                    },
                    caller_ctx,
                )

    finally:
        await app_pool.close()
