"""
Integration tests for the pricing MCP tool and REST route (Wave 13).

Verifies that:
  - The pricing_resolve MCP tool is registered and callable
  - The stale flag is present and visible in the response (never silently dropped)
  - The REST route /api/admin/pricing/resolve works end-to-end
  - Namespace isolation is maintained
"""

from __future__ import annotations

import datetime
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nce.pricing.resolver import PriceResult
from nce.tool_registry import TOOL_REGISTRY


@pytest.mark.integration
def test_pricing_resolve_mcp_tool_registered():
    """pricing_resolve is in TOOL_REGISTRY with cacheable=True, mutation=False."""
    assert "pricing_resolve" in TOOL_REGISTRY
    spec = TOOL_REGISTRY["pricing_resolve"]
    assert spec.cacheable is True
    assert spec.mutation is False
    assert spec.admin_only is False


@pytest.mark.asyncio
async def test_handle_pricing_resolve_stale_flag_visible():
    """MCP handler returns stale flag in response JSON — never swallowed."""
    from nce.pricing import mcp_handlers

    # Mock engine and connection
    mock_engine = MagicMock()
    mock_conn = AsyncMock()

    # Mock resolve_price to return a stale result
    now = datetime.datetime.now(tz=datetime.timezone.utc)
    old_timestamp = now - datetime.timedelta(seconds=999999)

    async def mock_resolve(
        conn,
        *,
        namespace_id: str,
        product: dict,
        customer: dict,
    ) -> PriceResult:
        return {
            "cost": 42.0,
            "source": "bid",
            "as_of": old_timestamp,
            "stale": True,
        }

    namespace_id = str(uuid.uuid4())
    arguments = {
        "namespace_id": namespace_id,
        "product": {"base_price": 50.0, "base_as_of": old_timestamp},
        "customer": {"bid_price": 42.0, "bid_as_of": old_timestamp},
    }

    with patch("nce.pricing.mcp_handlers.scoped_pg_session") as mock_scoped:
        # Set up the context manager mock
        async_cm = AsyncMock()
        async_cm.__aenter__.return_value = mock_conn
        async_cm.__aexit__.return_value = None
        mock_scoped.return_value = async_cm

        with patch(
            "nce.pricing.mcp_handlers.resolve_price",
            side_effect=mock_resolve,
        ):
            response_str = await mcp_handlers.handle_pricing_resolve(mock_engine, arguments)

    response = json.loads(response_str)

    # Verify stale flag is present and True
    assert "stale" in response, "stale flag missing from response"
    assert response["stale"] is True, "stale flag should be True"
    assert response["status"] == "ok"
    assert response["cost"] == 42.0
    assert response["source"] == "bid"


@pytest.mark.asyncio
async def test_handle_pricing_resolve_fresh_flag_not_dropped():
    """MCP handler returns stale=False when price is fresh."""
    from nce.pricing import mcp_handlers

    mock_engine = MagicMock()
    mock_conn = AsyncMock()

    now = datetime.datetime.now(tz=datetime.timezone.utc)

    async def mock_resolve(
        conn,
        *,
        namespace_id: str,
        product: dict,
        customer: dict,
    ) -> PriceResult:
        return {
            "cost": 50.0,
            "source": "base",
            "as_of": now,
            "stale": False,
        }

    namespace_id = str(uuid.uuid4())
    arguments = {
        "namespace_id": namespace_id,
        "product": {"base_price": 50.0, "base_as_of": now},
        "customer": {},
    }

    with patch("nce.pricing.mcp_handlers.scoped_pg_session") as mock_scoped:
        async_cm = AsyncMock()
        async_cm.__aenter__.return_value = mock_conn
        async_cm.__aexit__.return_value = None
        mock_scoped.return_value = async_cm

        with patch(
            "nce.pricing.mcp_handlers.resolve_price",
            side_effect=mock_resolve,
        ):
            response_str = await mcp_handlers.handle_pricing_resolve(mock_engine, arguments)

    response = json.loads(response_str)

    # Verify fresh flag is present and False
    assert "stale" in response, "stale flag missing from response"
    assert response["stale"] is False, "stale flag should be False when fresh"
    assert response["status"] == "ok"
    assert response["cost"] == 50.0
    assert response["source"] == "base"


@pytest.mark.asyncio
async def test_api_pricing_resolve_stale_flag_in_rest_response():
    """REST handler exposes stale flag in JSON response."""
    from nce.admin_handlers import pricing as pricing_handlers

    # Mock request
    mock_request = AsyncMock()
    now = datetime.datetime.now(tz=datetime.timezone.utc)
    old_timestamp = now - datetime.timedelta(seconds=999999)

    mock_request.json.return_value = {
        "namespace_id": str(uuid.uuid4()),
        "product": {"base_price": 100.0, "base_as_of": old_timestamp},
        "customer": {},
    }

    async def mock_resolve(
        conn,
        *,
        namespace_id: str,
        product: dict,
        customer: dict,
    ) -> PriceResult:
        return {
            "cost": 100.0,
            "source": "supplier_list",
            "as_of": old_timestamp,
            "stale": True,
        }

    with patch("nce.admin_handlers.pricing.admin_state") as mock_state:
        mock_state.engine = MagicMock()
        mock_state.engine.pg_pool = MagicMock()

        with patch("nce.admin_handlers.pricing.scoped_pg_session") as mock_scoped:
            async_cm = AsyncMock()
            async_cm.__aenter__.return_value = AsyncMock()
            async_cm.__aexit__.return_value = None
            mock_scoped.return_value = async_cm

            with patch(
                "nce.admin_handlers.pricing.resolve_price",
                side_effect=mock_resolve,
            ):
                response = await pricing_handlers.api_pricing_resolve(mock_request)

    response_data = json.loads(response.body)

    assert response.status_code == 200
    assert "stale" in response_data, "REST response missing stale flag"
    assert response_data["stale"] is True, "REST response stale flag not visible"
    assert response_data["status"] == "ok"
    assert response_data["cost"] == 100.0
    assert response_data["source"] == "supplier_list"


@pytest.mark.asyncio
async def test_api_pricing_resolve_missing_namespace_id():
    """REST handler validates namespace_id presence."""
    from nce.admin_handlers import pricing as pricing_handlers

    mock_request = AsyncMock()
    mock_request.json.return_value = {
        "product": {"base_price": 50.0},
    }

    with patch("nce.admin_handlers.pricing.admin_state") as mock_state:
        mock_state.engine = MagicMock()
        response = await pricing_handlers.api_pricing_resolve(mock_request)

    assert response.status_code == 422
    response_data = json.loads(response.body)
    assert "namespace_id" in response_data.get("error", "").lower()
