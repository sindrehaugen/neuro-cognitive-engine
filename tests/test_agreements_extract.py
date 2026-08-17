"""Integration tests for the Agreements OCR term extraction core (Batch 105)."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import asyncpg  # type: ignore[import-untyped]
import pytest

from nce.vertical_modules.agreements.extract import (
    ExtractedAgreementModel,
    ExtractedFieldFloat,
    ExtractedFieldInt,
    ExtractedFieldString,
    ExtractedKickbackTiers,
    KickbackTier,
    do_extract_agreement,
)


class EngineStub:
    """Stub representing the core engine context passed to vertical modules."""

    def __init__(self, pg_pool: asyncpg.Pool | None = None) -> None:
        self.pg_pool = pg_pool


@pytest.mark.integration
@pytest.mark.asyncio
async def test_extract_agreement_confidence_gates(
    pg_pool: asyncpg.Pool,
    make_namespace: Any,
) -> None:
    """Verify that extraction confidence is gated properly and money/legal fields are never auto_green."""
    ns_id = await make_namespace()
    engine = EngineStub(pg_pool)

    # 1. Standard Case: mock LLM return with varying confidences
    mock_extracted = ExtractedAgreementModel(
        supplierId=ExtractedFieldString(
            value="VENDOR:ACME", confidence=95.0
        ),  # Non-money >= 90 -> auto_green
        customerId=ExtractedFieldString(
            value="CUSTOMER:STEPS", confidence=85.0
        ),  # Non-money >= 70 -> needs_review_yellow
        validFrom=ExtractedFieldString(
            value="2026-06-01", confidence=60.0
        ),  # Non-money < 70 -> manual_red
        validTo=ExtractedFieldString(
            value="2027-06-01", confidence=92.0
        ),  # Non-money >= 90 -> auto_green
        paymentTermsDays=ExtractedFieldInt(
            value=30, confidence=100.0
        ),  # Money/legal (forced to needs_review_yellow)
        frameDiscountPct=ExtractedFieldFloat(
            value=10.0, confidence=95.0
        ),  # Money/legal (forced to needs_review_yellow)
        volumeCommitment=ExtractedFieldFloat(
            value=50000.0, confidence=50.0
        ),  # Money/legal < 70 -> manual_red
        kickbackTiers=ExtractedKickbackTiers(
            value=[KickbackTier(threshold=100000.0, pct=2.5)],
            confidence=99.0,  # Money/legal (forced to needs_review_yellow)
        ),
    )

    params = {
        "namespace_id": ns_id,
        "source_doc_ref": "sharepoint://contracts/supplier_agreement_123.pdf",
    }

    with patch(
        "nce.vertical_modules.agreements.extract._call_ocr_extraction",
        return_value=mock_extracted,
    ) as mock_ocr:
        res = await do_extract_agreement(engine, params)  # type: ignore[arg-type]
        mock_ocr.assert_called_once()

    # Verify non-money/legal autogreen/review/manual mapping
    assert res["supplierId"]["value"] == "VENDOR:ACME"
    assert res["supplierId"]["reviewStatus"] == "auto_green"

    assert res["customerId"]["value"] == "CUSTOMER:STEPS"
    assert res["customerId"]["reviewStatus"] == "needs_review_yellow"

    assert res["validFrom"]["value"] == "2026-06-01"
    assert res["validFrom"]["reviewStatus"] == "manual_red"

    assert res["validTo"]["value"] == "2027-06-01"
    assert res["validTo"]["reviewStatus"] == "auto_green"

    # Verify money/legal fields are never auto-promoted to auto_green
    assert res["paymentTermsDays"]["value"] == 30
    assert res["paymentTermsDays"]["reviewStatus"] == "needs_review_yellow"  # Capped

    assert res["frameDiscountPct"]["value"] == 10.0
    assert res["frameDiscountPct"]["reviewStatus"] == "needs_review_yellow"  # Capped

    assert res["volumeCommitment"]["value"] == 50000.0
    assert (
        res["volumeCommitment"]["reviewStatus"] == "manual_red"
    )  # Stays manual_red since conf < 70

    assert len(res["kickbackTiers"]["value"]) == 1
    assert res["kickbackTiers"]["value"][0]["threshold"] == 100000.0
    assert res["kickbackTiers"]["value"][0]["pct"] == 2.5
    assert res["kickbackTiers"]["reviewStatus"] == "needs_review_yellow"  # Capped


@pytest.mark.integration
@pytest.mark.asyncio
async def test_extract_agreement_missing_doc_ref(
    pg_pool: asyncpg.Pool,
    make_namespace: Any,
) -> None:
    """Verify do_extract_agreement raises ValueError when source_doc_ref is missing."""
    ns_id = await make_namespace()
    engine = EngineStub(pg_pool)

    params = {
        "namespace_id": ns_id,
    }

    with pytest.raises(ValueError, match="source_doc_ref is required"):
        await do_extract_agreement(engine, params)  # type: ignore[arg-type]
