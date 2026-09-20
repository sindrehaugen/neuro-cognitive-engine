"""tests.unit.test_sales_resources — Comprehensive unit tests for C12 Sales Resources.

Wave B-1:
Validates ResourceSpec definitions, REST API routes, tier redaction,
optimistic concurrency control, tenant RLS isolation, and MCP tool twins
for CUSTOMER, LEAD, DEAL, and QUOTE resources.
"""

from __future__ import annotations

import json
import uuid

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient
from tests._verified_tier_middleware import VERIFIED_TIER_TEST_MIDDLEWARE

from nce import admin_state
from nce.resource_surface import (
    get_resource_spec,
    load_all_engine_resources,
)
from nce.resource_surface.rest import _clear_mem_store, make_resource_routes
from nce.tool_registry import TOOL_REGISTRY
from nce.vertical_modules.sales.resources import (
    CUSTOMER_SPEC,
    DEAL_SPEC,
    LEAD_SPEC,
    QUOTE_SPEC,
)

_NS_A = str(uuid.UUID("11111111-1111-1111-1111-111111111111"))
_NS_B = str(uuid.UUID("22222222-2222-2222-2222-222222222222"))


@pytest.fixture(autouse=True)
def _reset_env():
    _clear_mem_store()
    admin_state.engine = None
    yield
    _clear_mem_store()


def _client_for_spec(spec) -> TestClient:
    app = Starlette(routes=make_resource_routes(spec), middleware=VERIFIED_TIER_TEST_MIDDLEWARE)
    return TestClient(app, raise_server_exceptions=False)


# ===========================================================================
# 1. ResourceSpec Contract & Discovery Tests
# ===========================================================================


def test_sales_specs_registered_and_configured():
    load_all_engine_resources()

    for spec, node_type, table, slug in [
        (CUSTOMER_SPEC, "CUSTOMER", "sales_customers", "customers"),
        (LEAD_SPEC, "LEAD", "sales_leads", "leads"),
        (DEAL_SPEC, "DEAL", "sales_deals", "deals"),
        (QUOTE_SPEC, "QUOTE", "sales_quotes", "quotes"),
    ]:
        registered = get_resource_spec("sales", slug)
        assert registered is not None
        assert registered == spec
        assert registered.node_type == node_type
        assert registered.table_name == table
        assert registered.tenant_scope == "tenant"
        assert registered.soft_delete_field == "is_archived"
        assert registered.version_field == "updated_at"


# ===========================================================================
# 2. REST Lifecycle (Create, Get, List, Patch, Archive, Concurrency)
# ===========================================================================


def test_customer_resource_crud_lifecycle():
    client = _client_for_spec(CUSTOMER_SPEC)

    # 1. Create
    resp = client.post(
        "/api/sales/customers",
        json={
            "namespace_id": _NS_A,
            "name": "Acme Corp",
            "org_number": "999888777",
            "email": "contact@acme.example",
            "tier": "enterprise",
            "status": "active",
        },
    )
    assert resp.status_code == 201, resp.text
    item = resp.json()
    cust_id = item["id"]
    v1 = item["version"]
    assert item["name"] == "Acme Corp"
    assert item["tier"] == "enterprise"

    emp = {"X-NCE-Principal-Tier": "employee"}

    # 2. Get single item
    get_resp = client.get(f"/api/sales/customers/{cust_id}?namespace_id={_NS_A}", headers=emp)
    assert get_resp.status_code == 200
    assert get_resp.json()["id"] == cust_id

    # 3. Patch with concurrency protection
    # Wrong version -> 409
    conflict_resp = client.patch(
        f"/api/sales/customers/{cust_id}",
        headers={"If-Match": '"wrong-version"'},
        json={"namespace_id": _NS_A, "tier": "vip"},
    )
    assert conflict_resp.status_code == 409

    # Valid patch
    patch_resp = client.patch(
        f"/api/sales/customers/{cust_id}",
        headers={"If-Match": f'"{v1}"'},
        json={"namespace_id": _NS_A, "tier": "vip"},
    )
    assert patch_resp.status_code == 200
    patched = patch_resp.json()
    assert patched["tier"] == "vip"

    # 4. Filtered List
    list_resp = client.get(f"/api/sales/customers?namespace_id={_NS_A}&status=active", headers=emp)
    assert list_resp.status_code == 200
    data = list_resp.json()
    assert len(data["items"]) == 1
    assert data["items"][0]["id"] == cust_id

    # 5. Archive (Soft delete)
    arc_resp = client.post(
        f"/api/sales/customers/{cust_id}/archive",
        json={"namespace_id": _NS_A, "reason": "Test archive"},
    )
    assert arc_resp.status_code == 200
    assert arc_resp.json()["archived"] is True

    # List should hide archived items by default
    assert len(client.get(f"/api/sales/customers?namespace_id={_NS_A}").json()["items"]) == 0

    # 6. Restore
    restore_resp = client.post(
        f"/api/sales/customers/{cust_id}/restore",
        json={"namespace_id": _NS_A},
    )
    assert restore_resp.status_code == 200
    assert restore_resp.json()["archived"] is False

    # List shows it again
    assert len(client.get(f"/api/sales/customers?namespace_id={_NS_A}").json()["items"]) == 1


def test_lead_deal_quote_creation_and_filtering():
    client_lead = _client_for_spec(LEAD_SPEC)
    client_deal = _client_for_spec(DEAL_SPEC)
    client_quote = _client_for_spec(QUOTE_SPEC)

    # Lead
    lead_resp = client_lead.post(
        "/api/sales/leads",
        json={
            "namespace_id": _NS_A,
            "title": "Hospital Expansion",
            "contact_name": "Dr. House",
            "company": "Princeton Hospital",
            "source": "referral",
            "status": "qualified",
            "estimated_value": 500000.0,
        },
    )
    assert lead_resp.status_code == 201
    lead_id = lead_resp.json()["id"]

    # Deal
    deal_resp = client_deal.post(
        "/api/sales/deals",
        json={
            "namespace_id": _NS_A,
            "title": "Princeton AV Infrastructure",
            "lead_id": lead_id,
            "stage": "proposal",
            "value": 450000.0,
            "currency": "NOK",
        },
    )
    assert deal_resp.status_code == 201
    deal_id = deal_resp.json()["id"]

    # Quote
    quote_resp = client_quote.post(
        "/api/sales/quotes",
        json={
            "namespace_id": _NS_A,
            "quote_number": "Q-2026-001",
            "deal_id": deal_id,
            "title": "Phase 1 Sound System",
            "version": 1,
            "status": "draft",
            "total_ex_vat": 360000.0,
            "total_inc_vat": 450000.0,
            "currency": "NOK",
        },
    )
    assert quote_resp.status_code == 201
    assert "id" in quote_resp.json()

    # Verify query by filters
    assert (
        client_lead.get(f"/api/sales/leads?namespace_id={_NS_A}&status=qualified").json()["total"]
        == 1
    )
    assert (
        client_deal.get(f"/api/sales/deals?namespace_id={_NS_A}&stage=proposal").json()["total"]
        == 1
    )
    assert (
        client_quote.get(f"/api/sales/quotes?namespace_id={_NS_A}&deal_id={deal_id}").json()[
            "total"
        ]
        == 1
    )


# ===========================================================================
# 3. Tier Redaction & Multi-Tenant Isolation
# ===========================================================================


def test_tier_redaction_for_external_customers():
    client = _client_for_spec(CUSTOMER_SPEC)
    resp = client.post(
        "/api/sales/customers",
        json={
            "namespace_id": _NS_A,
            "name": "Nordic Tech",
            "org_number": "123456789",
            "email": "info@nordic.example",
            "tier": "enterprise",
            "status": "active",
        },
    )
    item_id = resp.json()["id"]

    # External customer request: tier and status should be redacted per tier_allowlists
    redacted_resp = client.get(
        f"/api/sales/customers/{item_id}?namespace_id={_NS_A}",
        headers={"X-NCE-Principal-Tier": "external-customer"},
    )
    assert redacted_resp.status_code == 200
    redacted = redacted_resp.json()
    assert "name" in redacted
    assert "email" in redacted
    assert "tier" not in redacted
    assert "status" not in redacted


def test_cross_namespace_tenant_isolation():
    client = _client_for_spec(CUSTOMER_SPEC)
    resp = client.post(
        "/api/sales/customers",
        json={
            "namespace_id": _NS_A,
            "name": "Secret Tenant A Customer",
        },
    )
    item_id = resp.json()["id"]

    # Namespace B should NOT be able to read Namespace A's customer
    cross_resp = client.get(f"/api/sales/customers/{item_id}?namespace_id={_NS_B}")
    assert cross_resp.status_code == 404


# ===========================================================================
# 4. MCP Tool Twins Execution
# ===========================================================================


@pytest.mark.asyncio
async def test_sales_mcp_tool_twins_execution():
    fake_engine = object()

    # 1. Upsert customer
    upsert_customer_tool = TOOL_REGISTRY["sales_upsert_customers"]
    upsert_res = await upsert_customer_tool.handler(
        fake_engine,
        {
            "namespace_id": _NS_A,
            "name": "MCP Customer Inc",
            "email": "mcp@customer.example",
            "status": "active",
        },
    )
    upsert_data = json.loads(upsert_res)
    assert "id" in upsert_data
    cust_id = upsert_data["id"]

    # 2. Get customer
    get_customer_tool = TOOL_REGISTRY["sales_get_customers"]
    get_res = await get_customer_tool.handler(
        fake_engine,
        {"namespace_id": _NS_A, "id": cust_id},
    )
    get_data = json.loads(get_res)
    assert get_data["id"] == cust_id
    assert get_data["name"] == "MCP Customer Inc"

    # 3. List customers
    list_customer_tool = TOOL_REGISTRY["sales_list_customers"]
    list_res = await list_customer_tool.handler(
        fake_engine,
        {"namespace_id": _NS_A, "limit": 10},
    )
    list_data = json.loads(list_res)
    assert len(list_data["items"]) == 1

    # 4. Archive customer
    archive_customer_tool = TOOL_REGISTRY["sales_archive_customers"]
    archive_res = await archive_customer_tool.handler(
        fake_engine,
        {"namespace_id": _NS_A, "id": cust_id},
    )
    archive_data = json.loads(archive_res)
    assert archive_data["archived"] is True
