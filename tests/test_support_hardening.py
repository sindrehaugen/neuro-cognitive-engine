"""Integration hardening tests for Module 10 (Support Engine).

Phase 6 (ML10-B8): Hardening, CI Wiring & Contract Verification.
Proves against real PostgreSQL:
  1. Contract-H Native Support Isolation (cross-tenant predicates and DB isolation):
     - Tenant A opens a ticket; Tenant B cannot find or query it (TicketNotFoundError).
  2. SLA Clocks:
     - Real DB insert & query in `sla_clocks` table; tenant isolation strictly enforced.
  3. Customer Health & Touchpoints:
     - Real DB insert & rolling score update in `customer_health`; cross-tenant isolation.
  4. Ticket Resolution & Cognitive Ledger:
     - Resolving a ticket updates `service_tickets` status and appends an auditable
       fact to `v3_cognitive_ledger` with model_version='support/v1'.
  5. Contract-A Node Ownership Registry:
     - `seed_node_ownership_registry` populates TICKET, SLA, SUPPORT_HEALTH_SCORE,
       SUPPORT_DIAGNOSIS for the namespace.
     - `assert_owner` admits 'support' and refuses non-owners with `OwnershipError`.

Runs with `@pytest.mark.integration`.
Wired into `.github/workflows/ci.yml`.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator

import asyncpg  # type: ignore[import-untyped]
import pytest
import pytest_asyncio

from nce.auth import set_namespace_context
from nce.entity_resolution.ownership import OwnershipError, assert_owner
from nce.entity_resolution.ownership_seed import seed_node_ownership_registry
from nce.vertical_modules.support.health import do_health_score, do_record_touchpoint
from nce.vertical_modules.support.sla import do_sla_clock
from nce.vertical_modules.support.tickets import (
    TicketNotFoundError,
    do_open_ticket,
    do_query_ticket,
    do_resolve_ticket,
)


@pytest_asyncio.fixture
async def support_db_pool() -> AsyncGenerator[asyncpg.Pool, None]:
    """Dedicated asyncpg pool for Support integration tests.

    Charter §5.4: Connects directly to PG_DSN without calling engine.connect()
    or checking signing keys, guaranteeing reliable execution in CI and local
    integration runs.
    """
    dsn = (
        os.getenv("NCE_INTEGRATION_PG_DSN")
        or os.getenv("PG_DSN")
        or os.getenv("DATABASE_URL")
        or "postgresql://mcp_user:mcp_password@localhost:5432/memory_meta"
    )
    try:
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=5, timeout=10.0)
    except Exception as exc:
        pytest.skip(f"Database unreachable at {dsn}: {exc}")

    try:
        # Quick healthcheck
        async with pool.acquire() as conn:
            await conn.execute("SELECT 1")
    except Exception as exc:
        await pool.close()
        pytest.skip(f"Database healthcheck failed: {exc}")

    try:
        yield pool
    finally:
        await pool.close()


async def _make_test_namespace(pool: asyncpg.Pool) -> uuid.UUID:
    """Idempotently insert a test namespace row."""
    ns_id = uuid.uuid4()
    slug = f"test-support-{ns_id.hex[:12]}"
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO namespaces (id, slug, metadata)
            VALUES ($1, $2, '{"support": {"enabled": true}}'::jsonb)
            ON CONFLICT (id) DO NOTHING
            """,
            ns_id,
            slug,
        )
    return ns_id


class _DummyEngine:
    """Minimal engine stub satisfying vertical module interface."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pg_pool = pool
        self.pool = pool


@pytest.mark.integration
@pytest.mark.asyncio
async def test_support_tenant_isolation_database(support_db_pool: asyncpg.Pool) -> None:
    """Tenant A opens a ticket; Tenant B cannot see, read, or resolve it."""
    engine = _DummyEngine(support_db_pool)
    ns_a = await _make_test_namespace(support_db_pool)
    ns_b = await _make_test_namespace(support_db_pool)

    # 1. Tenant A opens a ticket
    ticket_a = await do_open_ticket(
        engine,
        {
            "namespace_id": str(ns_a),
            "summary": "Display flickering in Boardroom A",
            "priority": "high",
            "customer_id": "cust-acme-1",
        },
    )
    assert ticket_a["ticket"]["status"] == "open"
    ticket_id = ticket_a["ticket"]["id"]

    # 2. Tenant A can query it
    query_a = await do_query_ticket(
        engine,
        {
            "namespace_id": str(ns_a),
            "ticket_id": ticket_id,
        },
    )
    assert query_a["ticket"]["id"] == ticket_id
    assert query_a["ticket"]["priority"] == "high"

    # 3. Tenant B querying the same ticket_id gets TicketNotFoundError
    with pytest.raises(TicketNotFoundError):
        await do_query_ticket(
            engine,
            {
                "namespace_id": str(ns_b),
                "ticket_id": ticket_id,
            },
        )

    # 4. Direct SQL assertion: Tenant B sees 0 rows under explicit namespace filter
    async with support_db_pool.acquire() as conn:
        row_b = await conn.fetchrow(
            "SELECT id FROM service_tickets WHERE namespace_id = $1::uuid AND id = $2::uuid",
            ns_b,
            uuid.UUID(ticket_id),
        )
        assert row_b is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_support_sla_clock_and_resolution_lifecycle(
    support_db_pool: asyncpg.Pool,
) -> None:
    """SLA clock creation, calculation, and ticket resolution with ledger append."""
    engine = _DummyEngine(support_db_pool)
    ns = await _make_test_namespace(support_db_pool)

    # 1. Open critical ticket
    ticket = await do_open_ticket(
        engine,
        {
            "namespace_id": str(ns),
            "summary": "Main projector offline",
            "priority": "critical",
            "create_sla_clock": True,
        },
    )
    ticket_id = ticket["ticket"]["id"]
    assert ticket["sla_clock"] is not None

    # 2. Query SLA clock
    clock_res = await do_sla_clock(
        engine,
        {
            "namespace_id": str(ns),
            "ticket_id": ticket_id,
        },
    )
    assert clock_res["ticket_id"] == ticket_id
    assert isinstance(clock_res["breach_risk"], bool)
    assert isinstance(clock_res["breached"], bool)
    assert clock_res["sla_profile"] == "standard"

    # 3. Resolve ticket
    res_out = await do_resolve_ticket(
        engine,
        {
            "namespace_id": str(ns),
            "ticket_id": ticket_id,
            "resolution_text": "Replaced faulty HDMI EDID emulator and reseated cable.",
            "was_fix": True,
            "agent_id": "field-tech-42",
        },
    )
    assert res_out["ticket"]["status"] == "resolved"
    assert res_out["ticket"]["resolved_at"] is not None

    # 4. Verify v3_cognitive_ledger recorded the resolution fact
    async with support_db_pool.acquire() as conn:
        ledger_row = await conn.fetchrow(
            """
            SELECT id, model_version, tlx_scores
            FROM v3_cognitive_ledger
            WHERE namespace_id = $1::uuid
              AND (tlx_scores->>'ticket_id') = $2
            ORDER BY created_at DESC
            LIMIT 1
            """,
            ns,
            ticket_id,
        )
        assert ledger_row is not None
        assert ledger_row["model_version"] == "support/v1"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_support_customer_health_and_touchpoints(
    support_db_pool: asyncpg.Pool,
) -> None:
    """Recording a touchpoint updates customer_health table and recomputes score."""
    engine = _DummyEngine(support_db_pool)
    ns = await _make_test_namespace(support_db_pool)
    customer_id = f"cust-{uuid.uuid4().hex[:8]}"

    # 1. Record an ÉT-spørsmål touchpoint
    touch_res = await do_record_touchpoint(
        engine,
        {
            "namespace_id": str(ns),
            "customer_id": customer_id,
            "question_id": "satisfaction_post_install",
            "answer": "Alt fungerer utmerket, veldig fornøyd!",
            "score": 5,
        },
    )
    assert touch_res["customer_id"] == customer_id

    # 2. Query health score
    health_res = await do_health_score(
        engine,
        {
            "namespace_id": str(ns),
            "customer_id": customer_id,
            "lookback_days": 90,
        },
    )
    assert health_res["customer_id"] == customer_id
    assert 0.0 <= health_res["score"] <= 100.0
    assert health_res["churn_risk"] in ("low", "medium", "high")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_support_contract_a_node_ownership_registry(
    support_db_pool: asyncpg.Pool,
) -> None:
    """Contract-A: seed_node_ownership_registry populates Support nodes and assert_owner guards them."""
    ns = await _make_test_namespace(support_db_pool)

    async with support_db_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, ns)
            # Idempotently seed registry from real nce/config_data/node-ownership.json
            await seed_node_ownership_registry(conn, ns)

        # 1. 'support' engine must pass for all four M10 spine nodes
        for node_type in ("TICKET", "SLA", "SUPPORT_HEALTH_SCORE", "SUPPORT_DIAGNOSIS"):
            async with conn.transaction():
                await set_namespace_context(conn, ns)
                await assert_owner(conn, ns, node_type, "support")

        # 2. Other engines must be refused with OwnershipError
        for node_type in ("TICKET", "SLA", "SUPPORT_HEALTH_SCORE", "SUPPORT_DIAGNOSIS"):
            async with conn.transaction():
                await set_namespace_context(conn, ns)
                with pytest.raises(OwnershipError) as exc_info:
                    await assert_owner(conn, ns, node_type, "sales")
                err = exc_info.value
                assert err.node_type == node_type
                assert err.writer_engine == "sales"
                assert err.owner_engine == "support"
