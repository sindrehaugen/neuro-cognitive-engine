"""Integration tests for Agreements vertical module (Batch 107)."""

from __future__ import annotations

import hashlib
import hmac as _hmac
import json
import time
import uuid
from contextlib import asynccontextmanager
from unittest.mock import MagicMock, patch

import asyncpg
import httpx
import pytest

from nce.admin_app import app
from nce.auth import set_namespace_context
from nce.config import cfg
from nce.db_utils import scoped_pg_session
from nce.entity_resolution.ownership_seed import seed_node_ownership_registry
from nce.vertical_modules.agreements.extract import (
    ExtractedAgreementModel,
    ExtractedFieldFloat,
    ExtractedFieldInt,
    ExtractedFieldString,
    ExtractedKickbackTiers,
    KickbackTier,
)
from nce.vertical_modules.agreements.graph import do_upsert_agreement


@pytest.fixture(autouse=True)
def bypass_lifespan():
    """Bypass Starlette app lifespan to avoid real DB connections at startup."""

    @asynccontextmanager
    async def dummy_lifespan(app):
        yield

    original_lifespan = app.router.lifespan_context
    app.router.lifespan_context = dummy_lifespan
    yield
    app.router.lifespan_context = original_lifespan


@pytest.fixture(autouse=True)
def mock_embeddings():
    """Mock the embedding model load to return None, forcing fallback vectors and avoiding slow imports/downloads."""
    with patch("nce.embeddings._load_sentence_transformer", return_value=None):
        yield


def _make_signature(key: str, method: str, path: str, timestamp: int, body: bytes = b"") -> str:
    parts = [method.upper(), path, str(timestamp)]
    if body:
        parts.append(hashlib.sha256(body).hexdigest())
    canonical = "\n".join(parts)
    return _hmac.new(key.encode(), canonical.encode(), hashlib.sha256).hexdigest()


def _valid_headers(key: str, method: str, path: str, body: bytes = b"") -> dict[str, str]:
    ts = int(time.time())
    sig = _make_signature(key, method, path, ts, body)
    return {
        "X-NCE-Timestamp": str(ts),
        "Authorization": f"HMAC-SHA256 {sig}",
        "X-NCE-Nonce": uuid.uuid4().hex,
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_do_upsert_agreement_direct(pg_pool: asyncpg.Pool, namespace_id: uuid.UUID) -> None:
    """Verify upserting an agreement direct logic lands in kg_nodes, kg_edges, and memories."""
    # Seed node ownership for the namespace
    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, namespace_id)
            await seed_node_ownership_registry(conn, namespace_id)

    agreement_id = uuid.uuid4()
    source_doc_ref = "sharepoint://contracts/test_agreement.pdf"

    # Mock NCEEngine
    engine_mock = MagicMock()
    engine_mock.pg_pool = pg_pool
    engine_mock.mongo_client = None

    params = {
        "namespace_id": str(namespace_id),
        "agreement_id": str(agreement_id),
        "supplier_id": "VENDOR:ACME",
        "customer_id": "CUSTOMER:STEPS",
        "source_doc_ref": source_doc_ref,
        "valid_from": "2026-06-01",
        "valid_to": "2027-06-01",
        "payment_terms_days": 30,
        "frame_discount_pct": 10.0,
        "volume_commitment": 50000.0,
        "extraction_confidence": 95.0,
    }

    # Execute directly
    res = await do_upsert_agreement(engine_mock, params)
    assert res["status"] == "ok"
    assert res["agreement_id"] == str(agreement_id)

    # Verify db state
    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        # Check kg_nodes
        nodes = await conn.fetch(
            "SELECT label, entity_type, agreements_source_id FROM kg_nodes WHERE namespace_id = $1::uuid",
            namespace_id,
        )
        labels = {n["label"]: n for n in nodes}
        agreement_label = f"Agreement:{agreement_id}"
        assert agreement_label in labels
        assert labels[agreement_label]["entity_type"] == "AGREEMENT"
        assert labels[agreement_label]["agreements_source_id"] == str(agreement_id)

        # Check terms
        term_label = f"AgreementTerm:{agreement_id}:paymentTermsDays"
        assert term_label in labels
        assert labels[term_label]["entity_type"] == "AGREEMENT_TERM"
        assert labels[term_label]["agreements_source_id"] == str(agreement_id)

        # Check kg_edges
        edges = await conn.fetch(
            "SELECT subject_label, predicate, object_label, agreements_source_id, confidence FROM kg_edges WHERE namespace_id = $1::uuid",
            namespace_id,
        )
        term_edge = next(
            (
                e
                for e in edges
                if e["predicate"] == "has_term"
                and e["subject_label"] == agreement_label
                and e["object_label"] == term_label
            ),
            None,
        )
        assert term_edge is not None
        assert term_edge["agreements_source_id"] == str(agreement_id)
        assert abs(term_edge["confidence"] - 0.95) < 1e-4

        vendor_edge = next(
            (
                e
                for e in edges
                if e["predicate"] == "under"
                and e["subject_label"] == "Vendor:VENDOR:ACME"
                and e["object_label"] == agreement_label
            ),
            None,
        )
        assert vendor_edge is not None

        # Check memory
        memories = await conn.fetch(
            "SELECT id, payload_ref, memory_type, assertion_type FROM memories WHERE namespace_id = $1::uuid",
            namespace_id,
        )
        assert len(memories) == 1
        assert memories[0]["payload_ref"] == agreement_id.hex[:24]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_agreements_rest_endpoints(pg_pool: asyncpg.Pool, namespace_id: uuid.UUID) -> None:
    # 1. Seed node ownership and enable agreements vertical in namespace metadata
    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, namespace_id)
            await seed_node_ownership_registry(conn, namespace_id)
            await conn.execute(
                """
                UPDATE namespaces
                SET metadata = jsonb_set(COALESCE(metadata, '{}'::jsonb), '{agreements}', '{"enabled": true}')
                WHERE id = $1::uuid
                """,
                namespace_id,
            )

    agreement_id = uuid.uuid4()
    source_doc_ref = "sharepoint://contracts/api_test_agreement.pdf"

    # Seed the database directly for details/list test
    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        await conn.execute(
            """
            INSERT INTO agreement_review_queue (
                agreement_id, namespace_id, source_doc_ref, extraction_confidence, review_status, extracted
            ) VALUES ($1, $2, $3, $4, $5, $6::jsonb)
            """,
            agreement_id,
            namespace_id,
            source_doc_ref,
            75.5,
            "needs_review_yellow",
            json.dumps({"paymentTermsDays": 30, "frameDiscountPct": 10.0}),
        )
        await conn.execute(
            """
            INSERT INTO kg_nodes (label, entity_type, namespace_id, agreements_source_id, change_origin)
            VALUES ($1, 'AGREEMENT', $2::uuid, $3, 'agent')
            """,
            f"Agreement:{agreement_id}",
            str(namespace_id),
            str(agreement_id),
        )
        await conn.execute(
            """
            INSERT INTO kg_nodes (label, entity_type, namespace_id, agreements_source_id, change_origin)
            VALUES ($1, 'AGREEMENT_TERM', $2::uuid, $3, 'agent')
            """,
            f"AgreementTerm:{agreement_id}:paymentTermsDays",
            str(namespace_id),
            str(agreement_id),
        )
        await conn.execute(
            """
            INSERT INTO kg_edges (subject_label, predicate, object_label, confidence, namespace_id, agreements_source_id, change_origin)
            VALUES ($1, 'has_term', $2, 0.9, $3::uuid, $4, 'agent')
            """,
            f"Agreement:{agreement_id}",
            f"AgreementTerm:{agreement_id}:paymentTermsDays",
            str(namespace_id),
            str(agreement_id),
        )

    # Mock engine
    mock_engine = MagicMock()
    mock_engine.pg_pool = pg_pool
    mock_engine.mongo_client = None

    key = cfg.NCE_API_KEY or "test-key"

    with (
        patch("nce.admin_state.engine", mock_engine),
        patch("nce.config.cfg.NCE_ADMIN_MTLS_ENABLED", False),
    ):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # A. GET /api/agreements (list)
            headers = _valid_headers(key, "GET", "/api/agreements")
            url = f"/api/agreements?namespace_id={namespace_id}"
            r = await client.get(url, headers=headers)
            assert r.status_code == 200
            data = r.json()
            assert data["status"] == "ok"
            assert len(data["agreements"]) == 1
            assert data["agreements"][0]["id"] is not None
            assert data["kpis"]["total"] == 1

            # B. GET /api/agreements/{id} (detail)
            headers = _valid_headers(key, "GET", f"/api/agreements/{agreement_id}")
            url = f"/api/agreements/{agreement_id}?namespace_id={namespace_id}"
            r = await client.get(url, headers=headers)
            assert r.status_code == 200
            data = r.json()
            assert data["status"] == "ok"
            assert data["agreement"]["id"] is not None
            assert data["agreement"]["terms"]["paymenttermsdays"]["confidence"] == 0.9

            # C. POST /api/agreements/extract (extract)
            mock_extracted = ExtractedAgreementModel(
                supplierId=ExtractedFieldString(value="VENDOR:ACME", confidence=95.0),
                customerId=ExtractedFieldString(value="CUSTOMER:STEPS", confidence=85.0),
                validFrom=ExtractedFieldString(value="2026-06-01", confidence=95.0),
                validTo=ExtractedFieldString(value="2027-06-01", confidence=95.0),
                paymentTermsDays=ExtractedFieldInt(value=30, confidence=100.0),
                frameDiscountPct=ExtractedFieldFloat(value=10.0, confidence=95.0),
                volumeCommitment=ExtractedFieldFloat(value=50000.0, confidence=95.0),
                kickbackTiers=ExtractedKickbackTiers(
                    value=[KickbackTier(threshold=100000.0, pct=2.5)],
                    confidence=99.0,
                ),
            )

            payload = {
                "namespace_id": str(namespace_id),
                "source_doc_ref": source_doc_ref,
            }
            body_bytes = json.dumps(payload).encode("utf-8")
            headers = _valid_headers(key, "POST", "/api/agreements/extract", body=body_bytes)
            headers["Content-Type"] = "application/json"
            with patch(
                "nce.vertical_modules.agreements.extract._call_ocr_extraction",
                return_value=mock_extracted,
            ):
                r = await client.post(
                    "/api/agreements/extract", content=body_bytes, headers=headers
                )
            assert r.status_code == 200
            data = r.json()
            assert data["status"] == "ok"
            extracted_id = data["agreement_id"]
            # Since some fields like paymentTermsDays/frameDiscountPct are capped/never auto_green (forced needs_review_yellow),
            # the overall status is needs_review_yellow. Let's verify it!
            assert data["review_status"] == "needs_review_yellow"

            # D. POST /api/agreements/review (review)
            payload_review = {
                "namespace_id": str(namespace_id),
                "agreement_id": extracted_id,
                "decision": "confirm",
                "reviewed_by": "tester_agent",
                "corrected_terms": {"paymentTermsDays": 30},
            }
            body_bytes = json.dumps(payload_review).encode("utf-8")
            headers = _valid_headers(key, "POST", "/api/agreements/review", body=body_bytes)
            headers["Content-Type"] = "application/json"
            r = await client.post("/api/agreements/review", content=body_bytes, headers=headers)
            assert r.status_code == 200
            data = r.json()
            assert data["status"] == "ok"
            assert data["agreement"]["review_status"] == "auto_green"
