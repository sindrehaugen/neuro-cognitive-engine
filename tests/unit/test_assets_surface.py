"""
tests/unit/test_assets_surface.py
====================================
Acceptance tests for Batch 143 — Module 9.Wave 3 (assets-surface).

Covers:
  1. ``nce.admin_handlers.assets`` / ``nce.vertical_modules.assets.mcp_handlers``
     import cleanly.
  2. ``assets_get`` / ``assets_list`` / ``assets_advance_lifecycle`` are
     registered in ``TOOL_REGISTRY`` with the correct flags (matching the
     MCP tools table in ``docs/vertical_engines/09-assets-engine.md`` for
     ``assets_advance_lifecycle``: cacheable=False, admin_only=False,
     mutation=True).
  3. Tool-count assertion reflects the +3 assets tools (now 119).
  4. ``do_get_asset`` / ``do_list_assets`` / ``do_advance_lifecycle`` return
     the correct shape against a fully-mocked ``pg_pool``/connection —
     including the illegal-transition refusal and the not-found path —
     and each ``handle_*`` wrapper serialises the core's result to JSON.
  5. The three REST routes are mounted in the admin app and return the
     same shape as the cores, including the 404 (asset absent) / 409
     (illegal transition) mappings and 503 when no engine is connected.

All tests are pure unit tests — no DB, no Redis, no real Postgres
connection. ``scoped_pg_session`` is replaced with a trivial pass-through
(mirrors ``test_product_surface.py``'s ``_FakeScoped`` pattern), so
``do_advance_lifecycle``'s SQL runs against a mocked ``asyncpg.Connection``.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

_NAMESPACE_ID = "00000000-0000-4000-8000-000000000001"
_ASSET_ID = str(uuid4())

_ASSET_ROW: dict[str, Any] = {
    "id": _ASSET_ID,
    "bom_line_id": "Q001:AMP01",
    "serial": "SN-123",
    "functional_location_id": "ROOM-1",
    "lifecycle_state": "INSTALLED",
    "change_origin": "agent",
    "created_at": None,
    "updated_at": None,
}


# ---------------------------------------------------------------------------
# Shared fixtures / helpers — mirrors test_product_surface.py's _async_ctx /
# _FakeScoped pattern so do_* runs its real SQL against a mocked connection.
# ---------------------------------------------------------------------------


class _async_ctx:
    """Minimal async context manager that yields the given object."""

    def __init__(self, obj: Any) -> None:
        self._obj = obj

    async def __aenter__(self) -> Any:
        return self._obj

    async def __aexit__(self, *_: Any) -> None:
        pass


def _make_engine(fetch_return=None, fetchrow_return=None) -> tuple[MagicMock, AsyncMock]:
    """Build a minimal NCEEngine mock with a pg_pool that honours scoped_pg_session."""
    engine = MagicMock()
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=fetch_return or [])
    conn.fetchrow = AsyncMock(return_value=fetchrow_return)
    conn.execute = AsyncMock(return_value="UPDATE 1")

    pool = AsyncMock()
    pool.acquire = MagicMock(return_value=_async_ctx(conn))
    engine.pg_pool = pool
    return engine, conn


@pytest.fixture(autouse=True)
def _patch_scoped_session(monkeypatch):
    """Replace scoped_pg_session with a trivial pass-through for unit tests."""

    class _FakeScoped:
        def __init__(self, pool, ns):
            self._pool = pool
            self._ns = ns

        async def __aenter__(self):
            return await self._pool.acquire().__aenter__()

        async def __aexit__(self, *_):
            pass

    monkeypatch.setattr(
        "nce.vertical_modules.assets.mcp_handlers.scoped_pg_session",
        _FakeScoped,
    )


def _make_request(
    *,
    path_params: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
) -> MagicMock:
    """Minimal Starlette-like request mock (mirrors test_inventory_surface.py)."""
    req = MagicMock()
    req.json = AsyncMock(return_value=body or {})
    req.query_params = query or {}
    req.path_params = path_params or {}
    return req


# ---------------------------------------------------------------------------
# 1. Package imports
# ---------------------------------------------------------------------------


def test_package_imports() -> None:
    import nce.admin_handlers.assets  # noqa: F401
    import nce.vertical_modules.assets.mcp_handlers  # noqa: F401


# ---------------------------------------------------------------------------
# 2-3. Tool registry — flags + count
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool_name,expected_flags",
    [
        (
            "assets_get",
            {"cacheable": True, "admin_only": False, "mutation": False, "migration": False},
        ),
        (
            "assets_list",
            {"cacheable": True, "admin_only": False, "mutation": False, "migration": False},
        ),
        (
            "assets_advance_lifecycle",
            {"cacheable": False, "admin_only": False, "mutation": True, "migration": False},
        ),
    ],
)
def test_assets_tools_registered_with_correct_flags(
    tool_name: str, expected_flags: dict[str, bool]
) -> None:
    from nce.tool_registry import TOOL_REGISTRY

    assert tool_name in TOOL_REGISTRY, f"{tool_name!r} not found in TOOL_REGISTRY"
    spec = TOOL_REGISTRY[tool_name]
    for flag, expected in expected_flags.items():
        actual = getattr(spec, flag)
        assert actual == expected, f"{tool_name}.{flag}: expected {expected!r}, got {actual!r}"


def test_tool_count_updated_for_assets_surface() -> None:
    from nce.tool_registry import TOOL_REGISTRY

    assert "assets_get" in TOOL_REGISTRY
    assert "assets_list" in TOOL_REGISTRY
    assert "assets_advance_lifecycle" in TOOL_REGISTRY
    assert len(TOOL_REGISTRY) == 170, (
        f"Expected 135 tools (116 + 3 assets from Batch 143, M9.W3 + 1 system_design "
        f"from Batch 067b, M6.W13a + 2 system_design authoring tools from "
        f"Batch 067c, M6.W13b + 1 system_design validator from Batch 067d, M6.W13c "
        f"+ 1 system_design retire tool from Batch 067h, M6.W17 "
        f"+ 11 inventory tools from Batch 138a, M11.W10a -- the Inventory "
        f"surface-completion wave; this Assets test carries a repo-wide registry "
        f"ratchet, so it moves whenever ANY module registers a tool), "
        f"got {len(TOOL_REGISTRY)}: {sorted(TOOL_REGISTRY)}"
    )


# ---------------------------------------------------------------------------
# 4a. do_get_asset
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_get_asset_returns_expected_shape() -> None:
    from nce.vertical_modules.assets.mcp_handlers import do_get_asset

    engine, conn = _make_engine(fetchrow_return=_ASSET_ROW)

    result = await do_get_asset(engine, {"namespace_id": _NAMESPACE_ID, "asset_id": _ASSET_ID})

    assert result["ok"] is True
    assert result["asset"]["asset_id"] == _ASSET_ID
    assert result["asset"]["lifecycle_state"] == "INSTALLED"
    assert result["asset"]["bom_line_id"] == "Q001:AMP01"


@pytest.mark.asyncio
async def test_do_get_asset_not_found_returns_none() -> None:
    from nce.vertical_modules.assets.mcp_handlers import do_get_asset

    engine, _ = _make_engine(fetchrow_return=None)

    result = await do_get_asset(engine, {"namespace_id": _NAMESPACE_ID, "asset_id": _ASSET_ID})

    assert result == {"ok": True, "asset": None}


@pytest.mark.asyncio
async def test_do_get_asset_missing_asset_id_raises() -> None:
    from nce.vertical_modules.assets.mcp_handlers import do_get_asset

    engine, _ = _make_engine()
    with pytest.raises(ValueError, match="asset_id"):
        await do_get_asset(engine, {"namespace_id": _NAMESPACE_ID})


@pytest.mark.asyncio
async def test_do_get_asset_missing_namespace_id_raises() -> None:
    from nce.vertical_modules.assets.mcp_handlers import do_get_asset

    engine, _ = _make_engine()
    with pytest.raises(ValueError):
        await do_get_asset(engine, {"asset_id": _ASSET_ID})


@pytest.mark.asyncio
async def test_do_get_asset_malformed_asset_id_raises() -> None:
    """``_require_asset_id`` must reject a non-UUID string before any DB call
    — flagged by audit as unconstrained by any existing test."""
    from nce.vertical_modules.assets.mcp_handlers import do_get_asset

    engine, conn = _make_engine()
    with pytest.raises(ValueError, match="invalid asset_id"):
        await do_get_asset(engine, {"namespace_id": _NAMESPACE_ID, "asset_id": "not-a-real-uuid"})
    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_do_advance_lifecycle_malformed_asset_id_raises() -> None:
    """Same guard, exercised through do_advance_lifecycle's own call site."""
    from nce.vertical_modules.assets.mcp_handlers import do_advance_lifecycle

    engine, conn = _make_engine()
    with pytest.raises(ValueError, match="invalid asset_id"):
        await do_advance_lifecycle(
            engine,
            {
                "namespace_id": _NAMESPACE_ID,
                "asset_id": "not-a-real-uuid",
                "target_state": "CONFIGURED",
            },
        )
    conn.fetchrow.assert_not_awaited()


# ---------------------------------------------------------------------------
# 4b. do_list_assets
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_list_assets_returns_expected_shape() -> None:
    from nce.vertical_modules.assets.mcp_handlers import do_list_assets

    engine, conn = _make_engine(fetch_return=[_ASSET_ROW])

    result = await do_list_assets(engine, {"namespace_id": _NAMESPACE_ID})

    assert result["ok"] is True
    assert len(result["items"]) == 1
    assert result["items"][0]["asset_id"] == _ASSET_ID


@pytest.mark.asyncio
async def test_do_list_assets_empty_returns_empty_list() -> None:
    from nce.vertical_modules.assets.mcp_handlers import do_list_assets

    engine, _ = _make_engine(fetch_return=[])

    result = await do_list_assets(engine, {"namespace_id": _NAMESPACE_ID})

    assert result == {"ok": True, "items": []}


@pytest.mark.asyncio
async def test_do_list_assets_applies_filters() -> None:
    """Both optional filters, when supplied, must reach the SQL WHERE clause."""
    from nce.vertical_modules.assets.mcp_handlers import do_list_assets

    engine, conn = _make_engine(fetch_return=[_ASSET_ROW])

    await do_list_assets(
        engine,
        {
            "namespace_id": _NAMESPACE_ID,
            "functional_location_id": "ROOM-1",
            "lifecycle_state": "INSTALLED",
        },
    )

    query, *args = conn.fetch.call_args.args
    assert "functional_location_id = $2" in query
    assert "lifecycle_state = $3" in query
    assert args == [_NAMESPACE_ID, "ROOM-1", "INSTALLED"]


@pytest.mark.asyncio
async def test_do_list_assets_orders_most_recent_first() -> None:
    """Pins the ORDER BY direction the module docstring promises
    ("most-recent first") — flagged by audit as unconstrained by any
    existing test."""
    from nce.vertical_modules.assets.mcp_handlers import do_list_assets

    engine, conn = _make_engine(fetch_return=[])

    await do_list_assets(engine, {"namespace_id": _NAMESPACE_ID})

    query = conn.fetch.call_args.args[0]
    assert "ORDER BY created_at DESC" in query


@pytest.mark.asyncio
async def test_do_list_assets_caps_at_the_hardcoded_row_limit() -> None:
    """Pins ``_LIST_ROW_CAP`` flowing into the SQL LIMIT — flagged by audit
    as unconstrained by any existing test. This is a wiring check (the
    constant reaches the query), not a 501-row seed — the value itself is
    exercised as a real Postgres LIMIT by the integration suite's owner-pool
    list tests (tests/test_assets_surface_isolation.py)."""
    from nce.vertical_modules.assets.mcp_handlers import _LIST_ROW_CAP, do_list_assets

    assert _LIST_ROW_CAP == 500

    engine, conn = _make_engine(fetch_return=[])

    await do_list_assets(engine, {"namespace_id": _NAMESPACE_ID})

    query = conn.fetch.call_args.args[0]
    assert f"LIMIT {_LIST_ROW_CAP}" in query
    assert "LIMIT 500" in query


# ---------------------------------------------------------------------------
# 4c. do_advance_lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_advance_lifecycle_legal_transition_updates_and_returns_ok() -> None:
    from nce.vertical_modules.assets.mcp_handlers import do_advance_lifecycle

    engine, conn = _make_engine(fetchrow_return={"lifecycle_state": "INSTALLED"})

    result = await do_advance_lifecycle(
        engine,
        {"namespace_id": _NAMESPACE_ID, "asset_id": _ASSET_ID, "target_state": "CONFIGURED"},
    )

    assert result["ok"] is True
    assert result["not_found"] is False
    assert result["changed"] is True
    assert result["previous_state"] == "INSTALLED"
    assert result["new_state"] == "CONFIGURED"
    assert result["error"] is None
    conn.execute.assert_awaited_once()
    update_sql = conn.execute.call_args.args[0]
    assert "UPDATE assets" in update_sql
    assert "lifecycle_state" in update_sql


@pytest.mark.asyncio
async def test_do_advance_lifecycle_illegal_transition_is_refused_not_raised() -> None:
    """Jumping ahead (INSTALLED -> ACTIVE) is refused; the row is never written."""
    from nce.vertical_modules.assets.mcp_handlers import do_advance_lifecycle

    engine, conn = _make_engine(fetchrow_return={"lifecycle_state": "INSTALLED"})

    result = await do_advance_lifecycle(
        engine,
        {"namespace_id": _NAMESPACE_ID, "asset_id": _ASSET_ID, "target_state": "ACTIVE"},
    )

    assert result["ok"] is False
    assert result["not_found"] is False
    assert result["changed"] is False
    assert result["previous_state"] == "INSTALLED"
    assert result["new_state"] == "INSTALLED"  # unchanged
    assert result["error"] is not None
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_do_advance_lifecycle_self_transition_is_idempotent_no_op() -> None:
    from nce.vertical_modules.assets.mcp_handlers import do_advance_lifecycle

    engine, conn = _make_engine(fetchrow_return={"lifecycle_state": "INSTALLED"})

    result = await do_advance_lifecycle(
        engine,
        {"namespace_id": _NAMESPACE_ID, "asset_id": _ASSET_ID, "target_state": "INSTALLED"},
    )

    assert result["ok"] is True
    assert result["changed"] is False
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_do_advance_lifecycle_asset_not_found() -> None:
    from nce.vertical_modules.assets.mcp_handlers import do_advance_lifecycle

    engine, conn = _make_engine(fetchrow_return=None)

    result = await do_advance_lifecycle(
        engine,
        {"namespace_id": _NAMESPACE_ID, "asset_id": _ASSET_ID, "target_state": "CONFIGURED"},
    )

    assert result["ok"] is False
    assert result["not_found"] is True
    assert result["asset_id"] == _ASSET_ID
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_do_advance_lifecycle_missing_target_state_raises() -> None:
    from nce.vertical_modules.assets.mcp_handlers import do_advance_lifecycle

    engine, _ = _make_engine()
    with pytest.raises(ValueError, match="target_state"):
        await do_advance_lifecycle(engine, {"namespace_id": _NAMESPACE_ID, "asset_id": _ASSET_ID})


@pytest.mark.asyncio
async def test_do_advance_lifecycle_never_writes_warranty_columns() -> None:
    """Named scope limit (module docstring): no warranty_months is ever passed
    to advance(), so a VERIFIED-entering transition still updates only
    lifecycle_state + updated_at — never a warranty column."""
    from nce.vertical_modules.assets.mcp_handlers import do_advance_lifecycle

    engine, conn = _make_engine(fetchrow_return={"lifecycle_state": "CONFIGURED"})

    result = await do_advance_lifecycle(
        engine,
        {"namespace_id": _NAMESPACE_ID, "asset_id": _ASSET_ID, "target_state": "VERIFIED"},
    )

    assert result["ok"] is True
    assert result["changed"] is True
    update_sql = conn.execute.call_args.args[0]
    assert "warranty" not in update_sql.lower()


# ---------------------------------------------------------------------------
# 4d. handle_* — MCP surface
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_assets_get_returns_json_string() -> None:
    from nce.vertical_modules.assets import mcp_handlers

    with patch.object(
        mcp_handlers, "do_get_asset", AsyncMock(return_value={"ok": True, "asset": None})
    ):
        result = await mcp_handlers.handle_assets_get(
            MagicMock(), {"namespace_id": _NAMESPACE_ID, "asset_id": _ASSET_ID}
        )
    parsed = json.loads(result)
    assert parsed == {"ok": True, "asset": None}


@pytest.mark.asyncio
async def test_handle_assets_get_missing_namespace_id_raises_mcp_error() -> None:
    from nce.mcp_errors import McpError
    from nce.vertical_modules.assets.mcp_handlers import handle_assets_get

    with pytest.raises(McpError) as exc_info:
        await handle_assets_get(MagicMock(), {"asset_id": _ASSET_ID})
    assert exc_info.value.code == -32602


@pytest.mark.asyncio
async def test_handle_assets_list_returns_json_string() -> None:
    from nce.vertical_modules.assets import mcp_handlers

    with patch.object(
        mcp_handlers, "do_list_assets", AsyncMock(return_value={"ok": True, "items": []})
    ):
        result = await mcp_handlers.handle_assets_list(MagicMock(), {"namespace_id": _NAMESPACE_ID})
    parsed = json.loads(result)
    assert parsed == {"ok": True, "items": []}


@pytest.mark.asyncio
async def test_handle_assets_list_missing_namespace_id_raises_mcp_error() -> None:
    from nce.mcp_errors import McpError
    from nce.vertical_modules.assets.mcp_handlers import handle_assets_list

    with pytest.raises(McpError) as exc_info:
        await handle_assets_list(MagicMock(), {})
    assert exc_info.value.code == -32602


@pytest.mark.asyncio
async def test_handle_assets_advance_lifecycle_returns_json_string() -> None:
    from nce.vertical_modules.assets import mcp_handlers

    core_result = {
        "ok": True,
        "not_found": False,
        "changed": True,
        "asset_id": _ASSET_ID,
        "previous_state": "INSTALLED",
        "new_state": "CONFIGURED",
        "error": None,
    }
    with patch.object(mcp_handlers, "do_advance_lifecycle", AsyncMock(return_value=core_result)):
        result = await mcp_handlers.handle_assets_advance_lifecycle(
            MagicMock(),
            {
                "namespace_id": _NAMESPACE_ID,
                "asset_id": _ASSET_ID,
                "target_state": "CONFIGURED",
            },
        )
    parsed = json.loads(result)
    assert parsed["ok"] is True
    assert parsed["new_state"] == "CONFIGURED"


@pytest.mark.asyncio
async def test_handle_assets_advance_lifecycle_missing_namespace_id_raises_mcp_error() -> None:
    from nce.mcp_errors import McpError
    from nce.vertical_modules.assets.mcp_handlers import handle_assets_advance_lifecycle

    with pytest.raises(McpError) as exc_info:
        await handle_assets_advance_lifecycle(
            MagicMock(), {"asset_id": _ASSET_ID, "target_state": "CONFIGURED"}
        )
    assert exc_info.value.code == -32602


# ---------------------------------------------------------------------------
# 5. REST routes — mounted + same shape as the cores
# ---------------------------------------------------------------------------


def test_assets_routes_mounted_in_admin_app() -> None:
    from nce.admin_app import build_admin_routes

    routes = build_admin_routes()
    paths = {r.path for r in routes}
    assert "/api/assets" in paths
    assert "/api/assets/{id}" in paths
    assert "/api/assets/{id}/lifecycle" in paths


@pytest.mark.asyncio
async def test_api_assets_get_returns_ok_shape() -> None:
    from nce import admin_state
    from nce.admin_handlers import assets as assets_mod

    with patch.object(admin_state, "engine", MagicMock()):
        with patch.object(
            assets_mod, "do_get_asset", AsyncMock(return_value={"ok": True, "asset": None})
        ):
            req = _make_request(
                path_params={"id": _ASSET_ID}, query={"namespace_id": _NAMESPACE_ID}
            )
            resp = await assets_mod.api_assets_get(req)
            body = json.loads(bytes(resp.body).decode("utf-8"))
    assert resp.status_code == 200
    assert body == {"ok": True, "asset": None}


@pytest.mark.asyncio
async def test_api_assets_list_returns_ok_shape() -> None:
    from nce import admin_state
    from nce.admin_handlers import assets as assets_mod

    with patch.object(admin_state, "engine", MagicMock()):
        with patch.object(
            assets_mod, "do_list_assets", AsyncMock(return_value={"ok": True, "items": []})
        ):
            req = _make_request(query={"namespace_id": _NAMESPACE_ID})
            resp = await assets_mod.api_assets_list(req)
            body = json.loads(bytes(resp.body).decode("utf-8"))
    assert resp.status_code == 200
    assert body == {"ok": True, "items": []}


@pytest.mark.asyncio
async def test_api_assets_advance_lifecycle_returns_200_on_success() -> None:
    from nce import admin_state
    from nce.admin_handlers import assets as assets_mod

    core_result = {
        "ok": True,
        "not_found": False,
        "changed": True,
        "asset_id": _ASSET_ID,
        "previous_state": "INSTALLED",
        "new_state": "CONFIGURED",
        "error": None,
    }
    with patch.object(admin_state, "engine", MagicMock()):
        with patch.object(assets_mod, "do_advance_lifecycle", AsyncMock(return_value=core_result)):
            req = _make_request(
                path_params={"id": _ASSET_ID},
                body={"namespace_id": _NAMESPACE_ID, "target_state": "CONFIGURED"},
            )
            resp = await assets_mod.api_assets_advance_lifecycle(req)
            body = json.loads(bytes(resp.body).decode("utf-8"))
    assert resp.status_code == 200
    assert body["new_state"] == "CONFIGURED"


@pytest.mark.asyncio
async def test_api_assets_advance_lifecycle_returns_409_on_illegal_transition() -> None:
    from nce import admin_state
    from nce.admin_handlers import assets as assets_mod

    core_result = {
        "ok": False,
        "not_found": False,
        "changed": False,
        "asset_id": _ASSET_ID,
        "previous_state": "INSTALLED",
        "new_state": "INSTALLED",
        "error": "illegal transition: 'INSTALLED' -> 'ACTIVE'",
    }
    with patch.object(admin_state, "engine", MagicMock()):
        with patch.object(assets_mod, "do_advance_lifecycle", AsyncMock(return_value=core_result)):
            req = _make_request(
                path_params={"id": _ASSET_ID},
                body={"namespace_id": _NAMESPACE_ID, "target_state": "ACTIVE"},
            )
            resp = await assets_mod.api_assets_advance_lifecycle(req)
            body = json.loads(bytes(resp.body).decode("utf-8"))
    assert resp.status_code == 409
    assert "illegal transition" in body["error"]


@pytest.mark.asyncio
async def test_api_assets_advance_lifecycle_returns_404_when_asset_absent() -> None:
    from nce import admin_state
    from nce.admin_handlers import assets as assets_mod

    core_result = {
        "ok": False,
        "not_found": True,
        "asset_id": _ASSET_ID,
        "error": f"asset {_ASSET_ID!r} not found in this namespace",
    }
    with patch.object(admin_state, "engine", MagicMock()):
        with patch.object(assets_mod, "do_advance_lifecycle", AsyncMock(return_value=core_result)):
            req = _make_request(
                path_params={"id": _ASSET_ID},
                body={"namespace_id": _NAMESPACE_ID, "target_state": "CONFIGURED"},
            )
            resp = await assets_mod.api_assets_advance_lifecycle(req)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_api_assets_routes_missing_namespace_id_returns_422() -> None:
    from nce import admin_state
    from nce.admin_handlers.assets import (
        api_assets_advance_lifecycle,
        api_assets_get,
        api_assets_list,
    )

    with patch.object(admin_state, "engine", MagicMock()):
        resp = await api_assets_get(_make_request(path_params={"id": _ASSET_ID}, query={}))
        assert resp.status_code == 422

        resp = await api_assets_list(_make_request(query={}))
        assert resp.status_code == 422

        resp = await api_assets_advance_lifecycle(
            _make_request(path_params={"id": _ASSET_ID}, body={})
        )
        assert resp.status_code == 422


@pytest.mark.asyncio
async def test_api_assets_get_missing_id_path_param_returns_422() -> None:
    from nce import admin_state
    from nce.admin_handlers.assets import api_assets_get

    with patch.object(admin_state, "engine", MagicMock()):
        resp = await api_assets_get(
            _make_request(path_params={}, query={"namespace_id": _NAMESPACE_ID})
        )
        assert resp.status_code == 422


@pytest.mark.asyncio
async def test_api_assets_routes_no_engine_returns_503() -> None:
    from nce import admin_state
    from nce.admin_handlers.assets import (
        api_assets_advance_lifecycle,
        api_assets_get,
        api_assets_list,
    )

    with patch.object(admin_state, "engine", None):
        resp = await api_assets_get(
            _make_request(path_params={"id": _ASSET_ID}, query={"namespace_id": _NAMESPACE_ID})
        )
        assert resp.status_code == 503

        resp = await api_assets_list(_make_request(query={"namespace_id": _NAMESPACE_ID}))
        assert resp.status_code == 503

        resp = await api_assets_advance_lifecycle(
            _make_request(
                path_params={"id": _ASSET_ID},
                body={"namespace_id": _NAMESPACE_ID, "target_state": "CONFIGURED"},
            )
        )
        assert resp.status_code == 503
