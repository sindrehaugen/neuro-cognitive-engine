"""
tests/unit/test_economy_surface.py
====================================
Acceptance tests for Batch 119 — Module 8.Wave 4 (cores-surface).

Covers:
  1. The ``economy`` MCP handler / admin handler packages import cleanly.
  2. ``economy_match_invoice`` / ``economy_compute_periodisering`` /
     ``economy_emit_event`` are registered in ``TOOL_REGISTRY`` with the
     correct flags (cacheable=True, admin_only=False, mutation=False,
     migration=False).
  3. Each ``handle_*`` returns valid JSON for a good payload.
  4. Each ``handle_*`` returns ``{"error": ...}`` (a JSON string, never a
     raised exception) for a missing ``namespace_id``.
  5. ``handle_economy_emit_event`` returns a structured ``{"error": ...}``
     (never a crash, never auto-balanced) for an unbalanced event.
  6. Tool-count assertion reflects +3 economy tools.
  7. The three REST routes are mounted in the admin app and return the same
     shape as the cores.

All tests are pure unit tests (no DB, no Redis, no real config-file load —
``load_economy_thresholds`` / ``load_finago_chart_of_accounts`` /
``load_finago_account_mapping`` are patched to fixed fixtures, mirroring
``test_procurement_surface.py``'s ``_patch_load_config`` pattern).
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

_NAMESPACE_ID = "00000000-0000-4000-8000-000000000001"

# Mirrors tests/unit/test_economy_match.py's _FIXTURE_THRESHOLDS — a literal
# defined here, never the real economy-match-thresholds.json values.
_FIXTURE_THRESHOLDS: dict[str, Any] = {"green": 115, "yellow": 70, "supplier_overrides": {}}

# Mirrors tests/unit/test_economy_ngaap.py's _FIXTURE_BUCKET_ACCOUNTS /
# _FIXTURE_SHARED_ACCOUNTS / _FIXTURE_CHART / _FIXTURE_MAPPING — fake account
# numbers on purpose, covering all seven canonical buckets (ngaap.py's
# _resolve_accounts resolves every bucket regardless of which ones the
# caller populated in params["buckets"]).
_FIXTURE_BUCKET_ACCOUNTS: dict[str, dict[str, str]] = {
    "hardware": {"cogs": "9300", "revenue": "9000", "accrued": "9531", "deferred": "9901"},
    "materials": {"cogs": "9303", "revenue": "9003", "accrued": "9532", "deferred": "9902"},
    "freight": {"cogs": "9060", "revenue": "9520", "accrued": "9533", "deferred": "9903"},
    "pm": {"cogs": "9500", "revenue": "9016", "accrued": "9534", "deferred": "9904"},
    "tek": {"cogs": "9500", "revenue": "9015", "accrued": "9539", "deferred": "9905"},
    "programming": {"cogs": "9500", "revenue": "9014", "accrued": "9536", "deferred": "9906"},
    "travel": {"cogs": "9160", "revenue": "9013", "accrued": "9538", "deferred": "9908"},
}
_FIXTURE_SHARED_ACCOUNTS: dict[str, str] = {"wip": "9771"}


def _plan_for(bucket_accounts: dict, shared_accounts: dict) -> dict:
    numbers = {account for roles in bucket_accounts.values() for account in roles.values()} | set(
        shared_accounts.values()
    )
    return {number: {"name": f"Fixture account {number}", "type": "asset"} for number in numbers}


_FIXTURE_CHART: dict[str, Any] = {
    "country": "NO",
    "gaap": "NGAAP",
    "bucket_accounts": _FIXTURE_BUCKET_ACCOUNTS,
    "shared_accounts": _FIXTURE_SHARED_ACCOUNTS,
    "accounts": _plan_for(_FIXTURE_BUCKET_ACCOUNTS, _FIXTURE_SHARED_ACCOUNTS),
}

_FIXTURE_MAPPING: dict[str, Any] = {
    "role_mva_code": {"cogs": 0, "revenue": 3, "accrued": 0, "deferred": 0, "wip": 0},
    "role_balance_side": {
        "cogs": "debit",
        "revenue": "credit",
        "accrued": "debit",
        "deferred": "credit",
        "wip": "debit",
    },
    "account_mva_overrides": {},
}

_INVOICE: dict[str, Any] = {
    "supplier_orgnr": "987654321",
    "project_dimension_present": True,
    "lines": [
        {
            "article_no": "NETSET-42",
            "description": "Crestron DM-NVX-363 Network AV Encoder",
            "line_total": 30_000,
        }
    ],
}

_CANDIDATES: list[dict[str, Any]] = [
    {
        "bom_line": {
            "article_no": "NETSET-42",
            "description": "Crestron DM-NVX-363 Network AV Encoder",
            "project_id": "proj-1",
        },
        "context": {"supplier_exact": True, "expected_amount": 30_000},
        "three_way_result": None,
    }
]

_PERIODISERING_PARAMS: dict[str, Any] = {
    "buckets": {
        "hardware": {
            "expected_revenue": 100_000,
            "expected_cost": 60_000,
            "actual_cost": 60_000,
            "actual_invoiced": 50_000,
            "delivery_pct": 1.0,
        }
    },
}

_BALANCED_EVENT: dict[str, Any] = {
    "type": "supplier.invoice.approved",
    "postings": [
        {"account": "4300", "amount": 100_000.0},
        {"account": "2400", "amount": -100_000.0},
    ],
}

_UNBALANCED_EVENT: dict[str, Any] = {
    "type": "supplier.invoice.approved",
    "postings": [
        {"account": "4300", "amount": 100_000.0},
        {"account": "2400", "amount": -1.0},
    ],
}

# ---------------------------------------------------------------------------
# Fixtures for Batch 119 fix-forward Fix 4 (containment pinning) — these back the
# "does a caller-supplied config-as-IP key ever win?" tests further down. Both the
# "tight"/"alt" fixtures (what the loader is patched to) and the "poisoned" ones
# (what a caller supplies) must be distinguishable so a passing assertion proves the
# loader's value was actually used, not merely a coincidence of equal fixtures.
# ---------------------------------------------------------------------------

# Deliberately TIGHT: the real _INVOICE/_CANDIDATES pair (max ~180 pts) cannot reach
# GREEN, or even YELLOW, against this.
_TIGHT_THRESHOLDS: dict[str, Any] = {"green": 100_000, "yellow": 100_000, "supplier_overrides": {}}

# What a caller-override backdoor (`arguments.get("thresholds") or loader()`) would
# substitute if one existed: a loosened config that classifies anything GREEN.
_POISONED_LOOSE_THRESHOLDS: dict[str, Any] = {"green": 0, "yellow": 0, "supplier_overrides": {}}

# Distinguishable chart/mapping the loader is patched to for the containment tests —
# values a caller-supplied "chart"/"mapping" body/arguments key could never itself
# carry (it carries the plain _FIXTURE_CHART/_FIXTURE_MAPPING), so a passing
# assertion proves the loader, not the caller, governed the result.
_ALT_CHART: dict[str, Any] = {**_FIXTURE_CHART, "country": "ALT-COUNTRY", "gaap": "ALT-GAAP"}
_ALT_MAPPING: dict[str, Any] = {
    **_FIXTURE_MAPPING,
    "role_mva_code": {**_FIXTURE_MAPPING["role_mva_code"], "cogs": 9},
}

# Large enough to "balance" _UNBALANCED_EVENT's 99,999 NOK diff if a caller-override
# backdoor (`arguments.get("epsilon", _BALANCE_EPSILON_DEFAULT)`) existed. The real,
# hard-coded 0.01 epsilon must still reject it.
_POISONED_HIGH_EPSILON = 999_999


@pytest.fixture(autouse=True)
def _patch_loaders(monkeypatch):
    """Patch the three config-as-IP loaders in both surface modules.

    Each module binds its own name via ``from ... import ...``, so both the
    MCP-handler module and the admin-handler module must be patched
    separately (mirrors ``test_procurement_surface.py``'s
    ``_patch_load_config``).
    """
    for mod_path, fixture in (
        ("nce.vertical_modules.economy.mcp_handlers.load_economy_thresholds", _FIXTURE_THRESHOLDS),
        (
            "nce.vertical_modules.economy.mcp_handlers.load_finago_chart_of_accounts",
            _FIXTURE_CHART,
        ),
        (
            "nce.vertical_modules.economy.mcp_handlers.load_finago_account_mapping",
            _FIXTURE_MAPPING,
        ),
        ("nce.admin_handlers.economy.load_economy_thresholds", _FIXTURE_THRESHOLDS),
        ("nce.admin_handlers.economy.load_finago_chart_of_accounts", _FIXTURE_CHART),
        ("nce.admin_handlers.economy.load_finago_account_mapping", _FIXTURE_MAPPING),
    ):
        monkeypatch.setattr(mod_path, (lambda f=fixture: f))


def _make_engine() -> MagicMock:
    return MagicMock()


def _make_request(body: dict[str, Any] | None = None) -> MagicMock:
    """Minimal Starlette-like request mock (mirrors test_procurement_sync_routes.py)."""
    req = MagicMock()
    req.json = AsyncMock(return_value=body or {})
    return req


# ---------------------------------------------------------------------------
# 1. Package imports
# ---------------------------------------------------------------------------


def test_package_imports() -> None:
    import nce.admin_handlers.economy  # noqa: F401
    import nce.vertical_modules.economy.mcp_handlers  # noqa: F401


# ---------------------------------------------------------------------------
# 2. Tool registry — flags
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool_name",
    ["economy_match_invoice", "economy_compute_periodisering", "economy_emit_event"],
)
def test_economy_tools_registered_with_correct_flags(tool_name: str) -> None:
    from nce.tool_registry import TOOL_REGISTRY

    assert tool_name in TOOL_REGISTRY, f"{tool_name!r} not found in TOOL_REGISTRY"
    spec = TOOL_REGISTRY[tool_name]
    assert spec.cacheable is True
    assert spec.admin_only is False
    assert spec.mutation is False
    assert spec.migration is False


# ---------------------------------------------------------------------------
# 6. Tool-count assertion
# ---------------------------------------------------------------------------


def test_tool_count_includes_economy_tools() -> None:
    from nce.tool_registry import TOOL_REGISTRY

    assert "economy_match_invoice" in TOOL_REGISTRY
    assert "economy_compute_periodisering" in TOOL_REGISTRY
    assert "economy_emit_event" in TOOL_REGISTRY
    assert len(TOOL_REGISTRY) >= 112, (
        f"Expected at least 112 tools (+3 economy from Batch 119), "
        f"got {len(TOOL_REGISTRY)}: {sorted(TOOL_REGISTRY)}"
    )


# ---------------------------------------------------------------------------
# 3. handle_* returns valid JSON for a good payload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_match_invoice_returns_valid_json() -> None:
    from nce.vertical_modules.economy.mcp_handlers import handle_economy_match_invoice

    engine = _make_engine()
    result = await handle_economy_match_invoice(
        engine,
        {"namespace_id": _NAMESPACE_ID, "invoice": _INVOICE, "candidates": _CANDIDATES},
    )
    parsed = json.loads(result)
    assert "error" not in parsed
    assert "score" in parsed
    assert "tier" in parsed
    assert "breakdown" in parsed
    assert len(parsed["breakdown"]) == 1


@pytest.mark.asyncio
async def test_handle_compute_periodisering_returns_valid_json() -> None:
    from nce.vertical_modules.economy.mcp_handlers import handle_economy_compute_periodisering

    engine = _make_engine()
    result = await handle_economy_compute_periodisering(
        engine,
        {"namespace_id": _NAMESPACE_ID, "params": _PERIODISERING_PARAMS},
    )
    parsed = json.loads(result)
    assert "error" not in parsed
    assert "buckets" in parsed
    assert len(parsed["buckets"]) == 7
    assert "totals" in parsed


@pytest.mark.asyncio
async def test_handle_emit_event_returns_valid_json() -> None:
    from nce.vertical_modules.economy.mcp_handlers import handle_economy_emit_event

    engine = _make_engine()
    result = await handle_economy_emit_event(
        engine,
        {"namespace_id": _NAMESPACE_ID, "event": _BALANCED_EVENT},
    )
    parsed = json.loads(result)
    assert "error" not in parsed
    assert "hash" in parsed
    assert len(parsed["hash"]) == 64
    assert [p["account"] for p in parsed["postings"]] == ["4300", "2400"]


# ---------------------------------------------------------------------------
# 4. handle_* returns {"error": ...} for missing namespace_id (never raises)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_match_invoice_missing_namespace_id() -> None:
    from nce.vertical_modules.economy.mcp_handlers import handle_economy_match_invoice

    engine = _make_engine()
    result = await handle_economy_match_invoice(
        engine, {"invoice": _INVOICE, "candidates": _CANDIDATES}
    )
    parsed = json.loads(result)
    assert "error" in parsed


@pytest.mark.asyncio
async def test_handle_compute_periodisering_missing_namespace_id() -> None:
    from nce.vertical_modules.economy.mcp_handlers import handle_economy_compute_periodisering

    engine = _make_engine()
    result = await handle_economy_compute_periodisering(engine, {"params": _PERIODISERING_PARAMS})
    parsed = json.loads(result)
    assert "error" in parsed


@pytest.mark.asyncio
async def test_handle_emit_event_missing_namespace_id() -> None:
    from nce.vertical_modules.economy.mcp_handlers import handle_economy_emit_event

    engine = _make_engine()
    result = await handle_economy_emit_event(engine, {"event": _BALANCED_EVENT})
    parsed = json.loads(result)
    assert "error" in parsed


# ---------------------------------------------------------------------------
# 5. handle_economy_emit_event — unbalanced event -> structured error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_emit_event_unbalanced_returns_structured_error() -> None:
    from nce.vertical_modules.economy.mcp_handlers import handle_economy_emit_event

    engine = _make_engine()
    result = await handle_economy_emit_event(
        engine,
        {"namespace_id": _NAMESPACE_ID, "event": _UNBALANCED_EVENT},
    )
    parsed = json.loads(result)
    assert "error" in parsed
    assert parsed["event_type"] == "supplier.invoice.approved"
    assert "diff" in parsed
    assert "tolerance" in parsed
    # Never auto-balanced / repaired: no "hash" or "postings" key on the
    # error path (money-module briefing #7).
    assert "hash" not in parsed


# ---------------------------------------------------------------------------
# 7. REST routes — mounted + same shape as the cores
# ---------------------------------------------------------------------------


def test_economy_routes_mounted_in_admin_app() -> None:
    from nce.admin_app import build_admin_routes

    routes = build_admin_routes()
    paths = {r.path for r in routes}
    assert "/api/economy/match-invoice" in paths
    assert "/api/economy/periodisering" in paths
    assert "/api/economy/emit-event" in paths


@pytest.mark.asyncio
async def test_api_economy_match_invoice_returns_ok_shape() -> None:
    from nce import admin_state
    from nce.admin_handlers.economy import api_economy_match_invoice

    with patch.object(admin_state, "engine", MagicMock()):
        req = _make_request(
            {"namespace_id": _NAMESPACE_ID, "invoice": _INVOICE, "candidates": _CANDIDATES}
        )
        resp = await api_economy_match_invoice(req)
        body = json.loads(bytes(resp.body).decode("utf-8"))
        assert resp.status_code == 200
        assert body["status"] == "ok"
        assert "score" in body
        assert "breakdown" in body


@pytest.mark.asyncio
async def test_api_economy_periodisering_returns_ok_shape() -> None:
    from nce import admin_state
    from nce.admin_handlers.economy import api_economy_periodisering

    with patch.object(admin_state, "engine", MagicMock()):
        req = _make_request({"namespace_id": _NAMESPACE_ID, "params": _PERIODISERING_PARAMS})
        resp = await api_economy_periodisering(req)
        body = json.loads(bytes(resp.body).decode("utf-8"))
        assert resp.status_code == 200
        assert body["status"] == "ok"
        assert len(body["buckets"]) == 7


@pytest.mark.asyncio
async def test_api_economy_emit_event_returns_ok_shape() -> None:
    from nce import admin_state
    from nce.admin_handlers.economy import api_economy_emit_event

    with patch.object(admin_state, "engine", MagicMock()):
        req = _make_request({"namespace_id": _NAMESPACE_ID, "event": _BALANCED_EVENT})
        resp = await api_economy_emit_event(req)
        body = json.loads(bytes(resp.body).decode("utf-8"))
        assert resp.status_code == 200
        assert body["status"] == "ok"
        assert len(body["hash"]) == 64


@pytest.mark.asyncio
async def test_api_economy_emit_event_unbalanced_returns_422() -> None:
    from nce import admin_state
    from nce.admin_handlers.economy import api_economy_emit_event

    with patch.object(admin_state, "engine", MagicMock()):
        req = _make_request({"namespace_id": _NAMESPACE_ID, "event": _UNBALANCED_EVENT})
        resp = await api_economy_emit_event(req)
        body = json.loads(bytes(resp.body).decode("utf-8"))
        assert resp.status_code == 422
        assert "error" in body
        assert body["event_type"] == "supplier.invoice.approved"


@pytest.mark.asyncio
async def test_api_economy_routes_no_engine() -> None:
    from nce import admin_state
    from nce.admin_handlers.economy import (
        api_economy_emit_event,
        api_economy_match_invoice,
        api_economy_periodisering,
    )

    with patch.object(admin_state, "engine", None):
        for fn, body in (
            (api_economy_match_invoice, {"namespace_id": _NAMESPACE_ID}),
            (api_economy_periodisering, {"namespace_id": _NAMESPACE_ID}),
            (api_economy_emit_event, {"namespace_id": _NAMESPACE_ID}),
        ):
            resp = await fn(_make_request(body))
            assert resp.status_code == 503
            assert "Engine not connected" in json.loads(bytes(resp.body).decode("utf-8"))["error"]


# ---------------------------------------------------------------------------
# 8. Regression tests — Batch 119 fix-forward Fixes 1-3
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_economy_emit_event_status_field_cannot_override_envelope() -> None:
    """Fix 1 regression, superseded by round 3: a balanced event that itself carries a
    'status' key must not overwrite the response envelope's own 'status': 'ok'. Round 2
    only stopped the override (the request still succeeded, envelope wins the dict-spread
    order); round 3 tightens this further and rejects the request outright via
    ``_RESERVED_EVENT_KEYS`` (see ``test_rest_emit_event_status_key_rejected`` for the
    dedicated regression), since letting an "error"-shaped sibling through unrejected was
    exactly the adjacent hole round 3 closes. Goes RED if the reserved-key guard is
    removed and the envelope dict-literal is reverted to
    ``{"status": "ok", **_json_safe(result)}`` (caller key first -> caller wins)."""
    from nce import admin_state
    from nce.admin_handlers.economy import api_economy_emit_event

    poisoned_event = {
        "type": "invoice.posted",
        "status": "TOTALLY_NOT_OK_INJECTED",
        "postings": [
            {"account": "4300", "amount": 100_000.0},
            {"account": "2400", "amount": -100_000.0},
        ],
    }
    with patch.object(admin_state, "engine", MagicMock()):
        req = _make_request({"namespace_id": _NAMESPACE_ID, "event": poisoned_event})
        resp = await api_economy_emit_event(req)
        body = json.loads(bytes(resp.body).decode("utf-8"))
        assert resp.status_code == 422
        assert "'status'" in body["error"]
        assert "hash" not in body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fn_name", "field", "scalar"),
    [
        ("api_economy_match_invoice", "invoice", 42),
        ("api_economy_match_invoice", "candidates", True),
        ("api_economy_periodisering", "params", "not-a-dict"),
        ("api_economy_emit_event", "event", 3.14),
    ],
)
async def test_rest_routes_return_422_not_crash_on_malformed_scalar_field(
    fn_name: str, field: str, scalar: Any
) -> None:
    """Fix 2 regression: a JSON scalar for invoice/candidates/params/event must return
    a structured 422, never an uncaught ``TypeError`` through Starlette's
    ``ServerErrorMiddleware``. Goes RED if the ``dict(...)``/``list(...)`` coercion is
    moved back outside its route's ``try`` block, or if ``TypeError`` is dropped from
    the ``except`` tuple."""
    import nce.admin_handlers.economy as economy_mod
    from nce import admin_state

    fn = getattr(economy_mod, fn_name)
    with patch.object(admin_state, "engine", MagicMock()):
        req = _make_request({"namespace_id": _NAMESPACE_ID, field: scalar})
        resp = await fn(req)
        body = json.loads(bytes(resp.body).decode("utf-8"))
        assert resp.status_code == 422, f"expected a structured 422, got {resp.status_code}: {body}"
        assert "error" in body


@pytest.mark.asyncio
async def test_api_economy_match_invoice_nan_candidate_id_serializes_successfully() -> None:
    """Fix 3 regression (REST): a NaN echoed into the breakdown (a poisoned
    ``candidate_id``) must be neutralised to a JSON string, not crash Starlette's
    ``allow_nan=False`` encoder and get mis-filed as a 422 domain-validation error.
    Goes RED if ``_json_safe`` stops neutralising non-finite floats."""
    from nce import admin_state
    from nce.admin_handlers.economy import api_economy_match_invoice

    candidates = [{**_CANDIDATES[0], "candidate_id": float("nan")}]
    with patch.object(admin_state, "engine", MagicMock()):
        req = _make_request(
            {"namespace_id": _NAMESPACE_ID, "invoice": _INVOICE, "candidates": candidates}
        )
        resp = await api_economy_match_invoice(req)
        body = json.loads(bytes(resp.body).decode("utf-8"))
        assert resp.status_code == 200
        assert body["status"] == "ok"
        assert body["breakdown"][0]["candidate_id"] == "nan"


@pytest.mark.asyncio
async def test_api_economy_periodisering_nan_period_end_serializes_successfully() -> None:
    """Fix 3 regression (REST): a NaN ``period_end`` (echoed unchanged by the core)
    must serialize, not crash the response encoder and get mis-filed as an invalid
    invoice."""
    from nce import admin_state
    from nce.admin_handlers.economy import api_economy_periodisering

    params = {**_PERIODISERING_PARAMS, "period_end": float("nan")}
    with patch.object(admin_state, "engine", MagicMock()):
        req = _make_request({"namespace_id": _NAMESPACE_ID, "params": params})
        resp = await api_economy_periodisering(req)
        body = json.loads(bytes(resp.body).decode("utf-8"))
        assert resp.status_code == 200
        assert body["status"] == "ok"
        assert body["period_end"] == "nan"


@pytest.mark.asyncio
async def test_handle_match_invoice_nan_candidate_id_reports_serialization_error() -> None:
    """Fix 3 regression (MCP): a NaN ``candidate_id`` must produce a distinguishable
    serialization-failure error, never a bare invalid ``NaN`` token (RFC 8259) and
    never conflated with a domain-validation message ("your invoice is invalid").
    Goes RED if ``allow_nan=False`` is dropped from the MCP handler's final
    ``json.dumps``."""
    from nce.vertical_modules.economy.mcp_handlers import handle_economy_match_invoice

    candidates = [{**_CANDIDATES[0], "candidate_id": float("nan")}]
    engine = _make_engine()
    result = await handle_economy_match_invoice(
        engine,
        {"namespace_id": _NAMESPACE_ID, "invoice": _INVOICE, "candidates": candidates},
    )
    parsed = json.loads(result)
    assert "error" in parsed
    assert "score" not in parsed
    assert "cannot be serialized" in parsed["error"]


# ---------------------------------------------------------------------------
# 9. Fix 4 — containment pinning: config-as-IP must never be caller-supplied
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_match_invoice_thresholds_override_ignored(monkeypatch) -> None:
    """A caller-supplied ``thresholds`` key in MCP arguments must never influence the
    verdict — only ``load_economy_thresholds()`` governs it. Pins the containment
    property Batch 116 handed forward with ZERO test coverage: an auditor
    demonstrated an ``arguments.get("thresholds") or load_economy_thresholds()``
    backdoor passes all 18 of Batch 119's original tests. Goes RED the moment such a
    fallback is added."""
    from nce.vertical_modules.economy import mcp_handlers

    monkeypatch.setattr(mcp_handlers, "load_economy_thresholds", lambda: _TIGHT_THRESHOLDS)

    engine = _make_engine()
    result = await mcp_handlers.handle_economy_match_invoice(
        engine,
        {
            "namespace_id": _NAMESPACE_ID,
            "invoice": _INVOICE,
            "candidates": _CANDIDATES,
            "thresholds": _POISONED_LOOSE_THRESHOLDS,
        },
    )
    parsed = json.loads(result)
    assert "error" not in parsed
    assert parsed["tier"] == "RED", (
        "caller-supplied 'thresholds' took effect -- config-as-IP containment breached"
    )


@pytest.mark.asyncio
async def test_rest_match_invoice_thresholds_override_ignored(monkeypatch) -> None:
    """REST-surface twin of the MCP thresholds-containment test above."""
    from nce import admin_state
    from nce.admin_handlers import economy as economy_mod

    monkeypatch.setattr(economy_mod, "load_economy_thresholds", lambda: _TIGHT_THRESHOLDS)

    with patch.object(admin_state, "engine", MagicMock()):
        req = _make_request(
            {
                "namespace_id": _NAMESPACE_ID,
                "invoice": _INVOICE,
                "candidates": _CANDIDATES,
                "thresholds": _POISONED_LOOSE_THRESHOLDS,
            }
        )
        resp = await economy_mod.api_economy_match_invoice(req)
        body = json.loads(bytes(resp.body).decode("utf-8"))
        assert resp.status_code == 200
        assert body["tier"] == "RED", (
            "caller-supplied 'thresholds' took effect -- config-as-IP containment breached"
        )


@pytest.mark.asyncio
async def test_mcp_periodisering_chart_and_mapping_override_ignored(monkeypatch) -> None:
    """A caller-supplied ``chart``/``mapping`` key in MCP arguments must never be
    read — only ``load_finago_chart_of_accounts()``/``load_finago_account_mapping()``
    govern the posted accounts. Goes RED if a handler adds
    ``chart = arguments.get("chart") or load_finago_chart_of_accounts()`` (or the
    ``mapping`` equivalent)."""
    from nce.vertical_modules.economy import mcp_handlers

    monkeypatch.setattr(mcp_handlers, "load_finago_chart_of_accounts", lambda: _ALT_CHART)
    monkeypatch.setattr(mcp_handlers, "load_finago_account_mapping", lambda: _ALT_MAPPING)

    engine = _make_engine()
    result = await mcp_handlers.handle_economy_compute_periodisering(
        engine,
        {
            "namespace_id": _NAMESPACE_ID,
            "params": _PERIODISERING_PARAMS,
            "chart": _FIXTURE_CHART,
            "mapping": _FIXTURE_MAPPING,
        },
    )
    parsed = json.loads(result)
    assert "error" not in parsed
    assert parsed["country"] == "ALT-COUNTRY"
    assert parsed["gaap"] == "ALT-GAAP"
    assert parsed["buckets"][0]["accounts"]["cogs"]["mva_code"] == 9


@pytest.mark.asyncio
async def test_rest_periodisering_chart_and_mapping_override_ignored(monkeypatch) -> None:
    """REST-surface twin of the MCP chart/mapping-containment test above."""
    from nce import admin_state
    from nce.admin_handlers import economy as economy_mod

    monkeypatch.setattr(economy_mod, "load_finago_chart_of_accounts", lambda: _ALT_CHART)
    monkeypatch.setattr(economy_mod, "load_finago_account_mapping", lambda: _ALT_MAPPING)

    with patch.object(admin_state, "engine", MagicMock()):
        req = _make_request(
            {
                "namespace_id": _NAMESPACE_ID,
                "params": _PERIODISERING_PARAMS,
                "chart": _FIXTURE_CHART,
                "mapping": _FIXTURE_MAPPING,
            }
        )
        resp = await economy_mod.api_economy_periodisering(req)
        body = json.loads(bytes(resp.body).decode("utf-8"))
        assert resp.status_code == 200
        assert body["country"] == "ALT-COUNTRY"
        assert body["gaap"] == "ALT-GAAP"
        assert body["buckets"][0]["accounts"]["cogs"]["mva_code"] == 9


@pytest.mark.asyncio
async def test_mcp_emit_event_epsilon_override_ignored() -> None:
    """A caller-supplied ``epsilon`` key in MCP arguments must never widen the
    balance tolerance — only ``_BALANCE_EPSILON_DEFAULT`` (0.01) governs it. Goes RED
    if a handler adds ``epsilon = arguments.get("epsilon", _BALANCE_EPSILON_DEFAULT)``:
    a poisoned tolerance of 999,999 would then "balance" an event whose postings are
    99,999 NOK apart."""
    from nce.vertical_modules.economy.mcp_handlers import handle_economy_emit_event

    engine = _make_engine()
    result = await handle_economy_emit_event(
        engine,
        {
            "namespace_id": _NAMESPACE_ID,
            "event": _UNBALANCED_EVENT,
            "epsilon": _POISONED_HIGH_EPSILON,
        },
    )
    parsed = json.loads(result)
    assert "error" in parsed, "caller-supplied 'epsilon' took effect -- containment breached"
    assert parsed["event_type"] == "supplier.invoice.approved"


@pytest.mark.asyncio
async def test_rest_emit_event_epsilon_override_ignored() -> None:
    """REST-surface twin of the MCP epsilon-containment test above."""
    from nce import admin_state
    from nce.admin_handlers.economy import api_economy_emit_event

    with patch.object(admin_state, "engine", MagicMock()):
        req = _make_request(
            {
                "namespace_id": _NAMESPACE_ID,
                "event": _UNBALANCED_EVENT,
                "epsilon": _POISONED_HIGH_EPSILON,
            }
        )
        resp = await api_economy_emit_event(req)
        body = json.loads(bytes(resp.body).decode("utf-8"))
        assert resp.status_code == 422, (
            "caller-supplied 'epsilon' took effect -- containment breached"
        )
        assert body["event_type"] == "supplier.invoice.approved"


# ---------------------------------------------------------------------------
# 10. Fix 5 (round 3) — a caller-supplied "error" (or "status") key inside the
# event must be REJECTED, not echoed by do_emit_financial_event's documented
# dict(event) passthrough (events.py:493) into a result that looks like both a
# success (has "hash") and a failure (has "error") at once. Only an EXACT match
# on the reserved key set is rejected -- a merely similar key must still be
# accepted, pinning the deliberate no-normalisation decision.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rest_emit_event_error_key_rejected() -> None:
    """A balanced event carrying a caller-supplied 'error' key must be rejected
    with a structured 422 naming the key, and the response must NOT contain a
    hash or otherwise look like a success. Goes RED if the
    ``_RESERVED_EVENT_KEYS`` guard in ``api_economy_emit_event`` is removed or
    if 'error' is dropped from the reserved set."""
    from nce import admin_state
    from nce.admin_handlers.economy import api_economy_emit_event

    poisoned_event = {**_BALANCED_EVENT, "error": "FAKE_INJECTED_ERROR"}
    with patch.object(admin_state, "engine", MagicMock()):
        req = _make_request({"namespace_id": _NAMESPACE_ID, "event": poisoned_event})
        resp = await api_economy_emit_event(req)
        body = json.loads(bytes(resp.body).decode("utf-8"))
        assert resp.status_code == 422
        assert "'error'" in body["error"]
        assert "hash" not in body
        assert "status" not in body


@pytest.mark.asyncio
async def test_rest_emit_event_status_key_rejected() -> None:
    """Twin of the above for a caller-supplied 'status' key: round 2 already stops
    it from overwriting the envelope's own 'status': 'ok', but round 3 requires
    the request be REJECTED outright rather than silently let through with the
    caller's key discarded. Goes RED if the guard is removed."""
    from nce import admin_state
    from nce.admin_handlers.economy import api_economy_emit_event

    poisoned_event = {**_BALANCED_EVENT, "status": "TOTALLY_NOT_OK_INJECTED"}
    with patch.object(admin_state, "engine", MagicMock()):
        req = _make_request({"namespace_id": _NAMESPACE_ID, "event": poisoned_event})
        resp = await api_economy_emit_event(req)
        body = json.loads(bytes(resp.body).decode("utf-8"))
        assert resp.status_code == 422
        assert "'status'" in body["error"]
        assert "hash" not in body


@pytest.mark.asyncio
async def test_mcp_emit_event_error_key_rejected() -> None:
    """MCP-surface twin: a caller-supplied 'error' key must be rejected before
    the core runs, so the result can never carry 'error' and 'hash'
    simultaneously -- the MCP surface has no 'status' field, so the presence
    of 'error' alone is the caller's sole failure signal. Goes RED if the
    ``_RESERVED_EVENT_KEYS`` guard in ``handle_economy_emit_event`` is
    removed or if 'error' is dropped from the reserved set."""
    from nce.vertical_modules.economy.mcp_handlers import handle_economy_emit_event

    poisoned_event = {**_BALANCED_EVENT, "error": "FAKE_INJECTED_ERROR"}
    engine = _make_engine()
    result = await handle_economy_emit_event(
        engine, {"namespace_id": _NAMESPACE_ID, "event": poisoned_event}
    )
    parsed = json.loads(result)
    assert "error" in parsed
    assert "hash" not in parsed
    assert "'error'" in parsed["error"]


@pytest.mark.asyncio
async def test_mcp_emit_event_status_key_rejected() -> None:
    """MCP-surface twin of the 'status'-rejection test above."""
    from nce.vertical_modules.economy.mcp_handlers import handle_economy_emit_event

    poisoned_event = {**_BALANCED_EVENT, "status": "TOTALLY_NOT_OK_INJECTED"}
    engine = _make_engine()
    result = await handle_economy_emit_event(
        engine, {"namespace_id": _NAMESPACE_ID, "event": poisoned_event}
    )
    parsed = json.loads(result)
    assert "error" in parsed
    assert "hash" not in parsed
    assert "'status'" in parsed["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("near_miss_key", ["Status", "status ", "errors", "_error"])
async def test_rest_emit_event_near_miss_keys_still_accepted(near_miss_key: str) -> None:
    """Pins the deliberate EXACT-match decision: a key that merely resembles a
    reserved one ('Status', 'status ' with trailing whitespace, 'errors',
    '_error') is NOT reserved and must still be accepted and echoed through
    unchanged. Goes RED if the guard is changed to lowercase/strip/substring-
    match the caller's keys instead of comparing them exactly."""
    from nce import admin_state
    from nce.admin_handlers.economy import api_economy_emit_event

    near_miss_event = {**_BALANCED_EVENT, near_miss_key: "not-a-reserved-key"}
    with patch.object(admin_state, "engine", MagicMock()):
        req = _make_request({"namespace_id": _NAMESPACE_ID, "event": near_miss_event})
        resp = await api_economy_emit_event(req)
        body = json.loads(bytes(resp.body).decode("utf-8"))
        assert resp.status_code == 200
        assert body["status"] == "ok"
        assert body[near_miss_key] == "not-a-reserved-key"
        assert len(body["hash"]) == 64


@pytest.mark.asyncio
@pytest.mark.parametrize("near_miss_key", ["Status", "status ", "errors", "_error"])
async def test_mcp_emit_event_near_miss_keys_still_accepted(near_miss_key: str) -> None:
    """MCP-surface twin of the near-miss-acceptance test above."""
    from nce.vertical_modules.economy.mcp_handlers import handle_economy_emit_event

    near_miss_event = {**_BALANCED_EVENT, near_miss_key: "not-a-reserved-key"}
    engine = _make_engine()
    result = await handle_economy_emit_event(
        engine, {"namespace_id": _NAMESPACE_ID, "event": near_miss_event}
    )
    parsed = json.loads(result)
    assert "error" not in parsed
    assert parsed[near_miss_key] == "not-a-reserved-key"
    assert len(parsed["hash"]) == 64


@pytest.mark.asyncio
async def test_rest_emit_event_no_reserved_keys_still_succeeds() -> None:
    """Non-regression: a normal balanced event with no reserved keys at all
    must still succeed unchanged."""
    from nce import admin_state
    from nce.admin_handlers.economy import api_economy_emit_event

    with patch.object(admin_state, "engine", MagicMock()):
        req = _make_request({"namespace_id": _NAMESPACE_ID, "event": _BALANCED_EVENT})
        resp = await api_economy_emit_event(req)
        body = json.loads(bytes(resp.body).decode("utf-8"))
        assert resp.status_code == 200
        assert body["status"] == "ok"
        assert len(body["hash"]) == 64


@pytest.mark.asyncio
async def test_mcp_emit_event_no_reserved_keys_still_succeeds() -> None:
    """MCP-surface twin of the no-reserved-keys non-regression test above."""
    from nce.vertical_modules.economy.mcp_handlers import handle_economy_emit_event

    engine = _make_engine()
    result = await handle_economy_emit_event(
        engine, {"namespace_id": _NAMESPACE_ID, "event": _BALANCED_EVENT}
    )
    parsed = json.loads(result)
    assert "error" not in parsed
    assert len(parsed["hash"]) == 64
