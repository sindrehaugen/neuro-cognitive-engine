"""
tests/unit/test_project_pj1_sd3_ratchet.py
===========================================
Ratchet test suite for Wave PJ-1 (Project Outcome at G5) and
Wave SD-3 (Bounded Design Recall top_k) per Charter §4.

Validates:
1. Tool registrations in tool_registry and schemas in mcp_stdio_tools.
2. Removal of do_record_project_outcome from internal-cores.json.
3. do_record_project_outcome writes memory, decision_feedback, and kg_edges edge.
4. Gate G5 requires outcomes_recorded_or_waived.
5. do_advance_phase auto-resolves outcomes_recorded_or_waived when kg_edges has_outcome edge exists.
6. do_propose_design bounds and forwards top_k.
"""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nce.config import cfg
from nce.mcp_stdio_tools import TOOLS
from nce.tool_registry import (
    ADMIN_ONLY_TOOLS,
    CACHEABLE_TOOLS,
    MUTATION_TOOLS,
    TOOL_REGISTRY,
)
from nce.vertical_modules.project.advance import do_advance_phase
from nce.vertical_modules.project.phase_gates import can_enter_phase, load_gate_config
from nce.vertical_modules.project.recall import do_record_project_outcome
from nce.vertical_modules.system_design.propose import do_propose_design

# ---------------------------------------------------------------------------
# 1. Tool Registration & Schema Ratchet
# ---------------------------------------------------------------------------


def test_project_record_outcome_registered() -> None:
    """Wave PJ-1: project_record_outcome is registered with correct flags."""
    assert "project_record_outcome" in TOOL_REGISTRY
    spec = TOOL_REGISTRY["project_record_outcome"]
    assert spec.mutation is True
    assert spec.admin_only is True
    assert spec.cacheable is False
    assert "project_record_outcome" in MUTATION_TOOLS
    assert "project_record_outcome" in ADMIN_ONLY_TOOLS
    assert "project_record_outcome" not in CACHEABLE_TOOLS


def test_project_record_outcome_schema_in_stdio_tools() -> None:
    """Wave PJ-1: project_record_outcome is defined in MCP stdio tools."""
    by_name = {t.name: t for t in TOOLS}
    assert "project_record_outcome" in by_name
    tool = by_name["project_record_outcome"]
    schema = tool.inputSchema
    assert schema["type"] == "object"
    assert "namespace_id" in schema["required"]
    assert "project_id" in schema["required"]
    assert "description" in schema["required"]
    assert "slip_reason" in schema["required"]
    assert "margin_drift" in schema["properties"]
    assert "gate_dwell_time" in schema["properties"]
    assert "confidence" in schema["properties"]
    assert "waived" in schema["properties"]
    assert "actor" in schema["properties"]


def test_system_design_propose_design_schema_has_top_k() -> None:
    """Wave SD-3: system_design_propose_design schema defines top_k integer property."""
    by_name = {t.name: t for t in TOOLS}
    assert "system_design_propose_design" in by_name
    tool = by_name["system_design_propose_design"]
    props = tool.inputSchema["properties"]
    assert "top_k" in props
    assert props["top_k"]["type"] == "integer"


def test_do_record_project_outcome_removed_from_internal_cores() -> None:
    """Wave PJ-1: do_record_project_outcome must be removed from internal-cores.json."""
    repo_root = Path(__file__).resolve().parents[2]
    cores_path = repo_root / "nce" / "config_data" / "internal-cores.json"
    with open(cores_path, encoding="utf-8") as f:
        data = json.load(f)
    allowlist = set(data.keys()) if isinstance(data, dict) else set(data)
    assert "nce/vertical_modules/project/recall.py::do_record_project_outcome" not in allowlist
    assert len(allowlist) <= 69  # Shrink-only allowlist (68 after Wave RS-3)


# ---------------------------------------------------------------------------
# 2. Gate Criteria & do_advance_phase Auto-Resolution
# ---------------------------------------------------------------------------


def test_g5_gate_criteria_requires_outcomes_recorded() -> None:
    """Wave PJ-1: G5 gate criteria must include outcomes_recorded_or_waived."""
    config = load_gate_config()
    gate_criteria = config["GATE_CRITERIA"]
    assert "G5" in gate_criteria
    assert "outcomes_recorded_or_waived" in gate_criteria["G5"]


def test_gate_check_fails_without_outcomes_recorded() -> None:
    """can_enter_phase blocks G5 entry if outcomes_recorded_or_waived is missing."""
    project = {
        "current_phase": "G4",
        "criteria_met": [
            "all_bom_lines_delivered",
            "installation_complete",
            "testing_started",
        ],
    }
    result = can_enter_phase(project, "G5")
    assert result["ok"] is False
    assert "outcomes_recorded_or_waived" in result["missing_criteria"]


def test_gate_check_passes_with_explicit_waiver() -> None:
    """can_enter_phase permits G5 entry if outcomes_recorded_or_waived is asserted."""
    project = {
        "current_phase": "G4",
        "criteria_met": [
            "all_bom_lines_delivered",
            "installation_complete",
            "testing_started",
            "outcomes_recorded_or_waived",
        ],
    }
    result = can_enter_phase(project, "G5")
    assert result["ok"] is True
    assert result["missing_criteria"] == []


@pytest.mark.asyncio
async def test_do_advance_phase_auto_resolves_outcome_from_kg_edges() -> None:
    """do_advance_phase to G5 auto-resolves outcomes_recorded_or_waived if kg_edges edge exists."""
    ns_id = str(uuid.uuid4())
    proj_label = "PROJECT:QUOTE-G5-001"

    mock_conn = AsyncMock()

    async def _fetchrow_router(query: str, *args: Any) -> Any:
        if "FROM   kg_edges" in query:
            return {"object_label": "GATE:QUOTE-G5-001:G4"}
        return None

    async def _fetchval_router(query: str, *args: Any) -> Any:
        if "FROM kg_edges" in query and "has_outcome" in query:
            return 1
        return None

    mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow_router)
    mock_conn.fetchval = AsyncMock(side_effect=_fetchval_router)
    mock_conn.execute = AsyncMock(return_value="UPDATE 1")
    mock_conn.transaction = MagicMock()
    mock_conn.transaction.return_value.__aenter__ = AsyncMock(return_value=None)
    mock_conn.transaction.return_value.__aexit__ = AsyncMock(return_value=None)

    @asynccontextmanager
    async def _mock_scoped_session(pool: Any, ns_uuid: Any) -> Any:
        yield mock_conn

    engine = MagicMock()
    engine.pg_pool = MagicMock()

    with (
        patch(
            "nce.vertical_modules.project.advance.scoped_pg_session",
            side_effect=_mock_scoped_session,
        ),
        patch("nce.vertical_modules.project.advance.assert_owner", new=AsyncMock()),
        patch("nce.vertical_modules.project.advance.emit_graph_write", new=AsyncMock()),
        patch("nce.vertical_modules.project.advance.append_event", new=AsyncMock()),
    ):
        res = await do_advance_phase(
            engine,
            {
                "namespace_id": ns_id,
                "project_id": proj_label,
                "target_phase": "G5",
                "actor": "test@example.com",
                "criteria_met": [
                    "all_bom_lines_delivered",
                    "installation_complete",
                    "testing_started",
                ],
            },
        )

    assert res["ok"] is True
    assert res["phase"] == "G5"


# ---------------------------------------------------------------------------
# 3. do_record_project_outcome Execution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_record_project_outcome_writes_edges_and_feedback() -> None:
    """do_record_project_outcome stores memory, writes kg_edges, and records decision feedback."""
    ns_id = str(uuid.uuid4())
    proj_id = f"PRJ-{uuid.uuid4().hex[:8]}"

    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock(return_value="INSERT 1")
    mock_conn.fetch = AsyncMock(return_value=[])

    @asynccontextmanager
    async def _mock_scoped_session(pool: Any, ns_uuid: Any) -> Any:
        yield mock_conn

    mock_record_feedback = AsyncMock(return_value=uuid.uuid4())

    engine = MagicMock()
    engine.pg_pool = MagicMock()

    with (
        patch(
            "nce.vertical_modules.project.recall.scoped_pg_session",
            side_effect=_mock_scoped_session,
        ),
        patch(
            "nce.vertical_modules.project.recall.embed", new=AsyncMock(return_value=[0.1] * 1536)
        ),
        patch("nce.decision_feedback.record_decision_feedback", new=mock_record_feedback),
    ):
        res = await do_record_project_outcome(
            engine,
            {
                "namespace_id": ns_id,
                "project_id": proj_id,
                "description": "Executive boardroom deployment completed with minor acoustic slip.",
                "slip_reason": "vendor_hardware_delay",
                "margin_drift": -0.03,
                "gate_dwell_time": 4,
                "confidence": 0.95,
                "actor": "pm@example.com",
            },
        )

    assert res["ok"] is True
    assert res["project_id"] == proj_id
    assert res["confidence"] == 0.95
    assert res["waived"] is False
    assert f"OUTCOME:{proj_id}" in res["edge"]

    # Check decision feedback call
    assert mock_record_feedback.await_count == 1
    fb_kwargs = mock_record_feedback.call_args.kwargs
    assert fb_kwargs["engine"] == "project"
    assert fb_kwargs["context_id"] == proj_id
    assert fb_kwargs["decision"] == "recorded"
    assert fb_kwargs["actor"] == "pm@example.com"

    # Check kg_edges SQL executed
    executed_sqls = [call.args[0] for call in mock_conn.execute.call_args_list]
    edge_sqls = [sql for sql in executed_sqls if "INSERT INTO kg_edges" in sql]
    assert len(edge_sqls) == 1
    edge_call_args = [
        call.args
        for call in mock_conn.execute.call_args_list
        if "INSERT INTO kg_edges" in call.args[0]
    ][0]
    # Args: (sql, project_id, "has_outcome", outcome_label, edge_confidence, str(ns_uuid))
    assert edge_call_args[1] == proj_id
    assert edge_call_args[2] == "has_outcome"
    assert edge_call_args[3] == f"OUTCOME:{proj_id}"
    assert edge_call_args[4] == 0.95
    assert edge_call_args[5] == ns_id


# ---------------------------------------------------------------------------
# 4. do_propose_design top_k Bounding & Forwarding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_propose_design_bounds_top_k() -> None:
    """do_propose_design bounds top_k to [1, 50] and passes it to recall."""
    ns_id = str(uuid.uuid4())

    mock_conn = AsyncMock()

    @asynccontextmanager
    async def _mock_scoped_session(pool: Any, ns_uuid: Any) -> Any:
        yield mock_conn

    captured_top_ks = []

    async def _mock_recall(
        conn: Any, ns_uuid: Any, query_vec: list[float], top_k: int = 3
    ) -> list[dict[str, Any]]:
        captured_top_ks.append(top_k)
        return []

    engine = MagicMock()
    engine.pg_pool = MagicMock()

    with (
        patch(
            "nce.vertical_modules.system_design.propose.scoped_pg_session",
            side_effect=_mock_scoped_session,
        ),
        patch(
            "nce.vertical_modules.system_design.propose.embed",
            new=AsyncMock(return_value=[0.1] * 1536),
        ),
        patch(
            "nce.vertical_modules.system_design.propose._recall_similar_designs",
            side_effect=_mock_recall,
        ),
    ):
        # Default
        await do_propose_design(
            engine, {"namespace_id": ns_id, "room_brief": "Boardroom with dual display"}
        )
        assert captured_top_ks[-1] == cfg.NCE_SYSTEM_DESIGN_RECALL_TOP_K

        # Explicit top_k=1 (commissioning flow)
        await do_propose_design(
            engine, {"namespace_id": ns_id, "room_brief": "Executive huddle room", "top_k": 1}
        )
        assert captured_top_ks[-1] == 1

        # Clamped upper bound
        await do_propose_design(
            engine, {"namespace_id": ns_id, "room_brief": "Auditorium", "top_k": 100}
        )
        assert captured_top_ks[-1] == 50

        # Clamped lower bound
        await do_propose_design(
            engine, {"namespace_id": ns_id, "room_brief": "Classroom", "top_k": 0}
        )
        assert captured_top_ks[-1] == 1
