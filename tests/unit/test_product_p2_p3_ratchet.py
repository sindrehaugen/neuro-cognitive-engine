"""
tests/unit/test_product_p2_p3_ratchet.py
=========================================
Ratchet test suite for Wave P-2 (PDF Wedge) and Wave P-3 (Golden Record)
per Charter §4.

Validates:
1. Tool registrations in tool_registry and schemas in mcp_stdio_tools.
2. Confirm-first governance for product_ingest_spec (mutation=True).
3. Execution and memory creation on confirmed product_ingest_spec.
4. do_golden_record execution and structured golden record return.
5. do_enrich_product triggers do_golden_record on auto-merge.
6. accept_enrichment_proposal updates catalog, log, and triggers golden record.
7. Compile-time AST ratchet: do_ingest_spec and do_golden_record are removed
   from internal-cores.json and reachable from mcp_handlers.
"""

from __future__ import annotations

import ast
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nce.tool_registry import TOOL_REGISTRY
from nce.vertical_modules.product.enrich import (
    EnrichedFieldProposal,
    ProductEnrichmentModel,
    accept_enrichment_proposal,
    do_enrich_product,
)
from nce.vertical_modules.product.golden_record import do_golden_record
from nce.vertical_modules.product.ingestion import (
    _derive_ingest_idempotency_key,
    do_ingest_spec,
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
    def __init__(self, product_id: uuid.UUID | None = None) -> None:
        self.in_transaction = True
        self.product_id = product_id or uuid.uuid4()
        self.executed_queries: list[tuple[str, tuple[Any, ...]]] = []
        self.fetchrow_calls: list[tuple[str, tuple[Any, ...]]] = []

    def is_in_transaction(self) -> bool:
        return self.in_transaction

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        self.fetchrow_calls.append((query, args))
        if "FROM   product_catalog" in query or "FROM product_catalog" in query:
            return {
                "id": self.product_id,
                "sku": "SKU-TEST",
                "mfr_part_no": "PART-01",
                "brand": "Acme",
                "description": "Test Product",
                "category": "Lighting",
                "specs": {},
                "etim_specs": {
                    "EF000001": {
                        "value": "EV000001",
                        "confidence": 0.9,
                        "source": "test",
                        "source_trust": 0.8,
                        "as_of": "2026-09-01T00:00:00+00:00",
                    }
                },
            }
        if "FROM   product_enrichment_log" in query or "FROM product_enrichment_log" in query:
            return {
                "id": uuid.uuid4(),
                "product_id": self.product_id,
                "field_name": "color",
                "field_value": "black",
                "confidence": 0.95,
                "product_source_id": "manual",
                "needs_review": True,
            }
        if "FROM namespaces" in query:
            return {
                "metadata": json.dumps({"consolidation": {"llm_provider": "local-cognitive-model"}})
            }
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
        if "INSERT INTO v3_cognitive_ledger" in query:
            return uuid.uuid4()
        return None

    async def fetch(self, query: str, *args: Any) -> list[Any]:
        if "FROM   product_enrichment_log" in query:
            return []
        return []

    async def execute(self, query: str, *args: Any) -> str:
        self.executed_queries.append((query, args))
        return "UPDATE 1"


# ---------------------------------------------------------------------------
# 1. Tool Registration & Surface Ratchet
# ---------------------------------------------------------------------------


def test_product_ingest_spec_and_golden_record_registered() -> None:
    """Verify tool registration, mutation, cacheable, and admin flags."""
    from nce.mcp_stdio_tools import TOOLS

    # product_ingest_spec: mutation=True, cacheable=False, admin_only=False
    assert "product_ingest_spec" in TOOL_REGISTRY
    ingest_meta = TOOL_REGISTRY["product_ingest_spec"]
    assert ingest_meta.mutation is True
    assert ingest_meta.cacheable is False
    assert ingest_meta.admin_only is False

    # product_golden_record: mutation=False, cacheable=True, admin_only=False
    assert "product_golden_record" in TOOL_REGISTRY
    golden_meta = TOOL_REGISTRY["product_golden_record"]
    assert golden_meta.mutation is False
    assert golden_meta.cacheable is True
    assert golden_meta.admin_only is False

    # Verify both exist in stdio tool definitions
    stdio_names = {t.name for t in TOOLS}
    assert "product_ingest_spec" in stdio_names
    assert "product_golden_record" in stdio_names


# ---------------------------------------------------------------------------
# 2. Ingestion Confirm-First Default & Execution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_product_ingest_spec_confirm_first_default() -> None:
    """Confirm-first governance: confirm=False must return pending_approval without executing."""
    conn = _FakeConn()
    ns_id = uuid.uuid4()
    idem_key = _derive_ingest_idempotency_key("PROD-1", "Voltage: 230V\nWattage: 10W", "pdf")

    result = await do_ingest_spec(
        conn,  # type: ignore[arg-type]
        ns_id,
        idempotency_key=idem_key,
        confirm=False,
        product_id="PROD-1",
        spec_text="Voltage: 230V\nWattage: 10W",
        source="pdf",
        trigger="manual",
    )

    assert result["status"] == "pending_approval"
    assert result["action_type"] == "product_ingest_spec"
    assert result["idempotency_key"] == idem_key
    # Check that no memories queries executed
    assert not any("INSERT INTO memories" in q for q, _ in conn.executed_queries)


@pytest.mark.asyncio
async def test_product_ingest_spec_confirmed_execution() -> None:
    """Confirmed execution: confirm=True parses spec lines and writes memories."""
    conn = _FakeConn()
    ns_id = uuid.uuid4()
    idem_key = _derive_ingest_idempotency_key("PROD-1", "Voltage: 230V\nWattage: 10W", "pdf")

    result = await do_ingest_spec(
        conn,  # type: ignore[arg-type]
        ns_id,
        idempotency_key=idem_key,
        confirm=True,
        product_id="PROD-1",
        spec_text="Voltage: 230V\nWattage: 10W",
        source="pdf",
        trigger="manual",
    )

    assert result["status"] == "executed"
    assert result["idempotency_key"] == idem_key
    assert "result" in result
    assert "memory_id" in result["result"]
    # Check that memories and v3_cognitive_ledger inserts were executed
    assert any("INSERT INTO memories" in q for q, _ in conn.executed_queries)
    assert any("v3_cognitive_ledger" in q for q, _ in conn.executed_queries)


# ---------------------------------------------------------------------------
# 3. Golden Record Core & Handler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_golden_record_returns_golden_view() -> None:
    """do_golden_record builds a golden record view from catalog and memories."""
    prod_id = uuid.uuid4()
    conn = _FakeConn(product_id=prod_id)
    pool = MagicMock()

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

        assert result["product_id"] == str(prod_id)
        assert "field_winners" in result
        assert "publish_gate" in result
        assert "EF000001" in result["field_winners"]
        assert result["field_winners"]["EF000001"]["value"] == "EV000001"


# ---------------------------------------------------------------------------
# 4. Enrichment Triggers Golden Record on Auto-Merge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_enrich_product_triggers_golden_record_on_automerge() -> None:
    """When enrich auto-merges fields, do_golden_record must be triggered."""
    prod_id = uuid.uuid4()
    conn = _FakeConn(product_id=prod_id)
    ns_id = uuid.uuid4()
    pool = MagicMock()
    mock_provider = MagicMock()

    # Proposal that gets auto-merged: confidence 0.95 >= 0.85, not money/legal
    proposals_model = ProductEnrichmentModel(
        proposals=[
            EnrichedFieldProposal(
                field_name="weight_kg",
                field_value="1.2",
                confidence=0.95,
                reasoning="spec",
            )
        ]
    )

    with (
        patch(
            "nce.vertical_modules.product.enrich._call_product_enrichment",
            new=AsyncMock(return_value=proposals_model),
        ),
        patch(
            "nce.vertical_modules.product.golden_record.do_golden_record",
            new=AsyncMock(return_value={"product_id": str(prod_id), "status": "ok"}),
        ) as mock_golden,
    ):
        result = await do_enrich_product(
            conn,  # type: ignore[arg-type]
            ns_id,
            idempotency_key="test-idem-key",
            confirm=True,
            product_id=str(prod_id),
            trigger_context={"source": "test", "missing_fields": ["weight_kg"]},
            provider=mock_provider,
            engine_or_pool=pool,
        )

        assert result["status"] == "executed"
        inner = result["result"]
        assert inner["auto_merged"] == 1
        mock_golden.assert_awaited_once_with(
            pool,
            {"namespace_id": str(ns_id), "product_id": str(prod_id)},
        )


@pytest.mark.asyncio
async def test_do_enrich_product_does_not_trigger_golden_record_without_automerge() -> None:
    """When enrich produces zero auto-merges, do_golden_record is NOT triggered."""
    prod_id = uuid.uuid4()
    conn = _FakeConn(product_id=prod_id)
    ns_id = uuid.uuid4()
    pool = MagicMock()
    mock_provider = MagicMock()

    # Proposal with low confidence: needs_review=True, auto_merged=0
    proposals_model = ProductEnrichmentModel(
        proposals=[
            EnrichedFieldProposal(
                field_name="weight_kg",
                field_value="1.2",
                confidence=0.60,
                reasoning="weak",
            )
        ]
    )

    with (
        patch(
            "nce.vertical_modules.product.enrich._call_product_enrichment",
            new=AsyncMock(return_value=proposals_model),
        ),
        patch(
            "nce.vertical_modules.product.golden_record.do_golden_record",
            new=AsyncMock(return_value={"product_id": str(prod_id), "status": "ok"}),
        ) as mock_golden,
    ):
        result = await do_enrich_product(
            conn,  # type: ignore[arg-type]
            ns_id,
            idempotency_key="test-idem-key-2",
            confirm=True,
            product_id=str(prod_id),
            trigger_context={"source": "test", "missing_fields": ["weight_kg"]},
            provider=mock_provider,
            engine_or_pool=pool,
        )

        assert result["status"] == "executed"
        inner = result["result"]
        assert inner["auto_merged"] == 0
        mock_golden.assert_not_called()


# ---------------------------------------------------------------------------
# 5. Accept Enrichment Proposal Helper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_accept_enrichment_proposal_merges_and_triggers_golden() -> None:
    """accept_enrichment_proposal updates catalog, sets needs_review=false, and runs golden record."""
    prod_id = uuid.uuid4()
    conn = _FakeConn(product_id=prod_id)
    pool = MagicMock()

    with patch(
        "nce.vertical_modules.product.golden_record.do_golden_record",
        new=AsyncMock(return_value={"product_id": str(prod_id), "status": "ok"}),
    ) as mock_golden:
        res = await accept_enrichment_proposal(
            conn,  # type: ignore[arg-type]
            namespace_id=uuid.uuid4(),
            enrichment_id=uuid.uuid4(),
            engine_or_pool=pool,
        )

        assert res["status"] == "accepted"
        assert res["field_name"] == "color"
        assert res["product_id"] == str(prod_id)
        mock_golden.assert_awaited_once()


# ---------------------------------------------------------------------------
# 6. Compile-time AST Ratchet & Absence from Allowlist
# ---------------------------------------------------------------------------


def test_cores_removed_from_internal_cores_allowlist() -> None:
    """Wave P-2 and P-3 cores must be removed from internal-cores.json."""
    repo_root = Path(__file__).resolve().parents[2]
    allowlist_path = repo_root / "nce" / "config_data" / "internal-cores.json"

    with open(allowlist_path, encoding="utf-8") as f:
        data = json.load(f)

    allowlist = set(data.keys()) if isinstance(data, dict) else set(data)

    assert "nce/vertical_modules/product/ingestion.py::do_ingest_spec" not in allowlist
    assert "nce/vertical_modules/product/golden_record.py::do_golden_record" not in allowlist
    assert len(allowlist) == 70


def test_ast_reachability_from_mcp_handlers() -> None:
    """Verify MCP handlers import and call do_ingest_spec and do_golden_record."""
    repo_root = Path(__file__).resolve().parents[2]
    handlers_path = repo_root / "nce" / "vertical_modules" / "product" / "mcp_handlers.py"

    source = handlers_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(handlers_path))

    # Find functions handle_product_ingest_spec and handle_product_golden_record
    found_handlers: dict[str, ast.AsyncFunctionDef] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name in {
            "handle_product_ingest_spec",
            "handle_product_golden_record",
            "handle_product_enrich",
        }:
            found_handlers[node.name] = node

    assert "handle_product_ingest_spec" in found_handlers
    assert "handle_product_golden_record" in found_handlers
    assert "handle_product_enrich" in found_handlers

    # Check that handle_product_ingest_spec calls do_ingest_spec
    ingest_source = ast.unparse(found_handlers["handle_product_ingest_spec"])
    assert "do_ingest_spec" in ingest_source

    # Check that handle_product_golden_record calls do_golden_record
    golden_source = ast.unparse(found_handlers["handle_product_golden_record"])
    assert "do_golden_record" in golden_source

    # Check that handle_product_enrich passes engine_or_pool
    enrich_source = ast.unparse(found_handlers["handle_product_enrich"])
    assert "engine_or_pool" in enrich_source
