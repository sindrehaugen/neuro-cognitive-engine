"""
tests/unit/test_inventory_optin_gate.py
=========================================
Acceptance tests for Batch 140a -- Module 11.Wave 12a (``inventory-optin-gate``).

Proves the deny-by-default ``metadata.inventory.enabled`` namespace opt-in
gate on BOTH Inventory surfaces:

  * MCP  -- every ``handle_inventory_*`` in
    ``nce/vertical_modules/inventory/mcp_handlers.py`` refuses a namespace
    that has not opted in with a **structured** ``McpError(-32005)``, never a
    200-shaped ``{"error": ...}`` JSON string.
  * REST -- every ``api_inventory_*`` in ``nce/admin_handlers/inventory.py``
    refuses with a **409**, and a malformed ``namespace_id`` gets a clean
    **422** *before* the gate runs (so a raw ``asyncpg`` ``DataError`` can
    never escape the ASGI handler).

Pure unit tests -- mocked pool, no database, no Redis, no ``pytest.mark``
markers.  The fake connection **emulates** the guard's SQL predicate
``COALESCE((metadata->'inventory'->>'enabled')::boolean, false)`` in Python so
that NULL ``metadata``, ``enabled`` as a JSON bool and ``enabled`` as the
literal string ``"false"`` can be told apart at this tier; the emulation is
tied to the real statement by
``test_guard_sql_reads_the_documented_predicate``, which asserts the SQL text
the guard actually sends.

Completeness is a ratchet, not a sample: ``test_every_mcp_handler_is_gated``
and ``test_every_rest_route_is_gated`` derive their sets from the modules
themselves, so a handler or route added later without a gate fails here.
"""

from __future__ import annotations

import inspect
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from asyncpg.exceptions import DataError

from nce import admin_state
from nce.admin_handlers import inventory as inv_rest
from nce.mcp_errors import MCP_INVALID_PARAMS, MCP_SCOPE_FORBIDDEN, McpError
from nce.vertical_modules.inventory import mcp_handlers as inv_mcp
from nce.vertical_modules.inventory._guard import (
    InventoryDisabledError,
    require_inventory_enabled,
)

_NAMESPACE_ID = "00000000-0000-4000-8000-000000000001"
_MALFORMED_NAMESPACE_ID = "not-a-uuid"

# ---------------------------------------------------------------------------
# Handler / route -> core symbol.  Both dicts are checked for completeness
# against the modules themselves below -- never edit one without the other.
# ---------------------------------------------------------------------------

_MCP_HANDLER_CORES: dict[str, str] = {
    "handle_inventory_stock_levels": "do_stock_levels",
    "handle_inventory_transfer_stock": "do_transfer_stock",
    "handle_inventory_record_consumption": "do_record_consumption",
    "handle_inventory_record_goods_receipt": "do_record_goods_receipt",
    "handle_inventory_recommend_restock": "do_recommend_restock",
    "handle_inventory_forecast_demand": "do_forecast_demand",
    "handle_inventory_reserve_stock": "do_reserve_stock",
    "handle_inventory_release_stock": "do_release_stock",
    "handle_inventory_record_rma": "do_record_rma",
    "handle_inventory_valuation": "do_valuation",
    "handle_inventory_record_goods_receipt_and_match": (
        "do_record_goods_receipt_and_evaluate_match"
    ),
    "handle_inventory_reconcile_dead_stock": "do_reconcile_dead_stock",
    "handle_inventory_restock_from_rma": "do_restock_from_rma",
    "handle_inventory_dispose_rma_weee": "do_dispose_rma_weee",
}

# route name -> (core symbol, reads namespace_id from the query string?)
_REST_ROUTE_CORES: dict[str, tuple[str, bool]] = {
    "api_inventory_stock_levels": ("do_stock_levels", True),
    "api_inventory_transfer_stock": ("do_transfer_stock", False),
    "api_inventory_record_consumption": ("do_record_consumption", False),
    "api_inventory_record_goods_receipt": ("do_record_goods_receipt", False),
    "api_inventory_recommend_restock": ("do_recommend_restock", False),
    "api_inventory_forecast_demand": ("do_forecast_demand", False),
    "api_inventory_reserve_stock": ("do_reserve_stock", False),
    "api_inventory_release_stock": ("do_release_stock", False),
    "api_inventory_record_rma": ("do_record_rma", False),
    "api_inventory_valuation": ("do_valuation", True),
    "api_inventory_record_goods_receipt_and_match": (
        "do_record_goods_receipt_and_evaluate_match",
        False,
    ),
    "api_inventory_reconcile_dead_stock": ("do_reconcile_dead_stock", False),
    "api_inventory_restock_from_rma": ("do_restock_from_rma", False),
    "api_inventory_dispose_rma_weee": ("do_dispose_rma_weee", False),
}

# metadata JSONB value -> whether the guard must let the call through.
# Mirrors PostgreSQL's COALESCE((metadata->'inventory'->>'enabled')::boolean, false).
_METADATA_CASES: list[tuple[str, Any, bool]] = [
    ("metadata_null", None, False),
    ("metadata_empty", {}, False),
    ("inventory_key_absent", {"other": {"enabled": True}}, False),
    ("enabled_key_absent", {"inventory": {}}, False),
    ("enabled_json_false", {"inventory": {"enabled": False}}, False),
    ("enabled_string_false", {"inventory": {"enabled": "false"}}, False),
    ("enabled_json_true", {"inventory": {"enabled": True}}, True),
    ("enabled_string_true", {"inventory": {"enabled": "true"}}, True),
]

_REFUSING_METADATA = [c for c in _METADATA_CASES if not c[2]]


# ---------------------------------------------------------------------------
# Fake pool -- emulates the guard's SQL predicate over a metadata JSONB value
# ---------------------------------------------------------------------------


def _sql_enabled(metadata: Any) -> bool:
    """Python emulation of ``COALESCE((metadata->'inventory'->>'enabled')::boolean, false)``."""
    if not isinstance(metadata, dict):
        return False
    inventory = metadata.get("inventory")
    if not isinstance(inventory, dict) or "enabled" not in inventory:
        return False
    raw = inventory["enabled"]
    # ->> yields text; ::boolean accepts 'true'/'false' (and t/f/yes/no/1/0).
    return str(raw).strip().lower() in {"true", "t", "yes", "y", "on", "1"}


def _make_pool(
    *,
    metadata: Any = None,
    row_missing: bool = False,
    raises: BaseException | None = None,
) -> MagicMock:
    """Build a mock ``asyncpg`` pool for one ``namespaces`` read."""
    conn = MagicMock()
    if raises is not None:
        conn.fetchrow = AsyncMock(side_effect=raises)
    elif row_missing:
        conn.fetchrow = AsyncMock(return_value=None)
    else:
        conn.fetchrow = AsyncMock(return_value={"inventory_enabled": _sql_enabled(metadata)})
    pool = MagicMock()
    ctx = pool.acquire.return_value
    ctx.__aenter__.return_value = conn
    ctx.__aexit__.return_value = False
    pool._conn = conn
    return pool


def _enabled_pool() -> MagicMock:
    return _make_pool(metadata={"inventory": {"enabled": True}})


def _make_engine(pool: MagicMock) -> MagicMock:
    engine = MagicMock()
    engine.pg_pool = pool
    return engine


def _make_request(
    *,
    query: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
) -> MagicMock:
    req = MagicMock()
    req.json = AsyncMock(return_value=body or {})
    req.query_params = query or {}
    return req


def _request_for(route_name: str, namespace_id: str = _NAMESPACE_ID) -> MagicMock:
    _core, from_query = _REST_ROUTE_CORES[route_name]
    if from_query:
        return _make_request(query={"namespace_id": namespace_id})
    return _make_request(body={"namespace_id": namespace_id})


# ---------------------------------------------------------------------------
# 1. The guard itself
# ---------------------------------------------------------------------------


def test_guard_sql_reads_the_documented_predicate() -> None:
    """The emulation above is only honest if the real statement matches it."""
    src = inspect.getsource(require_inventory_enabled)
    assert "metadata->'inventory'->>'enabled'" in src
    assert "::boolean" in src
    assert "COALESCE(" in src
    assert "FROM   namespaces" in src
    assert "WHERE  id = $1::uuid" in src


@pytest.mark.parametrize(
    ("label", "metadata", "allowed"),
    _METADATA_CASES,
    ids=[c[0] for c in _METADATA_CASES],
)
@pytest.mark.asyncio
async def test_guard_fails_closed_per_metadata_shape(
    label: str, metadata: Any, allowed: bool
) -> None:
    pool = _make_pool(metadata=metadata)
    if allowed:
        await require_inventory_enabled(pool, _NAMESPACE_ID)
    else:
        with pytest.raises(InventoryDisabledError):
            await require_inventory_enabled(pool, _NAMESPACE_ID)


@pytest.mark.asyncio
async def test_guard_refuses_when_namespace_row_is_absent() -> None:
    with pytest.raises(InventoryDisabledError):
        await require_inventory_enabled(_make_pool(row_missing=True), _NAMESPACE_ID)


@pytest.mark.asyncio
async def test_guard_translates_asyncpg_dataerror_into_a_typed_refusal() -> None:
    """A malformed id reaching ``::uuid`` raises DataError -- NOT a ValueError."""
    pool = _make_pool(raises=DataError("invalid input syntax for type uuid"))
    with pytest.raises(InventoryDisabledError):
        await require_inventory_enabled(pool, _MALFORMED_NAMESPACE_ID)


@pytest.mark.asyncio
async def test_guard_binds_the_namespace_id_as_a_parameter() -> None:
    pool = _enabled_pool()
    await require_inventory_enabled(pool, _NAMESPACE_ID)
    args = pool._conn.fetchrow.await_args.args
    assert args[1] == _NAMESPACE_ID


# ---------------------------------------------------------------------------
# 2. Coverage ratchets -- every handler and every route is gated
# ---------------------------------------------------------------------------


def test_every_mcp_handler_is_gated() -> None:
    """Derived from the module, so a new ungated handler fails here."""
    found = {n for n in dir(inv_mcp) if n.startswith("handle_inventory_")}
    assert found == set(_MCP_HANDLER_CORES), found ^ set(_MCP_HANDLER_CORES)
    for name in sorted(found):
        src = inspect.getsource(getattr(inv_mcp, name))
        assert "await _check_inventory_enabled(engine, arguments)" in src, name
        assert "    require_namespace_id(arguments)" not in src, name


def test_every_rest_route_is_gated() -> None:
    found = {n for n in dir(inv_rest) if n.startswith("api_inventory_")}
    assert found == set(_REST_ROUTE_CORES), found ^ set(_REST_ROUTE_CORES)
    for name in sorted(found):
        src = inspect.getsource(getattr(inv_rest, name))
        assert "await _check_inventory_enabled_rest(namespace_id)" in src, name
        # Ordering is load-bearing: the UUID check must precede the gate.
        assert src.index("_require_namespace_id(") < src.index("_check_inventory_enabled_rest("), (
            name
        )


def test_gate_is_not_applied_inside_a_do_star_core() -> None:
    """Dependencies point inward -- the cores stay ignorant of opt-in."""
    from nce.vertical_modules.inventory import stock, transactions

    for module in (stock, transactions):
        assert "require_inventory_enabled" not in inspect.getsource(module)


# ---------------------------------------------------------------------------
# 3. MCP surface -- structured refusal, never flattened
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("handler_name", sorted(_MCP_HANDLER_CORES))
@pytest.mark.parametrize(
    ("label", "metadata", "_allowed"),
    _REFUSING_METADATA,
    ids=[c[0] for c in _REFUSING_METADATA],
)
@pytest.mark.asyncio
async def test_mcp_handler_refuses_namespace_that_has_not_opted_in(
    handler_name: str, label: str, metadata: Any, _allowed: bool
) -> None:
    core = _MCP_HANDLER_CORES[handler_name]
    engine = _make_engine(_make_pool(metadata=metadata))
    with patch.object(inv_mcp, core, new=AsyncMock(return_value={"ok": True})) as core_mock:
        with pytest.raises(McpError) as excinfo:
            await getattr(inv_mcp, handler_name)(engine, {"namespace_id": _NAMESPACE_ID})
    assert excinfo.value.code == MCP_SCOPE_FORBIDDEN == -32005
    assert excinfo.value.data is not None
    assert excinfo.value.data["reason"] == "inventory_disabled"
    core_mock.assert_not_awaited()


@pytest.mark.parametrize("handler_name", sorted(_MCP_HANDLER_CORES))
@pytest.mark.asyncio
async def test_mcp_refusal_is_structured_not_flattened_into_a_returned_payload(
    handler_name: str,
) -> None:
    """The refusal must PROPAGATE, never come back as a 200-shaped JSON string.

    Kept separate from the refusal test above on purpose: a handler that
    swallowed ``McpError`` into ``json.dumps({"error": ...})`` would still
    "refuse", and only this assertion would go red.
    """
    core = _MCP_HANDLER_CORES[handler_name]
    engine = _make_engine(_make_pool(metadata={"inventory": {"enabled": False}}))
    with patch.object(inv_mcp, core, new=AsyncMock(return_value={"ok": True})):
        try:
            result = await getattr(inv_mcp, handler_name)(engine, {"namespace_id": _NAMESPACE_ID})
        except McpError:
            return  # propagated -- correct
    pytest.fail(f"{handler_name} flattened the refusal into a returned payload: {result!r}")


@pytest.mark.parametrize("handler_name", sorted(_MCP_HANDLER_CORES))
@pytest.mark.asyncio
async def test_mcp_handler_reaches_the_core_exactly_once_when_opted_in(
    handler_name: str,
) -> None:
    core = _MCP_HANDLER_CORES[handler_name]
    engine = _make_engine(_enabled_pool())
    with patch.object(inv_mcp, core, new=AsyncMock(return_value={"ok": True})) as core_mock:
        out = await getattr(inv_mcp, handler_name)(engine, {"namespace_id": _NAMESPACE_ID})
    assert json.loads(out) == {"ok": True}
    assert core_mock.await_count == 1


@pytest.mark.parametrize("handler_name", sorted(_MCP_HANDLER_CORES))
@pytest.mark.asyncio
async def test_mcp_handler_rejects_malformed_namespace_id_before_touching_the_db(
    handler_name: str,
) -> None:
    """A malformed id never reaches ``::uuid``: ``require_namespace_id`` parses first.

    The refusal is a structured ``McpError`` -- never a raw ``DataError`` and
    never a returned ``{"error": ...}`` payload.  Note the code is
    ``-32602`` (invalid params), not ``-32005``: on MCP the UUID parse sits
    ahead of the gate, mirroring the REST surface's load-bearing ordering.
    """
    pool = _make_pool(metadata={"inventory": {"enabled": True}})
    engine = _make_engine(pool)
    with pytest.raises(McpError) as excinfo:
        await getattr(inv_mcp, handler_name)(engine, {"namespace_id": _MALFORMED_NAMESPACE_ID})
    assert excinfo.value.code == MCP_INVALID_PARAMS
    pool.acquire.assert_not_called()


@pytest.mark.asyncio
async def test_check_inventory_enabled_maps_a_dataerror_to_minus_32005() -> None:
    """If a future caller skips the UUID boundary, the guard still fails closed."""
    engine = _make_engine(_make_pool(raises=DataError("invalid input syntax for type uuid")))
    with pytest.raises(McpError) as excinfo:
        await inv_mcp._check_inventory_enabled(engine, {"namespace_id": _NAMESPACE_ID})
    assert excinfo.value.code == MCP_SCOPE_FORBIDDEN
    assert excinfo.value.data is not None
    assert excinfo.value.data["reason"] == "inventory_disabled"


# ---------------------------------------------------------------------------
# 4. REST surface -- 409 refusal, 422 for a malformed id, core reached on true
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route_name", sorted(_REST_ROUTE_CORES))
@pytest.mark.parametrize(
    ("label", "metadata", "_allowed"),
    _REFUSING_METADATA,
    ids=[c[0] for c in _REFUSING_METADATA],
)
@pytest.mark.asyncio
async def test_rest_route_refuses_namespace_that_has_not_opted_in(
    route_name: str, label: str, metadata: Any, _allowed: bool
) -> None:
    core, _from_query = _REST_ROUTE_CORES[route_name]
    engine = _make_engine(_make_pool(metadata=metadata))
    with patch.object(admin_state, "engine", engine):
        with patch.object(inv_rest, core, new=AsyncMock(return_value={"ok": True})) as core_mock:
            resp = await getattr(inv_rest, route_name)(_request_for(route_name))
    assert resp.status_code == 409
    payload = json.loads(bytes(resp.body))
    assert payload["error"] == "Inventory vertical is not enabled for this namespace"
    core_mock.assert_not_awaited()


@pytest.mark.parametrize("route_name", sorted(_REST_ROUTE_CORES))
@pytest.mark.asyncio
async def test_rest_route_refuses_when_namespace_row_is_absent(route_name: str) -> None:
    core, _from_query = _REST_ROUTE_CORES[route_name]
    engine = _make_engine(_make_pool(row_missing=True))
    with patch.object(admin_state, "engine", engine):
        with patch.object(inv_rest, core, new=AsyncMock(return_value={"ok": True})) as core_mock:
            resp = await getattr(inv_rest, route_name)(_request_for(route_name))
    assert resp.status_code == 409
    core_mock.assert_not_awaited()


@pytest.mark.parametrize("route_name", sorted(_REST_ROUTE_CORES))
@pytest.mark.asyncio
async def test_rest_route_malformed_namespace_id_is_422_before_the_gate(
    route_name: str,
) -> None:
    """422 from the UUID parse, and the guard's pool is never touched.

    If the gate ran first, the malformed id would reach ``WHERE id = $1::uuid``,
    asyncpg would raise ``DataError``, and it would escape ``admin_error_response``.
    """
    core, _from_query = _REST_ROUTE_CORES[route_name]
    pool = _make_pool(metadata={"inventory": {"enabled": True}})
    with patch.object(admin_state, "engine", _make_engine(pool)):
        with patch.object(inv_rest, core, new=AsyncMock(return_value={"ok": True})) as core_mock:
            resp = await getattr(inv_rest, route_name)(
                _request_for(route_name, _MALFORMED_NAMESPACE_ID)
            )
    assert resp.status_code == 422
    assert "Invalid namespace_id" in json.loads(bytes(resp.body))["error"]
    pool.acquire.assert_not_called()
    core_mock.assert_not_awaited()


@pytest.mark.parametrize("route_name", sorted(_REST_ROUTE_CORES))
@pytest.mark.asyncio
async def test_rest_route_reaches_the_core_exactly_once_when_opted_in(
    route_name: str,
) -> None:
    core, _from_query = _REST_ROUTE_CORES[route_name]
    with patch.object(admin_state, "engine", _make_engine(_enabled_pool())):
        with patch.object(inv_rest, core, new=AsyncMock(return_value={"ok": True})) as core_mock:
            resp = await getattr(inv_rest, route_name)(_request_for(route_name))
    assert resp.status_code == 200
    assert core_mock.await_count == 1
