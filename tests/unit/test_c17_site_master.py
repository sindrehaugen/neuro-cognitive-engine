"""Unit test suite for C17 Site Master Data (Wave A-9).

Tests:
  1. C17 Site Master Data CRUD lifecycle (register, get, get_by_cadastre_id, update, archive).
  2. Vessel site telemetry stream ingestion and dynamic positioning.
  3. Cadastre ID normalization pure function.
  4. Multi-tenant isolation across namespaces.
  5. C1 Site & FL Building Resolution Hook & Merge Queue Gating:
     - Cadastre ID is the FL-building match key (score = 1.0).
     - Two FL buildings with the same cadastre ID land in entity_merge_queue.
     - CRITICAL INVARIANT: NEVER auto-merged (auto_merged is False, status is 'pending').
  6. C12 Resource Surface REST API & Principal Tier Redaction (contractor vs external-customer).
  7. C12 MCP tool definitions and execution specs.
  8. U18 standing positive controls.
"""

from __future__ import annotations

import uuid

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient
from tests._verified_tier_middleware import VERIFIED_TIER_TEST_MIDDLEWARE

from nce import admin_state
from nce.entity_resolution.site_hook import (
    _clear_hook_mem_store,
    get_mem_merge_queue,
    reconcile_fl_building_with_site,
    register_mem_fl_building,
)
from nce.resource_surface import ResourceSpec, register_resource
from nce.resource_surface.rest import _clear_mem_store as _clear_rest_mem_store
from nce.resource_surface.rest import make_resource_routes
from nce.vertical_modules.sites.models import SiteCreate, SiteUpdate
from nce.vertical_modules.sites.resources import SITE_SPEC
from nce.vertical_modules.sites.service import (
    _clear_mem_store as _clear_sites_mem_store,
)
from nce.vertical_modules.sites.service import (
    archive_site,
    get_site,
    get_site_by_cadastre_id,
    list_sites,
    normalize_cadastre_id,
    register_site,
    update_site,
    update_vessel_telemetry,
)

_NS_A = str(uuid.UUID("11111111-1111-1111-1111-111111111111"))
_NS_B = str(uuid.UUID("22222222-2222-2222-2222-222222222222"))
_UUID_A = uuid.UUID(_NS_A)
_UUID_B = uuid.UUID(_NS_B)


@pytest.fixture(autouse=True)
def _reset_env():
    _clear_rest_mem_store()
    _clear_sites_mem_store()
    _clear_hook_mem_store()
    admin_state.engine = None
    register_resource(SITE_SPEC)
    yield
    _clear_rest_mem_store()
    _clear_sites_mem_store()
    _clear_hook_mem_store()


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
# 2. Service CRUD & Lifecycle Tests
# ===========================================================================


@pytest.mark.asyncio
async def test_site_crud_lifecycle():
    """Verify full CRUD lifecycle: register, get, update, list, and archive."""
    create_payload = SiteCreate(
        name="Oslo Innovation Hub",
        cadastre_id="0301-123/45/0/0",
        site_type="building",
        address={
            "street": "Storgata 1",
            "postal_code": "0155",
            "city": "Oslo",
            "country": "NO",
            "is_validated": True,
        },
        latitude=59.9139,
        longitude=10.7522,
        altitude=15.0,
        height=42.5,
        footprint_geometry={
            "type": "Polygon",
            "coordinates": [[[10.75, 59.91], [10.76, 59.91], [10.76, 59.92], [10.75, 59.91]]],
        },
        metadata={"campus": "Sentrum", "code": "HUB-01"},
    )

    created = await register_site(None, _UUID_A, create_payload)
    assert created.id is not None
    assert created.namespace_id == _UUID_A
    assert created.name == "Oslo Innovation Hub"
    assert created.cadastre_id == "0301-123/45/0/0"
    assert created.height == 42.5
    assert created.address["is_validated"] is True
    assert not created.archived

    # Retrieve by ID
    fetched = await get_site(None, _UUID_A, created.id)
    assert fetched is not None
    assert fetched.name == "Oslo Innovation Hub"

    # Retrieve by cadastre_id
    by_cad = await get_site_by_cadastre_id(None, _UUID_A, "0301-123/45/0/0")
    assert by_cad is not None
    assert by_cad.id == created.id

    # Update
    updated = await update_site(
        None,
        _UUID_A,
        created.id,
        SiteUpdate(name="Oslo Innovation Hub (Expanded)", height=48.0),
    )
    assert updated is not None
    assert updated.name == "Oslo Innovation Hub (Expanded)"
    assert updated.height == 48.0
    assert updated.cadastre_id == "0301-123/45/0/0"

    # List
    items = await list_sites(None, _UUID_A, site_type="building")
    assert len(items) == 1
    assert items[0].id == created.id

    # Archive
    archived = await archive_site(None, _UUID_A, created.id)
    assert archived is True

    # Check that archived site is not returned by default lookups
    assert await get_site(None, _UUID_A, created.id) is None
    assert await get_site_by_cadastre_id(None, _UUID_A, "0301-123/45/0/0") is None
    assert len(await list_sites(None, _UUID_A)) == 0


@pytest.mark.asyncio
async def test_vessel_telemetry_positioning():
    """Verify vessel site telemetry stream and coordinate updates."""
    create_payload = SiteCreate(
        name="Research Vessel Nansen",
        site_type="vessel",
        latitude=60.3913,
        longitude=5.3221,
        telemetry_stream={"heading": 180, "speed_knots": 12.4},
    )
    vessel = await register_site(None, _UUID_A, create_payload)
    assert vessel.site_type == "vessel"
    assert vessel.telemetry_stream["speed_knots"] == 12.4

    # Update dynamic position via telemetry stream
    updated = await update_vessel_telemetry(
        None,
        _UUID_A,
        vessel.id,
        latitude=60.4500,
        longitude=5.4000,
        telemetry={"heading": 195, "speed_knots": 14.1, "source": "AIS"},
    )
    assert updated is not None
    assert updated.latitude == 60.4500
    assert updated.longitude == 5.4000
    assert updated.telemetry_stream["speed_knots"] == 14.1
    assert updated.telemetry_stream["source"] == "AIS"


@pytest.mark.asyncio
async def test_multi_tenant_isolation():
    """Verify tenant isolation: Namespace B cannot see Namespace A sites."""
    site_a = await register_site(
        None,
        _UUID_A,
        SiteCreate(name="Site Alpha", cadastre_id="CAD-A-1"),
    )

    # Inaccessible from Namespace B
    assert await get_site(None, _UUID_B, site_a.id) is None
    assert await get_site_by_cadastre_id(None, _UUID_B, "CAD-A-1") is None
    assert len(await list_sites(None, _UUID_B)) == 0

    # Namespace B can register its own site with independent identity
    site_b = await register_site(
        None,
        _UUID_B,
        SiteCreate(name="Site Beta", cadastre_id="CAD-B-1"),
    )
    assert site_b.namespace_id == _UUID_B
    assert await get_site(None, _UUID_B, site_b.id) is not None
    assert await get_site(None, _UUID_A, site_b.id) is None


# ===========================================================================
# 3. C1 Site & FL Building Resolution Hook & Merge Queue Gating
# ===========================================================================


@pytest.mark.asyncio
async def test_gate_two_fl_buildings_with_same_cadastre_id_land_in_merge_queue():
    """GATE: Two FL buildings with the same cadastre ID land in the merge queue.

    C1 Invariant:
    Cadastre ID is the FL-building match key (score = 1.0).
    NEVER auto-merge: must enqueue with status 'pending' and auto_merged = False.
    """
    cadastre = "0301-400/10/0/0"

    # Register first FL building / Site
    site1 = await register_site(
        None,
        _UUID_A,
        SiteCreate(name="Headquarters Building A", cadastre_id=cadastre),
    )

    # Candidate 2: A second FL building claiming the exact same cadastre_id
    candidate_building = {
        "building_name": "Headquarters Main Wing",
        "cadastre_id": cadastre,
        "floor_count": 5,
    }

    result = await reconcile_fl_building_with_site(
        None,
        namespace_id=_UUID_A,
        candidate=candidate_building,
        entity_type="FUNCTIONAL_LOCATION",
    )

    # Verify matching result
    assert result["matched"] is True
    assert result["target_id"] == site1.id
    assert result["normalized_cadastre_id"] == cadastre
    assert result["score"] == 1.0
    assert result["queued_for_merge"] is True
    assert result["auto_merged"] is False
    assert result["status"] == "pending"

    # Verify queue contents
    queue = get_mem_merge_queue()
    assert len(queue) == 1
    entry = queue[0]
    assert entry["namespace_id"] == _UUID_A
    assert entry["node_type"] == "FUNCTIONAL_LOCATION"
    assert entry["target_id"] == site1.id
    assert entry["cadastre_id"] == cadastre
    assert entry["score"] == 1.0
    assert entry["status"] == "pending"
    assert entry["auto_merged"] is False


@pytest.mark.asyncio
async def test_two_fl_buildings_match_without_site_entity():
    """Verify that two FL buildings with matching cadastre IDs enqueue even when matching an existing FL building."""
    cadastre = "CAD-FL-MATCH-888"
    existing_fl_id = uuid.uuid4()
    register_mem_fl_building(_UUID_A, existing_fl_id, cadastre, "Existing FL Building")

    candidate = {"name": "Candidate FL Building", "cadastre_id": cadastre}
    result = await reconcile_fl_building_with_site(
        None,
        namespace_id=_UUID_A,
        candidate=candidate,
    )

    assert result["matched"] is True
    assert result["target_id"] == existing_fl_id
    assert result["target_type"] == "FUNCTIONAL_LOCATION"
    assert result["auto_merged"] is False
    assert result["status"] == "pending"


# ===========================================================================
# 4. C12 Resource Surface REST API & Principal Tier Redaction
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
# 5. MCP Tool Definitions & Surface
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


# ===========================================================================
# 6. U18 Standing Positive Controls
# ===========================================================================


@pytest.mark.asyncio
async def test_positive_control_different_cadastre_id_does_not_queue():
    """Standing positive control (U18): prove different cadastre_id does NOT queue for merge."""
    await register_site(
        None,
        _UUID_A,
        SiteCreate(name="Real Building", cadastre_id="CAD-EXISTS-111"),
    )

    candidate = {"name": "Unrelated Building", "cadastre_id": "CAD-DIFFERENT-222"}
    result = await reconcile_fl_building_with_site(
        None,
        namespace_id=_UUID_A,
        candidate=candidate,
    )

    assert result["matched"] is False
    assert result["reason"] == "not_found"
    assert result["auto_merged"] is False
    assert len(get_mem_merge_queue()) == 0
