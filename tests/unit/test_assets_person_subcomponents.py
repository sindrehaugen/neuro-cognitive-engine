"""Unit tests for Wave D-2: Person assignment & sub-components (ASSET vertical module).

Tests coverage:
  1. Parameter validation: missing/invalid namespace_id, asset_id, employee_id, etc.
  2. Asset not found error handling (AssetNotFoundError).
  3. C16 Principal resolution (principal_id -> employee_id from principal_bindings).
  4. Person assignment core operations:
     - do_assign_asset_person: writes EMPLOYEE -[uses]-> ASSET edge, touches asset updated_at.
     - do_unassign_asset_person: deletes uses edge.
     - do_get_person_assets: fetches assigned assets, with and without open faults (tickets).
  5. Sub-components core operations & cycle detection:
     - do_link_subcomponent: writes ASSET -[part_of]-> ASSET edge.
     - Self-cycle detection: parent == child raises SubcomponentCycleError.
     - Direct & transitive circular dependency detection raises SubcomponentCycleError.
     - do_unlink_subcomponent: deletes part_of edge.
     - do_get_asset_subcomponents: returns children, parent, and recursive tree.
  6. Contract-A single-writer ownership assertions for ASSET node type.
  7. MCP handlers:
     - handle_assets_assign_person
     - handle_assets_unassign_person
     - handle_assets_list_person_assets
     - handle_assets_link_subcomponent
     - handle_assets_unlink_subcomponent
     - handle_assets_list_subcomponents
  8. Admin REST endpoints (Starlette TestClient):
     - POST /api/assets/{id}/assign
     - POST /api/assets/{id}/unassign
     - GET /api/assets/by-person/{employee_id}
     - POST /api/assets/{id}/sub-components
     - DELETE /api/assets/{id}/sub-components/{sub_id}
     - GET /api/assets/{id}/sub-components
"""

from __future__ import annotations

import datetime
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from nce import admin_state
from nce.admin_handlers import assets as admin_assets
from nce.vertical_modules.assets.assignment import (
    AssetAssignmentError,
    AssetNotFoundError,
    PersonNotFoundError,
    PrincipalBindingNotFoundError,
    SubcomponentCycleError,
    do_assign_asset_person,
    do_get_asset_subcomponents,
    do_get_person_assets,
    do_link_subcomponent,
    do_unassign_asset_person,
    do_unlink_subcomponent,
)
from nce.vertical_modules.assets.mcp_handlers import (
    handle_assets_assign_person,
    handle_assets_link_subcomponent,
    handle_assets_list_person_assets,
    handle_assets_list_subcomponents,
    handle_assets_unassign_person,
    handle_assets_unlink_subcomponent,
)

_NS_ID = UUID("00000000-0000-4000-8000-000000000001")
_ASSET_1 = UUID("11111111-1111-4111-8111-111111111111")
_ASSET_2 = UUID("22222222-2222-4222-8222-222222222222")
_ASSET_3 = UUID("33333333-3333-4333-8333-333333333333")
_EMPLOYEE_ID = "EMP-90210"
_PRINCIPAL_ID = "user-alice@example.internal"


class _async_ctx:
    """Async context manager wrapper for mock db sessions."""

    def __init__(self, obj: Any) -> None:
        self._obj = obj

    async def __aenter__(self) -> Any:
        return self._obj

    async def __aexit__(self, *_: Any) -> None:
        pass


def _make_mock_engine(conn: Any = None) -> tuple[MagicMock, Any]:
    engine = MagicMock()
    if conn is None:
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=None)
        conn.fetchval = AsyncMock(return_value=None)
        conn.fetch = AsyncMock(return_value=[])
        conn.execute = AsyncMock(return_value="OK")
    conn.transaction = MagicMock(return_value=_async_ctx(conn))
    pool = AsyncMock()
    pool.acquire = MagicMock(return_value=_async_ctx(conn))
    engine.pg_pool = pool
    engine.redis_client = None
    return engine, conn


def _make_asset_row(asset_id: UUID) -> dict[str, Any]:
    now = datetime.datetime.now(datetime.timezone.utc)
    return {
        "id": asset_id,
        "bom_line_id": "BOM-01",
        "serial": f"SN-{str(asset_id)[:6]}",
        "functional_location_id": "ROOM-A",
        "lifecycle_state": "in_service",
        "is_shell": False,
        "product_id": uuid4(),
        "product_sku": "SKU-TEST-01",
        "created_at": now,
        "updated_at": now,
    }


# ===========================================================================
# 1. Parameter Validation Tests
# ===========================================================================


def test_exception_hierarchy():
    assert issubclass(AssetNotFoundError, AssetAssignmentError)
    assert issubclass(PersonNotFoundError, AssetAssignmentError)
    assert issubclass(PrincipalBindingNotFoundError, PersonNotFoundError)
    assert issubclass(SubcomponentCycleError, AssetAssignmentError)


@pytest.mark.asyncio
async def test_assign_person_missing_namespace():
    with pytest.raises(ValueError, match="namespace_id is required"):
        await do_assign_asset_person(MagicMock(), {"asset_id": str(_ASSET_1), "employee_id": _EMPLOYEE_ID})


@pytest.mark.asyncio
async def test_assign_person_missing_asset_id():
    with pytest.raises(ValueError, match="asset_id is required"):
        await do_assign_asset_person(MagicMock(), {"namespace_id": str(_NS_ID), "employee_id": _EMPLOYEE_ID})


@pytest.mark.asyncio
async def test_assign_person_missing_employee_and_principal():
    with pytest.raises(ValueError, match="Either 'employee_id' or 'principal_id' must be provided"):
        await do_assign_asset_person(MagicMock(), {"namespace_id": str(_NS_ID), "asset_id": str(_ASSET_1)})


@pytest.mark.asyncio
async def test_unassign_person_missing_params():
    with pytest.raises(ValueError, match="namespace_id is required"):
        await do_unassign_asset_person(MagicMock(), {"asset_id": str(_ASSET_1)})
    with pytest.raises(ValueError, match="asset_id is required"):
        await do_unassign_asset_person(MagicMock(), {"namespace_id": str(_NS_ID)})


@pytest.mark.asyncio
async def test_link_subcomponent_missing_params():
    with pytest.raises(ValueError, match="parent_asset_id is required"):
        await do_link_subcomponent(MagicMock(), {"namespace_id": str(_NS_ID), "sub_asset_id": str(_ASSET_2)})
    with pytest.raises(ValueError, match="sub_asset_id is required"):
        await do_link_subcomponent(MagicMock(), {"namespace_id": str(_NS_ID), "parent_asset_id": str(_ASSET_1)})


@pytest.mark.asyncio
async def test_unlink_subcomponent_missing_params():
    with pytest.raises(ValueError, match="parent_asset_id is required"):
        await do_unlink_subcomponent(MagicMock(), {"namespace_id": str(_NS_ID), "sub_asset_id": str(_ASSET_2)})


# ===========================================================================
# 2. Person Assignment Core Operations
# ===========================================================================


@pytest.mark.asyncio
async def test_assign_person_success():
    engine, conn = _make_mock_engine()
    conn.fetchrow = AsyncMock(return_value={"id": _ASSET_1})

    with patch("nce.vertical_modules.assets.assignment.assert_owner", new_callable=AsyncMock) as mock_owner:
        res = await do_assign_asset_person(
            engine,
            {
                "namespace_id": str(_NS_ID),
                "asset_id": str(_ASSET_1),
                "employee_id": _EMPLOYEE_ID,
            },
        )
        assert res["ok"] is True
        assert res["asset_id"] == str(_ASSET_1)
        assert res["employee_id"] == _EMPLOYEE_ID
        assert res["status"] == "assigned"

        mock_owner.assert_awaited_once_with(conn, _NS_ID, "ASSET", "assets")
        assert conn.execute.await_count >= 2  # INSERT kg_edges, UPDATE assets


@pytest.mark.asyncio
async def test_assign_person_principal_resolution():
    engine, conn = _make_mock_engine()

    async def mock_fetchrow(query: str, *args: Any) -> Any:
        if "FROM principal_bindings" in query:
            return {"employee_id": _EMPLOYEE_ID}
        if "FROM assets" in query:
            return {"id": _ASSET_1}
        return None

    conn.fetchrow = AsyncMock(side_effect=mock_fetchrow)

    with patch("nce.vertical_modules.assets.assignment.assert_owner", new_callable=AsyncMock):
        res = await do_assign_asset_person(
            engine,
            {
                "namespace_id": str(_NS_ID),
                "asset_id": str(_ASSET_1),
                "principal_id": _PRINCIPAL_ID,
            },
        )
        assert res["ok"] is True
        assert res["employee_id"] == _EMPLOYEE_ID


@pytest.mark.asyncio
async def test_assign_person_principal_not_found():
    engine, conn = _make_mock_engine()
    conn.fetchrow = AsyncMock(return_value=None)

    with pytest.raises(PrincipalBindingNotFoundError, match="No principal binding found"):
        await do_assign_asset_person(
            engine,
            {
                "namespace_id": str(_NS_ID),
                "asset_id": str(_ASSET_1),
                "principal_id": "unknown-principal",
            },
        )


@pytest.mark.asyncio
async def test_assign_person_asset_not_found():
    engine, conn = _make_mock_engine()
    conn.fetchrow = AsyncMock(return_value=None)

    with pytest.raises(AssetNotFoundError, match=f"Asset not found: {_ASSET_1}"):
        await do_assign_asset_person(
            engine,
            {
                "namespace_id": str(_NS_ID),
                "asset_id": str(_ASSET_1),
                "employee_id": _EMPLOYEE_ID,
            },
        )


@pytest.mark.asyncio
async def test_unassign_person_success():
    engine, conn = _make_mock_engine()
    conn.fetchrow = AsyncMock(return_value={"id": _ASSET_1})

    with patch("nce.vertical_modules.assets.assignment.assert_owner", new_callable=AsyncMock) as mock_owner:
        res = await do_unassign_asset_person(
            engine,
            {
                "namespace_id": str(_NS_ID),
                "asset_id": str(_ASSET_1),
                "employee_id": _EMPLOYEE_ID,
            },
        )
        assert res["ok"] is True
        assert res["unassigned"] is True
        mock_owner.assert_awaited_once_with(conn, _NS_ID, "ASSET", "assets")
        assert conn.execute.await_count >= 1


@pytest.mark.asyncio
async def test_get_person_assets_with_and_without_faults():
    engine, conn = _make_mock_engine()

    async def mock_fetch(query: str, *args: Any) -> list[Any]:
        if "FROM kg_edges" in query:
            return [{"object_label": f"ASSET:{_ASSET_1}"}]
        if "FROM assets" in query:
            return [_make_asset_row(_ASSET_1)]
        if "FROM service_tickets" in query:
            return [
                {
                    "id": uuid4(),
                    "status": "open",
                    "priority": "high",
                    "summary": "Audio distorted",
                    "description": "Crackling sound",
                    "asset_id": _ASSET_1,
                    "room_id": "ROOM-A",
                    "created_at": datetime.datetime.now(datetime.timezone.utc),
                }
            ]
        return []

    conn.fetch = AsyncMock(side_effect=mock_fetch)

    # 1. include_faults=False
    res_no_faults = await do_get_person_assets(
        engine,
        {
            "namespace_id": str(_NS_ID),
            "employee_id": _EMPLOYEE_ID,
            "include_faults": False,
        },
    )
    assert res_no_faults["ok"] is True
    assert res_no_faults["count"] == 1
    assert "faults" not in res_no_faults

    # 2. include_faults=True (default)
    res_with_faults = await do_get_person_assets(
        engine,
        {
            "namespace_id": str(_NS_ID),
            "employee_id": _EMPLOYEE_ID,
            "include_faults": True,
        },
    )
    assert res_with_faults["ok"] is True
    assert res_with_faults["count"] == 1
    assert len(res_with_faults["faults"]) == 1
    assert res_with_faults["faults"][0]["summary"] == "Audio distorted"


# ===========================================================================
# 3. Sub-components Hierarchy & Cycle Detection Tests
# ===========================================================================


@pytest.mark.asyncio
async def test_link_subcomponent_self_cycle():
    engine, _ = _make_mock_engine()
    with pytest.raises(SubcomponentCycleError, match="cannot be linked as a sub-component of itself"):
        await do_link_subcomponent(
            engine,
            {
                "namespace_id": str(_NS_ID),
                "parent_asset_id": str(_ASSET_1),
                "sub_asset_id": str(_ASSET_1),
            },
        )


@pytest.mark.asyncio
async def test_link_subcomponent_direct_cycle():
    engine, conn = _make_mock_engine()

    async def mock_fetchrow(query: str, *args: Any) -> Any:
        if "FROM assets" in query:
            return {"id": args[0]}
        return None

    conn.fetchrow = AsyncMock(side_effect=mock_fetchrow)
    conn.fetchval = AsyncMock(return_value=1)

    with patch("nce.vertical_modules.assets.assignment.assert_owner", new_callable=AsyncMock):
        with pytest.raises(SubcomponentCycleError, match="Circular dependency detected"):
            await do_link_subcomponent(
                engine,
                {
                    "namespace_id": str(_NS_ID),
                    "parent_asset_id": str(_ASSET_1),
                    "sub_asset_id": str(_ASSET_2),
                },
            )


@pytest.mark.asyncio
async def test_link_subcomponent_transitive_cycle():
    engine, conn = _make_mock_engine()

    async def mock_fetchrow(query: str, *args: Any) -> Any:
        if "FROM assets" in query:
            return {"id": args[0]}
        return None

    conn.fetchrow = AsyncMock(side_effect=mock_fetchrow)
    conn.fetchval = AsyncMock(return_value=1)

    with patch("nce.vertical_modules.assets.assignment.assert_owner", new_callable=AsyncMock):
        with pytest.raises(SubcomponentCycleError, match="Circular dependency detected"):
            await do_link_subcomponent(
                engine,
                {
                    "namespace_id": str(_NS_ID),
                    "parent_asset_id": str(_ASSET_1),
                    "sub_asset_id": str(_ASSET_3),
                },
            )


@pytest.mark.asyncio
async def test_link_subcomponent_success():
    engine, conn = _make_mock_engine()

    async def mock_fetchrow(query: str, *args: Any) -> Any:
        if "FROM assets" in query:
            return {"id": args[0]}
        return None

    conn.fetchrow = AsyncMock(side_effect=mock_fetchrow)
    conn.fetchval = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=[])

    with patch("nce.vertical_modules.assets.assignment.assert_owner", new_callable=AsyncMock) as mock_owner:
        res = await do_link_subcomponent(
            engine,
            {
                "namespace_id": str(_NS_ID),
                "parent_asset_id": str(_ASSET_1),
                "sub_asset_id": str(_ASSET_2),
            },
        )
        assert res["ok"] is True
        assert res["parent_asset_id"] == str(_ASSET_1)
        assert res["sub_asset_id"] == str(_ASSET_2)
        assert res["status"] == "linked"
        mock_owner.assert_awaited_once_with(conn, _NS_ID, "ASSET", "assets")
        assert conn.execute.await_count >= 2  # INSERT kg_edges, UPDATE assets


@pytest.mark.asyncio
async def test_unlink_subcomponent_success():
    engine, conn = _make_mock_engine()

    with patch("nce.vertical_modules.assets.assignment.assert_owner", new_callable=AsyncMock) as mock_owner:
        res = await do_unlink_subcomponent(
            engine,
            {
                "namespace_id": str(_NS_ID),
                "parent_asset_id": str(_ASSET_1),
                "sub_asset_id": str(_ASSET_2),
            },
        )
        assert res["ok"] is True
        assert res["unlinked"] is True
        mock_owner.assert_awaited_once_with(conn, _NS_ID, "ASSET", "assets")
        assert conn.execute.await_count >= 1


@pytest.mark.asyncio
async def test_get_asset_subcomponents():
    engine, conn = _make_mock_engine()

    async def mock_fetchrow(query: str, *args: Any) -> Any:
        if "SELECT id, bom_line_id" in query or "FROM assets" in query:
            return _make_asset_row(_ASSET_1)
        return None

    async def mock_fetch(query: str, *args: Any) -> list[Any]:
        if "predicate = 'part_of'" in query:
            # Return child edge: ASSET_2 is part_of ASSET_1
            return [{"subject_label": f"ASSET:{_ASSET_2}", "object_label": f"ASSET:{_ASSET_1}"}]
        if "WHERE namespace_id = $1::uuid AND id = ANY($2::uuid[])" in query:
            return [_make_asset_row(_ASSET_2)]
        return []

    conn.fetchrow = AsyncMock(side_effect=mock_fetchrow)
    conn.fetch = AsyncMock(side_effect=mock_fetch)

    res = await do_get_asset_subcomponents(
        engine,
        {
            "namespace_id": str(_NS_ID),
            "asset_id": str(_ASSET_1),
        },
    )
    assert res["ok"] is True
    assert res["asset_id"] == str(_ASSET_1)
    assert len(res["sub_components"]) == 1
    assert res["sub_components"][0]["id"] == str(_ASSET_2)
    assert res["parent_asset"] is None


# ===========================================================================
# 4. MCP Handler Tests
# ===========================================================================


@pytest.mark.asyncio
async def test_mcp_handlers():
    engine, conn = _make_mock_engine()

    async def mock_fetchrow(query: str, *args: Any) -> Any:
        if "FROM assets" in query:
            return _make_asset_row(_ASSET_1)
        return None

    conn.fetchrow = AsyncMock(side_effect=mock_fetchrow)
    conn.fetchval = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=[])

    with patch("nce.vertical_modules.assets.assignment.assert_owner", new_callable=AsyncMock):
        # 1. handle_assets_assign_person
        out = await handle_assets_assign_person(
            engine,
            {
                "namespace_id": str(_NS_ID),
                "asset_id": str(_ASSET_1),
                "employee_id": _EMPLOYEE_ID,
            },
        )
        data = json.loads(out)
        assert data["ok"] is True
        assert data["status"] == "assigned"

        # 2. handle_assets_unassign_person
        out = await handle_assets_unassign_person(
            engine,
            {
                "namespace_id": str(_NS_ID),
                "asset_id": str(_ASSET_1),
            },
        )
        data = json.loads(out)
        assert data["ok"] is True
        assert data["unassigned"] is True

        # 3. handle_assets_list_person_assets
        out = await handle_assets_list_person_assets(
            engine,
            {
                "namespace_id": str(_NS_ID),
                "employee_id": _EMPLOYEE_ID,
            },
        )
        data = json.loads(out)
        assert data["ok"] is True

        # 4. handle_assets_link_subcomponent
        out = await handle_assets_link_subcomponent(
            engine,
            {
                "namespace_id": str(_NS_ID),
                "parent_asset_id": str(_ASSET_1),
                "sub_asset_id": str(_ASSET_2),
            },
        )
        data = json.loads(out)
        assert data["ok"] is True
        assert data["status"] == "linked"

        # 5. handle_assets_unlink_subcomponent
        out = await handle_assets_unlink_subcomponent(
            engine,
            {
                "namespace_id": str(_NS_ID),
                "parent_asset_id": str(_ASSET_1),
                "sub_asset_id": str(_ASSET_2),
            },
        )
        data = json.loads(out)
        assert data["ok"] is True
        assert data["unlinked"] is True

        # 6. handle_assets_list_subcomponents
        out = await handle_assets_list_subcomponents(
            engine,
            {
                "namespace_id": str(_NS_ID),
                "asset_id": str(_ASSET_1),
            },
        )
        data = json.loads(out)
        assert data["ok"] is True


# ===========================================================================
# 5. Admin REST Endpoints (Starlette TestClient)
# ===========================================================================


def _build_test_app() -> Starlette:
    routes = [
        Route("/api/assets/{id}/assign", endpoint=admin_assets.api_assets_assign_person, methods=["POST"]),
        Route("/api/assets/{id}/unassign", endpoint=admin_assets.api_assets_unassign_person, methods=["POST"]),
        Route("/api/assets/by-person/{employee_id}", endpoint=admin_assets.api_assets_list_person_assets, methods=["GET"]),
        Route("/api/assets/{id}/sub-components", endpoint=admin_assets.api_assets_link_subcomponent, methods=["POST"]),
        Route("/api/assets/{id}/sub-components/{sub_id}", endpoint=admin_assets.api_assets_unlink_subcomponent, methods=["DELETE"]),
        Route("/api/assets/{id}/sub-components", endpoint=admin_assets.api_assets_list_subcomponents, methods=["GET"]),
    ]
    return Starlette(routes=routes)


def test_admin_api_assign_and_unassign():
    app = _build_test_app()
    client = TestClient(app, raise_server_exceptions=False)
    engine, conn = _make_mock_engine()
    conn.fetchrow = AsyncMock(return_value={"id": _ASSET_1})
    admin_state.engine = engine

    with patch("nce.vertical_modules.assets.assignment.assert_owner", new_callable=AsyncMock):
        # 1. Missing namespace header/field -> 422
        r = client.post(f"/api/assets/{_ASSET_1}/assign", json={"employee_id": _EMPLOYEE_ID})
        assert r.status_code == 422

        # 2. Assign success
        r = client.post(
            f"/api/assets/{_ASSET_1}/assign",
            json={"namespace_id": str(_NS_ID), "employee_id": _EMPLOYEE_ID},
        )
        assert r.status_code == 200
        assert r.json()["ok"] is True
        assert r.json()["status"] == "assigned"

        # 3. Unassign success
        r = client.post(
            f"/api/assets/{_ASSET_1}/unassign",
            json={"namespace_id": str(_NS_ID), "employee_id": _EMPLOYEE_ID},
        )
        assert r.status_code == 200
        assert r.json()["ok"] is True
        assert r.json()["unassigned"] is True


def test_admin_api_by_person():
    app = _build_test_app()
    client = TestClient(app, raise_server_exceptions=False)
    engine, conn = _make_mock_engine()
    conn.fetch = AsyncMock(return_value=[])
    admin_state.engine = engine

    # Missing namespace query param -> 422
    r_missing = client.get(f"/api/assets/by-person/{_EMPLOYEE_ID}")
    assert r_missing.status_code == 422

    # Success
    r = client.get(
        f"/api/assets/by-person/{_EMPLOYEE_ID}?namespace_id={_NS_ID}",
    )
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["employee_id"] == _EMPLOYEE_ID
    assert data["count"] == 0


def test_admin_api_subcomponents_crud_and_cycle():
    app = _build_test_app()
    client = TestClient(app, raise_server_exceptions=False)
    engine, conn = _make_mock_engine()
    admin_state.engine = engine

    with patch("nce.vertical_modules.assets.assignment.assert_owner", new_callable=AsyncMock):
        # 1. Self cycle error -> 409
        r = client.post(
            f"/api/assets/{_ASSET_1}/sub-components",
            json={"namespace_id": str(_NS_ID), "sub_asset_id": str(_ASSET_1)},
        )
        assert r.status_code == 409
        assert r.json()["cycle"] is True

        # 2. Link success
        async def mock_fetchrow(query: str, *args: Any) -> Any:
            if "FROM assets" in query:
                return _make_asset_row(_ASSET_1)
            return None

        conn.fetchrow = AsyncMock(side_effect=mock_fetchrow)
        conn.fetchval = AsyncMock(return_value=None)
        conn.fetch = AsyncMock(return_value=[])
        r = client.post(
            f"/api/assets/{_ASSET_1}/sub-components",
            json={"namespace_id": str(_NS_ID), "sub_asset_id": str(_ASSET_2)},
        )
        assert r.status_code == 200
        assert r.json()["ok"] is True
        assert r.json()["status"] == "linked"

        # 3. List sub-components
        r = client.get(
            f"/api/assets/{_ASSET_1}/sub-components?namespace_id={_NS_ID}",
        )
        assert r.status_code == 200
        assert r.json()["ok"] is True

        # 4. Unlink sub-component
        r = client.delete(
            f"/api/assets/{_ASSET_1}/sub-components/{_ASSET_2}?namespace_id={_NS_ID}",
        )
        assert r.status_code == 200
        assert r.json()["ok"] is True
        assert r.json()["unlinked"] is True
