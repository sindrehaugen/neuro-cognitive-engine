"""tests.unit.test_agreements_resources — Comprehensive unit tests for C12 Agreements Resources.

Wave B-9:
Validates ResourceSpec definitions, REST API routes, tier redaction,
optimistic concurrency control, tenant RLS isolation, and MCP tool twins
for AGREEMENT, AGREEMENT_PARTY (AGREEMENT_SIGNATURE), and AGREEMENT_TEMPLATE resources.
"""

from __future__ import annotations

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
from nce.vertical_modules.agreements.resources import (
    AGREEMENT_PARTY_SPEC,
    AGREEMENT_SPEC,
    AGREEMENT_TEMPLATE_SPEC,
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


def test_agreements_specs_registered_and_configured():
    load_all_engine_resources()

    for spec, node_type, table, slug in [
        (AGREEMENT_SPEC, "AGREEMENT", "agreements", "agreements"),
        (AGREEMENT_PARTY_SPEC, "AGREEMENT_SIGNATURE", "agreement_parties", "parties"),
        (AGREEMENT_TEMPLATE_SPEC, "AGREEMENT_TEMPLATE", "agreement_templates", "templates"),
    ]:
        registered = get_resource_spec("agreements", slug)
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


def test_agreement_resource_crud_lifecycle():
    client = _client_for_spec(AGREEMENT_SPEC)
    emp = {"X-NCE-Principal-Tier": "employee"}

    # 1. Create
    resp = client.post(
        "/api/agreements/agreements",
        json={
            "namespace_id": _NS_A,
            "agreement_number": "AGR-2026-001",
            "title": "Master AV Service Agreement",
            "status": "active",
            "agreement_type": "service",
            "annual_value": 240000.00,
            "monthly_value": 20000.00,
            "currency": "NOK",
            "auto_renewal": True,
            "notice_period_days": 60,
        },
        headers=emp,
    )
    assert resp.status_code == 201, resp.text
    item = resp.json()
    agr_id = item["id"]
    v1 = item["updated_at"]
    assert item["agreement_number"] == "AGR-2026-001"
    assert item["title"] == "Master AV Service Agreement"
    # "status" is the response envelope's own success indicator, not the
    # agreement's business status -- the real value lives under "item",
    # same object, unaffected by the envelope-key precedence fix.
    assert item["status"] == "ok"
    assert item["item"]["status"] == "active"
    assert item["annual_value"] == 240000.00

    # 2. Get single item
    resp = client.get(f"/api/agreements/agreements/{agr_id}?namespace_id={_NS_A}", headers=emp)
    assert resp.status_code == 200
    assert resp.json()["id"] == agr_id

    # 3. List
    resp = client.get(f"/api/agreements/agreements?namespace_id={_NS_A}&status=active", headers=emp)
    assert resp.status_code == 200
    listing = resp.json()
    assert len(listing["items"]) == 1
    assert listing["items"][0]["id"] == agr_id

    # 4. Patch with optimistic concurrency
    import time

    time.sleep(0.01)
    resp = client.patch(
        f"/api/agreements/agreements/{agr_id}?namespace_id={_NS_A}",
        json={
            "expected_version": v1,
            "annual_value": 260000.00,
            "notice_period_days": 90,
        },
        headers=emp,
    )
    assert resp.status_code == 200
    patched = resp.json()
    assert patched["annual_value"] == 260000.00
    assert patched["notice_period_days"] == 90
    v2 = patched["updated_at"]
    assert v2 != v1

    # 5. Concurrency failure (409) with stale version
    resp = client.patch(
        f"/api/agreements/agreements/{agr_id}?namespace_id={_NS_A}",
        json={
            "expected_version": v1,
            "annual_value": 300000.00,
        },
        headers=emp,
    )
    assert resp.status_code == 409

    # 6. Archive (soft delete)
    resp = client.post(
        f"/api/agreements/agreements/{agr_id}/archive?namespace_id={_NS_A}", headers=emp
    )
    assert resp.status_code == 200

    # List default excludes archived
    resp = client.get(f"/api/agreements/agreements?namespace_id={_NS_A}", headers=emp)
    assert resp.status_code == 200
    assert len(resp.json()["items"]) == 0

    # List with include_archived=true shows it
    resp = client.get(
        f"/api/agreements/agreements?namespace_id={_NS_A}&include_archived=true", headers=emp
    )
    assert resp.status_code == 200
    assert len(resp.json()["items"]) == 1

    # 7. Restore
    resp = client.post(
        f"/api/agreements/agreements/{agr_id}/restore?namespace_id={_NS_A}", headers=emp
    )
    assert resp.status_code == 200
    resp = client.get(f"/api/agreements/agreements?namespace_id={_NS_A}", headers=emp)
    assert len(resp.json()["items"]) == 1


def test_agreement_party_crud_and_signature_status():
    client = _client_for_spec(AGREEMENT_PARTY_SPEC)
    agr_id = str(uuid.uuid4())

    # Create party
    resp = client.post(
        "/api/agreements/parties",
        json={
            "namespace_id": _NS_A,
            "agreement_id": agr_id,
            "party_type": "customer",
            "party_name": "Nordic Solutions AS",
            "org_number": "912345678",
            "signatory_name": "Ola Nordmann",
            "signatory_email": "ola@nordic.example",
            "role": "signatory",
            "signature_status": "sent",
            "signature_source": "oneflow",
        },
    )
    assert resp.status_code == 201, resp.text
    party = resp.json()
    party_id = party["id"]
    assert party["signatory_name"] == "Ola Nordmann"
    assert party["signature_status"] == "sent"

    # Patch signature status to signed
    resp = client.patch(
        f"/api/agreements/parties/{party_id}?namespace_id={_NS_A}",
        json={
            "expected_version": party["updated_at"],
            "signature_status": "signed",
            "signed_at": "2026-09-19T17:00:00Z",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["signature_status"] == "signed"
    assert resp.json()["signed_at"] == "2026-09-19T17:00:00Z"


def test_agreement_template_crud():
    client = _client_for_spec(AGREEMENT_TEMPLATE_SPEC)
    emp = {"X-NCE-Principal-Tier": "employee"}

    resp = client.post(
        "/api/agreements/templates",
        json={
            "namespace_id": _NS_A,
            "name": "Standard SLA Bronze",
            "code": "SLA-BRONZE",
            "category": "sla",
            "description": "Standard 8x5 support SLA profile",
            "sla_profile": {"response_hours": 8, "resolution_hours": 24},
            "is_active": True,
        },
        headers=emp,
    )
    assert resp.status_code == 201, resp.text
    tmpl = resp.json()
    tmpl_id = tmpl["id"]
    assert tmpl["name"] == "Standard SLA Bronze"

    resp = client.get(f"/api/agreements/templates/{tmpl_id}?namespace_id={_NS_A}", headers=emp)
    assert resp.status_code == 200
    assert resp.json()["sla_profile"] == {"response_hours": 8, "resolution_hours": 24}


# ===========================================================================
# 3. Multi-Tenant RLS Isolation
# ===========================================================================


def test_agreements_multi_tenant_isolation():
    client = _client_for_spec(AGREEMENT_SPEC)
    emp = {"X-NCE-Principal-Tier": "employee"}

    # Create agreement in Namespace A
    resp_a = client.post(
        "/api/agreements/agreements",
        json={
            "namespace_id": _NS_A,
            "agreement_number": "AGR-TENANT-A",
            "title": "Confidential Contract A",
            "annual_value": 500000.00,
        },
        headers=emp,
    )
    assert resp_a.status_code == 201
    agr_a_id = resp_a.json()["id"]

    # Create agreement in Namespace B
    resp_b = client.post(
        "/api/agreements/agreements",
        json={
            "namespace_id": _NS_B,
            "agreement_number": "AGR-TENANT-B",
            "title": "Confidential Contract B",
            "annual_value": 750000.00,
        },
        headers=emp,
    )
    assert resp_b.status_code == 201
    agr_b_id = resp_b.json()["id"]

    # Namespace A cannot see Namespace B's item
    resp = client.get(f"/api/agreements/agreements/{agr_b_id}?namespace_id={_NS_A}", headers=emp)
    assert resp.status_code == 404

    # Namespace B cannot see Namespace A's item
    resp = client.get(f"/api/agreements/agreements/{agr_a_id}?namespace_id={_NS_B}", headers=emp)
    assert resp.status_code == 404

    # Listing is isolated
    resp = client.get(f"/api/agreements/agreements?namespace_id={_NS_A}", headers=emp)
    items_a = resp.json()["items"]
    assert len(items_a) == 1
    assert items_a[0]["id"] == agr_a_id


# ===========================================================================
# 4. Principal Tier Redaction
# ===========================================================================


def test_agreement_tier_redaction():
    client = _client_for_spec(AGREEMENT_SPEC)

    resp = client.post(
        "/api/agreements/agreements",
        json={
            "namespace_id": _NS_A,
            "agreement_number": "AGR-CONFIDENTIAL",
            "title": "Enterprise SLA",
            "annual_value": 900000.00,
            "monthly_value": 75000.00,
            "currency": "NOK",
        },
    )
    assert resp.status_code == 201
    agr_id = resp.json()["id"]

    # 1. Internal employee tier sees financial values
    resp = client.get(
        f"/api/agreements/agreements/{agr_id}?namespace_id={_NS_A}",
        headers={"x-nce-principal-tier": "employee"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "annual_value" in data
    assert data["annual_value"] == 900000.00

    # 2. External customer tier does NOT see annual_value / monthly_value
    resp = client.get(
        f"/api/agreements/agreements/{agr_id}?namespace_id={_NS_A}",
        headers={"x-nce-principal-tier": "external-customer"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "annual_value" not in data
    assert "monthly_value" not in data
    assert data["title"] == "Enterprise SLA"
    assert data["agreement_number"] == "AGR-CONFIDENTIAL"


# ===========================================================================
# 5. MCP Tool Twins
# ===========================================================================


def test_agreements_mcp_tool_twins_registered():
    load_all_engine_resources()

    expected_tools = [
        "agreements_list_agreements",
        "agreements_get_agreements",
        "agreements_upsert_agreements",
        "agreements_archive_agreements",
        "agreements_list_parties",
        "agreements_get_parties",
        "agreements_upsert_parties",
        "agreements_archive_parties",
        "agreements_list_templates",
        "agreements_get_templates",
        "agreements_upsert_templates",
        "agreements_archive_templates",
    ]

    for tool_name in expected_tools:
        assert tool_name in TOOL_REGISTRY, f"Missing MCP tool in registry: {tool_name}"
        spec = TOOL_REGISTRY[tool_name]
        assert spec.handler is not None
        assert callable(spec.handler)
