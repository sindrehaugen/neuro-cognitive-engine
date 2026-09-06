"""Unit tests for Wave IN-1: GOODS_RECEIPT.created event emission and PO line linking."""

from __future__ import annotations

from contextlib import asynccontextmanager
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from nce.events import catalogue
from nce.vertical_modules.inventory.goods_receipt import do_record_goods_receipt


class TestGoodsReceiptCreatedEventContract:
    """Validate EVENT_CATALOGUE contract for GOODS_RECEIPT.created."""

    def test_goods_receipt_created_contract_is_active(self) -> None:
        contract = catalogue.EVENT_CATALOGUE.get("GOODS_RECEIPT.created")
        assert contract is not None
        assert contract.status == "ACTIVE"
        assert "nce/vertical_modules/inventory/goods_receipt.py" in contract.producers
        assert "nce/vertical_modules/project/automation.py" in contract.consumers


class TestGoodsReceiptCreatedEmission:
    """Validate that do_record_goods_receipt emits GOODS_RECEIPT.created via publish."""

    @pytest.mark.asyncio
    async def test_fresh_receipt_emits_goods_receipt_created_with_params(self) -> None:
        ns_id = uuid4()
        location_id = uuid4()
        receipt_id = uuid4()

        mock_conn = AsyncMock()
        mock_conn.fetchrow.side_effect = [
            {"id": receipt_id},  # INSERT RETURNING id
            {"qty_on_hand": 5.0},  # _increment_qty_on_hand
        ]
        mock_conn.fetch.return_value = []

        @asynccontextmanager
        async def fake_scoped(pool, namespace_id):
            yield mock_conn

        mock_engine = MagicMock()

        params = {
            "namespace_id": str(ns_id),
            "po_ref": "PO-TEST-1001",
            "location_id": str(location_id),
            "project_id": "proj-101",
            "bom_line_label": "BOM_LINE:Q-101:L1",
            "project_value": 45000.0,
            "lines": [{"sku": "SKU-A", "qty": 5, "unit_cost": Decimal("100.00")}],
        }

        with (
            patch(
                "nce.vertical_modules.inventory.goods_receipt.scoped_pg_session",
                side_effect=fake_scoped,
            ),
            patch(
                "nce.vertical_modules.inventory.goods_receipt.assert_owner", new_callable=AsyncMock
            ),
            patch(
                "nce.vertical_modules.inventory.goods_receipt.append_transaction",
                new_callable=AsyncMock,
            ),
            patch(
                "nce.vertical_modules.inventory.goods_receipt.emit_graph_write",
                new_callable=AsyncMock,
            ),
            patch(
                "nce.vertical_modules.inventory.goods_receipt.publish", new_callable=AsyncMock
            ) as mock_pub,
        ):
            result = await do_record_goods_receipt(mock_engine, params)

            assert result["ok"] is True
            assert result["duplicate"] is False
            assert result["receipt_id"] == str(receipt_id)

            mock_pub.assert_awaited_once()
            _, kwargs = mock_pub.call_args
            assert kwargs["node_type"] == "GOODS_RECEIPT"
            assert kwargs["op"] == "created"
            assert kwargs["namespace_id"] == ns_id
            payload = kwargs["payload"]
            assert payload["project_id"] == "proj-101"
            assert payload["bom_line_label"] == "BOM_LINE:Q-101:L1"
            assert payload["status"] == "DELIVERED"
            assert payload["po_ref"] == "PO-TEST-1001"
            assert payload["project_value"] == 45000.0

    @pytest.mark.asyncio
    async def test_fresh_receipt_resolves_po_lines_from_relational_store(self) -> None:
        ns_id = uuid4()
        location_id = uuid4()
        receipt_id = uuid4()

        mock_conn = AsyncMock()
        mock_conn.fetchrow.side_effect = [
            {"id": receipt_id},
            {"qty_on_hand": 10.0},
        ]
        # Return matched procurement_po_lines row
        mock_conn.fetch.return_value = [
            {
                "line_ref": "L1",
                "bom_line_label": "BOM_LINE:Q-200:L1",
                "project_id": "proj-200",
                "artnr": "SKU-B",
                "status": "ORDERED",
            }
        ]

        @asynccontextmanager
        async def fake_scoped(pool, namespace_id):
            yield mock_conn

        mock_engine = MagicMock()

        params = {
            "namespace_id": str(ns_id),
            "po_ref": "PO-TEST-2002",
            "location_id": str(location_id),
            "lines": [{"sku": "SKU-B", "qty": 10}],
        }

        with (
            patch(
                "nce.vertical_modules.inventory.goods_receipt.scoped_pg_session",
                side_effect=fake_scoped,
            ),
            patch(
                "nce.vertical_modules.inventory.goods_receipt.assert_owner", new_callable=AsyncMock
            ),
            patch(
                "nce.vertical_modules.inventory.goods_receipt.append_transaction",
                new_callable=AsyncMock,
            ),
            patch(
                "nce.vertical_modules.inventory.goods_receipt.emit_graph_write",
                new_callable=AsyncMock,
            ),
            patch(
                "nce.vertical_modules.inventory.goods_receipt.publish", new_callable=AsyncMock
            ) as mock_pub,
        ):
            result = await do_record_goods_receipt(mock_engine, params)

            assert result["ok"] is True
            assert result["duplicate"] is False

            mock_pub.assert_awaited_once()
            _, kwargs = mock_pub.call_args
            payload = kwargs["payload"]
            assert payload["project_id"] == "proj-200"
            assert payload["bom_line_label"] == "BOM_LINE:Q-200:L1"
            assert payload["status"] == "DELIVERED"

    @pytest.mark.asyncio
    async def test_duplicate_receipt_does_not_publish_event(self) -> None:
        ns_id = uuid4()
        location_id = uuid4()
        existing_receipt_id = uuid4()

        mock_conn = AsyncMock()
        mock_conn.fetchrow.side_effect = [
            None,  # INSERT ON CONFLICT returns None (duplicate)
            {"id": existing_receipt_id},  # SELECT id from goods_receipts
        ]

        @asynccontextmanager
        async def fake_scoped(pool, namespace_id):
            yield mock_conn

        mock_engine = MagicMock()

        params = {
            "namespace_id": str(ns_id),
            "po_ref": "PO-TEST-REPLAY",
            "location_id": str(location_id),
            "lines": [{"sku": "SKU-A", "qty": 1}],
        }

        with (
            patch(
                "nce.vertical_modules.inventory.goods_receipt.scoped_pg_session",
                side_effect=fake_scoped,
            ),
            patch(
                "nce.vertical_modules.inventory.goods_receipt.publish", new_callable=AsyncMock
            ) as mock_pub,
        ):
            result = await do_record_goods_receipt(mock_engine, params)

            assert result["ok"] is True
            assert result["duplicate"] is True
            assert result["receipt_id"] == str(existing_receipt_id)
            mock_pub.assert_not_awaited()
