"""
tests/test_assets_qr_register.py
================================
Pure-unit tests for Wave A-4: Assets QR code generator and room-centric register.

Covers:
  1. Pure-Python QR matrix and vector SVG generation (ISO/IEC 18004 Model 2).
  2. Domain core ``do_generate_asset_qr`` (deep-linking, dimensions, SVG output, absent asset).
  3. Domain core ``do_get_room_register`` (room device listing, deep-links, QR endpoints).
  4. MCP tool handler ``handle_assets_generate_qr``.
  5. Admin REST routes ``api_assets_generate_qr`` and ``api_assets_register``.
  6. Customer portal projection in ``do_asset_register`` with database query fallback.

Hermetic test: mocks Postgres asyncpg connections via scoped_pg_session.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from nce.vertical_modules.assets.qr import (
    do_generate_asset_qr,
    do_get_room_register,
    generate_qr_matrix,
    render_qr_svg,
)

_NAMESPACE_ID = "00000000-0000-4000-8000-000000000001"
_ASSET_ID = str(uuid4())
_ROOM_ID = "ROOM-CONF-A"


class _async_ctx:
    def __init__(self, obj: Any) -> None:
        self._obj = obj

    async def __aenter__(self) -> Any:
        return self._obj

    async def __aexit__(self, *_: Any) -> None:
        pass


def _make_mock_engine(fetch_return=None, fetchrow_return=None) -> tuple[MagicMock, AsyncMock]:
    engine = MagicMock()
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=fetch_return or [])
    conn.fetchrow = AsyncMock(return_value=fetchrow_return)
    conn.execute = AsyncMock(return_value="UPDATE 1")
    conn.transaction = MagicMock(return_value=_async_ctx(conn))

    pool = AsyncMock()
    pool.acquire = MagicMock(return_value=_async_ctx(conn))
    engine.pg_pool = pool
    return engine, conn


@pytest.fixture(autouse=True)
def _patch_scoped_session(monkeypatch):
    class _FakeScoped:
        def __init__(self, pool, ns):
            self._pool = pool
            self._ns = ns

        async def __aenter__(self):
            return await self._pool.acquire().__aenter__()

        async def __aexit__(self, *_):
            pass

    monkeypatch.setattr("nce.vertical_modules.assets.qr.scoped_pg_session", _FakeScoped)
    monkeypatch.setattr(
        "nce.vertical_modules.customer_portal.rooms.scoped_pg_session",
        _FakeScoped,
        raising=False,
    )


def _make_request(
    *,
    path_params: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
) -> MagicMock:
    req = MagicMock()
    req.json = AsyncMock(return_value=body or {})
    req.query_params = query or {}
    req.path_params = path_params or {}
    return req


# =============================================================================
# 1. Pure QR Encoder Tests
# =============================================================================


def test_generate_qr_matrix_dimensions_and_finders():
    """Verify matrix size and finder pattern structure for short payload."""
    payload = "nce://assets/1"
    matrix = generate_qr_matrix(payload)
    size = len(matrix)
    assert size == 21  # Fits in Version 1 (21x21)
    assert all(len(row) == size for row in matrix)

    # Check top-left finder center (3x3 solid black at 2..4, 2..4)
    for r in range(2, 5):
        for c in range(2, 5):
            assert matrix[r][c] == 1

    # Check separator / white ring at (1, 1)
    assert matrix[1][1] == 0


def test_generate_qr_matrix_larger_version():
    """Verify larger payload automatically scales to appropriate version."""
    long_payload = "https://portal.company.com/rooms/conference-room-alpha/assets/00000000-0000-4000-8000-000000000001?action=inspect"
    matrix = generate_qr_matrix(long_payload)
    size = len(matrix)
    assert size > 21
    assert (size - 21) % 4 == 0  # Standard QR version step


def test_generate_qr_matrix_payload_too_large():
    """Payload exceeding max supported version raises ValueError."""
    giant_payload = "X" * 200
    with pytest.raises(ValueError, match="too large"):
        generate_qr_matrix(giant_payload)


def test_render_qr_svg_produces_valid_xml():
    """Verify SVG renderer produces valid standalone SVG."""
    matrix = [[1, 0], [0, 1]]
    svg = render_qr_svg(matrix, box_size=10, border=2)
    assert svg.startswith("<svg")
    assert svg.endswith("</svg>")
    assert 'fill="#000000"' in svg
    assert 'fill="#ffffff"' in svg
    # (2 modules + 4 border) * 10 = 60
    assert 'viewBox="0 0 60 60"' in svg


# =============================================================================
# 2. Domain Core: do_generate_asset_qr
# =============================================================================


@pytest.mark.asyncio
async def test_do_generate_asset_qr_success():
    mock_row = {
        "id": _ASSET_ID,
        "bom_line_id": "BOM-001",
        "serial": "SN-ABC-999",
        "functional_location_id": _ROOM_ID,
        "lifecycle_state": "VERIFIED",
    }
    engine, _ = _make_mock_engine(fetchrow_return=mock_row)

    res = await do_generate_asset_qr(
        engine,
        {
            "namespace_id": _NAMESPACE_ID,
            "asset_id": _ASSET_ID,
            "base_url": "https://portal.example.com",
        },
    )

    assert res["ok"] is True
    assert res["asset_id"] == _ASSET_ID
    assert res["serial"] == "SN-ABC-999"
    assert res["functional_location_id"] == _ROOM_ID
    assert res["lifecycle_state"] == "VERIFIED"
    assert res["deep_link_url"] == f"https://portal.example.com/rooms/{_ROOM_ID}/assets/{_ASSET_ID}"
    assert res["qr_payload"] == res["deep_link_url"]
    assert res["qr_svg"].startswith("<svg")
    assert isinstance(res["qr_matrix"], list)
    assert res["dimensions"]["modules"] == len(res["qr_matrix"])


@pytest.mark.asyncio
async def test_do_generate_asset_qr_not_found():
    engine, _ = _make_mock_engine(fetchrow_return=None)

    res = await do_generate_asset_qr(
        engine,
        {
            "namespace_id": _NAMESPACE_ID,
            "asset_id": _ASSET_ID,
        },
    )

    assert res["ok"] is False
    assert "not found" in res["error"].lower()
    assert res["asset_id"] == _ASSET_ID


@pytest.mark.asyncio
async def test_do_generate_asset_qr_invalid_args():
    engine, _ = _make_mock_engine()

    with pytest.raises(ValueError, match="asset_id"):
        await do_generate_asset_qr(engine, {"namespace_id": _NAMESPACE_ID})

    with pytest.raises(ValueError, match="namespace_id"):
        await do_generate_asset_qr(engine, {"asset_id": _ASSET_ID})


# =============================================================================
# 3. Domain Core: do_get_room_register
# =============================================================================


@pytest.mark.asyncio
async def test_do_get_room_register_success():
    mock_rows = [
        {
            "id": _ASSET_ID,
            "bom_line_id": "BOM-001",
            "serial": "SN-001",
            "functional_location_id": _ROOM_ID,
            "lifecycle_state": "OPERATIONAL",
            "change_origin": "sync",
            "created_at": None,
            "updated_at": None,
        }
    ]
    engine, _ = _make_mock_engine(fetch_return=mock_rows)

    res = await do_get_room_register(
        engine,
        {
            "namespace_id": _NAMESPACE_ID,
            "functional_location_id": _ROOM_ID,
        },
    )

    assert res["ok"] is True
    assert res["room_id"] == _ROOM_ID
    assert res["total_assets"] == 1
    asset = res["assets"][0]
    assert asset["asset_id"] == _ASSET_ID
    assert asset["deep_link_url"] == f"/rooms/{_ROOM_ID}/assets/{_ASSET_ID}"
    assert asset["qr_endpoint"] == f"/api/assets/{_ASSET_ID}/qr"


@pytest.mark.asyncio
async def test_do_get_room_register_missing_room():
    engine, _ = _make_mock_engine()
    with pytest.raises(ValueError, match="functional_location_id"):
        await do_get_room_register(engine, {"namespace_id": _NAMESPACE_ID})


# =============================================================================
# 4. MCP Surface: handle_assets_generate_qr
# =============================================================================


@pytest.mark.asyncio
async def test_handle_assets_generate_qr():
    from nce.vertical_modules.assets.mcp_handlers import handle_assets_generate_qr

    mock_row = {
        "id": _ASSET_ID,
        "bom_line_id": "BOM-001",
        "serial": "SN-100",
        "functional_location_id": _ROOM_ID,
        "lifecycle_state": "INSTALLED",
    }
    engine, _ = _make_mock_engine(fetchrow_return=mock_row)

    payload = {
        "namespace_id": _NAMESPACE_ID,
        "asset_id": _ASSET_ID,
    }
    raw = await handle_assets_generate_qr(engine, payload)
    data = json.loads(raw)
    assert data["ok"] is True
    assert data["asset_id"] == _ASSET_ID
    assert "qr_svg" in data


# =============================================================================
# 5. REST Surface: api_assets_generate_qr & api_assets_register
# =============================================================================


@pytest.mark.asyncio
async def test_api_assets_generate_qr_success():
    from nce.admin_handlers._shared import admin_state
    from nce.admin_handlers.assets import api_assets_generate_qr

    mock_row = {
        "id": _ASSET_ID,
        "bom_line_id": "BOM-001",
        "serial": "SN-100",
        "functional_location_id": _ROOM_ID,
        "lifecycle_state": "INSTALLED",
    }
    engine, _ = _make_mock_engine(fetchrow_return=mock_row)

    with patch.object(admin_state, "engine", engine):
        req = _make_request(
            path_params={"id": _ASSET_ID},
            query={"namespace_id": _NAMESPACE_ID, "base_url": "https://av.company.com"},
        )
        resp = await api_assets_generate_qr(req)
        assert resp.status_code == 200
        body = json.loads(resp.body)
        assert body["ok"] is True
        assert body["asset_id"] == _ASSET_ID


@pytest.mark.asyncio
async def test_api_assets_generate_qr_not_found():
    from nce.admin_handlers._shared import admin_state
    from nce.admin_handlers.assets import api_assets_generate_qr

    engine, _ = _make_mock_engine(fetchrow_return=None)

    with patch.object(admin_state, "engine", engine):
        req = _make_request(
            path_params={"id": _ASSET_ID},
            query={"namespace_id": _NAMESPACE_ID},
        )
        resp = await api_assets_generate_qr(req)
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_api_assets_register_success():
    from nce.admin_handlers._shared import admin_state
    from nce.admin_handlers.assets import api_assets_register

    mock_rows = [
        {
            "id": _ASSET_ID,
            "bom_line_id": "BOM-001",
            "serial": "SN-001",
            "functional_location_id": _ROOM_ID,
            "lifecycle_state": "OPERATIONAL",
            "change_origin": "sync",
            "created_at": None,
            "updated_at": None,
        }
    ]
    engine, _ = _make_mock_engine(fetch_return=mock_rows)

    with patch.object(admin_state, "engine", engine):
        req = _make_request(
            query={"namespace_id": _NAMESPACE_ID, "functional_location_id": _ROOM_ID}
        )
        resp = await api_assets_register(req)
        assert resp.status_code == 200
        body = json.loads(resp.body)
        assert body["ok"] is True
        assert body["room_id"] == _ROOM_ID
        assert len(body["assets"]) == 1


# =============================================================================
# 6. Customer Portal Safe Projection
# =============================================================================


@pytest.mark.asyncio
async def test_do_asset_register_db_fallback_projection():
    """Verify do_asset_register retrieves from DB and redacts sensitive internal data."""
    from nce.vertical_modules.customer_portal.rooms import do_asset_register

    mock_rows = [
        {
            "id": _ASSET_ID,
            "bom_line_id": "BOM-SECRET-123",
            "serial": "SN-CUST-VISIBLE",
            "functional_location_id": _ROOM_ID,
            "lifecycle_state": "OPERATIONAL",
        }
    ]
    engine, _ = _make_mock_engine(fetch_return=mock_rows)

    params = {
        "namespace_id": _NAMESPACE_ID,
        "customer_scope_id": "00000000-0000-4000-8000-000000000099",
        "room_id": _ROOM_ID,
    }
    res = await do_asset_register(engine, params)
    assert res["room_id"] == _ROOM_ID
    assert res["total_assets"] == 1
    asset = res["assets"][0]
    assert asset["asset_id"] == _ASSET_ID
    assert asset["room_id"] == _ROOM_ID
    assert asset["serial_number"] == "SN-CUST-VISIBLE"
    assert asset["status"] == "OPERATIONAL"
    # Verify unallowed fields (like internal bom_line_id) are not leaked
    assert "bom_line_id" not in asset
