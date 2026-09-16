"""tests/unit/test_sales_ai_surface.py
======================================
Unit tests for Wave S-5: Sales Lead Score & Quote Draft Grounded Advisors.

Covers:
  1. Core ``do_score_lead``:
     - Missing namespace validation.
     - Fallback query_text resolution (lead_name, subject, "lead").
     - Candidate scoring from similar historical won/lost deals.
     - Neutral default (0.5 score/confidence) when no candidates match threshold.
     - Metadata parsing (dict and JSON string).
  2. Core ``do_draft_quote``:
     - Missing namespace validation.
     - Opportunity ID / description resolution.
     - Line extraction, deduplication, margin calculation.
     - propose_only=True and validated=False contract guarantees.
  3. MCP Handlers:
     - ``handle_sales_score_lead``: missing namespace error, valid execution.
     - ``handle_sales_draft_quote``: missing namespace error, valid execution.
  4. Admin REST Handlers:
     - ``api_admin_sales_lead_score``: 503, 422 validation, 200 OK.
     - ``api_admin_sales_quote_draft``: 503, 422 validation, 200 OK.
  5. Surface Invariants:
     - Tool registration in ``TOOL_REGISTRY`` (cacheable, not admin_only, not mutation).
     - Tool schemas present in ``mcp_stdio_tools.TOOLS``.
     - REST routes mounted in ``build_admin_routes()``.
     - Pruned from ``internal-cores.json``.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from nce.admin_app import build_admin_routes
from nce.admin_handlers import sales as sales_admin_handlers
from nce.admin_handlers._shared import admin_state
from nce.mcp_errors import McpError
from nce.mcp_stdio_tools import TOOLS
from nce.tool_registry import TOOL_REGISTRY
from nce.vertical_modules.sales.ai import do_draft_quote, do_score_lead
from nce.vertical_modules.sales.mcp_handlers import (
    handle_sales_draft_quote,
    handle_sales_score_lead,
)

_NAMESPACE_ID = "00000000-0000-4000-8000-000000000001"


def _make_engine() -> MagicMock:
    engine = MagicMock()
    engine.pg_pool = MagicMock()
    return engine


# ---------------------------------------------------------------------------
# 1. Core do_score_lead
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_score_lead_missing_namespace() -> None:
    engine = _make_engine()
    with pytest.raises(ValueError, match="namespace_id is required"):
        await do_score_lead(engine, {})


@pytest.mark.asyncio
async def test_do_score_lead_empty_memory_returns_neutral() -> None:
    engine = _make_engine()
    mock_conn = AsyncMock()

    with (
        patch("nce.vertical_modules.sales.ai.scoped_pg_session") as mock_scoped,
        patch("nce.vertical_modules.sales.ai.recall_similar_deals", return_value=[]) as mock_recall,
    ):
        mock_scoped.return_value.__aenter__.return_value = mock_conn
        res = await do_score_lead(engine, {"namespace_id": _NAMESPACE_ID, "lead_name": "Acme Corp"})

    assert res["ok"] is True
    assert res["score"] == 0.5
    assert res["confidence"] == 0.5
    assert res["propose_only"] is True
    assert "neutral score" in res["reasons"][0]
    mock_recall.assert_awaited_once()


@pytest.mark.asyncio
async def test_do_score_lead_with_candidates() -> None:
    engine = _make_engine()
    mock_conn = AsyncMock()

    candidates = [
        {"similarity": 0.85, "metadata": json.dumps({"outcome": "won"})},
        {"similarity": 0.75, "metadata": {"outcome": "lost"}},
        {"similarity": 0.90, "metadata": json.dumps({"status": "won"})},
        {"similarity": 0.40, "metadata": {"outcome": "won"}},  # Below 0.6 threshold -> skipped
    ]

    with (
        patch("nce.vertical_modules.sales.ai.scoped_pg_session") as mock_scoped,
        patch("nce.vertical_modules.sales.ai.recall_similar_deals", return_value=candidates),
    ):
        mock_scoped.return_value.__aenter__.return_value = mock_conn
        res = await do_score_lead(
            engine,
            {
                "namespace_id": UUID(_NAMESPACE_ID),
                "query_text": "cloud migration enterprise deal",
            },
        )

    assert res["ok"] is True
    # 3 candidates above 0.6 similarity: 2 won, 1 lost -> score = 2/3
    assert abs(res["score"] - (2 / 3)) < 1e-4
    # confidence = average similarity of the 3 candidates = (0.85 + 0.75 + 0.90) / 3 = 2.50 / 3
    assert abs(res["confidence"] - (2.50 / 3)) < 1e-4
    assert res["propose_only"] is True
    assert len(res["reasons"]) == 2
    assert "Found 3 similar historical deals." in res["reasons"][0]


# ---------------------------------------------------------------------------
# 2. Core do_draft_quote
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_draft_quote_missing_namespace() -> None:
    engine = _make_engine()
    with pytest.raises(ValueError, match="namespace_id is required"):
        await do_draft_quote(engine, {})


@pytest.mark.asyncio
async def test_do_draft_quote_empty_memory_returns_default() -> None:
    engine = _make_engine()
    mock_conn = AsyncMock()

    with (
        patch("nce.vertical_modules.sales.ai.scoped_pg_session") as mock_scoped,
        patch("nce.vertical_modules.sales.ai.recall_similar_deals", return_value=[]) as mock_recall,
    ):
        mock_scoped.return_value.__aenter__.return_value = mock_conn
        res = await do_draft_quote(
            engine,
            {"namespace_id": _NAMESPACE_ID, "opportunity_id": "opp-123"},
        )

    assert res["ok"] is True
    assert res["proposed_lines"] == []
    assert res["suggested_margin_pct"] == 0.35
    assert res["propose_only"] is True
    assert res["validated"] is False
    mock_recall.assert_awaited_once()


@pytest.mark.asyncio
async def test_do_draft_quote_with_candidates() -> None:
    engine = _make_engine()
    mock_conn = AsyncMock()

    candidates = [
        {
            "similarity": 0.88,
            "metadata": json.dumps(
                {
                    "lines": [
                        {"product_ref": "PROD-A", "qty": 2, "unit_price": 500.0},
                        {"product_ref": "PROD-B", "qty": 1, "unit_price": 250.0},
                    ],
                    "margin_pct": 0.40,
                }
            ),
        },
        {
            "similarity": 0.82,
            "metadata": {
                "lines": [
                    {
                        "product_ref": "PROD-A",
                        "qty": 1,
                        "unit_price": 480.0,
                    },  # duplicate product_ref -> deduplicated
                    {"product_ref": "PROD-C", "qty": 5, "unit_price": 100.0},
                ],
                "signed_margin_pct": 0.30,
            },
        },
        {
            "similarity": 0.50,  # Below 0.6 threshold -> skipped
            "metadata": {
                "lines": [{"product_ref": "PROD-X", "qty": 1}],
                "margin_pct": 0.50,
            },
        },
    ]

    with (
        patch("nce.vertical_modules.sales.ai.scoped_pg_session") as mock_scoped,
        patch("nce.vertical_modules.sales.ai.recall_similar_deals", return_value=candidates),
    ):
        mock_scoped.return_value.__aenter__.return_value = mock_conn
        res = await do_draft_quote(
            engine,
            {
                "namespace_id": _NAMESPACE_ID,
                "opportunity_id": "opp-999",
                "description": "AV equipment installation",
            },
        )

    assert res["ok"] is True
    assert len(res["proposed_lines"]) == 3
    refs = [line["product_ref"] for line in res["proposed_lines"]]
    assert refs == ["PROD-A", "PROD-B", "PROD-C"]
    # Margin average = (0.40 + 0.30) / 2 = 0.35
    assert abs(res["suggested_margin_pct"] - 0.35) < 1e-4
    assert res["propose_only"] is True
    assert res["validated"] is False


# ---------------------------------------------------------------------------
# 3. MCP Handlers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_sales_score_lead_missing_namespace() -> None:
    engine = _make_engine()
    with pytest.raises(McpError):
        await handle_sales_score_lead(engine, {})


@pytest.mark.asyncio
async def test_handle_sales_score_lead_success() -> None:
    engine = _make_engine()
    expected_result = {
        "ok": True,
        "score": 0.8,
        "confidence": 0.85,
        "propose_only": True,
        "reasons": ["High historical win rate."],
    }

    with patch(
        "nce.vertical_modules.sales.mcp_handlers.do_score_lead",
        new=AsyncMock(return_value=expected_result),
    ) as mock_core:
        raw = await handle_sales_score_lead(
            engine,
            {
                "namespace_id": _NAMESPACE_ID,
                "lead_name": "MegaCorp",
                "query_text": "Enterprise license deal",
            },
        )

    assert json.loads(raw) == expected_result
    mock_core.assert_awaited_once_with(
        engine,
        {
            "namespace_id": _NAMESPACE_ID,
            "lead_name": "MegaCorp",
            "query_text": "Enterprise license deal",
        },
    )


@pytest.mark.asyncio
async def test_handle_sales_draft_quote_missing_namespace() -> None:
    engine = _make_engine()
    with pytest.raises(McpError):
        await handle_sales_draft_quote(engine, {})


@pytest.mark.asyncio
async def test_handle_sales_draft_quote_success() -> None:
    engine = _make_engine()
    expected_result = {
        "ok": True,
        "proposed_lines": [{"product_ref": "SKU-1", "qty": 1, "suggested_unit_price": 100.0}],
        "suggested_margin_pct": 0.32,
        "propose_only": True,
        "validated": False,
    }

    with patch(
        "nce.vertical_modules.sales.mcp_handlers.do_draft_quote",
        new=AsyncMock(return_value=expected_result),
    ) as mock_core:
        raw = await handle_sales_draft_quote(
            engine,
            {
                "namespace_id": _NAMESPACE_ID,
                "opportunity_id": "opp-456",
                "description": "Speaker rig installation",
            },
        )

    assert json.loads(raw) == expected_result
    mock_core.assert_awaited_once_with(
        engine,
        {
            "namespace_id": _NAMESPACE_ID,
            "opportunity_id": "opp-456",
            "description": "Speaker rig installation",
        },
    )


# ---------------------------------------------------------------------------
# 4. Admin REST Handlers
# ---------------------------------------------------------------------------


class _FakeRequest:
    def __init__(
        self, json_body: dict[str, Any] | None = None, query_params: dict[str, str] | None = None
    ):
        self._json_body = json_body or {}
        self.query_params = query_params or {}

    async def json(self) -> dict[str, Any]:
        return self._json_body


@pytest.mark.asyncio
async def test_api_admin_sales_lead_score_engine_not_connected() -> None:
    with patch.object(admin_state, "engine", None):
        resp = await sales_admin_handlers.api_admin_sales_lead_score(_FakeRequest())
        assert resp.status_code == 503
        assert json.loads(resp.body.decode()) == {"error": "Engine not connected"}


@pytest.mark.asyncio
async def test_api_admin_sales_lead_score_missing_namespace() -> None:
    with patch.object(admin_state, "engine", _make_engine()):
        resp = await sales_admin_handlers.api_admin_sales_lead_score(_FakeRequest())
        assert resp.status_code == 422
        assert "Missing required field: namespace_id" in json.loads(resp.body.decode())["error"]


@pytest.mark.asyncio
async def test_api_admin_sales_lead_score_invalid_namespace() -> None:
    with patch.object(admin_state, "engine", _make_engine()):
        resp = await sales_admin_handlers.api_admin_sales_lead_score(
            _FakeRequest(json_body={"namespace_id": "not-a-uuid"})
        )
        assert resp.status_code == 422
        assert "Invalid namespace_id" in json.loads(resp.body.decode())["error"]


@pytest.mark.asyncio
async def test_api_admin_sales_lead_score_success() -> None:
    expected = {
        "ok": True,
        "score": 0.75,
        "confidence": 0.8,
        "propose_only": True,
        "reasons": ["Historical match."],
    }
    with (
        patch.object(admin_state, "engine", _make_engine()),
        patch("nce.admin_handlers.sales.do_score_lead", new=AsyncMock(return_value=expected)),
    ):
        resp = await sales_admin_handlers.api_admin_sales_lead_score(
            _FakeRequest(
                json_body={
                    "namespace_id": _NAMESPACE_ID,
                    "lead_name": "Test Lead",
                    "query_text": "AV consult",
                }
            )
        )
        assert resp.status_code == 200
        assert json.loads(resp.body.decode()) == expected


@pytest.mark.asyncio
async def test_api_admin_sales_quote_draft_engine_not_connected() -> None:
    with patch.object(admin_state, "engine", None):
        resp = await sales_admin_handlers.api_admin_sales_quote_draft(_FakeRequest())
        assert resp.status_code == 503
        assert json.loads(resp.body.decode()) == {"error": "Engine not connected"}


@pytest.mark.asyncio
async def test_api_admin_sales_quote_draft_missing_namespace() -> None:
    with patch.object(admin_state, "engine", _make_engine()):
        resp = await sales_admin_handlers.api_admin_sales_quote_draft(_FakeRequest())
        assert resp.status_code == 422
        assert "Missing required field: namespace_id" in json.loads(resp.body.decode())["error"]


@pytest.mark.asyncio
async def test_api_admin_sales_quote_draft_invalid_namespace() -> None:
    with patch.object(admin_state, "engine", _make_engine()):
        resp = await sales_admin_handlers.api_admin_sales_quote_draft(
            _FakeRequest(json_body={"namespace_id": "bad-uuid"})
        )
        assert resp.status_code == 422
        assert "Invalid namespace_id" in json.loads(resp.body.decode())["error"]


@pytest.mark.asyncio
async def test_api_admin_sales_quote_draft_success() -> None:
    expected = {
        "ok": True,
        "proposed_lines": [{"product_ref": "MIC-1", "qty": 2, "suggested_unit_price": 120.0}],
        "suggested_margin_pct": 0.35,
        "propose_only": True,
        "validated": False,
    }
    with (
        patch.object(admin_state, "engine", _make_engine()),
        patch("nce.admin_handlers.sales.do_draft_quote", new=AsyncMock(return_value=expected)),
    ):
        resp = await sales_admin_handlers.api_admin_sales_quote_draft(
            _FakeRequest(
                json_body={
                    "namespace_id": _NAMESPACE_ID,
                    "opportunity_id": "opp-777",
                    "description": "Studio setup",
                }
            )
        )
        assert resp.status_code == 200
        assert json.loads(resp.body.decode()) == expected


# ---------------------------------------------------------------------------
# 5. Surface Invariants
# ---------------------------------------------------------------------------


def test_sales_ai_tool_registry_specs() -> None:
    score_spec = TOOL_REGISTRY.get("sales_score_lead")
    assert score_spec is not None
    assert score_spec.cacheable is True
    assert score_spec.admin_only is False
    assert score_spec.mutation is False

    quote_spec = TOOL_REGISTRY.get("sales_draft_quote")
    assert quote_spec is not None
    assert quote_spec.cacheable is True
    assert quote_spec.admin_only is False
    assert quote_spec.mutation is False


def test_sales_ai_tools_in_stdio_list() -> None:
    tool_names = {t.name for t in TOOLS}
    assert "sales_score_lead" in tool_names
    assert "sales_draft_quote" in tool_names


def test_sales_ai_routes_mounted() -> None:
    routes = build_admin_routes()
    route_map = {r.path: r for r in routes if hasattr(r, "path")}

    assert "/api/sales/lead-score" in route_map
    lead_score_route = route_map["/api/sales/lead-score"]
    assert "POST" in lead_score_route.methods
    assert lead_score_route.endpoint == sales_admin_handlers.api_admin_sales_lead_score

    assert "/api/sales/quote-draft" in route_map
    quote_draft_route = route_map["/api/sales/quote-draft"]
    assert "POST" in quote_draft_route.methods
    assert quote_draft_route.endpoint == sales_admin_handlers.api_admin_sales_quote_draft


def test_sales_ai_cores_pruned_from_internal_cores_json() -> None:
    with open("nce/config_data/internal-cores.json", encoding="utf-8") as f:
        data = json.load(f)

    assert "nce/vertical_modules/sales/ai.py::do_score_lead" not in data
    assert "nce/vertical_modules/sales/ai.py::do_draft_quote" not in data
