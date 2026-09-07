"""
tests/unit/test_economy_peppol_surface.py
=========================================
Acceptance and contract tests for MLV15D Wave E-2: Economy PEPPOL & Validation surface.

Covers:
  1. Registry metadata in TOOL_REGISTRY (cacheable, admin_only, mutation).
  2. Discoverability & JSON Schema in mcp_stdio_tools.TOOLS.
  3. MCP handler execution:
     - handle_economy_generate_kid (MOD10 check digit, leading zeros, lengths, error handling)
     - handle_economy_validate_kid (valid/invalid KID verification, error handling)
     - handle_economy_generate_ehf (EHF 3.0 UBL XML building, safety interlock when disabled)
     - handle_economy_validate_contract (CPI cap enforcement, renewal calculation, missing contract)
     - Economy opt-in enforcement (disabled economy returns refusal)
  4. Admin REST route mounting and HTTP responses:
     - GET/POST /api/economy/kid/generate
     - GET/POST /api/economy/kid/validate
     - POST /api/economy/ehf/generate
     - POST /api/economy/contracts/validate
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nce.admin_app import create_admin_app
from nce.admin_handlers import economy as economy_admin_handlers
from nce.admin_handlers._shared import admin_state
from nce.config import cfg
from nce.mcp_stdio_tools import TOOLS
from nce.tool_registry import (
    ADMIN_ONLY_TOOLS,
    CACHEABLE_TOOLS,
    MUTATION_TOOLS,
    TOOL_REGISTRY,
)
from nce.vertical_modules.economy.mcp_handlers import (
    handle_economy_generate_ehf,
    handle_economy_generate_kid,
    handle_economy_validate_contract,
    handle_economy_validate_kid,
)

_NAMESPACE_ID = "00000000-0000-4000-8000-000000000001"

_VALID_INVOICE: dict[str, Any] = {
    "invoice_id": "INV-2026-0042",
    "issue_date": "2026-08-01",
    "currency_code": "NOK",
    "payable_amount": 1234.5,
    "supplier_peppol_id": "0192:987654321",
    "buyer_peppol_id": "0192:123456789",
    "kid": "79927398713",
}


class _AsyncCtx:
    def __init__(self, val: Any) -> None:
        self.val = val

    async def __aenter__(self) -> Any:
        return self.val

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        pass


def _make_mock_engine(
    *,
    economy_enabled: bool = True,
    contract_row: dict[str, Any] | None = None,
) -> MagicMock:
    """Mock NCEEngine with asyncpg pool returning specified enabled and contract rows."""

    async def _fetchrow(query: str, *args: Any) -> dict[str, Any] | None:
        if "economy_enabled" in query:
            return {"economy_enabled": economy_enabled}
        if "FROM economy_contracts" in query:
            return contract_row
        return None

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=_fetchrow)
    conn.transaction = MagicMock(return_value=_AsyncCtx(None))
    conn.execute = AsyncMock()

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AsyncCtx(conn))

    engine = MagicMock()
    engine.pg_pool = pool
    return engine


def _make_rest_request(
    body: dict[str, Any] | None = None,
    query_params: dict[str, Any] | None = None,
) -> MagicMock:
    """Mock Starlette HTTP request."""
    req = MagicMock()
    req.json = AsyncMock(return_value=body or {})
    req.query_params = query_params or {}
    return req


# ---------------------------------------------------------------------------
# 1. TOOL_REGISTRY metadata
# ---------------------------------------------------------------------------


def test_wave_e2_tools_registered_in_registry() -> None:
    expected_tools = {
        "economy_generate_kid": {"cacheable": True, "admin_only": False, "mutation": False},
        "economy_validate_kid": {"cacheable": True, "admin_only": False, "mutation": False},
        "economy_generate_ehf": {"cacheable": False, "admin_only": True, "mutation": False},
        "economy_validate_contract": {"cacheable": True, "admin_only": False, "mutation": False},
    }

    for name, flags in expected_tools.items():
        assert name in TOOL_REGISTRY, f"{name} missing from TOOL_REGISTRY"
        spec = TOOL_REGISTRY[name]
        assert spec.cacheable == flags["cacheable"], f"{name} cacheable mismatch"
        assert spec.admin_only == flags["admin_only"], f"{name} admin_only mismatch"
        assert spec.mutation == flags["mutation"], f"{name} mutation mismatch"

        if flags["cacheable"]:
            assert name in CACHEABLE_TOOLS
        else:
            assert name not in CACHEABLE_TOOLS

        if flags["admin_only"]:
            assert name in ADMIN_ONLY_TOOLS
        else:
            assert name not in ADMIN_ONLY_TOOLS

        if flags["mutation"]:
            assert name in MUTATION_TOOLS
        else:
            assert name not in MUTATION_TOOLS


# ---------------------------------------------------------------------------
# 2. mcp_stdio_tools schemas
# ---------------------------------------------------------------------------


def test_wave_e2_tools_in_stdio_tools() -> None:
    tools_by_name = {t.name: t for t in TOOLS}

    for name in (
        "economy_generate_kid",
        "economy_validate_kid",
        "economy_generate_ehf",
        "economy_validate_contract",
    ):
        assert name in tools_by_name, f"{name} missing from TOOLS list"
        tool = tools_by_name[name]
        assert tool.description and len(tool.description) > 20
        schema = tool.inputSchema
        assert schema.get("type") == "object"
        assert "properties" in schema
        assert "namespace_id" in schema["properties"]
        assert "namespace_id" in schema.get("required", [])


# ---------------------------------------------------------------------------
# 3. MCP Handlers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_economy_generate_kid_success() -> None:
    engine = _make_mock_engine(economy_enabled=True)
    raw = await handle_economy_generate_kid(
        engine,
        {"namespace_id": _NAMESPACE_ID, "base_number": "7992739871"},
    )
    result = json.loads(raw)
    assert result["base_number"] == "7992739871"
    assert result["check_digit"] == "3"
    assert result["kid"] == "79927398713"
    assert result["variant"] == "MOD10"


@pytest.mark.asyncio
async def test_handle_economy_generate_kid_preserves_leading_zeros() -> None:
    engine = _make_mock_engine(economy_enabled=True)
    raw = await handle_economy_generate_kid(
        engine,
        {"namespace_id": _NAMESPACE_ID, "base_number": "00012345"},
    )
    result = json.loads(raw)
    assert result["base_number"] == "00012345"
    assert result["kid"].startswith("00012345")


@pytest.mark.asyncio
async def test_handle_economy_generate_kid_refusals() -> None:
    engine = _make_mock_engine(economy_enabled=True)

    # Missing base_number
    res = json.loads(await handle_economy_generate_kid(engine, {"namespace_id": _NAMESPACE_ID}))
    assert "error" in res

    # Non-digit character
    res = json.loads(
        await handle_economy_generate_kid(
            engine, {"namespace_id": _NAMESPACE_ID, "base_number": "123a"}
        )
    )
    assert "error" in res

    # Exceeds 24 digits
    res = json.loads(
        await handle_economy_generate_kid(
            engine, {"namespace_id": _NAMESPACE_ID, "base_number": "1" * 25}
        )
    )
    assert "error" in res

    # MOD11 scheme not implemented
    res = json.loads(
        await handle_economy_generate_kid(
            engine,
            {"namespace_id": _NAMESPACE_ID, "base_number": "7992739871", "variant": "MOD11"},
        )
    )
    assert "error" in res


@pytest.mark.asyncio
async def test_handle_economy_validate_kid_success_and_invalid() -> None:
    engine = _make_mock_engine(economy_enabled=True)

    # Valid KID
    raw_valid = await handle_economy_validate_kid(
        engine,
        {"namespace_id": _NAMESPACE_ID, "kid": "79927398713"},
    )
    res_valid = json.loads(raw_valid)
    assert res_valid["kid"] == "79927398713"
    assert res_valid["valid"] is True
    assert res_valid["variant"] == "MOD10"

    # Invalid check digit
    raw_invalid = await handle_economy_validate_kid(
        engine,
        {"namespace_id": _NAMESPACE_ID, "kid": "79927398710"},
    )
    res_invalid = json.loads(raw_invalid)
    assert res_invalid["kid"] == "79927398710"
    assert res_invalid["valid"] is False


@pytest.mark.asyncio
async def test_handle_economy_validate_kid_refusals() -> None:
    engine = _make_mock_engine(economy_enabled=True)

    # Missing kid
    res = json.loads(await handle_economy_validate_kid(engine, {"namespace_id": _NAMESPACE_ID}))
    assert "error" in res

    # Too short (< 2 digits)
    res = json.loads(
        await handle_economy_validate_kid(engine, {"namespace_id": _NAMESPACE_ID, "kid": "5"})
    )
    assert "error" in res

    # MOD11 not implemented
    res = json.loads(
        await handle_economy_validate_kid(
            engine,
            {"namespace_id": _NAMESPACE_ID, "kid": "79927398713", "variant": "MOD11"},
        )
    )
    assert "error" in res


@pytest.mark.asyncio
async def test_handle_economy_generate_ehf_disabled_safety_interlock() -> None:
    engine = _make_mock_engine(economy_enabled=True)

    # By default NCE_ECONOMY_PEPPOL_ENABLED is False
    with patch.object(cfg, "NCE_ECONOMY_PEPPOL_ENABLED", False):
        raw = await handle_economy_generate_ehf(
            engine,
            {
                "namespace_id": _NAMESPACE_ID,
                "invoice": _VALID_INVOICE,
            },
        )
        result = json.loads(raw)
        assert result["sent"] is False
        assert result["peppol_enabled"] is False
        assert "<Invoice" in result["ehf_xml"]
        assert "INV-2026-0042" in result["ehf_xml"]


@pytest.mark.asyncio
async def test_handle_economy_generate_ehf_refusals() -> None:
    engine = _make_mock_engine(economy_enabled=True)

    # Missing invoice dict
    res = json.loads(await handle_economy_generate_ehf(engine, {"namespace_id": _NAMESPACE_ID}))
    assert "error" in res


@pytest.mark.asyncio
async def test_handle_economy_validate_contract_success() -> None:
    contract_row = {
        "annual_amount": 100_000,
        "cpi_cap": 0.05,
    }
    engine = _make_mock_engine(economy_enabled=True, contract_row=contract_row)

    raw = await handle_economy_validate_contract(
        engine,
        {
            "namespace_id": _NAMESPACE_ID,
            "contract_id": "CTR-1001",
            "proposed_cpi_pct": 0.03,
        },
    )
    result = json.loads(raw)
    assert result["ok"] is True
    assert result["contract_id"] == "CTR-1001"
    assert float(result["proposed_cpi_pct"]) == 0.03
    assert float(result["current_annual_amount"]) == 100000.0
    assert float(result["renewal_annual_amount"]) == 103000.0


@pytest.mark.asyncio
async def test_handle_economy_validate_contract_refusals() -> None:
    # 1. Proposed exceeds cap
    contract_row = {"annual_amount": 100_000, "cpi_cap": 0.05}
    engine = _make_mock_engine(economy_enabled=True, contract_row=contract_row)

    res = json.loads(
        await handle_economy_validate_contract(
            engine,
            {
                "namespace_id": _NAMESPACE_ID,
                "contract_id": "CTR-1001",
                "proposed_cpi_pct": 0.06,
            },
        )
    )
    assert "error" in res
    assert "exceeds this contract's cap" in res["error"]

    # 2. Contract not found
    engine_empty = _make_mock_engine(economy_enabled=True, contract_row=None)
    res = json.loads(
        await handle_economy_validate_contract(
            engine_empty,
            {
                "namespace_id": _NAMESPACE_ID,
                "contract_id": "CTR-NOTFOUND",
                "proposed_cpi_pct": 0.03,
            },
        )
    )
    assert "error" in res
    assert "no contract" in res["error"] and "found in namespace" in res["error"]

    # 3. Missing parameters
    res = json.loads(
        await handle_economy_validate_contract(engine, {"namespace_id": _NAMESPACE_ID})
    )
    assert "error" in res


@pytest.mark.asyncio
async def test_economy_disabled_opt_in_refusal() -> None:
    engine = _make_mock_engine(economy_enabled=False)

    for handler, args in (
        (handle_economy_generate_kid, {"namespace_id": _NAMESPACE_ID, "base_number": "12345"}),
        (handle_economy_validate_kid, {"namespace_id": _NAMESPACE_ID, "kid": "123456"}),
        (handle_economy_generate_ehf, {"namespace_id": _NAMESPACE_ID, "invoice": _VALID_INVOICE}),
        (
            handle_economy_validate_contract,
            {"namespace_id": _NAMESPACE_ID, "contract_id": "CTR-1", "proposed_cpi_pct": 0.02},
        ),
    ):
        with pytest.raises(Exception) as exc_info:
            await handler(engine, args)
        # Opt-in refusal raises McpError with code -32005
        err = exc_info.value
        assert "not enabled" in str(err).lower() or getattr(err, "code", None) == -32005


# ---------------------------------------------------------------------------
# 4. REST Endpoints
# ---------------------------------------------------------------------------


def test_rest_routes_mounted_in_admin_app() -> None:
    app = create_admin_app()
    routes = {r.path: r for r in app.routes if hasattr(r, "path")}

    assert "/api/economy/kid/generate" in routes
    assert "/api/economy/kid/validate" in routes
    assert "/api/economy/ehf/generate" in routes
    assert "/api/economy/contracts/validate" in routes

    assert "GET" in routes["/api/economy/kid/generate"].methods
    assert "POST" in routes["/api/economy/kid/generate"].methods
    assert "GET" in routes["/api/economy/kid/validate"].methods
    assert "POST" in routes["/api/economy/kid/validate"].methods
    assert "POST" in routes["/api/economy/ehf/generate"].methods
    assert "POST" in routes["/api/economy/contracts/validate"].methods


@pytest.mark.asyncio
async def test_api_economy_generate_kid_rest_success() -> None:
    engine = _make_mock_engine(economy_enabled=True)
    admin_state.engine = engine

    # POST
    req_post = _make_rest_request(body={"namespace_id": _NAMESPACE_ID, "base_number": "7992739871"})
    resp = await economy_admin_handlers.api_economy_generate_kid(req_post)
    assert resp.status_code == 200
    data = json.loads(resp.body)
    assert data["status"] == "ok"
    assert data["kid"] == "79927398713"

    # GET
    req_get = _make_rest_request(
        query_params={"namespace_id": _NAMESPACE_ID, "base_number": "7992739871"}
    )
    resp = await economy_admin_handlers.api_economy_generate_kid(req_get)
    assert resp.status_code == 200
    data = json.loads(resp.body)
    assert data["status"] == "ok"
    assert data["kid"] == "79927398713"


@pytest.mark.asyncio
async def test_api_economy_validate_kid_rest_success() -> None:
    engine = _make_mock_engine(economy_enabled=True)
    admin_state.engine = engine

    req = _make_rest_request(body={"namespace_id": _NAMESPACE_ID, "kid": "79927398713"})
    resp = await economy_admin_handlers.api_economy_validate_kid(req)
    assert resp.status_code == 200
    data = json.loads(resp.body)
    assert data["status"] == "ok"
    assert data["valid"] is True


@pytest.mark.asyncio
async def test_api_economy_generate_ehf_rest_success() -> None:
    engine = _make_mock_engine(economy_enabled=True)
    admin_state.engine = engine

    with patch.object(cfg, "NCE_ECONOMY_PEPPOL_ENABLED", False):
        req = _make_rest_request(body={"namespace_id": _NAMESPACE_ID, "invoice": _VALID_INVOICE})
        resp = await economy_admin_handlers.api_economy_generate_ehf(req)
        assert resp.status_code == 200
        data = json.loads(resp.body)
        assert data["status"] == "ok"
        assert "<Invoice" in data["ehf_xml"]


@pytest.mark.asyncio
async def test_api_economy_validate_contract_rest_success() -> None:
    contract_row = {"annual_amount": 200_000, "cpi_cap": 0.05}
    engine = _make_mock_engine(economy_enabled=True, contract_row=contract_row)
    admin_state.engine = engine

    req = _make_rest_request(
        body={
            "namespace_id": _NAMESPACE_ID,
            "contract_id": "CTR-9999",
            "proposed_cpi_pct": 0.04,
        }
    )
    resp = await economy_admin_handlers.api_economy_validate_contract(req)
    assert resp.status_code == 200
    data = json.loads(resp.body)
    assert data["status"] == "ok"
    assert data["contract_id"] == "CTR-9999"
    assert float(data["renewal_annual_amount"]) == 208000.0


@pytest.mark.asyncio
async def test_api_economy_rest_opt_in_disabled() -> None:
    engine = _make_mock_engine(economy_enabled=False)
    admin_state.engine = engine

    req = _make_rest_request(body={"namespace_id": _NAMESPACE_ID, "base_number": "12345"})
    resp = await economy_admin_handlers.api_economy_generate_kid(req)
    assert resp.status_code == 409
