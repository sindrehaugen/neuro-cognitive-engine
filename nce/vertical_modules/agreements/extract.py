"""
nce/vertical_modules/agreements/extract.py
===========================================
Agreement OCR term extraction core — Module 3.Wave 1.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from nce.config import cfg
from nce.db_utils import scoped_pg_session
from nce.mcp_args import require_namespace_id
from nce.providers.base import Message
from nce.providers.factory import get_provider

if TYPE_CHECKING:
    from nce.orchestrator import NCEEngine
    from nce.providers.base import LLMProvider

log = logging.getLogger("nce.vertical_modules.agreements.extract")


# ---------------------------------------------------------------------------
# Pydantic Extraction Models
# ---------------------------------------------------------------------------


class KickbackTier(BaseModel):
    threshold: float = Field(description="The spend threshold value")
    pct: float = Field(description="The rebate percentage for this tier")


class ExtractedFieldString(BaseModel):
    value: str | None = Field(default=None, description="The extracted text value")
    confidence: float = Field(default=0.0, description="Confidence score from 0.0 to 100.0")


class ExtractedFieldInt(BaseModel):
    value: int | None = Field(default=None, description="The extracted integer value")
    confidence: float = Field(default=0.0, description="Confidence score from 0.0 to 100.0")


class ExtractedFieldFloat(BaseModel):
    value: float | None = Field(default=None, description="The extracted float value")
    confidence: float = Field(default=0.0, description="Confidence score from 0.0 to 100.0")


class ExtractedKickbackTiers(BaseModel):
    value: list[KickbackTier] | None = Field(
        default=None, description="The extracted list of kickback tiers"
    )
    confidence: float = Field(default=0.0, description="Confidence score from 0.0 to 100.0")


class ExtractedAgreementModel(BaseModel):
    supplierId: ExtractedFieldString = Field(default_factory=ExtractedFieldString)
    customerId: ExtractedFieldString = Field(default_factory=ExtractedFieldString)
    validFrom: ExtractedFieldString = Field(default_factory=ExtractedFieldString)
    validTo: ExtractedFieldString = Field(default_factory=ExtractedFieldString)
    paymentTermsDays: ExtractedFieldInt = Field(default_factory=ExtractedFieldInt)
    frameDiscountPct: ExtractedFieldFloat = Field(default_factory=ExtractedFieldFloat)
    volumeCommitment: ExtractedFieldFloat = Field(default_factory=ExtractedFieldFloat)
    kickbackTiers: ExtractedKickbackTiers = Field(default_factory=ExtractedKickbackTiers)


# ---------------------------------------------------------------------------
# Confidence Gate Mapping
# ---------------------------------------------------------------------------


def _map_confidence_to_status(
    field_name: str,
    confidence: float,
    autogreen_thresh: float,
    review_thresh: float,
) -> str:
    """Map field-level confidence score to a reviewStatus enum.

    Statuses:
      - auto_green: Approved automatically (no review needed).
      - needs_review_yellow: Demoted/moderate confidence, needs review.
      - manual_red: Low confidence, requires operator input.

    §9.3 Guard:
      - Money/legal fields (kickbackTiers, frameDiscountPct, paymentTermsDays,
        volumeCommitment) are ALWAYS review-flagged, meaning they never resolve
        to auto_green. If their confidence exceeds the autogreen threshold,
        they are capped at needs_review_yellow.
    """
    is_money_legal = field_name in {
        "kickbackTiers",
        "frameDiscountPct",
        "paymentTermsDays",
        "volumeCommitment",
    }

    if confidence >= autogreen_thresh:
        if is_money_legal:
            return "needs_review_yellow"
        return "auto_green"
    elif confidence >= review_thresh:
        return "needs_review_yellow"
    else:
        return "manual_red"


# ---------------------------------------------------------------------------
# Isolated OCR LLM Caller
# ---------------------------------------------------------------------------


async def _call_ocr_extraction(
    provider: LLMProvider,
    source_doc_ref: str,
) -> ExtractedAgreementModel:
    """Invoke the cognitive provider to parse the document referenced by source_doc_ref."""
    prompt = (
        f"Perform OCR on the agreement document referenced by '{source_doc_ref}' and "
        "extract its core commercial terms. For each field, provide your best estimation of "
        "the value along with a self-reported confidence score between 0.0 and 100.0."
    )
    messages = [
        Message.system(
            "You are an expert contract analysis system. You extract key values from legal agreements "
            "and report your confidence for each field."
        ),
        Message.user(prompt),
    ]
    # Sends messages and parses them directly into ExtractedAgreementModel.
    return await provider.complete(messages, ExtractedAgreementModel)


# ---------------------------------------------------------------------------
# Core Entry Point
# ---------------------------------------------------------------------------


async def do_extract_agreement(
    engine: NCEEngine,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Extract commercial terms from an agreement document.

    Accepts source_doc_ref, resolves the namespace's LLM provider,
    runs OCR term extraction, and evaluates each field's extraction confidence
    against the autogreen and review thresholds.

    Parameters
    ----------
    engine:
        NCEEngine instance providing connection pools.
    params:
        dict containing:
          - namespace_id: str/UUID (required)
          - source_doc_ref: str (required)

    Returns
    -------
    dict:
        Each field mapped to its extracted value, confidence, and status.
    """
    namespace_id = require_namespace_id(params)
    source_doc_ref = params.get("source_doc_ref")
    if not source_doc_ref:
        raise ValueError("source_doc_ref is required")

    # 1. Resolve namespace metadata from the DB to choose the proper LLM provider
    async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
        row = await conn.fetchrow(
            "SELECT metadata FROM namespaces WHERE id = $1::uuid",
            uuid.UUID(str(namespace_id)),
        )
        metadata = json.loads(row["metadata"]) if row and row["metadata"] else {}

    provider = get_provider(metadata)

    # 2. Call the isolated OCR extraction function
    extracted = await _call_ocr_extraction(provider, source_doc_ref)

    # 3. Apply the confidence gate to each field
    autogreen_thresh = float(cfg.NCE_AGREEMENTS_OCR_AUTOGREEN_THRESHOLD)
    review_thresh = float(cfg.NCE_AGREEMENTS_OCR_REVIEW_THRESHOLD)

    result: dict[str, Any] = {}
    fields = [
        "supplierId",
        "customerId",
        "validFrom",
        "validTo",
        "paymentTermsDays",
        "frameDiscountPct",
        "volumeCommitment",
        "kickbackTiers",
    ]

    for field_name in fields:
        field_obj = getattr(extracted, field_name)
        val = field_obj.value

        # Serialize kickbackTiers list if populated
        if field_name == "kickbackTiers" and val is not None:
            val = [t.model_dump() for t in val]

        conf = field_obj.confidence
        status = _map_confidence_to_status(
            field_name,
            conf,
            autogreen_thresh,
            review_thresh,
        )

        result[field_name] = {
            "value": val,
            "extractionConfidence": conf,
            "reviewStatus": status,
        }

    return result
