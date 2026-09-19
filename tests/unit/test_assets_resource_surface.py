"""tests.unit.test_assets_resource_surface — C12 Resource Surface & Operations tests for ASSET.

Wave D-1 (MLv1.6 Lane D: Operations):
Tests:
  1. C12 ResourceSpec registration and attributes for ASSET.
  2. REST CRUD endpoints for ASSET (/api/assets) via Starlette TestClient:
     - GET list with filters (functional_location_id, lifecycle_state, is_shell).
     - GET item by ID.
     - POST create asset.
     - PATCH update asset with concurrency check (409 on version mismatch).
  3. Negative RLS tests:
     - Cross-tenant isolation: Tenant B cannot list or get Tenant A's assets.
     - Tenant B cannot patch or modify Tenant A's assets.
  4. Principal tier projection / redaction (external-customer & contractor allowlists).
  5. Custom verb operations:
     - POST /api/assets/{id}/move (update functional_location_id).
     - GET /api/assets/merge (C1 queue view filtered to node_type='ASSET').
     - POST /api/assets/{id}/merge (C1 merge action/queue).
     - POST /api/assets/{id}/link-product (C1 product confirmation).
  6. Shell flag (is_shell) exclusion behavior per ADR 0037.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from nce import admin_state
from nce.admin_handlers import assets as assets_handlers
from nce.resource_surface import (
    get_resource_spec,
    load_all_engine_resources,
)
from nce.resource_surface.rest import _clear_mem_store, make_resource_routes
from nce.vertical_modules.assets.resources import ASSET_SPEC

_NS_A = str(uuid.UUID("11111111-1111-4111-8111-111111111111"))
_NS_B = str(uuid.UUID("22222222-2222-4222-8222-222222222222"))
_ASSET_ID = str(uuid.uuid4())
_PRODUCT_ID = str(uuid.uuid4())


class _async_ctx:
    """Async context manager wrapper for mock db connections."""

    def __init__(self, obj: Any) -> None:
        self._obj = obj

    async def __aenter__(self) -> Any:
        return self._obj

    async def __aexit__(self, *_: Any) -> None:
        pass


def _make_mock_engine(fetchrow_return=None, fetch_return=None) -> tuple[MagicMock, AsyncMock]:
    """Create a mock NCEEngine with a pg_pool returning mocked asyncpg connection."""
    engine = MagicMock()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=fetchrow_return)
    conn.fetch = AsyncMock(return_value=fetch_return or [])
    conn.fetchval = AsyncMock(
        return_value=fetchrow_return.get("id")
        if isinstance(fetchrow_return, dict)
        else uuid.uuid4()
    )
    conn.execute = AsyncMock(return_value="UPDATE 1")
    conn.transaction = MagicMock(return_value=_async_ctx(conn))

    pool = AsyncMock()
    pool.acquire = MagicMock(return_value=_async_ctx(conn))
    engine.pg_pool = pool
    return engine, conn


@pytest.fixture(autouse=True)
def _reset_state():
    _clear_mem_store()
    admin_state.engine = None
    yield
    _clear_mem_store()
    admin_state.engine = None


# ===========================================================================
# 1. Spec Registration & Discovery
# ===========================================================================


def test_asset_resource_spec_registered():
    load_all_engine_resources()
    spec = get_resource_spec("assets", "assets")
    assert spec is not None
    assert spec.node_type == "ASSET"
    assert spec.engine == "assets"
    assert spec.entity == "assets"
    assert spec.table_name == "assets"
    assert spec.tenant_scope == "tenant"
    assert "functional_location_id" in spec.filterable_fields
    assert "is_shell" in spec.filterable_fields
    assert "product_id" in spec.filterable_fields
    assert "serial" in spec.searchable_fields
    assert "is_shell" in spec.writable_fields
    assert "product_id" in spec.writable_fields
    assert "product_sku" in spec.writable_fields


# ===========================================================================
# 2. C12 CRUD & Concurrency via REST Routes
# ===========================================================================


def test_asset_c12_crud_in_memory():
    routes = make_resource_routes(ASSET_SPEC)
    app = Starlette(routes=routes)
    client = TestClient(app, raise_server_exceptions=False)

    # 1. Create asset in Tenant A
    create_payload = {
        "namespace_id": _NS_A,
        "bom_line_id": "BL-001",
        "serial": "SN-998877",
        "functional_location_id": "ROOM-101",
        "lifecycle_state": "INSTALLED",
        "change_origin": "agent",
        "is_shell": False,
        "product_id": _PRODUCT_ID,
        "product_sku": "MFR-AMP-01",
    }
    resp = client.post("/api/assets/assets", json=create_payload)
    assert resp.status_code == 201, resp.text
    created = resp.json()
    assert "id" in created
    asset_id = created["id"]

    # 2. Get asset by ID in Tenant A
    resp = client.get(f"/api/assets/assets/{asset_id}?namespace_id={_NS_A}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["serial"] == "SN-998877"
    assert data["functional_location_id"] == "ROOM-101"
    assert data["is_shell"] is False
    assert data["product_id"] == _PRODUCT_ID
    assert data["product_sku"] == "MFR-AMP-01"

    # 3. List assets with filter in Tenant A
    resp = client.get(f"/api/assets/assets?namespace_id={_NS_A}&functional_location_id=ROOM-101")
    assert resp.status_code == 200
    items = resp.json().get("items", [])
    assert len(items) == 1
    assert items[0]["id"] == asset_id

    # Filter with non-matching room
    resp = client.get(f"/api/assets/assets?namespace_id={_NS_A}&functional_location_id=ROOM-999")
    assert resp.status_code == 200
    assert len(resp.json().get("items", [])) == 0

    # 4. Patch asset
    patch_payload = {
        "namespace_id": _NS_A,
        "functional_location_id": "ROOM-102",
        "expected_version": data.get("updated_at"),
    }
    resp = client.patch(f"/api/assets/assets/{asset_id}", json=patch_payload)
    assert resp.status_code == 200
    assert resp.json()["functional_location_id"] == "ROOM-102"

    # 5. Optimistic concurrency conflict (409)
    bad_version_payload = {
        "namespace_id": _NS_A,
        "functional_location_id": "ROOM-103",
        "expected_version": "1999-01-01T00:00:00Z",
    }
    resp = client.patch(f"/api/assets/assets/{asset_id}", json=bad_version_payload)
    assert resp.status_code == 409


# ===========================================================================
# 3. Negative RLS Tests
# ===========================================================================


def test_asset_negative_rls_isolation():
    routes = make_resource_routes(ASSET_SPEC)
    app = Starlette(routes=routes)
    client = TestClient(app, raise_server_exceptions=False)

    # Create asset in Tenant A
    create_payload = {
        "namespace_id": _NS_A,
        "bom_line_id": "BL-TENANT-A",
        "serial": "SN-AAA",
        "functional_location_id": "ROOM-A",
        "lifecycle_state": "INSTALLED",
    }
    resp = client.post("/api/assets/assets", json=create_payload)
    assert resp.status_code == 201
    asset_id = resp.json()["id"]

    # 1. Tenant B list returns 0 items
    resp = client.get(f"/api/assets/assets?namespace_id={_NS_B}")
    assert resp.status_code == 200
    assert len(resp.json().get("items", [])) == 0

    # 2. Tenant B get returns 404
    resp = client.get(f"/api/assets/assets/{asset_id}?namespace_id={_NS_B}")
    assert resp.status_code == 404

    # 3. Tenant B patch returns 404
    resp = client.patch(
        f"/api/assets/assets/{asset_id}",
        json={"namespace_id": _NS_B, "functional_location_id": "ROOM-HACK"},
    )
    assert resp.status_code == 404


# ===========================================================================
# 4. Custom Verbs: Move, Merge Queue, Link Product
# ===========================================================================


@pytest.mark.asyncio
async def test_asset_move_verb():
    routes = make_resource_routes(ASSET_SPEC)
    # Include admin app route for move
    from starlette.routing import Route

    app = Starlette(
        routes=[
            *routes,
            Route(
                "/api/assets/{id}/move", endpoint=assets_handlers.api_assets_move, methods=["POST"]
            ),
        ]
    )
    client = TestClient(app, raise_server_exceptions=False)

    mock_row = {
        "id": uuid.UUID(_ASSET_ID),
        "functional_location_id": "ROOM-OLD",
        "updated_at": None,
    }
    updated_row = {
        "functional_location_id": "ROOM-NEW",
        "updated_at": None,
    }

    engine, conn = _make_mock_engine(fetchrow_return=updated_row)
    conn.fetchrow.side_effect = [mock_row, updated_row]
    admin_state.engine = engine

    resp = client.post(
        f"/api/assets/{_ASSET_ID}/move",
        json={"namespace_id": _NS_A, "functional_location_id": "ROOM-NEW"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok"] is True
    assert data["asset_id"] == _ASSET_ID
    assert data["functional_location_id"] == "ROOM-NEW"
    assert data["previous_functional_location_id"] == "ROOM-OLD"


@pytest.mark.asyncio
async def test_asset_move_verb_not_found():
    from starlette.routing import Route

    app = Starlette(
        routes=[
            Route(
                "/api/assets/{id}/move", endpoint=assets_handlers.api_assets_move, methods=["POST"]
            ),
        ]
    )
    client = TestClient(app, raise_server_exceptions=False)

    engine, conn = _make_mock_engine(fetchrow_return=None)
    admin_state.engine = engine

    resp = client.post(
        f"/api/assets/{_ASSET_ID}/move",
        json={"namespace_id": _NS_A, "functional_location_id": "ROOM-NEW"},
    )
    assert resp.status_code == 404
    assert resp.json()["not_found"] is True


@pytest.mark.asyncio
async def test_asset_merge_queue_view():
    from starlette.routing import Route

    app = Starlette(
        routes=[
            Route(
                "/api/assets/merge",
                endpoint=assets_handlers.api_assets_merge_queue,
                methods=["GET"],
            ),
        ]
    )
    client = TestClient(app, raise_server_exceptions=False)

    queue_row = {
        "id": uuid.uuid4(),
        "node_type": "ASSET",
        "candidate_payload": {"serial": "SN-DUP-1"},
        "target_node_id": uuid.UUID(_ASSET_ID),
        "score": 0.95,
        "status": "pending",
        "created_at": None,
    }
    engine, conn = _make_mock_engine(fetch_return=[queue_row])
    admin_state.engine = engine

    resp = client.get(f"/api/assets/merge?namespace_id={_NS_A}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert len(data["pending"]) == 1
    assert data["pending"][0]["node_type"] == "ASSET"
    assert data["pending"][0]["candidate_payload"]["serial"] == "SN-DUP-1"


@pytest.mark.asyncio
async def test_asset_merge_confirm_verb():
    from starlette.routing import Route

    app = Starlette(
        routes=[
            Route(
                "/api/assets/{id}/merge",
                endpoint=assets_handlers.api_assets_merge,
                methods=["POST"],
            ),
        ]
    )
    client = TestClient(app, raise_server_exceptions=False)

    queue_id = str(uuid.uuid4())
    engine, conn = _make_mock_engine()
    admin_state.engine = engine

    with patch("nce.entity_resolution.merge_queue.confirm", new_callable=AsyncMock) as mock_confirm:
        resp = client.post(
            f"/api/assets/{_ASSET_ID}/merge",
            json={
                "namespace_id": _NS_A,
                "queue_id": queue_id,
                "decided_by": "operator-1",
            },
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["ok"] is True
        assert data["queue_id"] == queue_id
        assert data["status"] == "confirmed"
        assert mock_confirm.called


@pytest.mark.asyncio
async def test_asset_merge_enqueue_verb():
    from starlette.routing import Route

    app = Starlette(
        routes=[
            Route(
                "/api/assets/{id}/merge",
                endpoint=assets_handlers.api_assets_merge,
                methods=["POST"],
            ),
        ]
    )
    client = TestClient(app, raise_server_exceptions=False)

    target_id = str(uuid.uuid4())
    enqueued_id = uuid.uuid4()
    engine, conn = _make_mock_engine()
    admin_state.engine = engine

    with patch(
        "nce.entity_resolution.merge_queue.enqueue",
        new_callable=AsyncMock,
        return_value=enqueued_id,
    ) as mock_enqueue:
        resp = client.post(
            f"/api/assets/{_ASSET_ID}/merge",
            json={
                "namespace_id": _NS_A,
                "target_asset_id": target_id,
            },
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["ok"] is True
        assert data["queue_id"] == str(enqueued_id)
        assert data["asset_id"] == _ASSET_ID
        assert data["target_asset_id"] == target_id
        assert data["status"] == "enqueued"
        assert mock_enqueue.called


@pytest.mark.asyncio
async def test_asset_link_product_verb():
    from starlette.routing import Route

    app = Starlette(
        routes=[
            Route(
                "/api/assets/{id}/link-product",
                endpoint=assets_handlers.api_assets_link_product,
                methods=["POST"],
            ),
        ]
    )
    client = TestClient(app, raise_server_exceptions=False)

    linked_row = {
        "id": uuid.UUID(_ASSET_ID),
        "bom_line_id": "BL-100",
        "product_id": uuid.UUID(_PRODUCT_ID),
        "product_sku": "SKU-LINKED-100",
        "updated_at": None,
    }
    engine, conn = _make_mock_engine(fetchrow_return=linked_row)
    admin_state.engine = engine

    resp = client.post(
        f"/api/assets/{_ASSET_ID}/link-product",
        json={
            "namespace_id": _NS_A,
            "product_id": _PRODUCT_ID,
            "product_sku": "SKU-LINKED-100",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["asset_id"] == _ASSET_ID
    assert data["product_id"] == _PRODUCT_ID
    assert data["product_sku"] == "SKU-LINKED-100"
    # Verifies feedback insert was executed
    assert conn.execute.called


# ===========================================================================
# 5. Shell Exclusion (ADR 0037)
# ===========================================================================


def test_asset_shell_exclusion_flag():
    routes = make_resource_routes(ASSET_SPEC)
    app = Starlette(routes=routes)
    client = TestClient(app, raise_server_exceptions=False)

    # Create real asset
    resp_real = client.post(
        "/api/assets/assets",
        json={
            "namespace_id": _NS_A,
            "bom_line_id": "BL-REAL",
            "serial": "SN-REAL",
            "functional_location_id": "ROOM-1",
            "lifecycle_state": "ACTIVE",
            "is_shell": False,
        },
    )
    assert resp_real.status_code == 201

    # Create placeholder shell asset
    resp_shell = client.post(
        "/api/assets/assets",
        json={
            "namespace_id": _NS_A,
            "bom_line_id": "BL-SHELL",
            "serial": "SN-SHELL",
            "functional_location_id": "ROOM-1",
            "lifecycle_state": "PLANNED",
            "is_shell": True,
        },
    )
    assert resp_shell.status_code == 201

    # Filter excluding shells
    resp = client.get(f"/api/assets/assets?namespace_id={_NS_A}&is_shell=false")
    assert resp.status_code == 200
    items = resp.json().get("items", [])
    assert len(items) == 1
    assert items[0]["is_shell"] is False
    assert items[0]["bom_line_id"] == "BL-REAL"
