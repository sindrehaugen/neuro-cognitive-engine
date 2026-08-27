"""
tests/unit/test_project_routes.py
==================================
Acceptance tests for Batch 072 — Module 7.Wave 5 (phase-routes).

Covers:
  1. POST /api/project/convert-signed-quote returns {project_id, baseline}
     and delegates to ``do_convert_signed_quote`` (no duplicated logic).
  2. GET /api/project/{id}/phase returns the current phase.
  3. POST /api/project/{id}/phase advances on a legal + criteria-met transition.
  4. POST /api/project/{id}/phase returns HTTP 409 + missing_criteria on gate fail.
  5. Routes return 503 when engine is not connected.
  6. Routes are mounted in the admin app.
  7. Routes contain no duplicated gate/conversion logic (structural assertion).

All tests are pure unit tests (no DB, no Redis, no real config load).
``do_convert_signed_quote``, ``do_advance_phase``, and ``read_current_phase``
are patched so the handlers are tested as thin adapters.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_NAMESPACE_ID = "00000000-0000-4000-8000-000000000001"
_PROJECT_ID = "PROJECT:Q123"
_QUOTE_ID = "Q123"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(
    body: dict[str, Any] | None = None,
    qp: dict[str, str] | None = None,
    path_params: dict[str, str] | None = None,
) -> MagicMock:
    """Minimal Starlette-like request mock."""
    req = MagicMock()
    req.json = AsyncMock(return_value=body or {})
    req.query_params = qp or {}
    req.path_params = path_params or {}
    return req


# ---------------------------------------------------------------------------
# 1. POST /api/project/convert-signed-quote
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_convert_signed_quote_returns_project_id_and_baseline():
    """Handler returns {project_id, baseline} from the core and nothing else."""
    from nce.admin_handlers.project import api_project_convert_signed_quote

    core_result = {
        "project_id": _PROJECT_ID,
        "gate": "G0",
        "bom_lines_linked": 3,
        "baseline": {"signed_baseline_id": "SB-001", "sales_available": True},
    }

    with (
        patch("nce.admin_handlers.project.admin_state") as mock_state,
        patch(
            "nce.admin_handlers.project.do_convert_signed_quote",
            new=AsyncMock(return_value=core_result),
        ) as mock_core,
    ):
        mock_state.engine = MagicMock()
        req = _make_request(
            body={
                "namespace_id": _NAMESPACE_ID,
                "quote_id": _QUOTE_ID,
                "signed_by": "alice",
                "signature_ref": "SIG-001",
            }
        )
        resp = await api_project_convert_signed_quote(req)

    assert resp.status_code == 200
    data = json.loads(resp.body)
    assert data["project_id"] == _PROJECT_ID
    assert "baseline" in data
    # Core was called exactly once — handler delegates, does not duplicate logic.
    mock_core.assert_awaited_once()


@pytest.mark.asyncio
async def test_convert_signed_quote_503_when_engine_not_connected():
    from nce.admin_handlers.project import api_project_convert_signed_quote

    with patch("nce.admin_handlers.project.admin_state") as mock_state:
        mock_state.engine = None
        req = _make_request(body={"namespace_id": _NAMESPACE_ID})
        resp = await api_project_convert_signed_quote(req)

    assert resp.status_code == 503
    data = json.loads(resp.body)
    assert "error" in data


@pytest.mark.asyncio
async def test_convert_signed_quote_missing_namespace_id():
    from nce.admin_handlers.project import api_project_convert_signed_quote

    with patch("nce.admin_handlers.project.admin_state") as mock_state:
        mock_state.engine = MagicMock()
        req = _make_request(body={})
        resp = await api_project_convert_signed_quote(req)

    assert resp.status_code == 422
    data = json.loads(resp.body)
    assert "error" in data


@pytest.mark.asyncio
async def test_convert_signed_quote_core_value_error_returns_422():
    """A ValueError from the core (bad params) maps to 422, not 500."""
    from nce.admin_handlers.project import api_project_convert_signed_quote

    with (
        patch("nce.admin_handlers.project.admin_state") as mock_state,
        patch(
            "nce.admin_handlers.project.do_convert_signed_quote",
            new=AsyncMock(side_effect=ValueError("'quote_id' is required")),
        ),
    ):
        mock_state.engine = MagicMock()
        req = _make_request(body={"namespace_id": _NAMESPACE_ID})
        resp = await api_project_convert_signed_quote(req)

    assert resp.status_code == 422
    data = json.loads(resp.body)
    assert "error" in data


# ---------------------------------------------------------------------------
# 2. GET /api/project/{id}/phase
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_phase_returns_current_phase():
    """Handler returns the current phase from read_current_phase."""
    from nce.admin_handlers.project import api_project_get_phase

    with (
        patch("nce.admin_handlers.project.admin_state") as mock_state,
        patch(
            "nce.admin_handlers.project.read_current_phase",
            new=AsyncMock(return_value="G1"),
        ),
    ):
        mock_state.engine = MagicMock()
        req = _make_request(
            qp={"namespace_id": _NAMESPACE_ID},
            path_params={"id": _PROJECT_ID},
        )
        resp = await api_project_get_phase(req)

    assert resp.status_code == 200
    data = json.loads(resp.body)
    assert data["project_id"] == _PROJECT_ID
    assert data["phase"] == "G1"


@pytest.mark.asyncio
async def test_get_phase_returns_null_when_no_phase():
    """Returns {"phase": null} when project has no in_phase edge yet."""
    from nce.admin_handlers.project import api_project_get_phase

    with (
        patch("nce.admin_handlers.project.admin_state") as mock_state,
        patch(
            "nce.admin_handlers.project.read_current_phase",
            new=AsyncMock(return_value=None),
        ),
    ):
        mock_state.engine = MagicMock()
        req = _make_request(
            qp={"namespace_id": _NAMESPACE_ID},
            path_params={"id": _PROJECT_ID},
        )
        resp = await api_project_get_phase(req)

    assert resp.status_code == 200
    data = json.loads(resp.body)
    assert data["phase"] is None


@pytest.mark.asyncio
async def test_get_phase_503_when_engine_not_connected():
    from nce.admin_handlers.project import api_project_get_phase

    with patch("nce.admin_handlers.project.admin_state") as mock_state:
        mock_state.engine = None
        req = _make_request(
            qp={"namespace_id": _NAMESPACE_ID},
            path_params={"id": _PROJECT_ID},
        )
        resp = await api_project_get_phase(req)

    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_get_phase_missing_namespace_id():
    from nce.admin_handlers.project import api_project_get_phase

    with patch("nce.admin_handlers.project.admin_state") as mock_state:
        mock_state.engine = MagicMock()
        req = _make_request(
            qp={},
            path_params={"id": _PROJECT_ID},
        )
        resp = await api_project_get_phase(req)

    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 3. POST /api/project/{id}/phase — successful advance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_advance_phase_success_returns_ok_and_phase():
    """Handler returns {ok: True, phase: ...} on a legal+criteria-met transition."""
    from nce.admin_handlers.project import api_project_advance_phase

    core_result = {"ok": True, "phase": "G1"}

    with (
        patch("nce.admin_handlers.project.admin_state") as mock_state,
        patch(
            "nce.admin_handlers.project.do_advance_phase",
            new=AsyncMock(return_value=core_result),
        ) as mock_core,
    ):
        mock_state.engine = MagicMock()
        req = _make_request(
            body={
                "namespace_id": _NAMESPACE_ID,
                "target_phase": "G1",
                "actor": "alice",
                "criteria_met": ["sales_signed", "pl_assigned"],
            },
            path_params={"id": _PROJECT_ID},
        )
        resp = await api_project_advance_phase(req)

    assert resp.status_code == 200
    data = json.loads(resp.body)
    assert data["ok"] is True
    assert data["phase"] == "G1"
    # Core was called; params forwarded — no logic duplicated in the handler.
    mock_core.assert_awaited_once()
    call_params = mock_core.call_args[0][1]
    assert call_params["project_id"] == _PROJECT_ID
    assert call_params["target_phase"] == "G1"
    assert call_params["actor"] == "alice"


@pytest.mark.asyncio
async def test_advance_phase_noop_returns_200():
    """Idempotent advance (already in target phase) returns 200."""
    from nce.admin_handlers.project import api_project_advance_phase

    with (
        patch("nce.admin_handlers.project.admin_state") as mock_state,
        patch(
            "nce.admin_handlers.project.do_advance_phase",
            new=AsyncMock(return_value={"ok": True, "phase": "G0", "noop": True}),
        ),
    ):
        mock_state.engine = MagicMock()
        req = _make_request(
            body={"namespace_id": _NAMESPACE_ID, "target_phase": "G0", "actor": "alice"},
            path_params={"id": _PROJECT_ID},
        )
        resp = await api_project_advance_phase(req)

    assert resp.status_code == 200
    data = json.loads(resp.body)
    assert data["ok"] is True
    assert data.get("noop") is True


# ---------------------------------------------------------------------------
# 4. POST /api/project/{id}/phase — gate fail → 409 + missing_criteria
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_advance_phase_gate_fail_returns_409_with_missing_criteria():
    """Gate-refused result maps to HTTP 409 with missing_criteria in body."""
    from nce.admin_handlers.project import api_project_advance_phase

    gate_fail = {
        "ok": False,
        "missing_criteria": ["sales_signed", "pl_assigned"],
        "current_phase": "G0",
    }

    with (
        patch("nce.admin_handlers.project.admin_state") as mock_state,
        patch(
            "nce.admin_handlers.project.do_advance_phase",
            new=AsyncMock(return_value=gate_fail),
        ),
    ):
        mock_state.engine = MagicMock()
        req = _make_request(
            body={"namespace_id": _NAMESPACE_ID, "target_phase": "G1", "actor": "alice"},
            path_params={"id": _PROJECT_ID},
        )
        resp = await api_project_advance_phase(req)

    assert resp.status_code == 409
    data = json.loads(resp.body)
    assert "missing_criteria" in data
    assert data["missing_criteria"] == ["sales_signed", "pl_assigned"]
    assert data["current_phase"] == "G0"


@pytest.mark.asyncio
async def test_advance_phase_bad_params_returns_400():
    """Bad params / absent project (ok=False, error=...) maps to HTTP 400."""
    from nce.admin_handlers.project import api_project_advance_phase

    bad_result = {"ok": False, "error": "project 'PROJECT:MISSING' has no 'in_phase' edge"}

    with (
        patch("nce.admin_handlers.project.admin_state") as mock_state,
        patch(
            "nce.admin_handlers.project.do_advance_phase",
            new=AsyncMock(return_value=bad_result),
        ),
    ):
        mock_state.engine = MagicMock()
        req = _make_request(
            body={"namespace_id": _NAMESPACE_ID, "target_phase": "G1", "actor": "alice"},
            path_params={"id": "PROJECT:MISSING"},
        )
        resp = await api_project_advance_phase(req)

    assert resp.status_code == 400
    data = json.loads(resp.body)
    assert "error" in data


@pytest.mark.asyncio
async def test_advance_phase_503_when_engine_not_connected():
    from nce.admin_handlers.project import api_project_advance_phase

    with patch("nce.admin_handlers.project.admin_state") as mock_state:
        mock_state.engine = None
        req = _make_request(
            body={"namespace_id": _NAMESPACE_ID, "target_phase": "G1", "actor": "alice"},
            path_params={"id": _PROJECT_ID},
        )
        resp = await api_project_advance_phase(req)

    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_advance_phase_missing_namespace_id():
    from nce.admin_handlers.project import api_project_advance_phase

    with patch("nce.admin_handlers.project.admin_state") as mock_state:
        mock_state.engine = MagicMock()
        req = _make_request(body={}, path_params={"id": _PROJECT_ID})
        resp = await api_project_advance_phase(req)

    assert resp.status_code == 422
    data = json.loads(resp.body)
    assert "error" in data


# ---------------------------------------------------------------------------
# 5. No duplicated gate / conversion logic in handlers
# ---------------------------------------------------------------------------


def test_handler_module_contains_no_gate_logic():
    """Structural: the handler module must not define can_enter_phase or phase
    transition tables — logic lives in the do_* cores."""
    import inspect

    import nce.admin_handlers.project as handler_mod

    source = inspect.getsource(handler_mod)
    # Gate config / transition tables must not be defined here.
    assert "VALID_PHASE_TRANSITIONS" not in source
    assert "GATE_CRITERIA" not in source
    assert "can_enter_phase" not in source
    # No SQL in the handler (SQL is in the cores / db helpers).
    assert "INSERT INTO" not in source
    assert "SELECT " not in source


# ---------------------------------------------------------------------------
# 6. Routes mounted in admin app
# ---------------------------------------------------------------------------


def test_project_routes_mounted_in_admin_app():
    from nce.admin_app import build_admin_routes

    routes = build_admin_routes()
    paths = {r.path for r in routes}
    assert "/api/project/convert-signed-quote" in paths
    assert "/api/project/{id}/phase" in paths
