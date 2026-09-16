"""
tests/unit/test_product_manufacturer_sources.py
================================================
Unit tests for Wave P-5: manufacturer-sources.json trust weights in Product Engine.

Validates:
1. Config-as-IP: manufacturer-sources.json schema, validity, and trust ordering invariants:
   manual_override (1.0) > manufacturer_verified (0.95) > distributor (0.80) > ai_derived (0.60) > scraped (0.40).
2. resolve_source_trust resolution logic: exact matches, normalization, heuristic fallbacks,
   manufacturer adapter overrides, and default fallbacks.
3. _build_field_candidates candidate parsing: single dicts, candidates lists, explicit trust overrides,
   and enrichment log candidates.
4. do_golden_record multi-source survivorship: manufacturer-verified beats distributor,
   distributor beats AI-derived, AI-derived beats scraped.
5. Provenance audit tracking and __init__.py re-exports.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nce.vertical_modules.product import (
    load_manufacturer_sources,
    resolve_source_trust,
)
from nce.vertical_modules.product.golden_record import (
    _build_field_candidates,
    do_golden_record,
)

# ---------------------------------------------------------------------------
# 1. Config-as-IP: manufacturer-sources.json Schema & Ordering
# ---------------------------------------------------------------------------


def test_manufacturer_sources_config_exists_and_loads() -> None:
    """manufacturer-sources.json exists and load_manufacturer_sources parses it."""
    cfg = load_manufacturer_sources()
    assert isinstance(cfg, dict)
    assert "source_trust_weights" in cfg
    assert "manufacturer_adapters" in cfg

    weights = cfg["source_trust_weights"]
    assert isinstance(weights, dict)

    # Core hierarchy from 02-product-engine.md A2:
    # manufacturer-verified > distributor > AI-derived > scraped
    assert weights["manual_override"] >= 1.0
    assert weights["manufacturer_verified"] > weights["distributor"]
    assert weights["distributor"] > weights["ai_derived"]
    assert weights["ai_derived"] > weights["scraped"]

    # All weights must be floats in [0.0, 1.0]
    for src, wt in weights.items():
        assert isinstance(wt, (int, float)), f"Weight for {src} must be numeric"
        assert 0.0 <= wt <= 1.0, f"Weight for {src} ({wt}) must be in [0.0, 1.0]"


def test_manufacturer_adapters_configuration() -> None:
    """Verified manufacturers have adapter definitions and high trust weights."""
    cfg = load_manufacturer_sources()
    adapters = cfg["manufacturer_adapters"]

    expected_manufacturers = [
        "cisco",
        "crestron",
        "shure",
        "biamp",
        "qsc",
        "microsoft",
        "neat",
        "poly",
        "huddly",
    ]
    for mfr in expected_manufacturers:
        assert mfr in adapters, f"Missing adapter definition for {mfr}"
        entry = adapters[mfr]
        assert "adapter" in entry
        assert "trust_weight" in entry
        assert entry["trust_weight"] >= 0.90


# ---------------------------------------------------------------------------
# 2. Source Trust Resolution (resolve_source_trust)
# ---------------------------------------------------------------------------


def test_resolve_source_trust_exact_and_normalized() -> None:
    """resolve_source_trust correctly resolves exact and normalized source keys."""
    assert resolve_source_trust("manufacturer_verified") == 0.95
    assert resolve_source_trust("MANUFACTURER_VERIFIED") == 0.95
    assert resolve_source_trust("manufacturer-verified") == 0.95

    assert resolve_source_trust("distributor") == 0.80
    assert resolve_source_trust("nettailer") == 0.80
    assert resolve_source_trust("distributor-nettailer") == 0.80
    assert resolve_source_trust("supplier_catalog") == 0.80

    assert resolve_source_trust("ai_derived") == 0.60
    assert resolve_source_trust("ai-enrichment") == 0.60

    assert resolve_source_trust("datasheet_ocr") == 0.50
    assert resolve_source_trust("product_spec") == 0.50

    assert resolve_source_trust("scraped") == 0.40
    assert resolve_source_trust("manual_override") == 1.0
    assert resolve_source_trust("human_accepted") == 1.0


def test_resolve_source_trust_heuristics() -> None:
    """resolve_source_trust classifies unknown variants via prefix/keyword heuristics."""
    # Prefix / substring matching
    assert resolve_source_trust("custom_manufacturer_feed") == 0.95
    assert resolve_source_trust("nordic_distributor_api") == 0.80
    assert resolve_source_trust("claude_ai_extraction") == 0.60
    assert resolve_source_trust("datasheet_pdf_scan") == 0.50
    assert resolve_source_trust("partner_web_scraper") == 0.40
    assert resolve_source_trust("unrecognized_third_party") == 0.50
    assert resolve_source_trust(None) == 0.50
    assert resolve_source_trust("") == 0.50


def test_resolve_source_trust_manufacturer_override() -> None:
    """resolve_source_trust applies manufacturer adapter overrides when manufacturer matches."""
    # Manufacturer adapter match
    assert resolve_source_trust("manufacturer_api", manufacturer="Cisco") == 0.95
    assert resolve_source_trust(None, manufacturer="Crestron") == 0.95
    assert resolve_source_trust("api", manufacturer="Shure") == 0.95

    # Distributor source for a manufacturer keeps distributor weight
    assert resolve_source_trust("distributor", manufacturer="Cisco") == 0.80


# ---------------------------------------------------------------------------
# 3. Candidate Extraction & Trust Assignment (_build_field_candidates)
# ---------------------------------------------------------------------------


def test_build_field_candidates_resolves_trust_from_config() -> None:
    """_build_field_candidates infers source_trust when omitted in input."""
    etim_specs = {
        "mfr_part_no": {
            "value": "CP-8841-K9",
            "source": "manufacturer_api",
        },
        "description": {
            "value": "Cisco IP Phone",
            "source": "nettailer",
        },
        "weight_kg": {
            "value": "1.2",
            "source": "ai_enrichment",
        },
    }

    candidates = _build_field_candidates(etim_specs, manufacturer="Cisco")

    assert candidates["mfr_part_no"][0]["source_trust"] == 0.95
    assert candidates["description"][0]["source_trust"] == 0.80
    assert candidates["weight_kg"][0]["source_trust"] == 0.60


def test_build_field_candidates_preserves_explicit_source_trust() -> None:
    """_build_field_candidates honors explicit source_trust override."""
    etim_specs = {
        "voltage": {
            "value": "230V",
            "source": "ai_enrichment",
            "source_trust": 0.99,  # Explicit override
        }
    }
    candidates = _build_field_candidates(etim_specs)
    assert candidates["voltage"][0]["source_trust"] == 0.99


def test_build_field_candidates_supports_candidate_lists() -> None:
    """_build_field_candidates parses candidate lists under candidates key and direct lists."""
    etim_specs = {
        "short_description": {
            "candidates": [
                {"value": "Scraped desc", "source": "scraped"},
                {"value": "Distributor desc", "source": "nettailer"},
                {"value": "Mfr desc", "source": "manufacturer_api"},
            ]
        },
        "color": [
            {"value": "Dark Gray", "source": "nettailer"},
            {"value": "Black", "source": "ai_enrichment"},
        ],
    }

    candidates = _build_field_candidates(etim_specs)
    assert len(candidates["short_description"]) == 3
    trusts = [c["source_trust"] for c in candidates["short_description"]]
    assert trusts == [0.40, 0.80, 0.95]

    assert len(candidates["color"]) == 2
    assert candidates["color"][0]["source_trust"] == 0.80
    assert candidates["color"][1]["source_trust"] == 0.60


# ---------------------------------------------------------------------------
# 4. Survivorship in do_golden_record (Pure Mock Execution)
# ---------------------------------------------------------------------------


class _FakeConn:
    def __init__(
        self,
        product_id: uuid.UUID,
        etim_specs: dict[str, Any],
        manufacturer: str = "Cisco",
        enrichment_rows: list[dict[str, Any]] | None = None,
    ) -> None:
        self.product_id = product_id
        self.etim_specs = etim_specs
        self.manufacturer = manufacturer
        self.enrichment_rows = enrichment_rows or []

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        if "FROM   product_catalog" in query or "FROM product_catalog" in query:
            return {
                "id": self.product_id,
                "manufacturer": self.manufacturer,
                "etim_specs": self.etim_specs,
            }
        return None

    async def fetchval(self, query: str, *args: Any) -> Any:
        if "SELECT 1 FROM product_catalog" in query:
            return 1
        return None

    async def fetch(self, query: str, *args: Any) -> list[Any]:
        if "needs_review = true" in query:
            return []  # No unreviewed money fields
        if "needs_review = false" in query:
            return self.enrichment_rows
        return []


@pytest.mark.asyncio
async def test_do_golden_record_multi_source_survivorship() -> None:
    """do_golden_record resolves multi-source candidate conflicts via trust weights."""
    prod_id = uuid.uuid4()
    pool = MagicMock()

    # etim_specs has competing candidates for short_description
    etim_specs = {
        "short_description": {
            "candidates": [
                {"value": "Scraped title", "source": "scraped"},
                {"value": "Netset title", "source": "nettailer"},
                {"value": "Official Cisco title", "source": "manufacturer_api"},
            ]
        },
        "lifecycle_status": {
            "value": "active",
            "source": "nettailer",
        },
        "category": {
            "value": "Collaboration Endpoints",
            "source": "manufacturer_verified",
        },
        "mfr_part_no": {
            "value": "CP-8841-K9",
            "source": "manufacturer_verified",
        },
        "manufacturer": {
            "value": "Cisco",
            "source": "manufacturer_verified",
        },
    }

    conn = _FakeConn(prod_id, etim_specs=etim_specs, manufacturer="Cisco")

    @asynccontextmanager
    async def _fake_session(
        p: Any, ns: Any, statement_timeout_ms: int = 15000
    ) -> AsyncIterator[_FakeConn]:
        yield conn

    with (
        patch("nce.vertical_modules.product.golden_record.scoped_pg_session", _fake_session),
        patch(
            "nce.vertical_modules.product.golden_record.append_survivorship_provenance",
            new=AsyncMock(return_value=uuid.uuid4()),
        ) as mock_provenance,
    ):
        result = await do_golden_record(
            pool,
            {"namespace_id": str(uuid.uuid4()), "product_id": str(prod_id)},
        )

        winners = result["field_winners"]
        assert "short_description" in winners
        desc_winner = winners["short_description"]

        # manufacturer_api (0.95) must beat nettailer (0.80) and scraped (0.40)
        assert desc_winner["value"] == "Official Cisco title"
        assert desc_winner["source"] == "manufacturer_api"
        assert desc_winner["reason"] == "source_trust"

        # Provenance appended for each field
        assert mock_provenance.call_count >= 5


@pytest.mark.asyncio
async def test_do_golden_record_enrichment_candidates_participate() -> None:
    """Reviewed enrichment candidates participate in survivorship against catalog specs."""
    prod_id = uuid.uuid4()
    pool = MagicMock()

    # Catalog has scraped spec, enrichment log has accepted manufacturer_api proposal
    etim_specs = {
        "weight_kg": {
            "value": "1.0",
            "source": "scraped",
        },
        "short_description": {
            "value": "Widget",
            "source": "nettailer",
        },
        "lifecycle_status": {"value": "active", "source": "nettailer"},
        "category": {"value": "Audio", "source": "nettailer"},
        "mfr_part_no": {"value": "WDG-1", "source": "nettailer"},
        "manufacturer": {"value": "Shure", "source": "nettailer"},
    }

    # Enrichment log row: higher trust source for weight_kg
    enrichment_rows = [
        {
            "field_name": "weight_kg",
            "field_value": "1.15",
            "confidence": 0.95,
            "product_source_id": "manufacturer_api",
            "created_at": None,
        }
    ]

    conn = _FakeConn(
        prod_id, etim_specs=etim_specs, manufacturer="Shure", enrichment_rows=enrichment_rows
    )

    @asynccontextmanager
    async def _fake_session(
        p: Any, ns: Any, statement_timeout_ms: int = 15000
    ) -> AsyncIterator[_FakeConn]:
        yield conn

    with (
        patch("nce.vertical_modules.product.golden_record.scoped_pg_session", _fake_session),
        patch(
            "nce.vertical_modules.product.golden_record.append_survivorship_provenance",
            new=AsyncMock(return_value=uuid.uuid4()),
        ),
    ):
        result = await do_golden_record(
            pool,
            {"namespace_id": str(uuid.uuid4()), "product_id": str(prod_id)},
        )

        winners = result["field_winners"]
        weight_winner = winners["weight_kg"]

        # manufacturer_api from enrichment log beats scraped in etim_specs
        assert weight_winner["value"] == "1.15"
        assert weight_winner["source"] == "manufacturer_api"
        assert weight_winner["reason"] == "source_trust"
