"""
tests/unit/test_marketing_advisor.py
====================================
Unit tests for Marketing Engine Advisor surfaces (ML14-B6):
  - do_suggest_content (drip/thought-leadership grounded in graph patterns)
  - do_audit_seo (AEO/GEO citation readiness & Schema.org JSON-LD generator)
  - Marketing REST API handlers in nce/admin_handlers/marketing.py
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from starlette.datastructures import QueryParams

from nce.admin_handlers.marketing import (
    api_marketing_approve_content,
    api_marketing_assets,
    api_marketing_audit_seo,
    api_marketing_candidates,
    api_marketing_capture_testimonial,
    api_marketing_draft_case_study,
    api_marketing_publish_content,
    api_marketing_suggest_content,
    api_marketing_testimonials,
)
from nce.vertical_modules.marketing._guard import (
    MarketingSensitiveDataLeakError,
)
from nce.vertical_modules.marketing.advisor import (
    do_audit_seo,
    do_suggest_content,
)

_NAMESPACE_ID = "00000000-0000-4000-8000-000000000001"
_ASSET_ID = str(uuid4())


def _make_mock_engine(
    *,
    enabled: bool = True,
    case_rows: list[dict] | None = None,
    asset_row: dict | None = None,
) -> MagicMock:
    engine = MagicMock()
    conn = AsyncMock()

    async def _fetchrow_side_effect(query: str, *args: list) -> dict | None:
        q_lower = query.lower()
        if "namespaces" in q_lower:
            return {"marketing_enabled": enabled}
        if "content_assets" in q_lower:
            return asset_row
        if "case_studies" in q_lower:
            if case_rows:
                return case_rows[0]
            return None
        if "count(*)" in q_lower:
            return {"total": len(case_rows or [])}
        return None

    conn.fetchrow.side_effect = _fetchrow_side_effect
    conn.fetch.return_value = case_rows or []
    conn.execute.return_value = "UPDATE 1"

    ctx = AsyncMock()
    ctx.__aenter__.return_value = conn
    ctx.__aexit__.return_value = None

    pool = MagicMock()
    pool.acquire.return_value = ctx
    engine.pg_pool = pool
    engine.pool = pool
    engine.redis_client = AsyncMock()
    return engine


# ---------------------------------------------------------------------------
# Advisor: do_suggest_content
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_suggest_content_returns_grounded_suggestions() -> None:
    """do_suggest_content returns structured ideas with graph citations."""
    engine = _make_mock_engine(
        case_rows=[
            {
                "id": str(uuid4()),
                "project_id": "P-AUD-01",
                "title": "Auditorium AV-over-IP Overhaul",
                "raw": {},
            }
        ]
    )

    res = await do_suggest_content(
        engine,
        {
            "namespace_id": _NAMESPACE_ID,
            "theme": "hybrid_workplace",
            "count": 2,
        },
    )

    assert res["ok"] is True
    assert res["count"] >= 1
    suggestion = res["suggestions"][0]
    assert "theme" in suggestion
    assert "title" in suggestion
    assert "angle" in suggestion
    assert "grounded_references" in suggestion
    assert len(suggestion["grounded_references"]) >= 1
    assert "suggested_outline" in suggestion


@pytest.mark.asyncio
async def test_suggest_content_refuses_when_disabled() -> None:
    """REST endpoint returns 409 if marketing is disabled in namespace."""
    from nce import admin_state

    engine = _make_mock_engine(enabled=False)
    admin_state.engine = engine

    req = _MockRequest(
        json_data={
            "namespace_id": _NAMESPACE_ID,
            "count": 3,
        }
    )
    resp = await api_marketing_suggest_content(req)
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Advisor: do_audit_seo (AEO/GEO & Schema.org)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_seo_aeo_score_and_json_ld() -> None:
    """do_audit_seo evaluates citation readiness and builds valid JSON-LD."""
    engine = _make_mock_engine()

    content = """
    ## Challenge
    The client experienced acoustic fatigue and 350 ms latency in hybrid meetings.

    ## Architectural Solution
    Deployed Dante audio arrays with PTP boundary clocking and steerable ceiling lobes.

    ## Verified Outcome
    Achieved 99.99% uptime, reduced ambient noise to 28 dB, and improved STI score to 0.78.
    Evidence node: urn:nce:project:delivered-boardroom.
    """

    res = await do_audit_seo(
        engine,
        {
            "namespace_id": _NAMESPACE_ID,
            "title": "Hybrid Executive Boardroom Acoustic Design",
            "content": content,
            "url": "https://example.test/case-studies/boardroom",
        },
    )

    assert res["ok"] is True
    assert res["aeo_score"] >= 80
    assert res["citation_readiness"] == "ready"
    assert "@context" in res["json_ld"]
    assert res["json_ld"]["@context"] == "https://schema.org"
    assert res["json_ld"]["@type"] == "TechArticle"
    assert res["json_ld"]["headline"] == "Hybrid Executive Boardroom Acoustic Design"
    assert "citation" in res["json_ld"]


@pytest.mark.asyncio
async def test_audit_seo_sensitive_financials_refusal() -> None:
    """MK-3 RED: do_audit_seo raises MarketingSensitiveDataLeakError on sensitive fields."""
    engine = _make_mock_engine()

    with pytest.raises(MarketingSensitiveDataLeakError):
        await do_audit_seo(
            engine,
            {
                "namespace_id": _NAMESPACE_ID,
                "title": "Study with Margin Leak",
                "content": {
                    "summary": "Installed display systems",
                    "margin": "42%",
                    "internal_cost": 15000,
                },
            },
        )


# ---------------------------------------------------------------------------
# Admin REST route tests
# ---------------------------------------------------------------------------


class _MockRequest:
    def __init__(self, json_data: dict | None = None, query_params: dict | None = None):
        self._json_data = json_data or {}
        self.query_params = QueryParams(query_params or {})

    async def json(self) -> dict:
        return self._json_data


@pytest.mark.asyncio
async def test_rest_api_marketing_candidates() -> None:
    """GET /api/marketing/candidates handles valid requests and missing namespace."""
    from nce import admin_state

    engine = _make_mock_engine()
    admin_state.engine = engine

    # Missing namespace_id -> 422
    req_bad = _MockRequest(query_params={})
    resp_bad = await api_marketing_candidates(req_bad)
    assert resp_bad.status_code == 422

    # Valid namespace_id -> 200
    req_good = _MockRequest(query_params={"namespace_id": _NAMESPACE_ID})
    resp_good = await api_marketing_candidates(req_good)
    assert resp_good.status_code == 200


@pytest.mark.asyncio
async def test_rest_api_marketing_suggest_content() -> None:
    """POST /api/marketing/suggest-content generates suggestions via REST."""
    from nce import admin_state

    engine = _make_mock_engine()
    admin_state.engine = engine

    req = _MockRequest(
        json_data={
            "namespace_id": _NAMESPACE_ID,
            "theme": "room_acoustic_design",
            "count": 2,
        }
    )
    resp = await api_marketing_suggest_content(req)
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_rest_api_marketing_audit_seo() -> None:
    """POST /api/marketing/audit-seo performs audit via REST."""
    from nce import admin_state

    engine = _make_mock_engine()
    admin_state.engine = engine

    req = _MockRequest(
        json_data={
            "namespace_id": _NAMESPACE_ID,
            "title": "AV Network Architecture",
            "content": "Challenge and solution with 10 Gbps throughput and 99.9% uptime. Verified evidence.",
        }
    )
    resp = await api_marketing_audit_seo(req)
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_rest_api_marketing_approve_and_publish() -> None:
    """POST /api/marketing/approve and POST /api/marketing/publish enforce MK-1."""
    from nce import admin_state

    artifact_id = str(uuid4())
    engine = _make_mock_engine(
        case_rows=[
            {
                "id": artifact_id,
                "namespace_id": _NAMESPACE_ID,
                "status": "draft",
                "title": "Modern Campus AV",
                "body": "Grounded facts...",
            }
        ]
    )
    admin_state.engine = engine

    # Attempting to publish draft refuses with 403 (MK-1)
    req_pub_draft = _MockRequest(
        json_data={
            "namespace_id": _NAMESPACE_ID,
            "artifact_id": artifact_id,
            "transport": "manual",
        }
    )
    resp_pub_draft = await api_marketing_publish_content(req_pub_draft)
    assert resp_pub_draft.status_code == 403

    # Approval
    req_app = _MockRequest(
        json_data={
            "namespace_id": _NAMESPACE_ID,
            "artifact_id": artifact_id,
            "approver": "Jane Doe",
            "decision": "approved",
        }
    )
    resp_app = await api_marketing_approve_content(req_app)
    assert resp_app.status_code == 200

    # Publish approved item
    engine_approved = _make_mock_engine(
        case_rows=[
            {
                "id": artifact_id,
                "namespace_id": _NAMESPACE_ID,
                "status": "approved",
                "approver": "Jane Doe",
                "title": "Modern Campus AV",
                "body": "Verified case study with zero downtime.",
                "marketing_source_id": "marketing:case:001",
            }
        ]
    )
    admin_state.engine = engine_approved

    req_pub = _MockRequest(
        json_data={
            "namespace_id": _NAMESPACE_ID,
            "artifact_id": artifact_id,
            "transport": "manual",
        }
    )
    resp_pub = await api_marketing_publish_content(req_pub)
    assert resp_pub.status_code == 200


@pytest.mark.asyncio
async def test_rest_api_marketing_draft_case_study() -> None:
    """POST /api/marketing/draft creates a grounded case study draft."""
    from nce import admin_state

    engine = _make_mock_engine()
    admin_state.engine = engine

    req = _MockRequest(
        json_data={
            "namespace_id": _NAMESPACE_ID,
            "project_id": "P-AUD-101",
            "anonymize": True,
            "grounded_facts": [
                {
                    "node_id": "urn:nce:project:p1",
                    "fact": "Delivered auditorium AV with zero dropped packets.",
                }
            ],
        }
    )
    resp = await api_marketing_draft_case_study(req)
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_rest_api_marketing_testimonials_and_capture() -> None:
    """GET /api/marketing/testimonials and POST /api/marketing/testimonials/capture work."""
    from nce import admin_state

    testimonial_id = str(uuid4())
    engine = _make_mock_engine(
        case_rows=[
            {
                "id": testimonial_id,
                "namespace_id": _NAMESPACE_ID,
                "customer_id": "CUST-001",
                "project_id": "P-001",
                "quote": "Outstanding reliability.",
                "status": "received",
                "consent": True,
                "consent_tier": "web_retractable",
                "consent_scope": {},
                "consent_recorded_at": None,
                "nps_at_capture": 9.5,
                "marketing_source_id": "marketing:test:001",
                "created_at": None,
                "updated_at": None,
            }
        ]
    )
    admin_state.engine = engine

    # List testimonials
    req_list = _MockRequest(query_params={"namespace_id": _NAMESPACE_ID})
    resp_list = await api_marketing_testimonials(req_list)
    assert resp_list.status_code == 200

    # Capture testimonial
    req_cap = _MockRequest(
        json_data={
            "namespace_id": _NAMESPACE_ID,
            "testimonial_id": testimonial_id,
            "quote": "Flawless audio deployment.",
            "consent": True,
            "consent_tier": "web_retractable",
            "consent_scope": {"scope": "all"},
        }
    )
    resp_cap = await api_marketing_capture_testimonial(req_cap)
    assert resp_cap.status_code == 200


@pytest.mark.asyncio
async def test_rest_api_marketing_assets() -> None:
    """GET /api/marketing/assets lists brand and content assets."""
    from nce import admin_state

    engine = _make_mock_engine(
        case_rows=[
            {
                "id": str(uuid4()),
                "namespace_id": _NAMESPACE_ID,
                "kind": "case_study",
                "ref_id": "CS-001",
                "title": "Corporate Boardroom Case Study",
                "seo": {},
                "storage_uri": "s3://assets/study.pdf",
                "status": "approved",
                "marketing_source_id": "marketing:asset:001",
                "created_at": None,
                "updated_at": None,
            }
        ]
    )
    admin_state.engine = engine

    req = _MockRequest(query_params={"namespace_id": _NAMESPACE_ID})
    resp = await api_marketing_assets(req)
    assert resp.status_code == 200
