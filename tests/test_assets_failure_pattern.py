"""
tests/test_assets_failure_pattern.py
====================================
Comprehensive tests for Assets failure pattern recording (Wave A-5).

Verifies:
  1. do_record_failure_pattern:
     - Valid write creates ASSET -[failure_pattern]-> PRODUCT_SKU in kg_edges.
     - Enforces Contract-A single-writer ownership check (assert_owner).
     - Missing asset returns not_found / raises AssetNotFoundError.
     - Input validation (missing product_sku, confidence bounds, uuid parsing).
  2. do_get_failure_patterns:
     - Queries failure patterns by asset_id, product_sku, or namespace.
  3. MCP handler:
     - handle_assets_record_failure_pattern schema validation and dispatch.
  4. REST routes:
     - POST /api/assets/failure-pattern and POST /api/assets/{id}/failure-pattern
       with cache generation bump on success.
  5. Cross-engine loop:
     - Edge recorded by Assets is readable by Product's get_failure_patterns helper.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from nce.admin_handlers import assets as assets_admin_handlers
from nce.entity_resolution.ownership import OwnershipError
from nce.vertical_modules.assets.failure_pattern import (
    AssetNotFoundError,
    do_get_failure_patterns,
    do_record_failure_pattern,
)
from nce.vertical_modules.assets.mcp_handlers import (
    handle_assets_record_failure_pattern,
)


@pytest.fixture
def sample_ns_id():
    return str(uuid4())


@pytest.fixture
def sample_asset_id():
    return str(uuid4())


@pytest.fixture
def mock_pool():
    pool = MagicMock()
    return pool


@pytest.fixture
def mock_engine(mock_pool):
    engine = MagicMock()
    engine.pg_pool = mock_pool
    return engine


class FakeRecord(dict):
    def __getitem__(self, key):
        return super().__getitem__(key)


@pytest.mark.asyncio
async def test_record_failure_pattern_success(sample_ns_id, sample_asset_id):
    """Verify recording a failure pattern edge succeeds and executes expected SQL."""
    mock_conn = AsyncMock()
    # 1. asset exists
    mock_conn.fetchrow.return_value = FakeRecord(
        {
            "id": sample_asset_id,
            "bom_line_id": "BL-001",
            "serial": "SN-ABC-123",
            "functional_location_id": "ROOM-101",
            "lifecycle_state": "ACTIVE",
        }
    )

    with (
        patch("nce.vertical_modules.assets.failure_pattern.scoped_pg_session") as mock_scoped,
        patch(
            "nce.vertical_modules.assets.failure_pattern.assert_owner", new_callable=AsyncMock
        ) as mock_assert,
    ):
        mock_scoped.return_value.__aenter__.return_value = mock_conn

        params = {
            "namespace_id": sample_ns_id,
            "asset_id": sample_asset_id,
            "product_sku": "SKU-AMP-800",
            "confidence": 0.95,
            "failure_mode": "overheating_shutdown",
            "severity": "critical",
            "notes": "Unit shuts down after 45 minutes under 8-ohm load",
        }

        mock_pool = MagicMock()
        result = await do_record_failure_pattern(mock_pool, params)

        assert result["ok"] is True
        assert result["asset_id"] == sample_asset_id
        assert result["product_sku"] == "SKU-AMP-800"
        assert result["confidence"] == 0.95
        assert result["failure_mode"] == "overheating_shutdown"
        assert result["severity"] == "critical"
        assert (
            result["edge"]
            == f"ASSET:{sample_asset_id} -[failure_pattern]-> PRODUCT_SKU:SKU-AMP-800"
        )

        # Assert Contract-A was called
        mock_assert.assert_awaited_once()
        args = mock_assert.call_args[0]
        assert args[2] == "ASSET"
        assert args[3] == "assets"

        # Assert INSERT into kg_edges and UPDATE assets were called
        assert mock_conn.execute.call_count == 2
        insert_sql = mock_conn.execute.call_args_list[0][0][0]
        assert "INSERT INTO kg_edges" in insert_sql
        assert "failure_pattern" in insert_sql


@pytest.mark.asyncio
async def test_record_failure_pattern_asset_not_found(sample_ns_id, sample_asset_id):
    """Missing asset returns not_found dictionary by default, or raises when requested."""
    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = None

    with patch("nce.vertical_modules.assets.failure_pattern.scoped_pg_session") as mock_scoped:
        mock_scoped.return_value.__aenter__.return_value = mock_conn

        params = {
            "namespace_id": sample_ns_id,
            "asset_id": sample_asset_id,
            "product_sku": "SKU-001",
        }

        mock_pool = MagicMock()
        result = await do_record_failure_pattern(mock_pool, params)
        assert result["ok"] is False
        assert result["not_found"] is True
        assert "not found" in result["error"].lower()

        # With raise_on_not_found=True
        params["raise_on_not_found"] = True
        with pytest.raises(AssetNotFoundError):
            await do_record_failure_pattern(mock_pool, params)


@pytest.mark.asyncio
async def test_record_failure_pattern_contract_a_violation(sample_ns_id, sample_asset_id):
    """If Contract-A ownership check fails, OwnershipError is propagated."""
    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = FakeRecord({"id": sample_asset_id})

    with (
        patch("nce.vertical_modules.assets.failure_pattern.scoped_pg_session") as mock_scoped,
        patch(
            "nce.vertical_modules.assets.failure_pattern.assert_owner",
            side_effect=OwnershipError(
                node_type="ASSET", writer_engine="assets", owner_engine=None, transition=None
            ),
        ),
    ):
        mock_scoped.return_value.__aenter__.return_value = mock_conn

        params = {
            "namespace_id": sample_ns_id,
            "asset_id": sample_asset_id,
            "product_sku": "SKU-001",
        }

        mock_pool = MagicMock()
        with pytest.raises(OwnershipError):
            await do_record_failure_pattern(mock_pool, params)


@pytest.mark.asyncio
async def test_record_failure_pattern_validation(sample_ns_id, sample_asset_id):
    """Test validation errors for missing SKU, bad confidence, invalid UUIDs."""
    mock_pool = MagicMock()

    # Missing product_sku
    with pytest.raises(ValueError, match="product_sku is required"):
        await do_record_failure_pattern(
            mock_pool,
            {
                "namespace_id": sample_ns_id,
                "asset_id": sample_asset_id,
                "product_sku": "",
            },
        )

    # Invalid confidence (< 0 or > 1)
    with pytest.raises(ValueError, match="confidence must be between"):
        await do_record_failure_pattern(
            mock_pool,
            {
                "namespace_id": sample_ns_id,
                "asset_id": sample_asset_id,
                "product_sku": "SKU-01",
                "confidence": 1.5,
            },
        )

    # Invalid asset UUID
    with pytest.raises(ValueError, match="invalid asset_id"):
        await do_record_failure_pattern(
            mock_pool,
            {
                "namespace_id": sample_ns_id,
                "asset_id": "not-a-uuid",
                "product_sku": "SKU-01",
            },
        )


@pytest.mark.asyncio
async def test_get_failure_patterns(sample_ns_id, sample_asset_id):
    """Verify querying failure patterns with filters."""
    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = [
        FakeRecord(
            {
                "subject_label": f"ASSET:{sample_asset_id}",
                "predicate": "failure_pattern",
                "object_label": "PRODUCT_SKU:SKU-MIC-01",
                "confidence": 0.9,
                "created_at": None,
                "updated_at": None,
            }
        )
    ]

    with patch("nce.vertical_modules.assets.failure_pattern.scoped_pg_session") as mock_scoped:
        mock_scoped.return_value.__aenter__.return_value = mock_conn

        mock_pool = MagicMock()
        res = await do_get_failure_patterns(
            mock_pool,
            {
                "namespace_id": sample_ns_id,
                "asset_id": sample_asset_id,
                "product_sku": "SKU-MIC-01",
            },
        )

        assert res["ok"] is True
        assert res["count"] == 1
        assert res["patterns"][0]["object_label"] == "PRODUCT_SKU:SKU-MIC-01"


@pytest.mark.asyncio
async def test_handle_assets_record_failure_pattern_mcp(mock_engine, sample_ns_id, sample_asset_id):
    """Verify MCP handler dispatches cleanly."""
    expected = {
        "ok": True,
        "asset_id": sample_asset_id,
        "product_sku": "SKU-001",
        "edge": f"ASSET:{sample_asset_id} -[failure_pattern]-> PRODUCT_SKU:SKU-001",
    }

    with patch(
        "nce.vertical_modules.assets.mcp_handlers.do_record_failure_pattern", new_callable=AsyncMock
    ) as mock_core:
        mock_core.return_value = expected

        raw = await handle_assets_record_failure_pattern(
            mock_engine,
            {
                "namespace_id": sample_ns_id,
                "asset_id": sample_asset_id,
                "product_sku": "SKU-001",
            },
        )

        data = json.loads(raw)
        assert data["ok"] is True
        assert data["asset_id"] == sample_asset_id
        mock_core.assert_awaited_once()


@pytest.mark.asyncio
async def test_rest_record_failure_pattern_success(mock_engine, sample_ns_id, sample_asset_id):
    """Verify REST route returns JSONResponse and bumps MCP cache generation."""
    req = AsyncMock()
    req.path_params = {"id": sample_asset_id}
    req.query_params = {}
    req.json = AsyncMock(
        return_value={
            "namespace_id": sample_ns_id,
            "product_sku": "SKU-SWITCH-48P",
            "confidence": 1.0,
            "failure_mode": "poe_controller_fault",
        }
    )

    expected = {
        "ok": True,
        "asset_id": sample_asset_id,
        "product_sku": "SKU-SWITCH-48P",
        "edge": f"ASSET:{sample_asset_id} -[failure_pattern]-> PRODUCT_SKU:SKU-SWITCH-48P",
    }

    with (
        patch("nce.admin_handlers.assets.admin_state") as mock_state,
        patch(
            "nce.admin_handlers.assets.do_record_failure_pattern", new_callable=AsyncMock
        ) as mock_core,
        patch(
            "nce.admin_handlers.assets.bump_mcp_cache_generation", new_callable=AsyncMock
        ) as mock_bump,
    ):
        mock_state.engine = mock_engine
        mock_core.return_value = expected

        response = await assets_admin_handlers.api_assets_record_failure_pattern(req)

        assert response.status_code == 200
        data = json.loads(response.body.decode("utf-8"))
        assert data["ok"] is True
        assert data["product_sku"] == "SKU-SWITCH-48P"

        mock_bump.assert_awaited_once_with(mock_engine, route="api_assets_record_failure_pattern")


@pytest.mark.asyncio
async def test_rest_record_failure_pattern_not_found(mock_engine, sample_ns_id, sample_asset_id):
    """Verify REST route returns 404 when asset not found."""
    req = AsyncMock()
    req.path_params = {"id": sample_asset_id}
    req.query_params = {}
    req.json = AsyncMock(
        return_value={
            "namespace_id": sample_ns_id,
            "product_sku": "SKU-001",
        }
    )

    with (
        patch("nce.admin_handlers.assets.admin_state") as mock_state,
        patch(
            "nce.admin_handlers.assets.do_record_failure_pattern", new_callable=AsyncMock
        ) as mock_core,
    ):
        mock_state.engine = mock_engine
        mock_core.return_value = {"ok": False, "not_found": True, "error": "Asset not found"}

        response = await assets_admin_handlers.api_assets_record_failure_pattern(req)
        assert response.status_code == 404


@pytest.mark.asyncio
async def test_cross_engine_product_reader_sees_asset_edge(sample_ns_id, sample_asset_id):
    """Verify that Product's get_failure_patterns reads edges recorded from Assets."""
    from nce.vertical_modules.product.watchers import get_failure_patterns

    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = [
        FakeRecord(
            {
                "subject_label": f"ASSET:{sample_asset_id}",
                "predicate": "failure_pattern",
                "object_label": "PRODUCT_SKU:SKU-DISPLAY-75",
                "confidence": 0.88,
            }
        )
    ]

    from uuid import UUID

    patterns = await get_failure_patterns(
        mock_conn,
        UUID(sample_ns_id),
        "PRODUCT_SKU:SKU-DISPLAY-75",
    )

    assert len(patterns) == 1
    assert patterns[0]["subject_label"] == f"ASSET:{sample_asset_id}"
    assert patterns[0]["predicate"] == "failure_pattern"
    assert patterns[0]["object_label"] == "PRODUCT_SKU:SKU-DISPLAY-75"
    assert patterns[0]["confidence"] == 0.88
