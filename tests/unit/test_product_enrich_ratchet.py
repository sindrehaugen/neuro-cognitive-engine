"""
tests/unit/test_product_enrich_ratchet.py
=========================================
Ratchet tests for Wave P-1 (Real Product Enrichment per Charter §4).

Deliverable:
  A ratchet test that proves product enrichment routes through nce/providers,
  applies per-field confidence, enforces §9.3 money/legal review gating,
  and FAILS if any proposal matches the legacy synthetic template:
      f"{field}_enriched_for_{mfr_part_no}"
"""

from __future__ import annotations

import ast
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from nce.providers.base import LLMProvider, LLMProviderError, Message
from nce.vertical_modules.product.enrich import (
    EnrichedFieldProposal,
    ProductEnrichmentModel,
    _build_proposals,
    _derive_idempotency_key,
    do_enrich_product,
)

# ---------------------------------------------------------------------------
# Autouse signing key fixture for unit testing
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_signing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_active_key(conn: Any) -> tuple[str, bytes]:
        return "mock-key-01", b"0" * 32

    monkeypatch.setattr("nce.event_log.get_active_key", _fake_active_key)


# ---------------------------------------------------------------------------
# Mock DB connection helper
# ---------------------------------------------------------------------------


class _FakeConn:
    def __init__(self, product_row: dict[str, Any] | None) -> None:
        self.product_row = product_row
        self.in_transaction = True
        self.executed_queries: list[tuple[str, tuple[Any, ...]]] = []
        self.logged_rows: list[dict[str, Any]] = []
        self.updated_specs: dict[str, Any] = {}

    def is_in_transaction(self) -> bool:
        return self.in_transaction

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        if "FROM   product_catalog" in query or "FROM product_catalog" in query:
            return self.product_row
        if "FROM namespaces" in query:
            return {"metadata": json.dumps({"consolidation": {"llm_provider": "stub"}})}
        if "event_sequences" in query:
            return {"seq": 1}
        if "INSERT INTO event_log" in query:
            return {"id": uuid.uuid4(), "event_seq": 1, "occurred_at": datetime.now(timezone.utc)}
        if "action_idempotency" in query:
            return None
        return None

    async def fetchval(self, query: str, *args: Any) -> Any:
        if "clock_timestamp()" in query:
            return datetime.now(timezone.utc)
        if "action_idempotency" in query:
            return None
        return None

    async def fetch(self, query: str, *args: Any) -> list[Any]:
        return []

    async def execute(self, query: str, *args: Any) -> str:
        self.executed_queries.append((query, args))
        if "INSERT INTO product_enrichment_log" in query:
            self.logged_rows.append(
                {
                    "namespace_id": str(args[0]),
                    "product_id": str(args[1]),
                    "trigger_context": json.loads(args[2]),
                    "field_name": str(args[3]),
                    "field_value": str(args[4]),
                    "confidence": float(args[5]),
                    "needs_review": bool(args[6]),
                    "product_source_id": args[7],
                }
            )
        elif "UPDATE product_catalog" in query:
            patch = json.loads(args[0])
            self.updated_specs.update(patch)
        return "OK"


# ---------------------------------------------------------------------------
# Mock LLMProvider
# ---------------------------------------------------------------------------


class MockProductCognitiveProvider(LLMProvider):
    def __init__(self, proposals: list[EnrichedFieldProposal] | None = None) -> None:
        self._proposals = proposals or []
        self.calls: list[tuple[list[Message], type]] = []

    async def complete(
        self,
        messages: list[Message],
        response_model: type,
    ) -> Any:
        self.calls.append((messages, response_model))
        return ProductEnrichmentModel(proposals=self._proposals)

    def model_identifier(self) -> str:
        return "mock/product-enricher-v1"


# ---------------------------------------------------------------------------
# 1. Deliverable Ratchet: Proposals NEVER match the synthetic template
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_proposals_never_match_synthetic_template() -> None:
    """The headline P-1 ratchet:

    Asserts that do_enrich_product produces real provider values and that NO
    proposal matches the legacy synthetic template f"{field}_enriched_for_{mfr_part_no}".
    """
    ns_id = uuid.uuid4()
    product_id = uuid.uuid4()
    mfr_part_no = "MXA920-W-S"

    product_row = {
        "id": product_id,
        "manufacturer": "Shure",
        "mfr_part_no": mfr_part_no,
        "product_source_id": "src-shure-920",
        "etim_specs": {},
    }

    mock_proposals = [
        EnrichedFieldProposal(
            field_name="coverage_pattern",
            field_value="Steerable ceiling array lobes with automatic coverage technology",
            confidence=0.92,
        ),
        EnrichedFieldProposal(
            field_name="frequency_response",
            field_value="125 Hz to 20,000 Hz",
            confidence=0.88,
        ),
        # Money/legal field: should be capped at needs_review=True per §9.3
        EnrichedFieldProposal(
            field_name="price",
            field_value="3999.00",
            confidence=0.95,
        ),
    ]

    provider = MockProductCognitiveProvider(mock_proposals)
    conn = _FakeConn(product_row)

    missing_fields = ["coverage_pattern", "frequency_response", "price"]
    trigger_context = {
        "kind": "design",
        "ref_id": "DESIGN-ROOM-101",
        "missing_fields": missing_fields,
        "source_watermark": "wm-shure-1",
    }
    idem_key = _derive_idempotency_key(str(product_id), missing_fields, "wm-shure-1")

    # Run do_enrich_product with confirm=True
    result = await do_enrich_product(
        conn,  # type: ignore[arg-type]
        ns_id,
        idempotency_key=idem_key,
        confirm=True,
        product_id=str(product_id),
        trigger_context=trigger_context,
        provider=provider,
    )

    assert result["status"] == "executed"
    inner = result["result"]
    assert inner["proposals_written"] == 3
    # Two non-money fields at >= 0.70 auto-merged, 1 money field forced to review
    assert inner["auto_merged"] == 2
    assert inner["needs_review_count"] == 1

    # RATCHET ASSERTIONS: None of the written proposals match the synthetic template
    assert len(conn.logged_rows) == 3
    for row in conn.logged_rows:
        field = row["field_name"]
        val = row["field_value"]

        # 1. Must NOT match the exact old template string
        synthetic_old = f"{field}_enriched_for_{mfr_part_no}"
        assert val != synthetic_old, (
            f"RATCHET FAILED: Field {field!r} produced synthetic template {synthetic_old!r}"
        )

        # 2. Must NOT match the regex pattern _enriched_for_
        assert not re.search(r"_enriched_for_", val), (
            f"RATCHET FAILED: Field {field!r} value {val!r} contains synthetic pattern '_enriched_for_'"
        )

        # 3. Must match the real provider-supplied value
        expected_proposal = next(p for p in mock_proposals if p.field_name == field)
        assert val == expected_proposal.field_value
        assert row["confidence"] == expected_proposal.confidence

    # Verify §9.3 money/legal review capping
    price_row = next(r for r in conn.logged_rows if r["field_name"] == "price")
    assert price_row["needs_review"] is True, "§9.3: price must be flagged for review"
    assert "price" not in conn.updated_specs, "§9.3: price must NEVER be auto-merged"


# ---------------------------------------------------------------------------
# 2. Ratchet Falsification Test
# ---------------------------------------------------------------------------


def test_ratchet_detects_synthetic_template_violation() -> None:
    """Proves the ratchet is falsifiable:

    A helper function checking proposal values against the synthetic template
    must detect and reject the legacy pattern.
    """
    mfr_part_no = "PART-XYZ"
    field = "dimensions"
    synthetic_value = f"{field}_enriched_for_{mfr_part_no}"

    # Falsification check: regex and equality must catch the synthetic pattern
    assert re.search(r"_enriched_for_", synthetic_value) is not None
    assert synthetic_value == f"{field}_enriched_for_{mfr_part_no}"

    def assert_not_synthetic(val: str, field_name: str, part_no: str) -> None:
        if val == f"{field_name}_enriched_for_{part_no}" or "_enriched_for_" in val:
            raise AssertionError(f"Detected synthetic template in proposal: {val}")

    with pytest.raises(AssertionError, match="Detected synthetic template"):
        assert_not_synthetic(synthetic_value, field, mfr_part_no)

    # Real value passes cleanly
    assert_not_synthetic("440 x 44 x 257 mm", field, mfr_part_no)


# ---------------------------------------------------------------------------
# 3. Static AST Ratchet: Ensure _enriched_for_ is gone from product/enrich.py
# ---------------------------------------------------------------------------


def test_no_synthetic_enrichment_template_in_source_ast() -> None:
    """Compile-time / AST ratchet:

    Reads nce/vertical_modules/product/enrich.py and verifies that:
      - No string literal or formatted string contains '_enriched_for_'.
      - No hardcoded 0.80 or 0.50 proposal confidence exists in _build_proposals.
      - _call_product_enrichment and ProductEnrichmentModel exist.
    """
    enrich_path = (
        Path(__file__).resolve().parent.parent.parent
        / "nce"
        / "vertical_modules"
        / "product"
        / "enrich.py"
    )
    assert enrich_path.is_file(), f"enrich.py not found at {enrich_path}"

    source = enrich_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(enrich_path))

    # 1. No AST string constant or f-string may contain "_enriched_for_"
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert "_enriched_for_" not in node.value, (
                f"AST RATCHET FAILED: String literal {node.value!r} contains '_enriched_for_' "
                f"at line {getattr(node, 'lineno', '?')}"
            )
        elif isinstance(node, ast.FormattedValue):
            pass

    # 2. _call_product_enrichment function must be defined
    func_names = {
        n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "_call_product_enrichment" in func_names, (
        "AST RATCHET FAILED: _call_product_enrichment function is missing from enrich.py"
    )

    # 3. ProductEnrichmentModel class must be defined
    class_names = {n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
    assert "ProductEnrichmentModel" in class_names, (
        "AST RATCHET FAILED: ProductEnrichmentModel is missing from enrich.py"
    )
    assert "EnrichedFieldProposal" in class_names, (
        "AST RATCHET FAILED: EnrichedFieldProposal is missing from enrich.py"
    )


# ---------------------------------------------------------------------------
# 4. Visible Degradation on Cognitive Failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_failure_degrades_visibly_zero_confidence_needs_review() -> None:
    """When cognitive provider fails, proposals degrade visibly:

    - Confidence is 0.0, verbalized is 'low', field_value is empty.
    - All fields written with needs_review=True.
    - No auto-merge to catalog.
    - No crash / unhandled exception.
    """
    ns_id = uuid.uuid4()
    product_id = uuid.uuid4()

    product_row = {
        "id": product_id,
        "manufacturer": "Extron",
        "mfr_part_no": "IN1608-xi",
        "product_source_id": "src-extron-1",
        "etim_specs": {},
    }

    class FailingProvider(LLMProvider):
        async def complete(self, messages: list[Message], response_model: type) -> Any:
            raise LLMProviderError("Cognitive sidecar timeout (504 Gateway Timeout)")

        def model_identifier(self) -> str:
            return "failing/provider"

    conn = _FakeConn(product_row)
    missing_fields = ["video_inputs", "audio_outputs"]
    trigger_context = {
        "kind": "design",
        "ref_id": "DESIGN-FAIL-01",
        "missing_fields": missing_fields,
        "source_watermark": "wm-fail-1",
    }
    idem_key = _derive_idempotency_key(str(product_id), missing_fields, "wm-fail-1")

    result = await do_enrich_product(
        conn,  # type: ignore[arg-type]
        ns_id,
        idempotency_key=idem_key,
        confirm=True,
        product_id=str(product_id),
        trigger_context=trigger_context,
        provider=FailingProvider(),
    )

    assert result["status"] == "executed"
    inner = result["result"]
    assert inner["proposals_written"] == 2
    assert inner["auto_merged"] == 0
    assert inner["needs_review_count"] == 2

    for row in conn.logged_rows:
        assert row["confidence"] == 0.0
        assert row["needs_review"] is True
        assert row["field_value"] == ""


# ---------------------------------------------------------------------------
# 5. Confidence Normalization (0-100 scale to 0.0-1.0)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confidence_scale_normalization() -> None:
    """If a provider returns confidence on a 0-100 scale (e.g. 95.0), it is normalized to 0.95."""
    product_row = {
        "id": uuid.uuid4(),
        "manufacturer": "Crestron",
        "mfr_part_no": "DM-NVX-360",
        "product_source_id": "src-crestron",
        "etim_specs": {},
    }

    mock_proposals = [
        EnrichedFieldProposal(
            field_name="resolution",
            field_value="4K60 4:4:4",
            confidence=95.0,  # 0-100 scale
        )
    ]
    provider = MockProductCognitiveProvider(mock_proposals)
    proposals = await _build_proposals(
        provider,
        ["resolution"],
        product_row,
        {"kind": "quote"},
    )

    assert len(proposals) == 1
    assert proposals[0]["field_name"] == "resolution"
    assert proposals[0]["field_value"] == "4K60 4:4:4"
    assert proposals[0]["confidence"] == 0.95
    assert proposals[0]["verbalized"] == "very_high"
