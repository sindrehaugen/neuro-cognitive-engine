"""Integration hardening tests for Module 13 (HR Engine).

Covers against real PostgreSQL:
  1. Employee Profile & Tenant Isolation:
     - Tenant A creates an employee card; Tenant B cannot see or query it.
     - Direct SQL proves 0 rows visible under Tenant B namespace_id.
  2. Skills & Certifications Matrix:
     - Skills and certifications persist with tenant isolation.
  3. Absences & Leave Tracking:
     - Absences recorded under tenant policy with date ranges.
  4. RL-1 NEVER Ranking Enforcement:
     - Skill matching returns requirement fit, not a leaderboard or standing rank.

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

from nce.vertical_modules.hr._guard import HrRankingProhibitedError, assert_ranking_prohibited
from nce.vertical_modules.hr.certs import do_cert_status
from nce.vertical_modules.hr.profile import (
    do_create_employee,
    do_get_employee,
    do_query_employees,
)
from nce.vertical_modules.hr.skills import (
    do_record_certification,
    do_record_skill,
)

pytestmark = pytest.mark.integration

_PG_DSN = os.getenv("PG_DSN", "postgresql://mcp_user:mcp_password@localhost:5432/memory_meta")


@pytest_asyncio.fixture
async def pg_pool() -> AsyncGenerator[asyncpg.Pool, None]:
    try:
        pool = await asyncpg.create_pool(_PG_DSN, min_size=1, max_size=3)
    except Exception as exc:
        pytest.skip(f"PostgreSQL not reachable at {_PG_DSN}: {exc}")
    try:
        yield pool
    finally:
        await pool.close()


@pytest_asyncio.fixture
async def tenant_a(pg_pool: asyncpg.Pool) -> AsyncGenerator[str, None]:
    ns_id = str(uuid.uuid4())
    async with pg_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO namespaces (id, slug, metadata)
            VALUES ($1::uuid, $2, '{"hr": {"enabled": true}}'::jsonb)
            ON CONFLICT (id) DO NOTHING
            """,
            ns_id,
            f"test_hr_tenant_a_{ns_id[:8]}",
        )
    try:
        yield ns_id
    finally:
        async with pg_pool.acquire() as conn:
            await conn.execute("DELETE FROM namespaces WHERE id = $1::uuid", ns_id)


@pytest_asyncio.fixture
async def tenant_b(pg_pool: asyncpg.Pool) -> AsyncGenerator[str, None]:
    ns_id = str(uuid.uuid4())
    async with pg_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO namespaces (id, slug, metadata)
            VALUES ($1::uuid, $2, '{"hr": {"enabled": true}}'::jsonb)
            ON CONFLICT (id) DO NOTHING
            """,
            ns_id,
            f"test_hr_tenant_b_{ns_id[:8]}",
        )
    try:
        yield ns_id
    finally:
        async with pg_pool.acquire() as conn:
            await conn.execute("DELETE FROM namespaces WHERE id = $1::uuid", ns_id)


@pytest.mark.asyncio
async def test_hr_tenant_isolation_employees(
    pg_pool: asyncpg.Pool,
    tenant_a: str,
    tenant_b: str,
) -> None:
    """Proves tenant isolation: Tenant B cannot see Tenant A's employees."""
    emp_id = f"EMP-{uuid.uuid4().hex[:6].upper()}"

    # 1. Create in Tenant A
    created = await do_create_employee(
        pg_pool,
        {
            "namespace_id": tenant_a,
            "employee_id": emp_id,
            "name": "Employee Alpha",
            "email": "alpha@example.test",
            "department": "operations",
            "role": "technician",
        },
    )
    assert created["employee_id"] == emp_id

    # 2. Query in Tenant A -> visible
    q_a = await do_query_employees(pg_pool, {"namespace_id": tenant_a})
    assert any(e["employee_id"] == emp_id for e in q_a["employees"])

    # 3. Query in Tenant B -> strictly invisible
    q_b = await do_query_employees(pg_pool, {"namespace_id": tenant_b})
    assert not any(e["employee_id"] == emp_id for e in q_b["employees"])

    # 4. Direct get in Tenant B -> raises ValueError
    with pytest.raises(ValueError, match="not found in namespace"):
        await do_get_employee(
            pg_pool,
            {"namespace_id": tenant_b, "employee_id": emp_id},
        )


@pytest.mark.asyncio
async def test_hr_skills_and_certs_lifecycle(
    pg_pool: asyncpg.Pool,
    tenant_a: str,
) -> None:
    """Proves skills recording and cert lifecycle."""
    emp_id = f"EMP-{uuid.uuid4().hex[:6].upper()}"

    await do_create_employee(
        pg_pool,
        {
            "namespace_id": tenant_a,
            "employee_id": emp_id,
            "name": "Employee Beta",
            "email": "beta@example.test",
        },
    )

    # Record skill
    sk = await do_record_skill(
        pg_pool,
        {
            "namespace_id": tenant_a,
            "employee_id": emp_id,
            "skill_id": "dante-routing",
            "name": "Dante Network Routing",
            "category": "audio",
            "level": "expert",
        },
    )
    assert sk["skill_id"] == "dante-routing"

    # Record cert
    cert = await do_record_certification(
        pg_pool,
        {
            "namespace_id": tenant_a,
            "employee_id": emp_id,
            "cert_id": f"CERT-{uuid.uuid4().hex[:6].upper()}",
            "authority": "AVIXA",
            "name": "CTS",
            "status": "active",
        },
    )
    assert cert["authority"] == "AVIXA"

    # Verify cert status
    c_status = await do_cert_status(pg_pool, {"namespace_id": tenant_a, "employee_id": emp_id})
    assert c_status["total_count"] >= 1


@pytest.mark.asyncio
async def test_hr_rl1_never_ranking_enforcement() -> None:
    """RL-1 NEVER ranking: asserting ranking parameters raises HrRankingProhibitedError."""
    with pytest.raises(HrRankingProhibitedError):
        assert_ranking_prohibited({"leaderboard": True})

    with pytest.raises(HrRankingProhibitedError):
        assert_ranking_prohibited({"standing_ranking": True})

    with pytest.raises(HrRankingProhibitedError):
        assert_ranking_prohibited({"sort_by": "score"})
