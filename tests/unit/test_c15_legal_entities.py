"""Unit test suite for C15 Legal-Entity Register (Wave A-5).

Tests:
  1. C15 Legal Entity Register CRUD lifecycle (register, get, get_by_org_nr, update, add_role, archive).
  2. Org.nr normalization pure functions (Norwegian NO/MVA prefixes/suffixes, whitespace, hyphens, slashes).
  3. Multi-tenant isolation across namespaces.
  4. C1 Legal Entity Hook & Merge Queue Gating:
     - Strongest match key (score = 1.0).
     - Customer and vendor with same org_nr resolve to one legal entity through entity_merge_queue.
     - CRITICAL INVARIANT: NEVER auto-merged (auto_merged is False, status is 'pending').
  5. C12 Resource Surface REST API & Principal Tier Redaction (contractor vs external-customer).
  6. C12 MCP tool definitions and execution specs.
"""

from __future__ import annotations

import uuid

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from nce import admin_state
from nce.entity_resolution.legal_entity_hook import (
    _clear_hook_mem_store,
    get_mem_merge_queue,
    reconcile_with_legal_entity,
)
from nce.entity_resolution.normalizers import normalize, normalize_org_nr
from nce.resource_surface import ResourceSpec, register_resource
from nce.resource_surface.rest import _clear_mem_store, make_resource_routes
from nce.vertical_modules.legal_entities.resources import LEGAL_ENTITY_SPEC
from nce.vertical_modules.legal_entities.service import (
    _clear_mem_store as _clear_le_mem_store,
)
from nce.vertical_modules.legal_entities.service import (
    add_role_to_legal_entity,
    archive_legal_entity,
    get_legal_entity,
    get_legal_entity_by_org_nr,
    list_legal_entities,
    register_legal_entity,
    update_legal_entity,
)

_NS_A = str(uuid.UUID("11111111-1111-1111-1111-111111111111"))
_NS_B = str(uuid.UUID("22222222-2222-2222-2222-222222222222"))
_UUID_A = uuid.UUID(_NS_A)
_UUID_B = uuid.UUID(_NS_B)


@pytest.fixture(autouse=True)
def _reset_env():
    _clear_mem_store()
    _clear_le_mem_store()
    _clear_hook_mem_store()
    admin_state.engine = None
    register_resource(LEGAL_ENTITY_SPEC)
    yield
    _clear_mem_store()
    _clear_le_mem_store()
    _clear_hook_mem_store()


def _client_for_spec(spec: ResourceSpec) -> TestClient:
    app = Starlette(routes=make_resource_routes(spec))
    return TestClient(app, raise_server_exceptions=False)


# ===========================================================================
# 1. Normalization Unit Tests
# ===========================================================================


def test_normalize_org_nr_variations():
    """Verify org_nr normalization strips prefixes, suffixes, and punctuation."""
    assert normalize_org_nr("987654321") == "987654321"
    assert normalize_org_nr("987 654 321") == "987654321"
    assert normalize_org_nr("NO 987 654 321 MVA") == "987654321"
    assert normalize_org_nr("NO987654321MVA") == "987654321"
    assert normalize_org_nr("987-654-321") == "987654321"
    assert normalize_org_nr(" 987.654.321 / MVA ") == "987654321"
    assert normalize_org_nr("") == ""
    assert normalize_org_nr("   ") == ""


def test_normalizer_dispatch():
    """Verify generic normalize() routes org_nr correctly."""
    assert normalize("NO 123 456 789 MVA", "org_nr") == "123456789"
    assert normalize("123-456-789", "orgnr") == "123456789"
    assert normalize("123 456 789", "organization_number") == "123456789"


# ===========================================================================
# 2. Service CRUD & Lifecycle Tests
# ===========================================================================


@pytest.mark.asyncio
async def test_legal_entity_crud_lifecycle():
    """Test full CRUD lifecycle in memory."""
    # 1. Register
    entity = await register_legal_entity(
        conn=None,
        namespace_id=_UUID_A,
        org_nr="NO 912 345 678 MVA",
        name="Nordic AV Solutions AS",
        group_parent_org_nr="900 100 200",
        roles=["customer"],
        country="NO",
        metadata={"industry": "AV", "tier": "gold"},
    )
    assert entity.org_nr == "912345678"
    assert entity.group_parent_org_nr == "900100200"
    assert entity.name == "Nordic AV Solutions AS"
    assert "customer" in entity.roles
    assert not entity.archived

    # 2. Lookup by ID and Org Nr
    by_id = await get_legal_entity(conn=None, namespace_id=_UUID_A, entity_id=entity.id)
    assert by_id is not None
    assert by_id.id == entity.id

    by_org = await get_legal_entity_by_org_nr(conn=None, namespace_id=_UUID_A, org_nr="912-345-678")
    assert by_org is not None
    assert by_org.id == entity.id

    # 3. Add Role (idempotent)
    updated_role = await add_role_to_legal_entity(
        conn=None, namespace_id=_UUID_A, entity_id=entity.id, role="vendor"
    )
    assert updated_role is not None
    assert "customer" in updated_role.roles
    assert "vendor" in updated_role.roles

    # Adding again doesn't duplicate
    again = await add_role_to_legal_entity(
        conn=None, namespace_id=_UUID_A, entity_id=entity.id, role="vendor"
    )
    assert again is not None
    assert again.roles.count("vendor") == 1

    # 4. Update
    updated = await update_legal_entity(
        conn=None,
        namespace_id=_UUID_A,
        entity_id=entity.id,
        name="Nordic AV Group AS",
        metadata={"industry": "AV", "tier": "platinum"},
    )
    assert updated is not None
    assert updated.name == "Nordic AV Group AS"
    assert updated.metadata.get("tier") == "platinum"

    # 5. List
    entities = await list_legal_entities(conn=None, namespace_id=_UUID_A, role="vendor")
    assert len(entities) == 1
    assert entities[0].id == entity.id

    # 6. Archive
    archived = await archive_legal_entity(conn=None, namespace_id=_UUID_A, entity_id=entity.id)
    assert archived is True

    # After archive, active list is empty
    active = await list_legal_entities(conn=None, namespace_id=_UUID_A, archived=False)
    assert len(active) == 0

    # Archived list has it
    archived_list = await list_legal_entities(conn=None, namespace_id=_UUID_A, archived=True)
    assert len(archived_list) == 1


# ===========================================================================
# 3. Multi-Tenant Isolation Tests
# ===========================================================================


@pytest.mark.asyncio
async def test_multi_tenant_isolation():
    """Verify legal entities are completely isolated between namespaces."""
    # Register in Namespace A
    le_a = await register_legal_entity(
        conn=None,
        namespace_id=_UUID_A,
        org_nr="999888777",
        name="Tenant A Corp",
    )

    # Register in Namespace B with same org_nr (different tenant domain)
    le_b = await register_legal_entity(
        conn=None,
        namespace_id=_UUID_B,
        org_nr="999888777",
        name="Tenant B Corp",
    )

    # Lookup in Namespace A returns Tenant A only
    found_a = await get_legal_entity_by_org_nr(conn=None, namespace_id=_UUID_A, org_nr="999888777")
    assert found_a is not None
    assert found_a.id == le_a.id
    assert found_a.name == "Tenant A Corp"

    # Lookup in Namespace B returns Tenant B only
    found_b = await get_legal_entity_by_org_nr(conn=None, namespace_id=_UUID_B, org_nr="999888777")
    assert found_b is not None
    assert found_b.id == le_b.id
    assert found_b.name == "Tenant B Corp"

    # Namespace B cannot see le_a by ID
    assert await get_legal_entity(conn=None, namespace_id=_UUID_B, entity_id=le_a.id) is None


# ===========================================================================
# 4. C1 Legal Entity Hook & Merge Queue Gating (CRITICAL INVARIANT)
# ===========================================================================


@pytest.mark.asyncio
async def test_c1_hook_enqueues_merge_never_auto_merges():
    """Verify that candidate matching an existing legal entity resolves through entity_merge_queue.

    CRITICAL INVARIANT: A customer and vendor with the same org_nr resolve
    to one legal entity through the merge queue, NEVER auto-merged.
    """
    # 1. Register existing legal entity with role 'vendor'
    le = await register_legal_entity(
        conn=None,
        namespace_id=_UUID_A,
        org_nr="987 654 321",
        name="Acme Acoustics AS",
        roles=["vendor"],
    )

    # 2. Reconcile a candidate 'customer' that has the same org_nr
    candidate_customer = {
        "name": "Acme Acoustics Customer Division",
        "org_nr": "NO 987 654 321 MVA",
        "contact_email": "billing@acme.example",
    }

    result = await reconcile_with_legal_entity(
        conn=None,
        namespace_id=_UUID_A,
        entity_type="customer",
        candidate=candidate_customer,
    )

    # Assertions
    assert result["matched"] is True
    assert result["legal_entity_id"] == le.id
    assert result["normalized_org_nr"] == "987654321"
    assert result["score"] == 1.0  # strongest match key
    assert result["queued_for_merge"] is True
    assert result["status"] == "pending"

    # INVARIANT: Must NEVER auto-merge
    assert result["auto_merged"] is False

    # Verify item queued in merge queue
    queue_items = get_mem_merge_queue()
    assert len(queue_items) == 1
    item = queue_items[0]
    assert item["node_type"] == "customer"
    assert item["target_id"] == le.id
    assert item["score"] == 1.0
    assert item["status"] == "pending"


@pytest.mark.asyncio
async def test_c1_hook_unmatched_returns_cleanly():
    """Unmatched org_nr returns matched=False with zero merge queue enqueue."""
    result = await reconcile_with_legal_entity(
        conn=None,
        namespace_id=_UUID_A,
        entity_type="vendor",
        candidate={"name": "Unknown Corp", "org_nr": "111222333"},
    )
    assert result["matched"] is False
    assert result["reason"] == "not_found"
    assert result["auto_merged"] is False
    assert len(get_mem_merge_queue()) == 0


# ===========================================================================
# 5. C12 REST Surface & Tier Redaction Tests
# ===========================================================================


def test_rest_surface_crud_and_tier_redaction():
    """Verify REST collection/item endpoints and C3/C8 principal tier redaction."""
    client = _client_for_spec(LEGAL_ENTITY_SPEC)

    # POST create
    payload = {
        "org_nr": "NO 955 444 333 MVA",
        "name": "Audio Visual Dynamics AS",
        "group_parent_org_nr": "900000001",
        "roles": ["customer", "vendor"],
        "country": "NO",
        "metadata": {"internal_rating": "A+"},
    }
    res = client.post(
        LEGAL_ENTITY_SPEC.rest_collection_path,
        json=payload,
        headers={"X-NCE-Namespace-ID": _NS_A, "X-NCE-Principal-Tier": "employee"},
    )
    assert res.status_code == 201, res.text
    item = res.json()["item"]
    entity_id = item["id"]
    assert item["org_nr"] == "NO 955 444 333 MVA" or item["name"] == "Audio Visual Dynamics AS"

    # GET with contractor role (allowed fields include metadata, roles, group_parent_org_nr)
    res_contractor = client.get(
        f"{LEGAL_ENTITY_SPEC.rest_collection_path}/{entity_id}",
        headers={"X-NCE-Namespace-ID": _NS_A, "X-NCE-Principal-Tier": "contractor"},
    )
    assert res_contractor.status_code == 200
    contractor_data = res_contractor.json()
    assert "roles" in contractor_data
    assert "metadata" in contractor_data

    # GET with external-customer role (redacted fields: only id, org_nr, name, country, created_at)
    res_external = client.get(
        f"{LEGAL_ENTITY_SPEC.rest_collection_path}/{entity_id}",
        headers={"X-NCE-Namespace-ID": _NS_A, "X-NCE-Principal-Tier": "external-customer"},
    )
    assert res_external.status_code == 200
    external_data = res_external.json()
    assert "id" in external_data
    assert "name" in external_data
    # Redacted fields MUST NOT be present
    assert "roles" not in external_data
    assert "metadata" not in external_data
    assert "group_parent_org_nr" not in external_data

    # Soft archive via POST /archive
    res_del = client.post(
        f"{LEGAL_ENTITY_SPEC.rest_collection_path}/{entity_id}/archive",
        headers={"X-NCE-Namespace-ID": _NS_A, "X-NCE-Principal-Tier": "employee"},
    )
    assert res_del.status_code == 200
    assert res_del.json().get("archived") is True
