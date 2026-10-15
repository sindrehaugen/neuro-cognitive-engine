"""
tests/unit/test_sales_graph_relational_bridge.py
================================================
Unit tests for Wave R-6 (`Q-46` Option A — closing `break-h9b` & `break-h9c`):
Bridging Sales graph writes (`do_create_customer`, `do_create_lead`, `do_create_deal`,
`do_edit_deal`) to C12 relational parent tables (`sales_customers`, `sales_leads`,
`sales_deals`).
"""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import UUID, uuid5

import pytest

from nce.vertical_modules.sales.graph import (
    _resolve_entity_uuid,
    _upsert_relational_customer,
    _upsert_relational_deal,
    _upsert_relational_lead,
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
async def test_upsert_relational_customer_bind_parameters() -> None:
    """_upsert_relational_customer passes fallback $3 for INSERT and optional $4 for COALESCE."""
    conn = AsyncMock()
    conn.execute.return_value = "INSERT 0 1"

    cust_uuid = await _upsert_relational_customer(
        conn, _NS_UUID, f"nce:{_RAW_CUST_UUID}", name="Acme Audio AS"
    )
    assert cust_uuid == UUID(_RAW_CUST_UUID)
    args = conn.execute.call_args[0]
    assert "COALESCE($4, sales_customers.name)" in args[0]
    assert args[1] == _RAW_CUST_UUID
    assert args[2] == str(_NS_UUID)
    assert args[3] == "Acme Audio AS"
    assert args[4] == "Acme Audio AS"

    conn.reset_mock()
    await _upsert_relational_customer(conn, _NS_UUID, "CUST-TEST-001", name=None)
    args_unnamed = conn.execute.call_args[0]
    assert args_unnamed[3] == "CUST-TEST-001"
    assert args_unnamed[4] is None


@pytest.mark.asyncio
async def test_upsert_relational_lead_and_deal_bind_parameters() -> None:
    """_upsert_relational_lead and _upsert_relational_deal preserve existing titles when title=None."""
    conn = AsyncMock()
    conn.execute.return_value = "INSERT 0 1"
    cust_uuid = UUID(_RAW_CUST_UUID)

    lead_uuid = await _upsert_relational_lead(
        conn, _NS_UUID, f"nce:{_RAW_LEAD_UUID}", customer_uuid=cust_uuid, title=None
    )
    assert lead_uuid == UUID(_RAW_LEAD_UUID)
    lead_args = conn.execute.call_args[0]
    assert "COALESCE($5, sales_leads.title)" in lead_args[0]
    assert lead_args[1] == _RAW_LEAD_UUID
    assert lead_args[2] == str(_NS_UUID)
    assert lead_args[3] == f"nce:{_RAW_LEAD_UUID}"
    assert lead_args[4] == _RAW_CUST_UUID
    assert lead_args[5] is None

    conn.reset_mock()
    deal_uuid = await _upsert_relational_deal(
        conn,
        _NS_UUID,
        f"nce:{_RAW_DEAL_UUID}",
        customer_uuid=cust_uuid,
        lead_uuid=lead_uuid,
        title=None,
    )
    assert deal_uuid == UUID(_RAW_DEAL_UUID)
    deal_args = conn.execute.call_args[0]
    assert "COALESCE($6, sales_deals.title)" in deal_args[0]
    assert deal_args[1] == _RAW_DEAL_UUID
    assert deal_args[2] == str(_NS_UUID)
    assert deal_args[3] == f"nce:{_RAW_DEAL_UUID}"
    assert deal_args[4] == _RAW_CUST_UUID
    assert deal_args[5] == _RAW_LEAD_UUID
    assert deal_args[6] is None
