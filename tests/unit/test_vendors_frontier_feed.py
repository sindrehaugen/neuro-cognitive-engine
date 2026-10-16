"""Unit tests for Vendors tiers volume calculation and frontier feed."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from nce.vertical_modules.vendors.tiers import do_get_tier_status


@pytest.mark.asyncio
async def test_get_tier_status_sums_monetary_volume_never_match_score(monkeypatch) -> None:
    """Verify do_get_tier_status sums amount/volume/value and strictly ignores score."""
    ns_id = uuid4()
    vendor_label = "VENDOR:ACME_TEST"

    # Outcome 1: real transaction amount 15,000.0
    # Outcome 2: match decision with score=95.0 but no amount
    # Outcome 3: match decision with score=88.0 and volume=5,000.0
    ledger_rows = [
        {
            "tlx_scores": json.dumps(
                {
                    "event_type": "match_decision",
                    "supplier_id": vendor_label,
                    "amount": 15000.0,
                    "score": 99.0,
                }
            )
        },
        {
            "tlx_scores": {
                "event_type": "match_decision",
                "vendor_id": vendor_label,
                "score": 95.0,  # MUST NOT be added to volume
            }
        },
        {
            "tlx_scores": {
                "event_type": "match_decision",
                "vendor_id": vendor_label,
                "volume": 5000.0,
                "score": 88.0,
            }
        },
    ]

    conn = AsyncMock()

    async def fake_fetch(query: str, *args: Any) -> list[dict[str, Any]]:
        q = query.lower()
        if "from v3_cognitive_ledger" in q:
            return ledger_rows
        return []

    async def fake_fetchrow(query: str, *args: Any) -> dict[str, Any] | None:
        q = query.lower()
        if "from kg_nodes" in q:
            return {"id": uuid4(), "label": vendor_label, "payload_ref": None}
        return None

    conn.fetch = AsyncMock(side_effect=fake_fetch)
    conn.fetchrow = AsyncMock(side_effect=fake_fetchrow)
    conn.execute = AsyncMock(return_value="INSERT 0 1")

    @asynccontextmanager
    async def fake_session(_pool: Any, _ns: Any):
        yield conn

    monkeypatch.setattr(
        "nce.vertical_modules.vendors.tiers.scoped_pg_session",
        fake_session,
    )

    engine = MagicMock()
    engine.pg_pool = MagicMock()

    status = await do_get_tier_status(
        engine,
        {
            "namespace_id": str(ns_id),
            "vendor_id": vendor_label,
        },
    )

    # 15,000 + 5,000 = 20,000 (score 95 and 99 and 88 are not added)
    assert status["ytd_volume"] == 20000.0
    assert status["current_tier"] == "Bronze"
