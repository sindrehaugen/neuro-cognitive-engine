"""
tests/unit/test_sales_graph_relational_bridge.py
================================================
Unit tests for Wave R-6 (`Q-46` Option A — closing `break-h9b` & `break-h9c`):
Bridging Sales graph writes (`do_create_customer`, `do_create_lead`, `do_create_deal`,
`do_edit_deal`) to C12 relational parent tables (`sales_customers`, `sales_leads`,
`sales_deals`).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid5

import pytest

from nce.vertical_modules.sales.graph import (
    _resolve_entity_uuid,
    do_create_customer,
    do_create_deal,
    do_create_lead,
    do_edit_deal,
)

_NS_UUID = UUID("00000000-0000-4000-8000-000000000001")
_RAW_CUST_UUID = "11111111-1111-4111-8111-111111111111"
_RAW_LEAD_UUID = "22222222-2222-4222-8222-222222222222"
_RAW_DEAL_UUID = "33333333-3333-4333-8333-333333333333"


def test_resolve_entity_uuid_raw_uuid() -> None:
    """A raw UUID string resolves directly to that exact UUID."""
    resolved = _resolve_entity_uuid(_NS_UUID, _RAW_CUST_UUID, "CUSTOMER")
    assert resolved == UUID(_RAW_CUST_UUID)


def test_resolve_entity_uuid_nce_prefixed_uuid() -> None:
    """An 'nce:<uuid>' string resolves directly to the underlying UUID."""
    resolved = _resolve_entity_uuid(_NS_UUID, f"nce:{_RAW_DEAL_UUID}", "DEAL")
    assert resolved == UUID(_RAW_DEAL_UUID)

    resolved_upper = _resolve_entity_uuid(_NS_UUID, f"  NCE:{_RAW_DEAL_UUID}  ", "DEAL")
    assert resolved_upper == UUID(_RAW_DEAL_UUID)


def test_resolve_entity_uuid_non_uuid_string() -> None:
    """A non-UUID string resolves deterministically via uuid5(ns_uuid, '<PREFIX>:<ID>')."""
    resolved = _resolve_entity_uuid(_NS_UUID, "deal-001", "DEAL")
    assert resolved == uuid5(_NS_UUID, "DEAL:DEAL-001")

    resolved_nce = _resolve_entity_uuid(_NS_UUID, "nce:cust-12345", "CUSTOMER")
    assert resolved_nce == uuid5(_NS_UUID, "CUSTOMER:NCE:CUST-12345")


@pytest.mark.asyncio
async def test_do_create_customer_upserts_sales_customers() -> None:
    """do_create_customer upserts into sales_customers with exact UUID and name parameters."""
    conn = AsyncMock()
    conn.execute.return_value = "INSERT 0 1"

    with patch("nce.vertical_modules.sales.graph.assert_owner", new_callable=AsyncMock):
        res = await do_create_customer(
            conn,
            _NS_UUID,
            customer_id=f"nce:{_RAW_CUST_UUID}",
            name="Acme Audio AS",
        )

    assert res["ok"] is True
    rel_calls = [
        call for call in conn.execute.call_args_list if "INSERT INTO sales_customers" in call[0][0]
    ]
    assert len(rel_calls) == 1
    args = rel_calls[0][0]
    assert args[1] == _RAW_CUST_UUID
    assert args[2] == str(_NS_UUID)
    assert args[3] == "Acme Audio AS"
    assert args[4] == "Acme Audio AS"


@pytest.mark.asyncio
async def test_do_create_lead_upserts_sales_customers_and_sales_leads() -> None:
    """do_create_lead upserts customer (when provided) and lead into relational tables."""
    conn = AsyncMock()
    conn.execute.return_value = "INSERT 0 1"

    with patch("nce.vertical_modules.sales.graph.assert_owner", new_callable=AsyncMock):
        res = await do_create_lead(
            conn,
            _NS_UUID,
            lead_id=f"nce:{_RAW_LEAD_UUID}",
            customer_id="CUST-TEST-001",
            name="Arena Upgrade Lead",
        )

    assert res["ok"] is True
    expected_cust_uuid = str(uuid5(_NS_UUID, "CUSTOMER:CUST-TEST-001"))

    cust_calls = [
        call for call in conn.execute.call_args_list if "INSERT INTO sales_customers" in call[0][0]
    ]
    assert len(cust_calls) == 1
    assert cust_calls[0][0][1] == expected_cust_uuid
    assert cust_calls[0][0][2] == str(_NS_UUID)
    assert cust_calls[0][0][3] == "CUST-TEST-001"
    # $4 is None so ON CONFLICT DO UPDATE preserves any pre-existing sales_customers.name
    assert cust_calls[0][0][4] is None

    lead_calls = [
        call for call in conn.execute.call_args_list if "INSERT INTO sales_leads" in call[0][0]
    ]
    assert len(lead_calls) == 1
    assert lead_calls[0][0][1] == _RAW_LEAD_UUID
    assert lead_calls[0][0][2] == str(_NS_UUID)
    assert lead_calls[0][0][3] == "Arena Upgrade Lead"
    assert lead_calls[0][0][4] == expected_cust_uuid
    assert lead_calls[0][0][5] == "Arena Upgrade Lead"


@pytest.mark.asyncio
async def test_do_create_deal_upserts_relational_hierarchy() -> None:
    """do_create_deal upserts sales_customers, sales_leads (if lead_id given), and sales_deals."""
    conn = AsyncMock()
    conn.execute.return_value = "INSERT 0 1"

    with patch("nce.vertical_modules.sales.graph.assert_owner", new_callable=AsyncMock):
        res = await do_create_deal(
            conn,
            _NS_UUID,
            deal_id=f"nce:{_RAW_DEAL_UUID}",
            customer_id=f"nce:{_RAW_CUST_UUID}",
            quote_id="QUOTE-001",
            lead_id=f"nce:{_RAW_LEAD_UUID}",
            name="Main Stage Deal",
        )

    assert res["ok"] is True

    cust_calls = [
        call for call in conn.execute.call_args_list if "INSERT INTO sales_customers" in call[0][0]
    ]
    assert len(cust_calls) == 1
    assert cust_calls[0][0][1] == _RAW_CUST_UUID
    assert cust_calls[0][0][3] == f"nce:{_RAW_CUST_UUID}"
    assert cust_calls[0][0][4] is None

    lead_calls = [
        call for call in conn.execute.call_args_list if "INSERT INTO sales_leads" in call[0][0]
    ]
    assert len(lead_calls) == 1
    assert lead_calls[0][0][1] == _RAW_LEAD_UUID
    assert lead_calls[0][0][4] == _RAW_CUST_UUID
    assert lead_calls[0][0][5] is None

    deal_calls = [
        call for call in conn.execute.call_args_list if "INSERT INTO sales_deals" in call[0][0]
    ]
    assert len(deal_calls) == 1
    assert deal_calls[0][0][1] == _RAW_DEAL_UUID
    assert deal_calls[0][0][2] == str(_NS_UUID)
    assert deal_calls[0][0][3] == "Main Stage Deal"
    assert deal_calls[0][0][4] == _RAW_CUST_UUID
    assert deal_calls[0][0][5] == _RAW_LEAD_UUID
    assert deal_calls[0][0][6] == "Main Stage Deal"


@pytest.mark.asyncio
async def test_do_create_deal_without_name_preserves_existing_title_on_conflict() -> None:
    """When deal name is omitted, $3 falls back to deal_id for INSERT while $6 is None for COALESCE."""
    conn = AsyncMock()
    conn.execute.return_value = "INSERT 0 1"

    with patch("nce.vertical_modules.sales.graph.assert_owner", new_callable=AsyncMock):
        res = await do_create_deal(
            conn,
            _NS_UUID,
            deal_id="DEAL-unnamed",
            customer_id="CUST-1",
            quote_id="QUOTE-001",
            name=None,
        )

    assert res["ok"] is True
    deal_calls = [
        call for call in conn.execute.call_args_list if "INSERT INTO sales_deals" in call[0][0]
    ]
    assert len(deal_calls) == 1
    assert "COALESCE($6, sales_deals.title)" in deal_calls[0][0][0]
    assert deal_calls[0][0][3] == "DEAL-unnamed"
    assert deal_calls[0][0][6] is None


@pytest.mark.asyncio
async def test_do_edit_deal_updates_sales_deals_title() -> None:
    """do_edit_deal updates sales_deals.title when name is provided."""
    conn = AsyncMock()
    conn.execute.return_value = "UPDATE 1"

    with patch("nce.vertical_modules.sales.graph.assert_owner", new_callable=AsyncMock):
        res = await do_edit_deal(
            conn,
            _NS_UUID,
            deal_id="DEAL-001",
            name="Renamed Deal Title",
        )

    assert res["ok"] is True
    expected_deal_uuid = str(uuid5(_NS_UUID, "DEAL:DEAL-001"))

    upd_calls = [call for call in conn.execute.call_args_list if "UPDATE sales_deals" in call[0][0]]
    assert len(upd_calls) == 1
    assert upd_calls[0][0][1] == "Renamed Deal Title"
    assert upd_calls[0][0][2] == str(_NS_UUID)
    assert upd_calls[0][0][3] == expected_deal_uuid
