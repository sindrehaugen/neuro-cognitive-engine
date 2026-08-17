"""Integration tests for Batch 106 (Module 3 Wave 2 - review-queue)."""

from __future__ import annotations

import json
import os
import uuid
from typing import Any
from urllib.parse import urlparse, urlunparse

import asyncpg  # type: ignore[import-untyped]
import pytest

from nce.config import cfg
from nce.db_utils import scoped_pg_session
from nce.vertical_modules.agreements.review import do_review_extraction


class EngineStub:
    """Stub representing the core engine context passed to vertical modules."""

    def __init__(self, pg_pool: asyncpg.Pool) -> None:
        self.pg_pool = pg_pool


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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_review_extraction_lifecycle(pg_pool: asyncpg.Pool, make_namespace: Any) -> None:
    """Verify that do_review_extraction confirm/reject decisions work correctly."""
    ns_id = await make_namespace()
    agreement_id = uuid.uuid4()
    source_doc_ref = "sharepoint://contracts/test_agreement.pdf"

    # Seed data using the admin pool
    async with scoped_pg_session(pg_pool, ns_id) as conn:
        # Seed agreement_review_queue
        await conn.execute(
            """
            INSERT INTO agreement_review_queue (
                agreement_id, namespace_id, source_doc_ref, extraction_confidence, review_status, extracted
            ) VALUES ($1, $2, $3, $4, $5, $6::jsonb)
            """,
            agreement_id,
            uuid.UUID(str(ns_id)),
            source_doc_ref,
            75.5,
            "needs_review_yellow",
            json.dumps({"paymentTermsDays": 30, "frameDiscountPct": 10.0}),
        )

        # Seed agreement_extraction_runs
        await conn.execute(
            """
            INSERT INTO agreement_extraction_runs (
                namespace_id, run_id, source_doc_ref, extraction_confidence, status
            ) VALUES ($1, $2, $3, $4, $5)
            """,
            uuid.UUID(str(ns_id)),
            uuid.uuid4(),
            source_doc_ref,
            75.5,
            "ok",
        )

    # Setup restricted nce_app pool for the logic call
    app_dsn = _app_dsn()
    app_pool = await asyncpg.create_pool(app_dsn, min_size=1, max_size=2)
    engine = EngineStub(app_pool)

    try:
        # 1. Test Decision: Reject
        res_reject = await do_review_extraction(
            engine,  # type: ignore[arg-type]
            {
                "namespace_id": ns_id,
                "agreement_id": agreement_id,
                "decision": "reject",
                "reviewed_by": "operator_one",
            },
        )
        assert res_reject["review_status"] == "manual_red"
        assert res_reject["reviewed_by"] == "operator_one"
        assert res_reject["reviewed_at"] is not None
        assert res_reject["extracted"]["paymentTermsDays"] == 30

        # 2. Test Decision: Confirm (with corrected terms)
        corrected = {"paymentTermsDays": 45, "frameDiscountPct": 12.5}
        res_confirm = await do_review_extraction(
            engine,  # type: ignore[arg-type]
            {
                "namespace_id": ns_id,
                "agreement_id": agreement_id,
                "decision": "confirm",
                "reviewed_by": "operator_two",
                "corrected_terms": corrected,
            },
        )
        assert res_confirm["review_status"] == "auto_green"
        assert res_confirm["reviewed_by"] == "operator_two"
        assert res_confirm["extracted"]["paymentTermsDays"] == 45
        assert res_confirm["extracted"]["frameDiscountPct"] == 12.5

        # 3. Test Non-existent ID raises ValueError
        fake_id = uuid.uuid4()
        with pytest.raises(ValueError, match="Agreement review queue row not found"):
            await do_review_extraction(
                engine,  # type: ignore[arg-type]
                {
                    "namespace_id": ns_id,
                    "agreement_id": fake_id,
                    "decision": "confirm",
                    "reviewed_by": "operator_two",
                },
            )
    finally:
        await app_pool.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_review_extraction_rls_scoping(pg_pool: asyncpg.Pool, make_namespace: Any) -> None:
    """Verify that tenant isolation prevents cross-tenant reads/updates on the review queue."""
    ns_a = await make_namespace()
    ns_b = await make_namespace()
    agreement_id_a = uuid.uuid4()
    source_doc_ref = "sharepoint://contracts/test_agreement_a.pdf"

    # Seed row in Namespace A using the admin pool
    async with scoped_pg_session(pg_pool, ns_a) as conn:
        await conn.execute(
            """
            INSERT INTO agreement_review_queue (
                agreement_id, namespace_id, source_doc_ref, extraction_confidence, review_status, extracted
            ) VALUES ($1, $2, $3, $4, $5, $6::jsonb)
            """,
            agreement_id_a,
            uuid.UUID(str(ns_a)),
            source_doc_ref,
            80.0,
            "needs_review_yellow",
            json.dumps({"paymentTermsDays": 30}),
        )

    # Setup restricted nce_app pool for the logic call
    app_dsn = _app_dsn()
    app_pool = await asyncpg.create_pool(app_dsn, min_size=1, max_size=2)
    engine = EngineStub(app_pool)

    try:
        # Attempt to confirm/review Namespace A's row using Namespace B context
        # This must raise ValueError since RLS prevents Namespace B connection from seeing the row.
        with pytest.raises(ValueError, match="Agreement review queue row not found"):
            await do_review_extraction(
                engine,  # type: ignore[arg-type]
                {
                    "namespace_id": ns_b,
                    "agreement_id": agreement_id_a,
                    "decision": "confirm",
                    "reviewed_by": "operator_b",
                },
            )

        # Directly select from Namespace B to ensure RLS returns no rows
        async with scoped_pg_session(app_pool, ns_b) as conn:
            row = await conn.fetchrow(
                "SELECT * FROM agreement_review_queue WHERE agreement_id = $1",
                agreement_id_a,
            )
            assert row is None

        # Directly select from Namespace A to ensure RLS returns the row
        async with scoped_pg_session(app_pool, ns_a) as conn:
            row = await conn.fetchrow(
                "SELECT * FROM agreement_review_queue WHERE agreement_id = $1",
                agreement_id_a,
            )
            assert row is not None
            assert row["agreement_id"] == agreement_id_a
    finally:
        await app_pool.close()
