"""Unit test suite for C15 Legal-Entity Register (Wave A-5).

Tests:
  1. Org.nr normalization pure functions (Norwegian NO/MVA prefixes/suffixes, whitespace, hyphens, slashes).
  2. get_legal_entity/update_legal_entity -- the two service.py functions still called in
     production, by brreg_feed.py's BRREG enrichment. The register's own CRUD (register,
     get_by_org_nr, list, add_role, archive) and the C1 legal-entity hook were deleted as
     dead code (2026-09-21): the C12 resource surface generates the reachable CRUD from
     LEGAL_ENTITY_SPEC, and nothing called the hand-written versions or the hook outside
     their own tests. See DEAD_HAND_WRITTEN_SERVICE_LAYERS_2026-09-21.md.
  3. Multi-tenant isolation across namespaces (via get_legal_entity).
  4. C12 Resource Surface REST API & Principal Tier Redaction (contractor vs external-customer).
"""

from __future__ import annotations

import uuid

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient
from tests._verified_tier_middleware import VERIFIED_TIER_TEST_MIDDLEWARE

from nce import admin_state
from nce.entity_resolution.normalizers import normalize, normalize_org_nr
from nce.resource_surface import ResourceSpec, register_resource
from nce.resource_surface.rest import _clear_mem_store, make_resource_routes
from nce.vertical_modules.legal_entities.models import LegalEntityRecord
from nce.vertical_modules.legal_entities.resources import LEGAL_ENTITY_SPEC
from nce.vertical_modules.legal_entities.service import (
    _MEM_LEGAL_ENTITIES,
    get_legal_entity,
    update_legal_entity,
)
from nce.vertical_modules.legal_entities.service import (
    _clear_mem_store as _clear_le_mem_store,
)

_NS_A = str(uuid.UUID("11111111-1111-1111-1111-111111111111"))
_NS_B = str(uuid.UUID("22222222-2222-2222-2222-222222222222"))
_UUID_A = uuid.UUID(_NS_A)
_UUID_B = uuid.UUID(_NS_B)


@pytest.fixture(autouse=True)
def _reset_env():
    _clear_mem_store()
    _clear_le_mem_store()
    admin_state.engine = None
    register_resource(LEGAL_ENTITY_SPEC)
    yield
    _clear_mem_store()
    _clear_le_mem_store()


def _client_for_spec(spec: ResourceSpec) -> TestClient:
    app = Starlette(routes=make_resource_routes(spec), middleware=VERIFIED_TIER_TEST_MIDDLEWARE)
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
# 2. get_legal_entity / update_legal_entity -- the two functions kept for
#    brreg_feed.py's BRREG enrichment write-back. No register_legal_entity
#    remains to seed a fixture through the service API (deleted as dead code),
#    so these seed the in-memory store directly, the same store the deleted
#    function used to write to.
# ===========================================================================


@pytest.mark.asyncio
async def test_get_and_update_legal_entity_in_memory():
    """Retrieve and update an in-memory legal entity record."""
    entity = LegalEntityRecord(
        id=uuid.uuid4(),
        namespace_id=_UUID_A,
        org_nr="912345678",
        name="Nordic AV Solutions AS",
        roles=("customer",),
        metadata={"industry": "AV"},
    )
    _MEM_LEGAL_ENTITIES.setdefault(str(_UUID_A), {})[str(entity.id)] = entity

    fetched = await get_legal_entity(conn=None, namespace_id=_UUID_A, entity_id=entity.id)
    assert fetched is not None
    assert fetched.org_nr == "912345678"
    assert fetched.name == "Nordic AV Solutions AS"

    updated = await update_legal_entity(
        conn=None,
        namespace_id=_UUID_A,
        entity_id=entity.id,
        name="Nordic AV Group AS",
        metadata={"tier": "platinum"},
    )
    assert updated is not None
    assert updated.name == "Nordic AV Group AS"
    # metadata merges into what's already there, doesn't replace it
    assert updated.metadata.get("tier") == "platinum"
    assert updated.metadata.get("industry") == "AV"

    refetched = await get_legal_entity(conn=None, namespace_id=_UUID_A, entity_id=entity.id)
    assert refetched is not None
    assert refetched.name == "Nordic AV Group AS"


@pytest.mark.asyncio
async def test_update_legal_entity_missing_returns_none():
    """update_legal_entity on a non-existent id/namespace returns None, not an error."""
    result = await update_legal_entity(
        conn=None, namespace_id=_UUID_A, entity_id=uuid.uuid4(), name="Ghost AS"
    )
    assert result is None


# ===========================================================================
# 3. Multi-Tenant Isolation Tests
# ===========================================================================


@pytest.mark.asyncio
async def test_multi_tenant_isolation():
    """Verify get_legal_entity is completely isolated between namespaces."""
    le_a = LegalEntityRecord(
        id=uuid.uuid4(), namespace_id=_UUID_A, org_nr="999888777", name="Tenant A Corp"
    )
    _MEM_LEGAL_ENTITIES.setdefault(str(_UUID_A), {})[str(le_a.id)] = le_a

    found_a = await get_legal_entity(conn=None, namespace_id=_UUID_A, entity_id=le_a.id)
    assert found_a is not None
    assert found_a.name == "Tenant A Corp"

    # Namespace B cannot see le_a by ID
    assert await get_legal_entity(conn=None, namespace_id=_UUID_B, entity_id=le_a.id) is None
    # ...nor update it
    assert (
        await update_legal_entity(
            conn=None, namespace_id=_UUID_B, entity_id=le_a.id, name="Hijacked"
        )
        is None
    )


# ===========================================================================
# 4. C12 REST Surface & Tier Redaction Tests
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
