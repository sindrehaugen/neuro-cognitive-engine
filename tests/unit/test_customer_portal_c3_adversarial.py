"""tests/unit/test_customer_portal_c3_adversarial.py
===================================================
Wave T-6: Customer Portal C3 Adversarial-External Review & Auth Boundary.

Estate Charter §13 Verification Suite:
  1. Twin tenants fixture: Identical room names, document titles, invoice numbers,
     and SLA tier text; differentiated strictly on identifiers and tenant/customer scopes.
  2. Server-Verified Sessions (Option A): portal_login mints session in PortalSessionStore;
     CustomerPortalAuthMiddleware resolves scope from verified session only.
  3. Cross-Tenant IDOR Refusal: Tenant A credentials probing Tenant B's room_id, share_id,
     or namespace_id must refuse with 403 or 404 (NEVER 200 with empty rows).
  4. Scope Omission Refusal: Missing session/header must refuse with 401.
  5. Nil-UUID Scope & Namespace Refusal: Nil-UUID sentinels must refuse with 403.
  6. Client Body Override Refusal: Request body carrying different customer_scope_id loses
     to authoritative session scope.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from starlette.testclient import TestClient

from nce.vertical_modules.customer_portal.app import build_customer_portal_app
from nce.vertical_modules.customer_portal.sessions import default_session_store

_TENANT_A_NS = UUID("11111111-0000-4000-8000-000000000001")
_TENANT_A_SCOPE = UUID("aaaaaaaa-0000-4000-8000-000000000001")
_TENANT_A_EMAIL = "alex@tenant-a.no"

_TENANT_B_NS = UUID("22222222-0000-4000-8000-000000000002")
_TENANT_B_SCOPE = UUID("bbbbbbbb-0000-4000-8000-000000000002")
_TENANT_B_EMAIL = "bjorn@tenant-b.no"

_NIL_UUID = "00000000-0000-0000-0000-000000000000"


class TwinTenantEngine:
    """Mock engine holding twin tenants with identical resource labels but strict scope boundaries."""

    def __init__(
        self,
        tenant_a_ns: UUID,
        tenant_a_scope: UUID,
        tenant_b_ns: UUID,
        tenant_b_scope: UUID,
    ) -> None:
        self.tenant_a_ns = str(tenant_a_ns)
        self.tenant_a_scope = str(tenant_a_scope)
        self.tenant_b_ns = str(tenant_b_ns)
        self.tenant_b_scope = str(tenant_b_scope)
        self.strict_resources = True

        # Resource ownership mapping: (resource_type, resource_id) -> (namespace_id, customer_scope_id)
        self.resources: dict[tuple[str, str], tuple[str, str]] = {
            ("room", "room-alpha-1"): (self.tenant_a_ns, self.tenant_a_scope),
            ("room", "room-beta-1"): (self.tenant_b_ns, self.tenant_b_scope),
            ("document", "share-fdv-1"): (self.tenant_a_ns, self.tenant_a_scope),
            ("document", "share-fdv-2"): (self.tenant_b_ns, self.tenant_b_scope),
        }

        # Resource contents: Twin tenants share IDENTICAL labels and titles
        self.rooms_data = {
            "room-alpha-1": {
                "room_id": "room-alpha-1",
                "room_name": "Executive Boardroom",
                "site_name": "Nordic HQ",
                "stage": "in_progress",
                "percent_ready": 65,
                "bom_lines": [{"id": "bom-1", "status": "delivered"}],
                "assets": [{"id": "asset-1", "lifecycle": "active"}],
            },
            "room-beta-1": {
                "room_id": "room-beta-1",
                "room_name": "Executive Boardroom",  # IDENTICAL label
                "site_name": "Nordic HQ",  # IDENTICAL label
                "stage": "in_progress",
                "percent_ready": 80,
                "bom_lines": [{"id": "bom-2", "status": "delivered"}],
                "assets": [{"id": "asset-2", "lifecycle": "active"}],
            },
        }

        self.documents_data = {
            "share-fdv-1": {
                "share_id": "share-fdv-1",
                "title": "Operation & Maintenance Manual",
                "document_kind": "fdv",
                "expires_at": "2026-12-31T23:59:59Z",
                "customer_scope_id": self.tenant_a_scope,
                "namespace_id": self.tenant_a_ns,
            },
            "share-fdv-2": {
                "share_id": "share-fdv-2",
                "title": "Operation & Maintenance Manual",  # IDENTICAL title
                "document_kind": "fdv",
                "expires_at": "2026-12-31T23:59:59Z",
                "customer_scope_id": self.tenant_b_scope,
                "namespace_id": self.tenant_b_ns,
            },
        }

    def verify_resource_access(
        self,
        resource_type: str,
        resource_id: str,
        namespace_id: str,
        customer_scope_id: str,
    ) -> None:
        key = (resource_type, resource_id)
        if key not in self.resources:
            raise LookupError(f"{resource_type} {resource_id!r} not found")
        owner_ns, owner_scope = self.resources[key]
        if str(owner_ns) != str(namespace_id) or str(owner_scope) != str(customer_scope_id):
            raise PermissionError(
                f"Cross-tenant IDOR: {resource_type} {resource_id!r} does not belong to caller"
            )

    def get_room(
        self, room_id: str, namespace_id: str, customer_scope_id: str
    ) -> dict[str, Any] | None:
        self.verify_resource_access("room", room_id, namespace_id, customer_scope_id)
        return self.rooms_data.get(room_id)

    def list_rooms(self, namespace_id: str, customer_scope_id: str) -> list[dict[str, Any]]:
        if str(namespace_id) == self.tenant_a_ns and str(customer_scope_id) == self.tenant_a_scope:
            return [self.rooms_data["room-alpha-1"]]
        if str(namespace_id) == self.tenant_b_ns and str(customer_scope_id) == self.tenant_b_scope:
            return [self.rooms_data["room-beta-1"]]
        return []

    def list_assets(
        self, room_id: str, namespace_id: str, customer_scope_id: str
    ) -> list[dict[str, Any]]:
        self.verify_resource_access("room", room_id, namespace_id, customer_scope_id)
        return self.rooms_data[room_id]["assets"]

    def get_document(
        self, share_id: str, namespace_id: str, customer_scope_id: str
    ) -> dict[str, Any] | None:
        self.verify_resource_access("document", share_id, namespace_id, customer_scope_id)
        return self.documents_data.get(share_id)

    def list_documents(self, namespace_id: str, customer_scope_id: str) -> list[dict[str, Any]]:
        if str(namespace_id) == self.tenant_a_ns and str(customer_scope_id) == self.tenant_a_scope:
            return [self.documents_data["share-fdv-1"]]
        if str(namespace_id) == self.tenant_b_ns and str(customer_scope_id) == self.tenant_b_scope:
            return [self.documents_data["share-fdv-2"]]
        return []

    def list_invoices(self, namespace_id: str, customer_scope_id: str) -> list[dict[str, Any]]:
        if str(namespace_id) == self.tenant_a_ns and str(customer_scope_id) == self.tenant_a_scope:
            return [
                {
                    "invoice_id": "inv-2026-001",
                    "invoice_number": "INV-1001",
                    "amount_inc_vat": 1000.0,
                    "currency": "NOK",
                    "status": "paid",
                }
            ]
        if str(namespace_id) == self.tenant_b_ns and str(customer_scope_id) == self.tenant_b_scope:
            return [
                {
                    "invoice_id": "inv-2026-002",
                    "invoice_number": "INV-1001",  # IDENTICAL invoice number
                    "amount_inc_vat": 2000.0,
                    "currency": "NOK",
                    "status": "paid",
                }
            ]
        return []

    def get_sla(self, namespace_id: str, customer_scope_id: str) -> dict[str, Any]:
        if str(namespace_id) == self.tenant_a_ns and str(customer_scope_id) == self.tenant_a_scope:
            return {
                "tier_name": "Gold SLA",
                "sla_id": "sla-std-001",
                "current_clock_hours": 1.5,
            }
        if str(namespace_id) == self.tenant_b_ns and str(customer_scope_id) == self.tenant_b_scope:
            return {
                "tier_name": "Gold SLA",  # IDENTICAL tier name
                "sla_id": "sla-std-002",
                "current_clock_hours": 3.0,
            }
        return {"tier_name": "Gold SLA"}


@pytest.fixture(autouse=True)
def clean_session_store() -> None:
    """Ensure session store is cleared before each test run."""
    default_session_store.clear()


@pytest.fixture
def twin_engine() -> TwinTenantEngine:
    return TwinTenantEngine(_TENANT_A_NS, _TENANT_A_SCOPE, _TENANT_B_NS, _TENANT_B_SCOPE)


@pytest.fixture
def client(twin_engine: TwinTenantEngine) -> TestClient:
    app = build_customer_portal_app(engine=twin_engine)
    return TestClient(app)


@pytest.fixture
def tenant_a_session() -> str:
    session = default_session_store.create_session(
        namespace_id=_TENANT_A_NS,
        customer_scope_id=_TENANT_A_SCOPE,
        email=_TENANT_A_EMAIL,
    )
    return session.token


@pytest.fixture
def tenant_b_session() -> str:
    session = default_session_store.create_session(
        namespace_id=_TENANT_B_NS,
        customer_scope_id=_TENANT_B_SCOPE,
        email=_TENANT_B_EMAIL,
    )
    return session.token


# ============================================================================
# 1. SERVER-VERIFIED SESSIONS (OPTION A)
# ============================================================================


def test_portal_login_persists_session_in_store(client: TestClient) -> None:
    """portal_login must create a server-verified session in the session store."""
    resp = client.post(
        "/api/portal/login",
        json={
            "email": "customer@nordic.no",
            "token": "valid_broker_token_123",
            "namespace_id": str(_TENANT_A_NS),
            "customer_scope_id": str(_TENANT_A_SCOPE),
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "authenticated"
    token = data["token"]
    assert token.startswith("cp_sess_")
    assert "expires_at" in data

    # Verify session is persisted in store and resolvable
    resolved = default_session_store.resolve_session(token)
    assert resolved is not None
    assert str(resolved.namespace_id) == str(_TENANT_A_NS)
    assert str(resolved.customer_scope_id) == str(_TENANT_A_SCOPE)
    assert resolved.email == "customer@nordic.no"


def test_invalid_or_expired_session_refused(client: TestClient) -> None:
    """Invalid or expired session token must be refused with 401."""
    # Bogus token
    resp = client.get(
        "/api/portal/rooms/overview",
        headers={"Authorization": "Bearer cp_sess_nonexistent_fake_token"},
    )
    assert resp.status_code == 401
    assert "invalid or expired session" in resp.json()["error"]

    # X-Portal-Session-Token header format
    resp2 = client.get(
        "/api/portal/rooms/overview",
        headers={"X-Portal-Session-Token": "cp_sess_nonexistent_fake_token"},
    )
    assert resp2.status_code == 401
    assert "invalid or expired session" in resp2.json()["error"]


def test_revoked_session_refused(client: TestClient, tenant_a_session: str) -> None:
    """Revoked session token must be refused with 401."""
    # First request works
    resp = client.get(
        "/api/portal/rooms/overview",
        headers={"Authorization": f"Bearer {tenant_a_session}"},
    )
    assert resp.status_code == 200

    # Revoke session
    assert default_session_store.revoke_session(tenant_a_session) is True

    # Subsequent request fails
    resp_after = client.get(
        "/api/portal/rooms/overview",
        headers={"Authorization": f"Bearer {tenant_a_session}"},
    )
    assert resp_after.status_code == 401
    assert "invalid or expired session" in resp_after.json()["error"]


# ============================================================================
# 2. CROSS-TENANT / CROSS-CUSTOMER IDOR REFUSAL ACROSS ALL 10 PROTECTED ROUTES
# ============================================================================


def test_cross_tenant_namespace_header_tampering_refused(
    client: TestClient, tenant_a_session: str
) -> None:
    """Tenant A session sending Tenant B's namespace in header must refuse with 403."""
    routes_to_probe = [
        ("GET", "/api/portal/rooms/overview", None),
        ("GET", "/api/portal/rooms/room-alpha-1/tracker", None),
        ("GET", "/api/portal/rooms/room-alpha-1/assets", None),
        ("GET", "/api/portal/documents", None),
        ("GET", "/api/portal/documents/share-fdv-1", None),
        ("GET", "/api/portal/sla", None),
        ("GET", "/api/portal/invoices", None),
        ("POST", "/api/portal/service-requests", {"summary": "help"}),
        ("POST", "/api/portal/expansion-interest", {"summary": "expand"}),
        ("POST", "/api/portal/advisor", {"query": "how are my rooms?"}),
    ]

    for method, path, body in routes_to_probe:
        headers = {
            "Authorization": f"Bearer {tenant_a_session}",
            "X-Namespace-ID": str(_TENANT_B_NS),  # Mismatched namespace!
        }
        if method == "GET":
            r = client.get(path, headers=headers)
        else:
            r = client.post(path, headers=headers, json=body)

        assert r.status_code == 403, (
            f"Route {path} allowed cross-tenant namespace tampering! Got {r.status_code}: {r.text}"
        )
        assert "cross-tenant access denied" in r.json()["error"]


def test_cross_tenant_namespace_query_tampering_refused(
    client: TestClient, tenant_a_session: str
) -> None:
    """Tenant A session sending Tenant B's namespace in query string must refuse with 403."""
    headers = {"Authorization": f"Bearer {tenant_a_session}"}
    params = {"namespace_id": str(_TENANT_B_NS)}

    r = client.get("/api/portal/rooms/overview", headers=headers, params=params)
    assert r.status_code == 403
    assert "cross-tenant access denied" in r.json()["error"]

    r2 = client.get("/api/portal/documents", headers=headers, params=params)
    assert r2.status_code == 403
    assert "cross-tenant access denied" in r2.json()["error"]


def test_cross_tenant_resource_id_probes_refused_with_403_or_404(
    client: TestClient, tenant_a_session: str
) -> None:
    """Tenant A session probing Tenant B's resources must refuse with 403 or 404 (NEVER 200 with empty data)."""
    headers = {"Authorization": f"Bearer {tenant_a_session}"}

    # 1. Probing Tenant B's room tracker
    r_tracker = client.get("/api/portal/rooms/room-beta-1/tracker", headers=headers)
    assert r_tracker.status_code in (403, 404), (
        f"Tenant A could probe Tenant B room-beta-1 tracker! Got {r_tracker.status_code}: {r_tracker.text}"
    )
    assert r_tracker.status_code != 200

    # 2. Probing Tenant B's asset register
    r_assets = client.get("/api/portal/rooms/room-beta-1/assets", headers=headers)
    assert r_assets.status_code in (403, 404), (
        f"Tenant A could probe Tenant B room-beta-1 assets! Got {r_assets.status_code}: {r_assets.text}"
    )
    assert r_assets.status_code != 200

    # 3. Probing Tenant B's document share
    r_doc = client.get("/api/portal/documents/share-fdv-2", headers=headers)
    assert r_doc.status_code in (403, 404), (
        f"Tenant A could probe Tenant B share-fdv-2! Got {r_doc.status_code}: {r_doc.text}"
    )
    assert r_doc.status_code != 200

    # 4. Non-existent resource probe must refuse with 404 (NEVER 200 with empty rows)
    r_nonexistent = client.get("/api/portal/rooms/room-nonexistent-999/tracker", headers=headers)
    assert r_nonexistent.status_code == 404
    assert r_nonexistent.status_code != 200

    r_nonexistent_doc = client.get("/api/portal/documents/share-nonexistent-999", headers=headers)
    assert r_nonexistent_doc.status_code == 404
    assert r_nonexistent_doc.status_code != 200


def test_cross_tenant_resource_id_in_actions_refused(
    client: TestClient, tenant_a_session: str
) -> None:
    """Action endpoints (service requests, advisor) with Tenant B room_id must refuse."""
    headers = {"Authorization": f"Bearer {tenant_a_session}"}

    # 1. Raising service request for Tenant B's room
    r_req = client.post(
        "/api/portal/service-requests",
        headers=headers,
        json={"room_id": "room-beta-1", "summary": "Fix projector in beta room"},
    )
    assert r_req.status_code in (403, 404)
    assert r_req.status_code != 200

    # 2. Querying advisor about Tenant B's room
    r_adv = client.post(
        "/api/portal/advisor",
        headers=headers,
        json={"room_id": "room-beta-1", "query": "Status of room-beta-1"},
    )
    assert r_adv.status_code in (403, 404)
    assert r_adv.status_code != 200


# ============================================================================
# 3. SCOPE OMISSION REFUSAL ACROSS ALL 10 PROTECTED ROUTES
# ============================================================================


def test_omitted_scope_refused_with_401_across_all_protected_routes(
    client: TestClient,
) -> None:
    """Every protected route must refuse with 401 when customer scope / session is omitted."""
    endpoints = [
        ("GET", "/api/portal/rooms/overview", None),
        ("GET", "/api/portal/rooms/room-alpha-1/tracker", None),
        ("GET", "/api/portal/rooms/room-alpha-1/assets", None),
        ("GET", "/api/portal/documents", None),
        ("GET", "/api/portal/documents/share-fdv-1", None),
        ("GET", "/api/portal/sla", None),
        ("GET", "/api/portal/invoices", None),
        ("POST", "/api/portal/service-requests", {"summary": "help"}),
        ("POST", "/api/portal/expansion-interest", {"summary": "expansion"}),
        ("POST", "/api/portal/advisor", {"query": "help"}),
    ]

    for method, path, body in endpoints:
        if method == "GET":
            r = client.get(path)
        else:
            r = client.post(path, json=body)

        assert r.status_code == 401, (
            f"Route {path} did not reject unauthenticated access with 401! Got {r.status_code}: {r.text}"
        )
        assert "customer scope required" in r.json()["error"]


# ============================================================================
# 4. NIL-UUID SENTINEL REFUSAL ACROSS ALL 10 PROTECTED ROUTES
# ============================================================================


def test_nil_uuid_scope_sentinel_refused_with_403(client: TestClient) -> None:
    """Every protected route must refuse nil-UUID customer scope sentinel with 403."""
    endpoints = [
        ("GET", "/api/portal/rooms/overview", None),
        ("GET", "/api/portal/rooms/room-alpha-1/tracker", None),
        ("GET", "/api/portal/rooms/room-alpha-1/assets", None),
        ("GET", "/api/portal/documents", None),
        ("GET", "/api/portal/documents/share-fdv-1", None),
        ("GET", "/api/portal/sla", None),
        ("GET", "/api/portal/invoices", None),
        ("POST", "/api/portal/service-requests", {"summary": "help"}),
        ("POST", "/api/portal/expansion-interest", {"summary": "expansion"}),
        ("POST", "/api/portal/advisor", {"query": "help"}),
    ]

    headers = {
        "X-Customer-Scope-ID": _NIL_UUID,
        "X-Namespace-ID": str(_TENANT_A_NS),
    }

    for method, path, body in endpoints:
        if method == "GET":
            r = client.get(path, headers=headers)
        else:
            r = client.post(path, headers=headers, json=body)

        assert r.status_code == 403, (
            f"Route {path} did not reject nil-UUID sentinel with 403! Got {r.status_code}: {r.text}"
        )
        assert "nil customer scope sentinel rejected" in r.json()["error"]


def test_nil_uuid_namespace_sentinel_refused_with_403(client: TestClient) -> None:
    """Protected routes must refuse nil-UUID namespace sentinel with 403."""
    headers = {
        "X-Customer-Scope-ID": str(_TENANT_A_SCOPE),
        "X-Namespace-ID": _NIL_UUID,
    }
    r = client.get("/api/portal/rooms/overview", headers=headers)
    assert r.status_code == 403
    assert "nil namespace sentinel rejected" in r.json()["error"]


# ============================================================================
# 5. CLIENT BODY OVERRIDE REFUSAL (BODY LOSES TO SESSION SCOPE)
# ============================================================================


def test_post_body_carrying_different_scope_loses_to_authoritative_session(
    client: TestClient, tenant_a_session: str
) -> None:
    """Request body carrying Tenant B customer_scope_id MUST lose to authoritative Tenant A session."""
    headers = {"Authorization": f"Bearer {tenant_a_session}"}

    # 1. Service request with body carrying Tenant B scope
    r_req = client.post(
        "/api/portal/service-requests",
        headers=headers,
        json={
            "customer_scope_id": str(_TENANT_B_SCOPE),  # Attacker tries to override scope in body
            "room_id": "room-alpha-1",  # Belongs to Tenant A
            "summary": "Audio cable broken",
        },
    )
    assert r_req.status_code == 200
    data = r_req.json()
    assert data["customer_status"] == "received"
    assert data["room_id"] == "room-alpha-1"

    # 2. Advisor request with body carrying Tenant B scope
    r_adv = client.post(
        "/api/portal/advisor",
        headers=headers,
        json={
            "customer_scope_id": str(_TENANT_B_SCOPE),
            "room_id": "room-alpha-1",
            "query": "Status of my boardroom?",
        },
    )
    assert r_adv.status_code == 200
    adv_data = r_adv.json()
    assert str(adv_data["customer_scope_id"]) == str(_TENANT_A_SCOPE)

    # 3. Query param carrying Tenant B scope on GET route must also lose to session
    r_doc = client.get(
        "/api/portal/documents",
        headers=headers,
        params={"customer_scope_id": str(_TENANT_B_SCOPE)},
    )
    assert r_doc.status_code == 200
    doc_data = r_doc.json()
    assert str(doc_data["customer_scope_id"]) == str(_TENANT_A_SCOPE)


# ============================================================================
# 6. LEGITIMATE TWIN TENANT ACCESS WITH IDENTICAL RESOURCE LABELS
# ============================================================================


def test_twin_tenants_receive_isolated_resources_with_identical_labels(
    client: TestClient,
    tenant_a_session: str,
    tenant_b_session: str,
) -> None:
    """Both Tenant A and Tenant B receive their own data despite identical room names and titles."""
    headers_a = {"Authorization": f"Bearer {tenant_a_session}"}
    headers_b = {"Authorization": f"Bearer {tenant_b_session}"}

    # 1. Room Tracker: Same room name ('Executive Boardroom'), different content
    resp_a = client.get("/api/portal/rooms/room-alpha-1/tracker", headers=headers_a)
    assert resp_a.status_code == 200
    data_a = resp_a.json()
    assert data_a["room_name"] == "Executive Boardroom"
    assert data_a["percent_ready"] == 65

    resp_b = client.get("/api/portal/rooms/room-beta-1/tracker", headers=headers_b)
    assert resp_b.status_code == 200
    data_b = resp_b.json()
    assert data_b["room_name"] == "Executive Boardroom"  # Identical label
    assert data_b["percent_ready"] == 80  # Tenant B distinct content

    # 2. Documents List: Same title ('Operation & Maintenance Manual'), isolated share IDs
    docs_a = client.get("/api/portal/documents", headers=headers_a).json()["documents"]
    docs_b = client.get("/api/portal/documents", headers=headers_b).json()["documents"]
    assert len(docs_a) == 1
    assert len(docs_b) == 1
    assert docs_a[0]["title"] == "Operation & Maintenance Manual"
    assert docs_b[0]["title"] == "Operation & Maintenance Manual"
    assert docs_a[0]["share_id"] == "share-fdv-1"
    assert docs_b[0]["share_id"] == "share-fdv-2"

    # 3. Invoices: Same invoice number ('INV-1001'), isolated amounts
    inv_a = client.get("/api/portal/invoices", headers=headers_a).json()["invoices"]
    inv_b = client.get("/api/portal/invoices", headers=headers_b).json()["invoices"]
    assert inv_a[0]["invoice_number"] == "INV-1001"
    assert inv_b[0]["invoice_number"] == "INV-1001"
    assert inv_a[0]["amount_inc_vat"] == 1000.0
    assert inv_b[0]["amount_inc_vat"] == 2000.0

    # 4. SLA: Same tier ('Gold SLA'), isolated running clock
    sla_a = client.get("/api/portal/sla", headers=headers_a).json()
    sla_b = client.get("/api/portal/sla", headers=headers_b).json()
    assert sla_a["tier_name"] == "Gold SLA"
    assert sla_b["tier_name"] == "Gold SLA"
    assert sla_a["current_clock_hours"] == 1.5
    assert sla_b["current_clock_hours"] == 3.0
