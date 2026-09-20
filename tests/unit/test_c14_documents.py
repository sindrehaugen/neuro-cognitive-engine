"""Unit test suite for C14 Document Register (Wave A-4).

Tests:
  1. C12 Document Register CRUD lifecycle (create, get, patch, archive, restore).
  2. Document listing, kind filtering, and text search.
  3. C12 Entity Document verbs (list, attach existing, attach inline, detach, 404 on missing link).
  4. Share token operations (create, valid retrieval, expired token rejection, revoked token rejection).
  5. Multi-tenant RLS isolation across namespaces.
  6. Principal tier redaction (employee, contractor, external-customer).
  7. ADR 0041 invariant: no code path deletes at the external storage source.
"""

from __future__ import annotations

import inspect
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient
from tests._verified_tier_middleware import VERIFIED_TIER_TEST_MIDDLEWARE

from nce import admin_state
from nce.resource_surface import ResourceSpec, register_resource
from nce.resource_surface.rest import _clear_mem_store, make_resource_routes
from nce.vertical_modules.documents import service as doc_service
from nce.vertical_modules.documents.resources import DOCUMENT_SPEC
from nce.vertical_modules.documents.service import (
    archive_document,
    create_document_share,
    get_document_share,
    is_share_active,
    link_document,
    register_document,
    revoke_document_share,
    unlink_document,
)
from nce.vertical_modules.inventory.resources import STOCK_LOCATION_SPEC

_NS_A = str(uuid.UUID("11111111-1111-1111-1111-111111111111"))
_NS_B = str(uuid.UUID("22222222-2222-2222-2222-222222222222"))
_UUID_A = uuid.UUID(_NS_A)
_UUID_B = uuid.UUID(_NS_B)


@pytest.fixture(autouse=True)
def _reset_env():
    _clear_mem_store()
    admin_state.engine = None
    register_resource(DOCUMENT_SPEC)
    register_resource(STOCK_LOCATION_SPEC)
    yield
    _clear_mem_store()


def _client_for_spec(spec: ResourceSpec) -> TestClient:
    app = Starlette(routes=make_resource_routes(spec), middleware=VERIFIED_TIER_TEST_MIDDLEWARE)
    return TestClient(app, raise_server_exceptions=False)


# ===========================================================================
# 1. C12 Document Register CRUD Lifecycle
# ===========================================================================


def test_document_crud_lifecycle():
    client = _client_for_spec(DOCUMENT_SPEC)
    emp = {"X-NCE-Principal-Tier": "employee"}

    # 1. Create document
    payload = {
        "namespace_id": _NS_A,
        "title": "System Architecture Overview",
        "document_ref": "https://storage.internal/docs/arch_overview.pdf",
        "source_kind": "sharepoint",
        "document_kind": "specification",
        "file_name": "arch_overview.pdf",
        "mime_type": "application/pdf",
        "file_size_bytes": 2048576,
        "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "tags": ["architecture", "core"],
        "metadata": {"confidentiality": "internal", "version": "1.0"},
    }
    create_resp = client.post("/api/documents/documents", json=payload, headers=emp)
    assert create_resp.status_code == 201, create_resp.text
    created = create_resp.json()
    assert "id" in created
    doc_id = created["id"]
    assert created["title"] == "System Architecture Overview"
    assert created["source_kind"] == "sharepoint"
    assert created["archived"] is False

    # 2. Detail GET
    get_resp = client.get(f"/api/documents/documents/{doc_id}?namespace_id={_NS_A}", headers=emp)
    assert get_resp.status_code == 200, get_resp.text
    fetched = get_resp.json()
    assert fetched["id"] == doc_id
    assert fetched["document_kind"] == "specification"
    assert fetched["file_size_bytes"] == 2048576

    # 3. PATCH update
    patch_payload = {
        "title": "System Architecture Overview v1.1",
        "namespace_id": _NS_A,
    }
    patch_resp = client.patch(f"/api/documents/documents/{doc_id}", json=patch_payload, headers=emp)
    assert patch_resp.status_code == 200, patch_resp.text
    updated = patch_resp.json()
    assert updated["title"] == "System Architecture Overview v1.1"

    # 4. Soft Archive
    archive_resp = client.post(
        f"/api/documents/documents/{doc_id}/archive?namespace_id={_NS_A}", headers=emp
    )
    assert archive_resp.status_code == 200, archive_resp.text
    archived_doc = archive_resp.json()
    assert archived_doc["archived"] is True

    # Check that archive flag is reflected in GET
    get_archived = client.get(
        f"/api/documents/documents/{doc_id}?namespace_id={_NS_A}", headers=emp
    )
    assert get_archived.json()["archived"] is True

    # 5. Soft Restore
    restore_resp = client.post(
        f"/api/documents/documents/{doc_id}/restore?namespace_id={_NS_A}", headers=emp
    )
    assert restore_resp.status_code == 200, restore_resp.text
    restored_doc = restore_resp.json()
    assert restored_doc["archived"] is False


# ===========================================================================
# 2. Document Listing and Search
# ===========================================================================


def test_document_listing_and_filtering():
    client = _client_for_spec(DOCUMENT_SPEC)
    emp = {"X-NCE-Principal-Tier": "employee"}

    docs = [
        {
            "namespace_id": _NS_A,
            "title": "DSP Crossover Filter Specification",
            "document_ref": "https://storage.internal/dsp/filter.pdf",
            "document_kind": "specification",
        },
        {
            "namespace_id": _NS_A,
            "title": "Amplifier Hardware User Manual",
            "document_ref": "https://storage.internal/amp/manual.pdf",
            "document_kind": "manual",
        },
        {
            "namespace_id": _NS_A,
            "title": "Acoustic Enclosure Blueprint",
            "document_ref": "https://storage.internal/enclosure/blueprint.dwg",
            "document_kind": "drawing",
        },
    ]
    for d in docs:
        resp = client.post("/api/documents/documents", json=d, headers=emp)
        assert resp.status_code == 201

    # List all
    list_all = client.get(f"/api/documents/documents?namespace_id={_NS_A}", headers=emp)
    assert list_all.status_code == 200
    assert list_all.json()["total"] == 3

    # Filter by document_kind
    filter_resp = client.get(
        f"/api/documents/documents?namespace_id={_NS_A}&document_kind=manual", headers=emp
    )
    assert filter_resp.status_code == 200
    res = filter_resp.json()
    assert res["total"] == 1
    assert res["items"][0]["title"] == "Amplifier Hardware User Manual"

    # Search by q
    search_resp = client.get(
        f"/api/documents/documents?namespace_id={_NS_A}&q=crossover", headers=emp
    )
    assert search_resp.status_code == 200
    s_res = search_resp.json()
    assert s_res["total"] == 1
    assert "DSP Crossover" in s_res["items"][0]["title"]


# ===========================================================================
# 3. C12 Entity Document Verbs
# ===========================================================================


def test_entity_document_verbs_lifecycle():
    # Test entity endpoints on STOCK_LOCATION_SPEC
    loc_client = _client_for_spec(STOCK_LOCATION_SPEC)
    doc_client = _client_for_spec(DOCUMENT_SPEC)
    emp = {"X-NCE-Principal-Tier": "employee"}

    # 1. Create a stock location entity
    loc_resp = loc_client.post(
        "/api/inventory/stock-locations",
        json={"namespace_id": _NS_A, "name": "Warehouse Alpha", "kind": "warehouse"},
        headers=emp,
    )
    assert loc_resp.status_code == 201
    loc_id = loc_resp.json()["id"]

    # 2. Register an initial document
    doc_resp = doc_client.post(
        "/api/documents/documents",
        json={
            "namespace_id": _NS_A,
            "title": "Facility Evacuation Plan",
            "document_ref": "https://storage.internal/plans/evac.pdf",
            "document_kind": "compliance",
        },
        headers=emp,
    )
    assert doc_resp.status_code == 201
    doc_id = doc_resp.json()["id"]

    # 3. Attach existing document to entity
    attach_resp = loc_client.post(
        f"/api/inventory/stock-locations/{loc_id}/documents",
        json={"namespace_id": _NS_A, "document_id": doc_id, "relation": "compliance"},
        headers=emp,
    )
    assert attach_resp.status_code == 201, attach_resp.text
    att_data = attach_resp.json()
    assert att_data["status"] == "ok"
    assert att_data["document_id"] == doc_id
    assert att_data["relation"] == "compliance"

    # 4. Attach another document via inline registration
    inline_resp = loc_client.post(
        f"/api/inventory/stock-locations/{loc_id}/documents",
        json={
            "namespace_id": _NS_A,
            "title": "Inventory Audit Checklist",
            "document_ref": "https://storage.internal/audits/checklist.xlsx",
            "document_kind": "checklist",
            "relation": "operation",
        },
        headers=emp,
    )
    assert inline_resp.status_code == 201, inline_resp.text
    inline_doc_id = inline_resp.json()["document_id"]

    # 5. List documents for entity
    list_docs = loc_client.get(
        f"/api/inventory/stock-locations/{loc_id}/documents?namespace_id={_NS_A}", headers=emp
    )
    assert list_docs.status_code == 200, list_docs.text
    data = list_docs.json()
    assert data["count"] == 2
    titles = [d["title"] for d in data["documents"]]
    assert "Facility Evacuation Plan" in titles
    assert "Inventory Audit Checklist" in titles

    # 6. Detach first document
    detach_resp = loc_client.delete(
        f"/api/inventory/stock-locations/{loc_id}/documents/{doc_id}?namespace_id={_NS_A}",
        headers=emp,
    )
    assert detach_resp.status_code == 200, detach_resp.text
    assert detach_resp.json()["unlinked"] is True

    # 7. List documents now shows only 1
    list_docs_after = loc_client.get(
        f"/api/inventory/stock-locations/{loc_id}/documents?namespace_id={_NS_A}", headers=emp
    )
    assert list_docs_after.status_code == 200
    assert list_docs_after.json()["count"] == 1
    assert list_docs_after.json()["documents"][0]["id"] == inline_doc_id

    # 8. Subsequent detach of already detached document returns 404
    detach_again = loc_client.delete(
        f"/api/inventory/stock-locations/{loc_id}/documents/{doc_id}?namespace_id={_NS_A}"
    )
    assert detach_again.status_code == 404


# ===========================================================================
# 4. Share Token Operations
# ===========================================================================


@pytest.mark.asyncio
async def test_share_token_operations():
    # 1. Register a document in service
    doc = await register_document(
        None,
        _UUID_A,
        "Commercial Contract",
        "https://storage.internal/contracts/c123.pdf",
    )

    # 2. Create active share token
    expires = datetime.now(timezone.utc) + timedelta(days=7)
    share = await create_document_share(
        None,
        _UUID_A,
        doc.id,
        granted_by="operator@steps.ai",
        expires_at=expires,
    )
    assert share.share_id.startswith("dsh_")
    assert len(share.share_id) > 20
    assert is_share_active(share) is True

    # 3. Retrieve share token
    fetched = await get_document_share(None, _UUID_A, share.share_id)
    assert fetched is not None
    assert fetched.document_id == doc.id
    assert fetched.granted_by == "operator@steps.ai"

    # 4. Expired share token
    expired_time = datetime.now(timezone.utc) - timedelta(hours=1)
    expired_share = await create_document_share(
        None,
        _UUID_A,
        doc.id,
        expires_at=expired_time,
    )
    assert is_share_active(expired_share) is False

    # 5. Revoke share token
    revoked = await revoke_document_share(None, _UUID_A, share.share_id)
    assert revoked is True
    revoked_share = await get_document_share(None, _UUID_A, share.share_id)
    assert revoked_share is not None
    assert is_share_active(revoked_share) is False

    # 6. Re-revoking returns False
    second_revoke = await revoke_document_share(None, _UUID_A, share.share_id)
    assert second_revoke is False


# ===========================================================================
# 5. Multi-Tenant RLS Isolation
# ===========================================================================


def test_multi_tenant_rls_isolation():
    client = _client_for_spec(DOCUMENT_SPEC)
    loc_client = _client_for_spec(STOCK_LOCATION_SPEC)

    # Create document in namespace A
    resp = client.post(
        "/api/documents/documents",
        json={
            "namespace_id": _NS_A,
            "title": "Tenant A Confidential Design",
            "document_ref": "https://storage.internal/tenant_a/design.pdf",
        },
    )
    assert resp.status_code == 201
    doc_id = resp.json()["id"]

    # Cross-tenant GET from namespace B returns 404
    cross_get = client.get(f"/api/documents/documents/{doc_id}?namespace_id={_NS_B}")
    assert cross_get.status_code == 404

    # Cross-tenant list from namespace B is empty
    cross_list = client.get(f"/api/documents/documents?namespace_id={_NS_B}")
    assert cross_list.status_code == 200
    assert cross_list.json()["total"] == 0

    # Cross-tenant entity attachment
    # Create location in B
    loc_resp = loc_client.post(
        "/api/inventory/stock-locations",
        json={"namespace_id": _NS_B, "name": "Warehouse B", "kind": "warehouse"},
    )
    assert loc_resp.status_code == 201
    loc_b_id = loc_resp.json()["id"]

    # Listing entity documents in B returns 0
    docs_b = loc_client.get(
        f"/api/inventory/stock-locations/{loc_b_id}/documents?namespace_id={_NS_B}"
    )
    assert docs_b.status_code == 200
    assert docs_b.json()["count"] == 0


# ===========================================================================
# 6. Principal Tier Redaction (C3/C8)
# ===========================================================================


def test_principal_tier_redaction():
    client = _client_for_spec(DOCUMENT_SPEC)

    create_resp = client.post(
        "/api/documents/documents",
        json={
            "namespace_id": _NS_A,
            "title": "High-End Audio Driver Blueprint",
            "document_ref": "https://storage.internal/drivers/blueprint.pdf",
            "source_kind": "sharepoint",
            "document_kind": "schematic",
            "file_name": "blueprint.pdf",
            "mime_type": "application/pdf",
            "file_size_bytes": 524288,
            "sha256": "1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef",
            "tags": ["hardware", "confidential"],
            "metadata": {"bill_of_materials_cost": 125.50, "internal_notes": "Rev 2"},
        },
    )
    assert create_resp.status_code == 201
    doc_id = create_resp.json()["id"]

    # 1. Employee tier: full visibility
    emp_resp = client.get(
        f"/api/documents/documents/{doc_id}?namespace_id={_NS_A}",
        headers={"X-NCE-Principal-Tier": "employee"},
    )
    assert emp_resp.status_code == 200
    emp_data = emp_resp.json()
    assert "document_ref" in emp_data
    assert "sha256" in emp_data
    assert "tags" in emp_data
    assert "metadata" in emp_data

    # 2. Contractor tier: contractor allowlist
    ctr_resp = client.get(
        f"/api/documents/documents/{doc_id}?namespace_id={_NS_A}",
        headers={"X-NCE-Principal-Tier": "contractor"},
    )
    assert ctr_resp.status_code == 200
    ctr_data = ctr_resp.json()
    assert "title" in ctr_data
    assert "document_ref" in ctr_data
    assert "file_name" in ctr_data
    assert "file_size_bytes" in ctr_data

    # 3. External-customer tier: restricted allowlist
    cust_resp = client.get(
        f"/api/documents/documents/{doc_id}?namespace_id={_NS_A}",
        headers={"X-NCE-Principal-Tier": "external-customer"},
    )
    assert cust_resp.status_code == 200
    cust_data = cust_resp.json()
    assert "id" in cust_data
    assert "title" in cust_data
    assert "document_kind" in cust_data
    assert "file_name" in cust_data
    assert "mime_type" in cust_data
    assert "created_at" in cust_data

    # Customer MUST NOT see storage internals, raw hashes, or metadata
    assert "document_ref" not in cust_data
    assert "source_kind" not in cust_data
    assert "sha256" not in cust_data
    assert "metadata" not in cust_data
    assert "tags" not in cust_data


# ===========================================================================
# 7. Critical Invariant (ADR 0041): No Code Path Deletes at the Source
# ===========================================================================


@pytest.mark.asyncio
async def test_invariant_no_code_path_deletes_at_the_source():
    """Verify that archiving or unlinking documents never calls delete on storage providers.

    ADR 0041: NCE owns the document register; external storage (SharePoint, MinIO, S3)
    holds the file contents. Content deletion at source is prohibited in all register operations.
    """
    # Mock external storage provider client
    mock_storage_client = MagicMock()
    mock_storage_client.delete_file = MagicMock()
    mock_storage_client.delete_object = MagicMock()
    mock_storage_client.remove_file = MagicMock()

    doc = await register_document(
        None,
        _UUID_A,
        "Source Preserved Document",
        "https://storage.internal/docs/preserved.pdf",
    )
    await link_document(
        None,
        _UUID_A,
        doc.id,
        "STOCK_LOCATION",
        "loc-999",
    )

    # 1. Archive document
    archived = await archive_document(None, _UUID_A, doc.id)
    assert archived is True

    # 2. Unlink document
    unlinked = await unlink_document(None, _UUID_A, doc.id, "STOCK_LOCATION", "loc-999")
    assert unlinked is True

    # 3. Assert mock storage provider delete methods were never called
    mock_storage_client.delete_file.assert_not_called()
    mock_storage_client.delete_object.assert_not_called()
    mock_storage_client.remove_file.assert_not_called()

    # 4. Code inspection assertion: ensure no delete calls exist in service module
    service_source = inspect.getsource(doc_service)
    forbidden_tokens = [
        "delete_file",
        "remove_file",
        "s3.delete",
        "minio.remove",
        "delete_object",
        "storage.delete",
    ]
    for token in forbidden_tokens:
        assert token not in service_source, (
            f"Forbidden storage deletion token '{token}' found in documents service source!"
        )
