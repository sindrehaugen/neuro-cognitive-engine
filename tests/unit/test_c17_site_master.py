"""Unit test suite for C17 Site Master Data (Wave A-9).

Tests:
  1. Cadastre ID normalization pure function.
  2. get_site/update_site (plus multi-tenant isolation) -- the two service.py functions
     still called in production, by address_registry.py's Kartverket enrichment. The
     register's own CRUD (register, get_by_cadastre_id, list, archive, vessel telemetry)
     and the C1 site/FL-building hook were deleted as dead code (2026-09-21): the C12
     resource surface generates the reachable CRUD from SITE_SPEC, and nothing called
     the hand-written versions or the hook outside their own tests. See
     DEAD_HAND_WRITTEN_SERVICE_LAYERS_2026-09-21.md.
  3. C12 Resource Surface REST API & Principal Tier Redaction (contractor vs external-customer).
  4. C12 MCP tool definitions and execution specs.
"""

from __future__ import annotations

import uuid

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient
from tests._verified_tier_middleware import VERIFIED_TIER_TEST_MIDDLEWARE

from nce import admin_state
from nce.resource_surface import ResourceSpec, register_resource
from nce.resource_surface.rest import _clear_mem_store as _clear_rest_mem_store
from nce.resource_surface.rest import make_resource_routes
from nce.vertical_modules.sites.models import SiteItem, SiteUpdate
from nce.vertical_modules.sites.resources import SITE_SPEC
from nce.vertical_modules.sites.service import (
    _MEM_SITES,
    get_site,
    normalize_cadastre_id,
    update_site,
)
from nce.vertical_modules.sites.service import (
    _clear_mem_store as _clear_sites_mem_store,
)

_NS_A = str(uuid.UUID("11111111-1111-1111-1111-111111111111"))
_NS_B = str(uuid.UUID("22222222-2222-2222-2222-222222222222"))
_UUID_A = uuid.UUID(_NS_A)
_UUID_B = uuid.UUID(_NS_B)


@pytest.fixture(autouse=True)
def _reset_env():
    _clear_rest_mem_store()
    _clear_sites_mem_store()
    admin_state.engine = None
    register_resource(SITE_SPEC)
    yield
    _clear_rest_mem_store()
    _clear_sites_mem_store()


def _client_for_spec(spec: ResourceSpec) -> TestClient:
    app = Starlette(routes=make_resource_routes(spec), middleware=VERIFIED_TIER_TEST_MIDDLEWARE)
    return TestClient(app, raise_server_exceptions=False)


# ===========================================================================
# 1. Normalization Unit Tests
# ===========================================================================


def test_normalize_cadastre_id():
    """Verify cadastre_id normalization strips whitespace and handles nulls."""
    assert normalize_cadastre_id("0301-123/45/0/0") == "0301-123/45/0/0"
    assert normalize_cadastre_id("  0301-123/45/0/0  ") == "0301-123/45/0/0"
    assert normalize_cadastre_id("CAD-999") == "CAD-999"
    assert normalize_cadastre_id("") is None
    assert normalize_cadastre_id("   ") is None
    assert normalize_cadastre_id(None) is None


# ===========================================================================
# 2. get_site / update_site -- the two functions kept for
#    address_registry.py's Kartverket enrichment write-back. No register_site
#    remains to seed a fixture through the service API (deleted as dead code),
#    so these seed the in-memory store directly, the same store the deleted
#    function used to write to.
# ===========================================================================


def _seed_site(namespace_id: uuid.UUID, **overrides) -> SiteItem:
    site = SiteItem(
        id=overrides.pop("id", uuid.uuid4()),
        namespace_id=namespace_id,
        name=overrides.pop("name", "Oslo Innovation Hub"),
        **overrides,
    )
    _MEM_SITES.setdefault(str(namespace_id), {})[str(site.id)] = site
    return site


@pytest.mark.asyncio
async def test_get_and_update_site_in_memory():
    """Retrieve and update an in-memory site record."""
    site = _seed_site(
        _UUID_A,
        cadastre_id="0301-123/45/0/0",
        site_type="building",
        height=42.5,
    )

    fetched = await get_site(None, _UUID_A, site.id)
    assert fetched is not None
    assert fetched.name == "Oslo Innovation Hub"

    updated = await update_site(
        None,
        _UUID_A,
        site.id,
        SiteUpdate(name="Oslo Innovation Hub (Expanded)", height=48.0),
    )
    assert updated is not None
    assert updated.name == "Oslo Innovation Hub (Expanded)"
    assert updated.height == 48.0
    assert updated.cadastre_id == "0301-123/45/0/0"

    refetched = await get_site(None, _UUID_A, site.id)
    assert refetched is not None
    assert refetched.name == "Oslo Innovation Hub (Expanded)"


@pytest.mark.asyncio
async def test_update_site_missing_returns_none():
    """update_site on a non-existent id/namespace returns None, not an error."""
    result = await update_site(None, _UUID_A, uuid.uuid4(), SiteUpdate(name="Ghost Site"))
    assert result is None


@pytest.mark.asyncio
async def test_multi_tenant_isolation():
    """Verify tenant isolation: Namespace B cannot see or update Namespace A's site."""
    site_a = _seed_site(_UUID_A, cadastre_id="CAD-A-1")

    assert await get_site(None, _UUID_B, site_a.id) is None
    assert await update_site(None, _UUID_B, site_a.id, SiteUpdate(name="Hijacked")) is None

    # Namespace A itself still sees it
    assert await get_site(None, _UUID_A, site_a.id) is not None


# ===========================================================================
# 3. C12 Resource Surface REST API & Principal Tier Redaction
# ===========================================================================


def test_c12_resource_surface_rest_api_and_redaction():
    """Verify C12 REST endpoints for sites and field redaction per principal tier."""
    client = _client_for_spec(SITE_SPEC)

    # Create site via REST
    payload = {
        "name": "Bergen Technology Campus",
        "cadastre_id": "4601-50/1/0/0",
        "site_type": "campus",
        "address": {"city": "Bergen", "country": "NO"},
        "latitude": 60.39,
        "longitude": 5.32,
        "altitude": 10.0,
        "height": 25.0,
        "footprint_geometry": {"type": "Point", "coordinates": [5.32, 60.39]},
        "metadata": {"zone": "Nordic-1"},
    }

    headers_admin = {
        "X-NCE-Principal-Tier": "employee",
        "X-NCE-Namespace-ID": _NS_A,
    }

    res = client.post(SITE_SPEC.rest_collection_path, json=payload, headers=headers_admin)
    assert res.status_code == 201, res.text
    created = res.json()["item"]
    site_id = created["id"]
    assert created["name"] == "Bergen Technology Campus"
    assert created["cadastre_id"] == "4601-50/1/0/0"
    assert created["height"] == 25.0

    # Read as contractor: sees technical metadata, cadastre_id, height, footprint_geometry
    headers_contractor = {
        "X-NCE-Principal-Tier": "contractor",
        "X-NCE-Namespace-ID": _NS_A,
    }
    res_contractor = client.get(
        f"{SITE_SPEC.rest_collection_path}/{site_id}", headers=headers_contractor
    )
    assert res_contractor.status_code == 200
    contractor_data = res_contractor.json()
    assert "cadastre_id" in contractor_data
    assert "height" in contractor_data
    assert "footprint_geometry" in contractor_data

    # Read as external-customer: redacted!
    # Allowed: id, name, site_type, address, latitude, longitude, created_at
    # Redacted: cadastre_id, altitude, height, footprint_geometry, metadata
    headers_cust = {
        "X-NCE-Principal-Tier": "external-customer",
        "X-NCE-Namespace-ID": _NS_A,
    }
    res_cust = client.get(f"{SITE_SPEC.rest_collection_path}/{site_id}", headers=headers_cust)
    assert res_cust.status_code == 200
    cust_data = res_cust.json()
    assert cust_data["name"] == "Bergen Technology Campus"
    assert "cadastre_id" not in cust_data
    assert "height" not in cust_data
    assert "footprint_geometry" not in cust_data
    assert "metadata" not in cust_data

    # List endpoint
    res_list = client.get(SITE_SPEC.rest_collection_path, headers=headers_admin)
    assert res_list.status_code == 200
    assert len(res_list.json()["items"]) >= 1

    # Soft archive via POST /archive
    res_del = client.post(
        f"{SITE_SPEC.rest_collection_path}/{site_id}/archive", headers=headers_admin
    )
    assert res_del.status_code == 200
    assert res_del.json().get("archived") is True


# ===========================================================================
# 4. MCP Tool Definitions & Surface
# ===========================================================================


def test_c12_mcp_tools_advertised():
    """Verify C12 automatically registers all 4 MCP tools for SITE."""
    from nce.resource_surface.mcp import build_mcp_tool_definitions
    from nce.tool_registry import TOOL_REGISTRY

    tool_defs = build_mcp_tool_definitions(SITE_SPEC)
    assert len(tool_defs) == 4
    tool_names = {t.name for t in tool_defs}
    assert tool_names == {
        "sites_list_sites",
        "sites_get_sites",
        "sites_upsert_sites",
        "sites_archive_sites",
    }

    # Verify registered in TOOL_REGISTRY
    for name in tool_names:
        assert name in TOOL_REGISTRY, f"Tool {name} not found in TOOL_REGISTRY"
