"""
tests/unit/test_inventory_refusal_mapping.py
============================================
Acceptance tests for **D38** -- the six Inventory business refusals that used
to surface as ``MCP_INTERNAL_ERROR`` (-32603) on MCP and ``500`` on REST.

``InsufficientAvailableError``, ``OverReleaseError``, ``RmaNotFoundError``,
``RmaAlreadySettledError``, ``RmaNotWeeeScopeError`` and
``LedgerDivergenceError`` are all bare ``Exception`` subclasses, so before this
wave none of them was absorbed by either surface's ``ValueError`` arm and every
one fell to the generic internal-error arm. Delivered that way, *"you tried to
reserve more than is available"* is indistinguishable from *"the backend is
down"*, so the rational client retries -- forever.

What is asserted here
---------------------
1. The mapping table itself: one unique ``reason`` slug per refusal, payloads
   that are JSON primitives, ``Decimal`` as an exact string and never a float.
2. **Both surfaces map every declared refusal** -- MCP to
   ``McpError(-32005)`` with ``data.reason``, REST to ``409`` with ``reason``.
   These are the tests that go RED without the mapping.
3. **Neither surface reaches its generic arm** for a declared refusal. This is
   the assertion that actually encodes D38: a test that only checked "the code
   is -32005" would still pass if a handler grew a bespoke ``except`` that
   returned the wrong shape.
4. A **completeness ratchet**: every bare-``Exception`` refusal class in the
   inventory package is either declared in the mapping or in the explicit
   already-handled set. A seventh refusal added later without a mapping fails
   here rather than reaching a client as a 500.

Pure unit tests -- mocked pool and mocked cores, no database, no Redis.
"""

from __future__ import annotations

import ast
import inspect
import json
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from nce import admin_state
from nce.admin_handlers import inventory as inv_rest
from nce.mcp_errors import MCP_INTERNAL_ERROR, MCP_SCOPE_FORBIDDEN, McpError
from nce.vertical_modules.inventory import mcp_handlers as inv_mcp
from nce.vertical_modules.inventory import refusals as inv_refusals
from nce.vertical_modules.inventory.reconcile import LedgerDivergenceError
from nce.vertical_modules.inventory.refusals import (
    BUSINESS_REFUSALS,
    MCP_BUSINESS_REFUSED,
    REFUSAL_REASONS,
    REST_BUSINESS_REFUSED_STATUS,
    refusal_payload,
    refusal_reason,
)
from nce.vertical_modules.inventory.reservation import (
    InsufficientAvailableError,
    OverReleaseError,
)
from nce.vertical_modules.inventory.rma import (
    RmaAlreadySettledError,
    RmaNotFoundError,
    RmaNotWeeeScopeError,
)
from nce.vertical_modules.inventory.stock import InsufficientStockError

_NAMESPACE_ID = "00000000-0000-4000-8000-000000000001"
_LOCATION_ID = UUID("11111111-1111-4111-8111-111111111111")

# ---------------------------------------------------------------------------
# One realistic instance of each declared refusal.  Decimal and UUID are used
# deliberately: both are unserialisable by a plain ``json.dumps``, which is
# exactly what ``mcp_stdio_rpc.py`` uses to emit ``error.data``.
# ---------------------------------------------------------------------------


def _insufficient_available() -> InsufficientAvailableError:
    return InsufficientAvailableError(
        sku="SKU-1",
        location_id=_LOCATION_ID,
        project_id="PRJ-1",
        requested=Decimal("10.500"),
        on_hand=Decimal("4.000"),
        reserved=Decimal("1.000"),
        blocked=Decimal("0.500"),
    )


def _over_release() -> OverReleaseError:
    return OverReleaseError(
        sku="SKU-2",
        location_id=_LOCATION_ID,
        project_id="PRJ-2",
        requested=Decimal("7.000"),
        currently_reserved=Decimal("2.000"),
    )


def _ledger_divergence() -> LedgerDivergenceError:
    return LedgerDivergenceError(
        [
            {
                "sku": "SKU-3",
                "location_id": _LOCATION_ID,
                "on_hand": Decimal("5.000"),
                "ledger_sum": Decimal("4.000"),
                "difference": Decimal("1.000"),
            }
        ]
    )


#: label -> (factory, expected ``reason`` slug)
_REFUSAL_CASES: dict[str, tuple[Any, str]] = {
    "insufficient_available": (_insufficient_available, "insufficient_available"),
    "over_release": (_over_release, "over_release"),
    "rma_not_found": (lambda: RmaNotFoundError(rma_ref="RMA-1"), "rma_not_found"),
    "rma_already_settled": (
        lambda: RmaAlreadySettledError(rma_ref="RMA-2", stock_movement_state="restocked"),
        "rma_already_settled",
    ),
    "rma_not_weee_scope": (
        lambda: RmaNotWeeeScopeError(rma_ref="RMA-3"),
        "rma_not_weee_scope",
    ),
    "ledger_divergence": (_ledger_divergence, "ledger_divergence"),
}

#: The handlers whose cores can raise a declared refusal.  ``do_record_rma``
#: does NOT call ``_claim_rma``, so ``record_rma`` is deliberately absent.
_MCP_REFUSING_HANDLERS: dict[str, str] = {
    "handle_inventory_reserve_stock": "do_reserve_stock",
    "handle_inventory_release_stock": "do_release_stock",
    "handle_inventory_restock_from_rma": "do_restock_from_rma",
    "handle_inventory_dispose_rma_weee": "do_dispose_rma_weee",
    "handle_inventory_reconcile_dead_stock": "do_reconcile_dead_stock",
}

_REST_REFUSING_ROUTES: dict[str, str] = {
    "api_inventory_reserve_stock": "do_reserve_stock",
    "api_inventory_release_stock": "do_release_stock",
    "api_inventory_restock_from_rma": "do_restock_from_rma",
    "api_inventory_dispose_rma_weee": "do_dispose_rma_weee",
    "api_inventory_reconcile_dead_stock": "do_reconcile_dead_stock",
}

#: Refusal classes in the inventory package that are handled by an OLDER,
#: deliberately different contract -- see ``refusals.py``'s "known remaining
#: asymmetry" note.  Anything not here and not in the mapping is a gap.
_ALREADY_HANDLED_ELSEWHERE = {"InsufficientStockError", "InventoryDisabledError"}

_INVENTORY_PKG = Path(inspect.getsourcefile(inv_mcp)).parent


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


def _enabled_pool() -> MagicMock:
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"inventory_enabled": True})
    pool = MagicMock()
    ctx = pool.acquire.return_value
    ctx.__aenter__.return_value = conn
    ctx.__aexit__.return_value = False
    return pool


def _enabled_engine() -> MagicMock:
    engine = MagicMock()
    engine.pg_pool = _enabled_pool()
    return engine


def _body_request() -> MagicMock:
    req = MagicMock()
    req.json = AsyncMock(return_value={"namespace_id": _NAMESPACE_ID})
    req.query_params = {}
    return req


# ---------------------------------------------------------------------------
# 1. The mapping table
# ---------------------------------------------------------------------------


def test_all_six_refusals_are_declared() -> None:
    assert set(BUSINESS_REFUSALS) == {
        InsufficientAvailableError,
        OverReleaseError,
        RmaNotFoundError,
        RmaAlreadySettledError,
        RmaNotWeeeScopeError,
        LedgerDivergenceError,
    }


def test_reason_slugs_are_unique() -> None:
    slugs = list(REFUSAL_REASONS.values())
    assert len(slugs) == len(set(slugs)) == 6, slugs


@pytest.mark.parametrize("label", sorted(_REFUSAL_CASES))
def test_reason_slug_matches_the_declared_contract(label: str) -> None:
    factory, expected = _REFUSAL_CASES[label]
    assert refusal_reason(factory()) == expected


@pytest.mark.parametrize("label", sorted(_REFUSAL_CASES))
def test_payload_survives_a_plain_json_dumps(label: str) -> None:
    """``mcp_stdio_rpc.py`` emits ``error.data`` with NO ``default=`` hook.

    A ``UUID`` or ``Decimal`` left in the payload would raise ``TypeError``
    inside the error-reporting path, turning a refusal into a crash -- the
    very defect class D38 exists to remove.  So this must hold with no hook.
    """
    factory, _expected = _REFUSAL_CASES[label]
    json.dumps(refusal_payload(factory()))


@pytest.mark.parametrize("label", sorted(_REFUSAL_CASES))
def test_payload_always_carries_error_and_reason(label: str) -> None:
    factory, expected = _REFUSAL_CASES[label]
    exc = factory()
    payload = refusal_payload(exc)
    assert payload["reason"] == expected
    assert payload["error"] == str(exc)


def test_decimal_is_an_exact_string_never_a_float() -> None:
    """Money and NUMERIC(18,3) quantities must never round-trip via float."""
    payload = refusal_payload(_insufficient_available())
    for field in ("requested", "on_hand", "reserved", "blocked", "available"):
        assert isinstance(payload[field], str), (field, payload[field])
        assert not isinstance(payload[field], float)
    assert payload["requested"] == "10.500"
    assert payload["available"] == "2.500"


def test_uuid_is_stringified() -> None:
    payload = refusal_payload(_over_release())
    assert payload["location_id"] == str(_LOCATION_ID)


def test_ledger_divergence_pairs_are_structured_not_a_message() -> None:
    payload = refusal_payload(_ledger_divergence())
    assert isinstance(payload["pairs"], list)
    assert payload["pairs"][0]["sku"] == "SKU-3"
    assert payload["pairs"][0]["difference"] == "1.000"


def test_refusal_reason_rejects_an_undeclared_exception() -> None:
    """A server fault must never be told to the FE as a caller-fixable refusal."""
    with pytest.raises(KeyError):
        refusal_reason(RuntimeError("a real server fault"))
    with pytest.raises(KeyError):
        refusal_reason(
            InsufficientStockError(
                sku="SKU-9",
                location_id=_LOCATION_ID,
                requested=Decimal("1"),
                available_on_hand=Decimal("0"),
            )
        )


# ---------------------------------------------------------------------------
# 2. MCP surface -- RED without the mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("handler_name", sorted(_MCP_REFUSING_HANDLERS))
@pytest.mark.parametrize("label", sorted(_REFUSAL_CASES))
@pytest.mark.asyncio
async def test_mcp_handler_maps_refusal_to_business_refused(handler_name: str, label: str) -> None:
    core = _MCP_REFUSING_HANDLERS[handler_name]
    factory, expected = _REFUSAL_CASES[label]
    engine = _enabled_engine()
    with patch.object(inv_mcp, core, new=AsyncMock(side_effect=factory())):
        with pytest.raises(McpError) as excinfo:
            await getattr(inv_mcp, handler_name)(engine, {"namespace_id": _NAMESPACE_ID})
    err = excinfo.value
    assert err.code == MCP_BUSINESS_REFUSED == MCP_SCOPE_FORBIDDEN == -32005
    assert err.code != MCP_INTERNAL_ERROR
    assert err.data is not None
    assert err.data["reason"] == expected


@pytest.mark.parametrize("handler_name", sorted(_MCP_REFUSING_HANDLERS))
@pytest.mark.parametrize("label", sorted(_REFUSAL_CASES))
@pytest.mark.asyncio
async def test_mcp_refusal_never_reaches_the_internal_error_arm(
    handler_name: str, label: str
) -> None:
    """The assertion that actually encodes D38, stated as the negative."""
    core = _MCP_REFUSING_HANDLERS[handler_name]
    factory, _expected = _REFUSAL_CASES[label]
    engine = _enabled_engine()
    with patch.object(inv_mcp, core, new=AsyncMock(side_effect=factory())):
        with pytest.raises(McpError) as excinfo:
            await getattr(inv_mcp, handler_name)(engine, {"namespace_id": _NAMESPACE_ID})
    assert excinfo.value.code != -32603


@pytest.mark.parametrize("handler_name", sorted(_MCP_REFUSING_HANDLERS))
@pytest.mark.parametrize("label", sorted(_REFUSAL_CASES))
@pytest.mark.asyncio
async def test_mcp_refusal_error_data_is_wire_serialisable(handler_name: str, label: str) -> None:
    core = _MCP_REFUSING_HANDLERS[handler_name]
    factory, _expected = _REFUSAL_CASES[label]
    engine = _enabled_engine()
    with patch.object(inv_mcp, core, new=AsyncMock(side_effect=factory())):
        with pytest.raises(McpError) as excinfo:
            await getattr(inv_mcp, handler_name)(engine, {"namespace_id": _NAMESPACE_ID})
    # No default= hook, exactly as mcp_stdio_rpc.py does it.
    json.dumps({"jsonrpc": "2.0", "error": {"data": excinfo.value.data}})


# ---------------------------------------------------------------------------
# 3. REST surface -- RED without the mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route_name", sorted(_REST_REFUSING_ROUTES))
@pytest.mark.parametrize("label", sorted(_REFUSAL_CASES))
@pytest.mark.asyncio
async def test_rest_route_maps_refusal_to_409(route_name: str, label: str) -> None:
    core = _REST_REFUSING_ROUTES[route_name]
    factory, expected = _REFUSAL_CASES[label]
    with patch.object(admin_state, "engine", _enabled_engine()):
        with patch.object(inv_rest, core, new=AsyncMock(side_effect=factory())):
            resp = await getattr(inv_rest, route_name)(_body_request())
    assert resp.status_code == REST_BUSINESS_REFUSED_STATUS == 409, (
        route_name,
        label,
        resp.status_code,
    )
    body = json.loads(resp.body)
    assert body["reason"] == expected


@pytest.mark.parametrize("route_name", sorted(_REST_REFUSING_ROUTES))
@pytest.mark.parametrize("label", sorted(_REFUSAL_CASES))
@pytest.mark.asyncio
async def test_rest_refusal_is_never_a_500(route_name: str, label: str) -> None:
    core = _REST_REFUSING_ROUTES[route_name]
    factory, _expected = _REFUSAL_CASES[label]
    with patch.object(admin_state, "engine", _enabled_engine()):
        with patch.object(inv_rest, core, new=AsyncMock(side_effect=factory())):
            resp = await getattr(inv_rest, route_name)(_body_request())
    assert resp.status_code != 500, (route_name, label)
    assert 400 <= resp.status_code < 500, (route_name, label)


# ---------------------------------------------------------------------------
# 4. Ratchets -- one shared mapping, and no undeclared refusal
# ---------------------------------------------------------------------------


def test_every_refusing_handler_delegates_to_the_shared_tuple() -> None:
    """D18 precedent: ONE shared mapping, not a bespoke ``except`` per site."""
    for name in sorted(_MCP_REFUSING_HANDLERS):
        src = inspect.getsource(getattr(inv_mcp, name))
        assert "BUSINESS_REFUSALS" in src, name
    for name in sorted(_REST_REFUSING_ROUTES):
        src = inspect.getsource(getattr(inv_rest, name))
        assert "BUSINESS_REFUSALS" in src, name


def test_no_surface_names_an_individual_refusal_class() -> None:
    """A sixth bespoke ``except InsufficientAvailableError`` is the defect."""
    for module in (inv_mcp, inv_rest):
        src = inspect.getsource(module)
        for cls in BUSINESS_REFUSALS:
            assert f"except {cls.__name__}" not in src, (module.__name__, cls.__name__)


def test_every_bare_exception_refusal_in_the_package_is_declared() -> None:
    """Completeness ratchet -- a SEVENTH refusal cannot be added silently.

    Derived from the package's own source, so a new
    ``class SomethingError(Exception)`` in any inventory module fails here
    until it is either mapped or explicitly listed as handled elsewhere.
    """
    declared = {cls.__name__ for cls in BUSINESS_REFUSALS}
    found: set[str] = set()
    for path in sorted(_INVENTORY_PKG.glob("*.py")):
        tree = ast.parse(path.read_bytes().decode("utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            bases = {b.id for b in node.bases if isinstance(b, ast.Name)}
            if "Exception" in bases:
                found.add(node.name)
    undeclared = found - declared - _ALREADY_HANDLED_ELSEWHERE
    assert not undeclared, (
        f"undeclared Inventory refusal(s): {sorted(undeclared)} -- add each to "
        f"refusals._REFUSALS (so BOTH surfaces map it) or to "
        f"_ALREADY_HANDLED_ELSEWHERE with the contract that covers it"
    )
    # And the mapping must not declare a class that no longer exists.
    assert declared <= found, sorted(declared - found)


def test_the_mapping_module_is_the_only_place_the_code_is_chosen() -> None:
    """Neither surface may hard-code the refusal code/status of its own."""
    assert inv_refusals.MCP_BUSINESS_REFUSED == -32005
    assert inv_refusals.REST_BUSINESS_REFUSED_STATUS == 409
    for name in sorted(_REST_REFUSING_ROUTES):
        src = inspect.getsource(getattr(inv_rest, name))
        assert "REST_BUSINESS_REFUSED_STATUS" in src, name
