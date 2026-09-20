"""
tests/unit/test_system_design_one_validator.py
==============================================
Wave SD-4 — One Validator consolidation in System Design.

Validates:
1. do_validate_design acts as the single entrypoint for system design validation:
   - Graph validation mode (when decisions is omitted / None) delegates to validate_design_graph
   - Decision recording mode (when decisions is provided) validates decisions per propose-only §9.3,
     appends to v3_cognitive_ledger, bumps design version, and optionally runs graph validation.
2. ADR-0017 leak checks: no cost, cost_price, margin, or bid_id in outputs.
3. Surface wiring:
   - MCP handle_system_design_validate_design_graph routes to do_validate_design.
   - REST api_system_design_validate_design_graph routes to do_validate_design and bumps MCP cache on writes.
   - Tool schema in nce.mcp_stdio_tools exposes optional decisions and validate_graph.
4. Internal cores allowlist:
   - do_validate_design is pruned from internal-cores.json shrink-only allowlist.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nce.admin_handlers.system_design import (
    api_system_design_validate_design_graph,
)
from nce.mcp_errors import McpError
from nce.mcp_stdio_tools import TOOLS
from nce.vertical_modules.system_design.mcp_handlers import (
    handle_system_design_validate_design_graph,
)
from nce.vertical_modules.system_design.validate import (
    _validate_decisions,
    do_validate_design,
)

# ---------------------------------------------------------------------------
# 1. Propose-only §9.3 Decision Validation Pure-Unit Tests
# ---------------------------------------------------------------------------


class TestProposeOnlyDecisionValidation:
    """Tests for _validate_decisions helper."""

    def test_valid_accept_decision(self) -> None:
        errors = _validate_decisions([{"line_id": "L1", "verdict": "accept"}])
        assert errors == []

    def test_valid_override_decision(self) -> None:
        errors = _validate_decisions(
            [{"line_id": "L1", "verdict": "override", "reason": "custom spec"}]
        )
        assert errors == []

    def test_missing_line_id(self) -> None:
        errors = _validate_decisions([{"verdict": "accept"}])
        assert len(errors) == 1
        assert "'line_id' is required" in errors[0]

    def test_empty_verdict_violates_propose_only(self) -> None:
        errors = _validate_decisions([{"line_id": "L1", "verdict": ""}])
        assert len(errors) == 1
        assert "no auto-accept" in errors[0].lower() or "§9.3" in errors[0]

    def test_invalid_verdict_string(self) -> None:
        errors = _validate_decisions([{"line_id": "L1", "verdict": "auto-approved"}])
        assert len(errors) == 1
        assert "'verdict' must be one of" in errors[0]


# ---------------------------------------------------------------------------
# 2. do_validate_design Dual Mode Dispatch
# ---------------------------------------------------------------------------


class TestDoValidateDesignDualMode:
    """Tests for do_validate_design core dispatch."""

    @pytest.mark.asyncio
    async def test_graph_validation_mode_when_no_decisions(self) -> None:
        """When decisions is absent, delegates directly to validate_design_graph."""
        mock_engine = MagicMock()
        ns_id = str(uuid.uuid4())
        mock_result = {
            "passed": True,
            "reasons": [],
            "continuity": {"passed": True, "reasons": []},
            "format_compatibility": {"passed": True, "reasons": []},
            "power_heat_budget": {"total_watts": 120.0, "total_btu_hr": 409.4, "reasons": []},
            "spof_redundancy": {"passed": True, "reasons": []},
            "avixa_checkpoints": {"passed": True, "reasons": []},
        }

        with patch(
            "nce.vertical_modules.system_design.validate.validate_design_graph",
            new_callable=AsyncMock,
            return_value=mock_result,
        ) as mock_graph_val:
            params = {"namespace_id": ns_id, "design_id": "DESIGN-001"}
            res = await do_validate_design(mock_engine, params)

            assert res == mock_result
            mock_graph_val.assert_awaited_once_with(mock_engine, params)

    @pytest.mark.asyncio
    async def test_graph_validation_mode_when_decisions_is_none(self) -> None:
        """When decisions is None, delegates directly to validate_design_graph."""
        mock_engine = MagicMock()
        ns_id = str(uuid.uuid4())
        mock_result = {"passed": False, "reasons": ["Dangling input"]}

        with patch(
            "nce.vertical_modules.system_design.validate.validate_design_graph",
            new_callable=AsyncMock,
            return_value=mock_result,
        ) as mock_graph_val:
            params = {"namespace_id": ns_id, "design_id": "DESIGN-001", "decisions": None}
            res = await do_validate_design(mock_engine, params)

            assert res == mock_result
            mock_graph_val.assert_awaited_once_with(mock_engine, params)

    @pytest.mark.asyncio
    async def test_empty_decisions_list_raises_value_error(self) -> None:
        """Empty decisions list raises ValueError."""
        mock_engine = MagicMock()
        ns_id = str(uuid.uuid4())
        with pytest.raises(ValueError, match="decisions' must be a non-empty list"):
            await do_validate_design(
                mock_engine, {"namespace_id": ns_id, "design_id": "DESIGN-001", "decisions": []}
            )

    @pytest.mark.asyncio
    async def test_missing_design_id_raises_value_error(self) -> None:
        """Missing design_id in decision mode raises ValueError."""
        mock_engine = MagicMock()
        ns_id = str(uuid.uuid4())
        with pytest.raises(ValueError, match="design_id' is required"):
            await do_validate_design(
                mock_engine,
                {
                    "namespace_id": ns_id,
                    "decisions": [{"line_id": "L1", "verdict": "accept"}],
                },
            )

    @pytest.mark.asyncio
    async def test_missing_namespace_id_raises_value_error(self) -> None:
        """Missing namespace_id raises ValueError."""
        mock_engine = MagicMock()
        with pytest.raises(ValueError, match="namespace_id' is required"):
            await do_validate_design(
                mock_engine,
                {"design_id": "DESIGN-001"},
            )

    @pytest.mark.asyncio
    async def test_invalid_decision_raises_value_error(self) -> None:
        """Invalid decisions structure raises ValueError with violations list."""
        mock_engine = MagicMock()
        ns_id = str(uuid.uuid4())
        with pytest.raises(ValueError, match="malformed decisions"):
            await do_validate_design(
                mock_engine,
                {
                    "namespace_id": ns_id,
                    "design_id": "DESIGN-001",
                    "decisions": [{"line_id": "L1", "verdict": ""}],
                },
            )

    @pytest.mark.asyncio
    async def test_decision_recording_mode_success_accept(self) -> None:
        """Valid decisions recorded successfully; writes to ledger and bumps design node."""
        mock_engine = MagicMock()
        ns_id = uuid.uuid4()
        mock_conn = AsyncMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_conn
        mock_ctx.__aexit__.return_value = None

        with (
            patch(
                "nce.vertical_modules.system_design.validate.scoped_pg_session",
                return_value=mock_ctx,
            ),
            patch(
                "nce.vertical_modules.system_design.validate._bump_design_version",
                new_callable=AsyncMock,
            ) as mock_bump,
            patch(
                "nce.vertical_modules.system_design.validate._append_validation_ledger",
                new_callable=AsyncMock,
            ) as mock_append,
        ):
            res = await do_validate_design(
                mock_engine,
                {
                    "namespace_id": ns_id,
                    "design_id": "DESIGN-001",
                    "decisions": [{"line_id": "L1", "verdict": "accept"}],
                },
            )

            assert res["passed"] is True
            assert res["reasons"] == []
            assert res["decisions_recorded"] == 1
            assert res["design_version_bumped"] is True
            mock_bump.assert_awaited_once()
            mock_append.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_decision_recording_mode_override_fails_passed(self) -> None:
        """Override verdict causes passed=False and records reason."""
        mock_engine = MagicMock()
        ns_id = uuid.uuid4()
        mock_conn = AsyncMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_conn
        mock_ctx.__aexit__.return_value = None

        with (
            patch(
                "nce.vertical_modules.system_design.validate.scoped_pg_session",
                return_value=mock_ctx,
            ),
            patch(
                "nce.vertical_modules.system_design.validate._bump_design_version",
                new_callable=AsyncMock,
            ),
            patch(
                "nce.vertical_modules.system_design.validate._append_validation_ledger",
                new_callable=AsyncMock,
            ),
        ):
            res = await do_validate_design(
                mock_engine,
                {
                    "namespace_id": ns_id,
                    "design_id": "DESIGN-001",
                    "decisions": [
                        {
                            "line_id": "L1",
                            "verdict": "override",
                            "reason": "Requires redundant power supply",
                        }
                    ],
                },
            )

            assert res["passed"] is False
            assert len(res["reasons"]) == 1
            assert "Requires redundant power supply" in res["reasons"][0]

    @pytest.mark.asyncio
    async def test_decision_recording_with_validate_graph(self) -> None:
        """When validate_graph=True, graph validation results are combined into the response."""
        mock_engine = MagicMock()
        ns_id = uuid.uuid4()
        mock_conn = AsyncMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_conn
        mock_ctx.__aexit__.return_value = None

        graph_result = {
            "passed": False,
            "reasons": ["Dangling input port PORT:D1:IN1"],
        }

        with (
            patch(
                "nce.vertical_modules.system_design.validate.scoped_pg_session",
                return_value=mock_ctx,
            ),
            patch(
                "nce.vertical_modules.system_design.validate._bump_design_version",
                new_callable=AsyncMock,
            ),
            patch(
                "nce.vertical_modules.system_design.validate._append_validation_ledger",
                new_callable=AsyncMock,
            ),
            patch(
                "nce.vertical_modules.system_design.validate.validate_design_graph",
                new_callable=AsyncMock,
                return_value=graph_result,
            ) as mock_graph_val,
        ):
            res = await do_validate_design(
                mock_engine,
                {
                    "namespace_id": ns_id,
                    "design_id": "DESIGN-001",
                    "decisions": [{"line_id": "L1", "verdict": "accept"}],
                    "validate_graph": True,
                },
            )

            assert res["passed"] is False
            assert "Dangling input port PORT:D1:IN1" in res["reasons"]
            assert res["graph_validation"] == graph_result
            mock_graph_val.assert_awaited_once()


# ---------------------------------------------------------------------------
# 3. ADR-0017 Leak Invariants
# ---------------------------------------------------------------------------


class TestADR0017LeakConformance:
    """Ensure no cost, cost_price, margin, or bid_id data leaks in any output."""

    @pytest.mark.asyncio
    async def test_adr0017_no_financial_leaks(self) -> None:
        mock_engine = MagicMock()
        ns_id = uuid.uuid4()
        mock_conn = AsyncMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_conn
        mock_ctx.__aexit__.return_value = None

        with (
            patch(
                "nce.vertical_modules.system_design.validate.scoped_pg_session",
                return_value=mock_ctx,
            ),
            patch(
                "nce.vertical_modules.system_design.validate._bump_design_version",
                new_callable=AsyncMock,
            ),
            patch(
                "nce.vertical_modules.system_design.validate._append_validation_ledger",
                new_callable=AsyncMock,
            ),
        ):
            res = await do_validate_design(
                mock_engine,
                {
                    "namespace_id": ns_id,
                    "design_id": "DESIGN-001",
                    "decisions": [{"line_id": "L1", "verdict": "accept"}],
                },
            )

            forbidden_keys = {"cost", "cost_price", "margin", "bid_id"}
            found = forbidden_keys.intersection(set(res.keys()))
            assert not found, f"ADR-0017 leak detected: {found}"


# ---------------------------------------------------------------------------
# 4. Surface Wiring Tests (MCP & REST)
# ---------------------------------------------------------------------------


class _StubRequest:
    def __init__(self, body: Any) -> None:
        self._body = body

    async def json(self) -> Any:
        return self._body


class TestSurfaceWiring:
    """Tests for MCP handler and REST route wiring to do_validate_design."""

    @pytest.mark.asyncio
    async def test_mcp_handler_calls_do_validate_design(self) -> None:
        """MCP handler routes to do_validate_design."""
        mock_engine = MagicMock()
        mock_val_result = {"passed": True, "reasons": []}
        ns_id = str(uuid.uuid4())

        with patch(
            "nce.vertical_modules.system_design.mcp_handlers.do_validate_design",
            new_callable=AsyncMock,
            return_value=mock_val_result,
        ) as mock_do_val:
            tool_args = {"namespace_id": ns_id, "design_id": "DESIGN-001"}
            res_str = await handle_system_design_validate_design_graph(mock_engine, tool_args)

            mock_do_val.assert_awaited_once_with(mock_engine, tool_args)
            content = json.loads(res_str)
            assert content == mock_val_result

    @pytest.mark.asyncio
    async def test_mcp_handler_raises_mcp_error_on_missing_namespace(self) -> None:
        """MCP handler raises McpError on missing namespace."""
        mock_engine = MagicMock()

        with pytest.raises(McpError) as exc_info:
            await handle_system_design_validate_design_graph(
                mock_engine, {"design_id": "DESIGN-001", "decisions": []}
            )
        assert exc_info.value.code == -32602

    @pytest.mark.asyncio
    async def test_rest_route_calls_do_validate_design_and_bumps_cache(self) -> None:
        """REST route calls do_validate_design and invalidates cache when decisions recorded."""
        ns_id = str(uuid.uuid4())
        payload = {
            "namespace_id": ns_id,
            "design_id": "DESIGN-001",
            "decisions": [{"line_id": "L1", "verdict": "accept"}],
        }
        req = _StubRequest(payload)
        expected_res = {"passed": True, "reasons": []}

        with (
            patch("nce.admin_handlers.system_design.admin_state") as mock_admin_state,
            patch(
                "nce.admin_handlers.system_design.do_validate_design",
                new_callable=AsyncMock,
                return_value=expected_res,
            ) as mock_do_val,
            patch(
                "nce.admin_handlers.system_design.bump_mcp_cache_generation",
                new_callable=AsyncMock,
            ) as mock_bump,
        ):
            mock_admin_state.engine = MagicMock()
            response = await api_system_design_validate_design_graph(req)
            assert response.status_code == 200
            data = json.loads(response.body.decode("utf-8"))
            assert data["status"] == "ok"
            assert data["validation"] == expected_res
            mock_do_val.assert_awaited_once()
            mock_bump.assert_awaited_once_with(
                mock_admin_state.engine,
                route="api_system_design_validate_design_graph",
            )

    @pytest.mark.asyncio
    async def test_rest_route_graph_mode_no_cache_bump(self) -> None:
        """REST route does not bump cache when validating graph without decisions."""
        ns_id = str(uuid.uuid4())
        payload = {
            "namespace_id": ns_id,
            "design_id": "DESIGN-001",
        }
        req = _StubRequest(payload)
        expected_res = {"passed": True, "reasons": []}

        with (
            patch("nce.admin_handlers.system_design.admin_state") as mock_admin_state,
            patch(
                "nce.admin_handlers.system_design.do_validate_design",
                new_callable=AsyncMock,
                return_value=expected_res,
            ) as mock_do_val,
            patch(
                "nce.admin_handlers.system_design.bump_mcp_cache_generation",
                new_callable=AsyncMock,
            ) as mock_bump,
        ):
            mock_admin_state.engine = MagicMock()
            response = await api_system_design_validate_design_graph(req)
            assert response.status_code == 200
            data = json.loads(response.body.decode("utf-8"))
            assert data["status"] == "ok"
            assert data["validation"] == expected_res
            mock_do_val.assert_awaited_once()
            mock_bump.assert_not_called()


# ---------------------------------------------------------------------------
# 5. Tool Schema & Internal-Cores Allowlist Ratchet
# ---------------------------------------------------------------------------


class TestToolSchemaAndAllowlist:
    """Checks tool metadata and allowlist shrinkage."""

    def test_tool_definition_has_decisions_and_validate_graph(self) -> None:
        target_tool = next(
            (t for t in TOOLS if t.name == "system_design_validate_design_graph"), None
        )
        assert target_tool is not None, (
            "system_design_validate_design_graph tool not found in TOOLS"
        )
        props = target_tool.inputSchema["properties"]
        assert "design_id" in props
        assert "decisions" in props
        assert "validate_graph" in props
        assert "design_id" in target_tool.inputSchema["required"]

    def test_do_validate_design_pruned_from_internal_cores(self) -> None:
        """Inert-instrument audit (2026-09-20): this read ``data.get("internal_cores", [])``,
        but the file is a flat dict keyed by site id with no ``"internal_cores"`` key at all --
        ``cores`` was always ``[]`` and ``target not in cores`` always trivially True. Verified
        by re-inserting the exact target string into a copy of the real file and confirming
        the old assertion still passed. Fixed to read the file's actual shape.
        """
        allowlist_path = Path("nce/config_data/internal-cores.json")
        assert allowlist_path.exists()
        with open(allowlist_path, encoding="utf-8") as f:
            data = json.load(f)

        cores = set(data.keys()) if isinstance(data, dict) else set(data)
        assert cores, "internal-cores.json parsed empty -- the loader broke, not the estate"
        target = "nce/vertical_modules/system_design/validate.py::do_validate_design"
        assert target not in cores, f"{target} must be pruned from internal-cores.json"
