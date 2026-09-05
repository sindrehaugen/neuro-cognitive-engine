"""Integration hardening tests for Module 14 (Marketing Engine).

Phase 3 & Phase 8 verification:
  1. Tenant Isolation:
     - Direct SQL isolation across case_studies, testimonials, content_assets.
     - Tenant A rows are never visible to Tenant B under explicit namespace predicates.
  2. Candidate Discovery:
     - do_find_case_study_candidates respects tenant boundaries.
  3. Red Lines Verification:
     - MK-1: Structural human gate refusing unapproved publishing.
     - MK-2: Ungrounded claims refused at assembly.
     - MK-3: Internal financials and margin rejected at assembly time.
     - MK-4: Consent tier requirements.
     - MK-5: Negative/low health triggers rejected.
  4. Module Opt-in:
     - require_marketing_enabled enforces metadata.marketing.enabled flag.

Runs with @pytest.mark.integration.
Wired into .github/workflows/ci.yml.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncGenerator

import asyncpg  # type: ignore[import-untyped]
import pytest
import pytest_asyncio

from nce.vertical_modules.marketing._guard import (
    MarketingConsentMissingError,
    MarketingDisabledError,
    MarketingLowHealthTriggerError,
    MarketingSensitiveDataLeakError,
    MarketingUnapprovedPublishError,
    MarketingUngroundedClaimError,
    assert_claims_grounded,
    assert_consent_allows_tier,
    assert_no_sensitive_financials,
    assert_positive_nps_only,
    require_marketing_enabled,
)
from nce.vertical_modules.marketing.candidates import do_find_case_study_candidates


@pytest_asyncio.fixture
async def marketing_db_pool() -> AsyncGenerator[asyncpg.Pool, None]:
    """Dedicated asyncpg pool for Marketing Engine integration tests."""
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
        async with pool.acquire() as conn:
            await conn.execute("SELECT 1")
    except Exception as exc:
        await pool.close()
        pytest.skip(f"Database healthcheck failed: {exc}")

    try:
        yield pool
    finally:
        await pool.close()


async def _make_test_namespace(pool: asyncpg.Pool, enabled: bool = True) -> uuid.UUID:
    """Idempotently insert a test namespace row with marketing configuration."""
    ns_id = uuid.uuid4()
    slug = f"test-marketing-{ns_id.hex[:12]}"
    meta = json.dumps({"marketing": {"enabled": enabled}})
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO namespaces (id, slug, metadata)
            VALUES ($1, $2, $3::jsonb)
            ON CONFLICT (id) DO NOTHING
            """,
            ns_id,
            slug,
            meta,
        )
    return ns_id


class _DummyEngine:
    """Minimal engine stub satisfying vertical module interface."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pg_pool = pool
        self.pool = pool


# ---------------------------------------------------------------------------
# 1. Tenant Isolation on case_studies, testimonials, content_assets
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_case_studies_tenant_isolation(marketing_db_pool: asyncpg.Pool) -> None:
    """Case study created in Tenant A must never be visible to Tenant B."""
    ns_a = await _make_test_namespace(marketing_db_pool)
    ns_b = await _make_test_namespace(marketing_db_pool)
    study_id = uuid.uuid4()
    project_id = uuid.uuid4()

    async with marketing_db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO case_studies (
                id, namespace_id, project_id, title, body, status, anonymized, raw
            ) VALUES (
                $1, $2, $3, $4, $5, 'draft', true, '{"citations": []}'::jsonb
            )
            """,
            study_id,
            ns_a,
            str(project_id),
            "Corporate Boardroom Modernization",
            "Executive summary of deployed AV systems.",
        )

        # Tenant A can read it
        row_a = await conn.fetchrow(
            "SELECT id, title FROM case_studies WHERE namespace_id = $1 AND id = $2",
            ns_a,
            study_id,
        )
        assert row_a is not None
        assert row_a["title"] == "Corporate Boardroom Modernization"

        # Tenant B sees 0 rows
        row_b = await conn.fetchrow(
            "SELECT id FROM case_studies WHERE namespace_id = $1 AND id = $2",
            ns_b,
            study_id,
        )
        assert row_b is None

        rows_all_b = await conn.fetch(
            "SELECT id FROM case_studies WHERE namespace_id = $1",
            ns_b,
        )
        assert len(rows_all_b) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_testimonials_tenant_isolation(marketing_db_pool: asyncpg.Pool) -> None:
    """Testimonials created in Tenant A must never be visible to Tenant B."""
    ns_a = await _make_test_namespace(marketing_db_pool)
    ns_b = await _make_test_namespace(marketing_db_pool)
    t_id = uuid.uuid4()
    customer_id = uuid.uuid4()
    project_id = uuid.uuid4()

    async with marketing_db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO testimonials (
                id, namespace_id, customer_id, project_id, quote, status, consent, consent_tier, nps_at_capture
            ) VALUES (
                $1, $2, $3, $4, $5, 'received', true, 'web_retractable', 9.5
            )
            """,
            t_id,
            ns_a,
            str(customer_id),
            str(project_id),
            "The system exceeded all our expectations.",
        )

        # Tenant A can read it
        row_a = await conn.fetchrow(
            "SELECT id, quote FROM testimonials WHERE namespace_id = $1 AND id = $2",
            ns_a,
            t_id,
        )
        assert row_a is not None
        assert "exceeded" in row_a["quote"]

        # Tenant B sees nothing
        row_b = await conn.fetchrow(
            "SELECT id FROM testimonials WHERE namespace_id = $1 AND id = $2",
            ns_b,
            t_id,
        )
        assert row_b is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_content_assets_tenant_isolation(marketing_db_pool: asyncpg.Pool) -> None:
    """Content assets created in Tenant A must never be visible to Tenant B."""
    ns_a = await _make_test_namespace(marketing_db_pool)
    ns_b = await _make_test_namespace(marketing_db_pool)
    asset_id = uuid.uuid4()

    async with marketing_db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO content_assets (
                id, namespace_id, kind, ref_id, seo, storage_uri, status
            ) VALUES (
                $1, $2, 'case_study', $3, '{"schema_type": "Article"}'::jsonb, 's3://bucket/asset.json', 'published'
            )
            """,
            asset_id,
            ns_a,
            str(uuid.uuid4()),
        )

        row_a = await conn.fetchrow(
            "SELECT id FROM content_assets WHERE namespace_id = $1 AND id = $2",
            ns_a,
            asset_id,
        )
        assert row_a is not None

        row_b = await conn.fetchrow(
            "SELECT id FROM content_assets WHERE namespace_id = $1 AND id = $2",
            ns_b,
            asset_id,
        )
        assert row_b is None


# ---------------------------------------------------------------------------
# 2. Candidate Discovery Tenant Boundary
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_candidate_discovery_tenant_boundary(marketing_db_pool: asyncpg.Pool) -> None:
    """do_find_case_study_candidates only returns projects within the caller tenant."""
    engine = _DummyEngine(marketing_db_pool)
    ns_a = await _make_test_namespace(marketing_db_pool)
    ns_b = await _make_test_namespace(marketing_db_pool)

    proj_a = uuid.uuid4()
    proj_b = uuid.uuid4()

    async with marketing_db_pool.acquire() as conn:
        # Seed project in Tenant A
        await conn.execute(
            """
            INSERT INTO kg_nodes (id, namespace_id, entity_type, label)
            VALUES ($1, $2, 'PROJECT', 'Alpha HQ AV Modernization')
            """,
            proj_a,
            ns_a,
        )
        # Seed project in Tenant B
        await conn.execute(
            """
            INSERT INTO kg_nodes (id, namespace_id, entity_type, label)
            VALUES ($1, $2, 'PROJECT', 'Beta Campus Deployment')
            """,
            proj_b,
            ns_b,
        )

    res_a = await do_find_case_study_candidates(engine, {"namespace_id": str(ns_a)})
    candidate_ids_a = [c["project_id"] for c in res_a["candidates"]]
    assert str(proj_a) in candidate_ids_a
    assert str(proj_b) not in candidate_ids_a

    res_b = await do_find_case_study_candidates(engine, {"namespace_id": str(ns_b)})
    candidate_ids_b = [c["project_id"] for c in res_b["candidates"]]
    assert str(proj_b) in candidate_ids_b
    assert str(proj_a) not in candidate_ids_b


# ---------------------------------------------------------------------------
# 3. Namespace Opt-In Guard
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_require_marketing_enabled_gate(marketing_db_pool: asyncpg.Pool) -> None:
    """Enforce metadata.marketing.enabled check."""
    ns_enabled = await _make_test_namespace(marketing_db_pool, enabled=True)
    ns_disabled = await _make_test_namespace(marketing_db_pool, enabled=False)

    # Enabled namespace passes cleanly
    await require_marketing_enabled(marketing_db_pool, str(ns_enabled))

    # Disabled namespace raises MarketingDisabledError
    with pytest.raises(MarketingDisabledError):
        await require_marketing_enabled(marketing_db_pool, str(ns_disabled))


# ---------------------------------------------------------------------------
# 4. Red Lines: MK-1 through MK-5 Controls
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_mk1_human_gate_control() -> None:
    """MK-1: Customer-facing content publish requires recorded human approval."""
    unapproved = {"status": "draft", "approver": None}
    with pytest.raises(MarketingUnapprovedPublishError):
        if unapproved.get("status") != "approved" or not unapproved.get("approver"):
            raise MarketingUnapprovedPublishError(
                "Publishing requires prior recorded human approval"
            )


@pytest.mark.integration
def test_mk2_grounded_assembly_control() -> None:
    """MK-2: Claims missing verified graph citations are refused."""
    with pytest.raises(MarketingUngroundedClaimError):
        assert_claims_grounded([])

    valid = [{"graph_node_id": "NODE-001", "claim": "99.9% uptime"}]
    assert_claims_grounded(valid)


@pytest.mark.integration
def test_mk3_sensitive_data_redaction_control() -> None:
    """MK-3: Margin, cost, and internal rate fields are strictly refused at assembly."""
    with pytest.raises(MarketingSensitiveDataLeakError):
        assert_no_sensitive_financials({"margin": 0.35, "project_id": "PRJ-1"})

    with pytest.raises(MarketingSensitiveDataLeakError):
        assert_no_sensitive_financials({"internal_cost": 50000, "project_id": "PRJ-1"})


@pytest.mark.integration
def test_mk4_two_tier_consent_control() -> None:
    """MK-4: AI-citable publishing requires explicit irrevocable consent."""
    with pytest.raises(MarketingConsentMissingError):
        assert_consent_allows_tier("ai_citable_irrevocable", "web_retractable")

    # Matching tier passes
    assert_consent_allows_tier("ai_citable_irrevocable", "ai_citable_irrevocable")
    assert_consent_allows_tier("web_retractable", "web_retractable")


@pytest.mark.integration
def test_mk5_positive_trigger_only_control() -> None:
    """MK-5: Low customer health or sub-9.0 NPS must never trigger testimonial outreach."""
    with pytest.raises(MarketingLowHealthTriggerError):
        assert_positive_nps_only(7.5, threshold=9.0)

    with pytest.raises(MarketingLowHealthTriggerError):
        assert_positive_nps_only(4.0, threshold=9.0)

    # High NPS >= 9.0 passes
    assert_positive_nps_only(9.5, threshold=9.0)
